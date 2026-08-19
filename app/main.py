"""Deal Finder: a small, self-hosted price-watch service for Docker/NAS use."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .notifications import NotificationError, send_notification

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("deal-finder")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'deal-finder.db'}")
SEARCH_INTERVAL_MINUTES = max(5, int(os.getenv("SEARCH_INTERVAL_MINUTES", "60")))
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
STATIC_DIR = Path(__file__).parent / "static"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
scheduler = AsyncIOScheduler(timezone="UTC")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class WatchItem(Base):
    __tablename__ = "watch_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    min_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    max_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    region: Mapped[str] = mapped_column(String(160), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    notification_method: Mapped[str] = mapped_column(String(12), nullable=False, default="email")
    notification_target: Mapped[str] = mapped_column(String(320), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(24), nullable=False, default="never")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_notified_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    prices: Mapped[list["PriceHistory"]] = relationship(back_populates="item", cascade="all, delete-orphan")


class PriceHistory(Base):
    __tablename__ = "price_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("watch_items.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    retailer: Mapped[str] = mapped_column(String(240), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    deal_url: Mapped[str] = mapped_column(Text, nullable=False)
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    item: Mapped[WatchItem] = relationship(back_populates="prices")


class ItemInput(BaseModel):
    name: str = Field(min_length=2, max_length=240)
    min_price: Decimal | None = Field(default=None, ge=0)
    max_price: Decimal | None = Field(default=None, ge=0)
    region: str = Field(min_length=2, max_length=160)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    notification_method: Literal["email", "android"]
    notification_target: str = Field(min_length=3, max_length=320)
    enabled: bool = True

    @field_validator("name", "region", "notification_target")
    @classmethod
    def trim_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()

    def validate_price_range(self) -> None:
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise HTTPException(status_code=422, detail="Minimum price must not exceed maximum price.")


class ItemOutput(ItemInput):
    id: int
    last_checked_at: datetime | None
    last_status: str
    last_error: str | None
    last_notified_price: Decimal | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SearchResult(BaseModel):
    title: str
    retailer: str
    price: Decimal
    currency: str
    deal_url: str


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def serialize_item(item: WatchItem) -> ItemOutput:
    return ItemOutput.model_validate(item)


def require_basic_auth(request: Request) -> None:
    if request.url.path in {"/health", "/docs", "/openapi.json"}:
        return
    expected_user = os.getenv("APP_USERNAME", "admin")
    expected_password = os.getenv("APP_PASSWORD", "change-me")
    header = request.headers.get("authorization", "")
    try:
        scheme, value = header.split(" ", 1)
        import base64
        username, password = base64.b64decode(value).decode("utf-8").split(":", 1)
    except Exception:
        scheme, username, password = "", "", ""
    if not (scheme.lower() == "basic" and hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_password)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required", headers={"WWW-Authenticate": 'Basic realm="Deal Finder"'})


def parse_price(value: str | int | float | None) -> Decimal | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9.,]", "", str(value))
    if not cleaned:
        return None
    # SerpAPI's extracted_price is preferred; this also covers ordinary 1,299.99 prices.
    if cleaned.count(",") == 1 and cleaned.count(".") == 0 and len(cleaned.rsplit(",", 1)[1]) == 2:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


async def search_serpapi(item: WatchItem) -> list[SearchResult]:
    if not SERPAPI_API_KEY:
        raise RuntimeError("SERPAPI_API_KEY is not configured")
    params = {
        "engine": "google_shopping",
        "q": f"{item.name} available in {item.region}",
        "location": item.region,
        "gl": item.region[-2:].lower() if len(item.region) == 2 else "us",
        "hl": "en",
        "currency": item.currency,
        "api_key": SERPAPI_API_KEY,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
        response = await client.get("https://serpapi.com/search.json", params=params)
        response.raise_for_status()
        payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"Search provider error: {payload['error']}")
    results: list[SearchResult] = []
    for row in payload.get("shopping_results", []):
        price = parse_price(row.get("extracted_price") or row.get("price"))
        link = row.get("product_link") or row.get("link")
        if price is None or not link:
            continue
        results.append(SearchResult(
            title=str(row.get("title") or item.name)[:500], retailer=str(row.get("source") or "Unknown retailer")[:240],
            price=price, currency=item.currency, deal_url=str(link),
        ))
    return results


def is_eligible(item: WatchItem, offer: SearchResult) -> bool:
    return ((item.min_price is None or offer.price >= item.min_price) and (item.max_price is None or offer.price <= item.max_price))


async def check_item(item_id: int) -> dict:
    """Search one item, retain all returned offers, and alert once for a changed best match."""
    with SessionLocal() as db:
        item = db.get(WatchItem, item_id)
        if not item or not item.enabled:
            return {"status": "skipped"}
        try:
            offers = await search_serpapi(item)
            for offer in offers:
                db.add(PriceHistory(item_id=item.id, title=offer.title, retailer=offer.retailer, price=offer.price,
                                    currency=offer.currency, deal_url=offer.deal_url))
            eligible = sorted((offer for offer in offers if is_eligible(item, offer)), key=lambda offer: offer.price)
            item.last_checked_at = utcnow()
            item.last_status = "matched" if eligible else "ok"
            item.last_error = None
            if eligible:
                best = eligible[0]
                if item.last_notified_price != best.price:
                    try:
                        send_notification(item, best)
                        item.last_notified_price = best.price
                    except NotificationError as exc:
                        item.last_status = "notify_error"
                        item.last_error = str(exc)
                        logger.warning("Notification for item %s failed: %s", item.id, exc)
            db.commit()
            return {"status": item.last_status, "offers": len(offers), "eligible": len(eligible)}
        except Exception as exc:
            logger.exception("Price check failed for item %s", item.id)
            item.last_checked_at = utcnow()
            item.last_status = "search_error"
            item.last_error = str(exc)[:2000]
            db.commit()
            return {"status": "search_error", "error": str(exc)}


async def run_all_checks() -> None:
    with SessionLocal() as db:
        ids = list(db.scalars(select(WatchItem.id).where(WatchItem.enabled.is_(True))))
    logger.info("Starting scheduled check for %s enabled item(s)", len(ids))
    for item_id in ids:
        await check_item(item_id)
        await asyncio.sleep(1)  # polite spacing between provider requests


@asynccontextmanager
async def lifespan(_: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    scheduler.add_job(run_all_checks, "interval", minutes=SEARCH_INTERVAL_MINUTES, id="price_checks", replace_existing=True)
    scheduler.start()
    logger.info("Deal Finder started; checks run every %s minutes", SEARCH_INTERVAL_MINUTES)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Deal Finder", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    """Protect both the browser assets and JSON API, but keep Docker health checks public."""
    try:
        require_basic_auth(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
    return await call_next(request)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "time": utcnow().isoformat()}


@app.get("/", include_in_schema=False)
def web_ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/static/{filename}", include_in_schema=False)
def static_file(filename: str) -> FileResponse:
    target = (STATIC_DIR / filename).resolve()
    if STATIC_DIR.resolve() not in target.parents or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


@app.get("/api/items", response_model=list[ItemOutput])
def list_items(db: Session = Depends(get_db)) -> list[ItemOutput]:
    return [serialize_item(item) for item in db.scalars(select(WatchItem).order_by(WatchItem.created_at.desc()))]


@app.post("/api/items", response_model=ItemOutput, status_code=201)
def create_item(payload: ItemInput, db: Session = Depends(get_db)) -> ItemOutput:
    payload.validate_price_range()
    item = WatchItem(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return serialize_item(item)


@app.put("/api/items/{item_id}", response_model=ItemOutput)
async def update_item(item_id: int, payload: ItemInput, db: Session = Depends(get_db)) -> ItemOutput:
    payload.validate_price_range()
    item = db.get(WatchItem, item_id)
    if not item:
        raise HTTPException(404, "Watch item not found")
    for field, value in payload.model_dump().items():
        setattr(item, field, value)
    item.last_notified_price = None  # a changed rule may legitimately notify again
    db.commit()
    db.refresh(item)
    asyncio.create_task(check_item(item.id))
    return serialize_item(item)


@app.delete("/api/items/{item_id}", status_code=204)
def delete_item(item_id: int, db: Session = Depends(get_db)) -> None:
    item = db.get(WatchItem, item_id)
    if not item:
        raise HTTPException(404, "Watch item not found")
    db.delete(item)
    db.commit()


@app.post("/api/items/{item_id}/check")
async def check_one(item_id: int, db: Session = Depends(get_db)) -> dict:
    if not db.get(WatchItem, item_id):
        raise HTTPException(404, "Watch item not found")
    return await check_item(item_id)


@app.get("/api/items/{item_id}/history")
def price_history(item_id: int, db: Session = Depends(get_db)) -> list[dict]:
    if not db.get(WatchItem, item_id):
        raise HTTPException(404, "Watch item not found")
    rows = db.scalars(select(PriceHistory).where(PriceHistory.item_id == item_id).order_by(PriceHistory.found_at.desc()).limit(100))
    return [{"title": row.title, "retailer": row.retailer, "price": str(row.price), "currency": row.currency,
             "deal_url": row.deal_url, "found_at": row.found_at} for row in rows]


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
