"""Deal Finder: a small, self-hosted price-watch service for Docker/NAS use."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, create_engine, event, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .notifications import NotificationError, send_notification

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("deal-finder")
# SerpAPI credentials are query parameters. HTTPX logs full outbound URLs at
# INFO, so never allow its request logging into the application log stream.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'deal-finder.db'}")
SCHEDULER_TIMEZONE = os.getenv("SCHEDULER_TIMEZONE", "Europe/Amsterdam")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
STATIC_DIR = Path(__file__).parent / "static"

COUNTRIES = {
    "AU": "Australia", "AT": "Austria", "BE": "Belgium", "BR": "Brazil", "CA": "Canada", "CZ": "Czechia",
    "DK": "Denmark", "FI": "Finland", "FR": "France", "DE": "Germany", "GR": "Greece", "HK": "Hong Kong",
    "HU": "Hungary", "IN": "India", "ID": "Indonesia", "IE": "Ireland", "IL": "Israel", "IT": "Italy",
    "JP": "Japan", "MY": "Malaysia", "MX": "Mexico", "NL": "The Netherlands", "NZ": "New Zealand", "NO": "Norway",
    "PH": "Philippines", "PL": "Poland", "PT": "Portugal", "RO": "Romania", "SG": "Singapore", "SK": "Slovakia",
    "ZA": "South Africa", "KR": "South Korea", "ES": "Spain", "SE": "Sweden", "CH": "Switzerland", "TW": "Taiwan",
    "TH": "Thailand", "TR": "Turkey", "AE": "United Arab Emirates", "GB": "United Kingdom", "US": "United States",
}
CURRENCIES = ("EUR", "USD", "GBP", "AUD", "BRL", "CAD", "CHF", "CZK", "DKK", "HKD", "HUF", "IDR", "ILS", "INR", "JPY", "KRW", "MXN", "MYR", "NOK", "NZD", "PHP", "PLN", "RON", "SEK", "SGD", "THB", "TRY", "TWD", "ZAR")
COUNTRY_ALIASES = {name.upper(): code for code, name in COUNTRIES.items()} | {"NETHERLANDS": "NL", "THE NETHERLANDS": "NL", "UNITED STATES OF AMERICA": "US"}

SQLITE_DATABASE = DATABASE_URL.startswith("sqlite")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30} if SQLITE_DATABASE else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
scheduler = AsyncIOScheduler(timezone=SCHEDULER_TIMEZONE)

if SQLITE_DATABASE:
    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _: object) -> None:
        """Prevent transient concurrent-write failures and enforce declared cascades."""
        cursor = connection.cursor()
        cursor.execute("PRAGMA busy_timeout = 30000")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()


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
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="EUR")
    notification_method: Mapped[str] = mapped_column(String(12), nullable=False, default="email")
    notification_target: Mapped[str] = mapped_column(String(320), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(24), nullable=False, default="never")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_notified_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    current_deal_url: Mapped[str | None] = mapped_column(Text)
    current_retailer: Mapped[str | None] = mapped_column(String(240))
    current_price_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    region: str = "NL"
    currency: str = "EUR"
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
        value = value.strip().upper()
        if value not in CURRENCIES:
            raise ValueError("Select a supported currency.")
        return value

    @field_validator("region")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        value = value.strip().upper()
        value = COUNTRY_ALIASES.get(value, value)
        if value not in COUNTRIES:
            raise ValueError("Select a supported country.")
        return value

    def validate_price_range(self) -> None:
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise HTTPException(status_code=422, detail="Minimum price must not exceed maximum price.")


class ItemOutput(ItemInput):
    id: int
    last_checked_at: datetime | None
    last_status: str
    last_error: str | None
    last_notified_price: Decimal | None
    current_price: Decimal | None
    current_deal_url: str | None
    current_retailer: str | None
    current_price_updated_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SearchResult(BaseModel):
    title: str
    retailer: str
    price: Decimal
    currency: str
    deal_url: str | None
    immersive_page_token: str | None = None


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def serialize_item(item: WatchItem) -> ItemOutput:
    output = ItemOutput.model_validate(item)
    return output.model_copy(update={
        "last_checked_at": as_utc(output.last_checked_at),
        "current_price_updated_at": as_utc(output.current_price_updated_at),
        "created_at": as_utc(output.created_at),
        "updated_at": as_utc(output.updated_at),
    })


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns timezone-naive values even for DateTime(timezone=True)."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def ensure_database_schema() -> None:
    """Create new databases and add display fields to installations upgraded in place."""
    Base.metadata.create_all(engine)
    existing = {column["name"] for column in inspect(engine).get_columns("watch_items")}
    additions = {
        "current_price": "NUMERIC(12, 2)",
        "current_deal_url": "TEXT",
        "current_retailer": "VARCHAR(240)",
        "current_price_updated_at": "DATETIME",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE watch_items ADD COLUMN {name} {definition}"))
        for name, code in COUNTRY_ALIASES.items():
            connection.execute(text("UPDATE watch_items SET region = :code WHERE upper(region) = :name"), {"code": code, "name": name})


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
    # Treat the final punctuation mark as the decimal separator when it has
    # two fractional digits. This supports both 1,299.99 and 1.299,99.
    separators = [index for index, char in enumerate(cleaned) if char in ".,"]
    if separators:
        decimal_index = separators[-1]
        fractional_digits = len(cleaned) - decimal_index - 1
        if fractional_digits == 2:
            integer_part = re.sub(r"[.,]", "", cleaned[:decimal_index])
            cleaned = f"{integer_part}.{cleaned[decimal_index + 1:]}"
        else:
            cleaned = re.sub(r"[.,]", "", cleaned)
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


async def search_serpapi(item: WatchItem) -> list[SearchResult]:
    if not SERPAPI_API_KEY:
        raise RuntimeError("SERPAPI_API_KEY is not configured")
    country_name = COUNTRIES[item.region]
    params = {
        "engine": "google_shopping",
        "q": f"{item.name} available in {country_name}",
        "hl": "en",
        "currency": item.currency,
        "api_key": SERPAPI_API_KEY,
    }
    params["gl"] = item.region.lower()
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
        response = await client.get("https://serpapi.com/search.json", params=params)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            # HTTPX includes the full request URL in this exception. SerpAPI
            # uses a query-string credential, so do not let it reach logs.
            try:
                provider_error = response.json().get("error")
            except (ValueError, AttributeError):
                provider_error = None
            detail = str(provider_error or "Request rejected by the search provider")[:500]
            raise RuntimeError(f"Search provider returned HTTP {response.status_code}: {detail}") from None
        payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"Search provider error: {payload['error']}")
    results: list[SearchResult] = []
    for row in payload.get("shopping_results", []):
        price = parse_price(row.get("extracted_price") or row.get("price"))
        if price is None:
            continue
        direct_url = merchant_url(row.get("direct_link") or row.get("link"))
        page_token = immersive_page_token(row)
        if direct_url is None and page_token is None:
            continue
        results.append(SearchResult(
            title=str(row.get("title") or item.name)[:500], retailer=str(row.get("source") or "Unknown retailer")[:240],
            price=price, currency=item.currency, deal_url=direct_url, immersive_page_token=page_token,
        ))
    eligible = sorted((offer for offer in results if is_eligible(item, offer)), key=lambda offer: offer.price)
    if not eligible:
        return []
    best = eligible[0]
    if best.deal_url is None and item.current_price == best.price and item.current_retailer == best.retailer:
        best.deal_url = merchant_url(item.current_deal_url)
    if best.deal_url is None and best.immersive_page_token:
        best.deal_url = await immersive_offer_url(item, best)
    if best.deal_url is None:
        logger.warning("No direct merchant URL was available for item %s", item.id)
        return []
    return [best]


def merchant_url(value: object) -> str | None:
    """Accept only direct HTTP(S) merchant URLs, never Google Shopping pages."""
    parsed = urlparse(str(value)) if value else None
    host = parsed.hostname.lower() if parsed and parsed.hostname else ""
    is_google = host.startswith("google.") or ".google." in host
    if not parsed or parsed.scheme not in {"http", "https"} or not host or is_google:
        return None
    return parsed.geturl()


def immersive_page_token(row: dict) -> str | None:
    """Read the supported immersive-product token from a Shopping result."""
    if row.get("immersive_product_page_token"):
        return str(row["immersive_product_page_token"])
    api_url = str(row.get("serpapi_immersive_product_api") or "")
    return parse_qs(urlparse(api_url).query).get("page_token", [None])[0]


async def immersive_offer_url(item: WatchItem, offer: SearchResult) -> str | None:
    """Fetch the selected product's matching seller offer to obtain its direct URL."""
    params = {
        "engine": "google_immersive_product",
        "page_token": offer.immersive_page_token,
        "more_stores": "true",
        "api_key": SERPAPI_API_KEY,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
        response = await client.get("https://serpapi.com/search.json", params=params)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            try:
                provider_error = response.json().get("error")
            except (ValueError, AttributeError):
                provider_error = None
            detail = str(provider_error or "Request rejected by the search provider")[:500]
            raise RuntimeError(f"Search provider returned HTTP {response.status_code}: {detail}") from None
        payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"Search provider error: {payload['error']}")
    sellers = payload.get("product_results", {}).get("stores", [])
    matching_sellers = [
        seller for seller in sellers
        if str(seller.get("name") or "").casefold() == offer.retailer.casefold()
        and parse_price(seller.get("extracted_price") or seller.get("price") or seller.get("base_price")) == offer.price
    ]
    for seller in matching_sellers:
        direct_url = merchant_url(seller.get("direct_link") or seller.get("link"))
        if direct_url:
            return direct_url
    return None


def is_eligible(item: WatchItem, offer: SearchResult) -> bool:
    return ((item.min_price is None or offer.price >= item.min_price) and (item.max_price is None or offer.price <= item.max_price))


async def check_item(item_id: int) -> dict:
    """Search one item, retain all returned offers, and alert once for a changed best match."""
    with SessionLocal() as db:
        item = db.get(WatchItem, item_id)
        if not item or not item.enabled:
            return {"status": "skipped"}
        try:
            previous_query = select(PriceHistory.price).where(PriceHistory.item_id == item.id)
            if item.min_price is not None:
                previous_query = previous_query.where(PriceHistory.price >= item.min_price)
            if item.max_price is not None:
                previous_query = previous_query.where(PriceHistory.price <= item.max_price)
            previous_price = db.scalar(previous_query.order_by(PriceHistory.found_at.desc(), PriceHistory.id.desc()).limit(1))
            offers = [offer for offer in await search_serpapi(item) if is_eligible(item, offer)]
            for offer in offers:
                db.add(PriceHistory(item_id=item.id, title=offer.title, retailer=offer.retailer, price=offer.price,
                                    currency=offer.currency, deal_url=offer.deal_url))
            eligible = sorted((offer for offer in offers if is_eligible(item, offer)), key=lambda offer: offer.price)
            checked_at = utcnow()
            item.last_checked_at = checked_at
            item.last_status = "matched" if eligible else "ok"
            item.last_error = None
            if offers:
                best = offers[0]
                price_changed = item.current_price is not None and item.current_price != best.price
                has_price_baseline = item.current_price_updated_at is not None
                item.current_price = best.price
                item.current_deal_url = best.deal_url
                item.current_retailer = best.retailer
                if not has_price_baseline or price_changed:
                    item.current_price_updated_at = checked_at
            else:
                item.current_price = None
                item.current_deal_url = None
                item.current_retailer = None
            if eligible:
                best = eligible[0]
                price_decreased = previous_price is not None and best.price < previous_price
                price_is_stale = (
                    previous_price is not None
                    and item.current_price_updated_at is not None
                    and not price_changed
                    and checked_at - as_utc(item.current_price_updated_at) >= timedelta(days=7)
                )
                if price_decreased or price_is_stale:
                    try:
                        await asyncio.to_thread(send_notification, item, best)
                        item.last_notified_price = best.price
                        if price_is_stale:
                            # Keep retrying a stale-price reminder when delivery fails.
                            item.current_price_updated_at = checked_at
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
    ensure_database_schema()
    scheduler.add_job(run_all_checks, "cron", hour="9,17", minute=0, id="price_checks", replace_existing=True)
    scheduler.start()
    logger.info("Deal Finder started; checks run at 09:00 and 17:00 (%s)", SCHEDULER_TIMEZONE)
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


@app.get("/api/options")
def options() -> dict:
    return {"countries": [{"code": code, "name": name} for code, name in COUNTRIES.items()], "currencies": list(CURRENCIES)}


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


@app.delete("/api/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_item(item_id: int, db: Session = Depends(get_db)) -> Response:
    item = db.get(WatchItem, item_id)
    if not item:
        raise HTTPException(404, "Watch item not found")
    db.delete(item)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/items/{item_id}/check")
async def check_one(item_id: int, db: Session = Depends(get_db)) -> dict:
    if not db.get(WatchItem, item_id):
        raise HTTPException(404, "Watch item not found")
    return await check_item(item_id)


@app.get("/api/items/{item_id}/history")
def price_history(item_id: int, days: int | None = Query(default=30, ge=1, le=3650), db: Session = Depends(get_db)) -> list[dict]:
    if not db.get(WatchItem, item_id):
        raise HTTPException(404, "Watch item not found")
    query = select(PriceHistory).where(PriceHistory.item_id == item_id)
    if days is not None:
        query = query.where(PriceHistory.found_at >= utcnow() - timedelta(days=days))
    rows = db.scalars(query.order_by(PriceHistory.found_at.desc()).limit(100))
    return [{"title": row.title, "retailer": row.retailer, "price": str(row.price), "currency": row.currency,
             "deal_url": row.deal_url, "found_at": as_utc(row.found_at)} for row in rows]


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
