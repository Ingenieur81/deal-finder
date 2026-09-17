# Deal Finder — Contributor Context

## Purpose

Deal Finder is a self-hosted, low-resource price-watch application intended for Synology Container Manager. It provides a password-protected FastAPI web UI, SQLite persistence, scheduled Google Shopping searches via SerpAPI, price-history comparison, and SMTP/FCM notifications.

## Architecture and key files

- `app/main.py` — FastAPI app, SQLAlchemy models, schema upgrades, scheduler, SerpAPI client, and JSON API.
- `app/notifications.py` — synchronous SMTP and Firebase Cloud Messaging delivery; call it through `asyncio.to_thread` from async code.
- `app/static/` — vanilla HTML/CSS/JavaScript UI; do not introduce a frontend build step.
- `docker-compose.yml` — supported deployment compose file. Host port **8321** maps to container port **8080**.
- `Dockerfile` / `entrypoint.sh` — Python 3.12 container. The entrypoint repairs `/data` ownership, then starts Uvicorn as an unprivileged user.
- `tests/` — pytest suite; all external providers are mocked.

Do not use `compose.yaml` or `compose.debug.yaml` for normal deployment; they are local IDE artifacts. Always use `docker compose -f docker-compose.yml ...`.

## Current price-watch behavior

- A watch stores a two-letter ISO country code in `region`; only entries in `COUNTRIES` are accepted. The UI defaults to **NL / The Netherlands**.
- Currency must be in `CURRENCIES`; the UI and API default to **EUR**.
- SerpAPI receives `gl=<country code>` and no `location` parameter. Do not add `location=NL`: SerpAPI rejects it.
- Search results are parsed defensively and non-HTTP(S) deal links are discarded.
- Only the **lowest offer within the configured min/max target range** is retained. A price below the minimum is intentionally ignored, not displayed as the current lowest price, and not stored as the new comparison baseline.
- `PriceHistory` records the accepted lowest result from each successful check. `WatchItem.current_price`, `current_deal_url`, and `current_retailer` support the overview display. `current_price_updated_at` changes only when the accepted numeric price changes (or when a successful stale-price reminder is sent).
- The first accepted price is a baseline. Notify when the new accepted price is **strictly lower** than the previous accepted in-range price. When an accepted price has remained unchanged for seven days, send a reminder notification after a successful re-check and renew its timestamp. If no price is in range, clear the current-lowest display.
- Existing SQLite databases are upgraded in `ensure_database_schema()`; preserve that migration path when adding model columns.

## History details

- The History action opens the item detail dialog. It renders an in-browser canvas line chart alongside the individual price records; no charting dependency or frontend build step is used.
- The default history request is the past **30 days**. The dialog also offers 90-day and one-year ranges; `/api/items/{id}/history?days=<1..3650>` enforces the range server-side.

## Scheduling

APScheduler uses `SCHEDULER_TIMEZONE`, defaulting to `Europe/Amsterdam`. The only scheduled job, `price_checks`, runs every day at **09:00** and **17:00** local time via a cron trigger. Do not reintroduce the old hourly interval setting.

## Configuration and security

- Copy `.env.example` to `.env`; never commit `.env`, Firebase service-account JSON, or `data/`.
- `APP_USERNAME` / `APP_PASSWORD` protect the UI and API. `/health` remains public for Docker health checks.
- `SERPAPI_API_KEY` is passed as a query credential by SerpAPI. Error handling must never log request URLs or otherwise expose it. Rotate any key that appears in logs, issues, or chat.
- SMTP uses STARTTLS by default. FCM requires a mounted service-account JSON and a device registration token per watch.

## Development and testing

Use Python **3.12** to match the Docker image; Python 3.14 is incompatible with the pinned SQLAlchemy version.

```bash
python3.12 -m venv venv
venv/bin/python -m pip install -r requirements-dev.txt
venv/bin/python -m pytest
```

`pytest.ini` enforces a 90% coverage minimum. Keep unit tests deterministic: use the fixtures in `tests/conftest.py`, temporary SQLite databases, and mocks for SerpAPI/SMTP/Firebase. Do not make real network calls in tests.

## Deployment checks

```bash
docker compose -f docker-compose.yml config -q
docker compose -f docker-compose.yml up --build -d --force-recreate
curl http://NAS-IP:8321/health
```

The bind-mounted database lives at `./data/deal-finder.db`. Preserve it when recreating containers. On a port conflict, change only the host side of `8321:8080` unless the internal Uvicorn port also changes.

## Change guidelines

- Keep the deployment single-container and ARM64/amd64 compatible.
- Prefer additive SQLite migrations over destructive schema changes.
- Update `.env.example`, `README.md`, and tests when configuration or user-visible behavior changes.
- Treat tracked changes as intentional; leave local `.env`, `data/`, `venv/`, `.venv/`, `.vscode/`, `.op/`, and debug compose files alone unless specifically requested.
