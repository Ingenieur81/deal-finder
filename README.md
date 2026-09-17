# Deal Finder for Synology Container Manager

A low-footprint self-hosted price watchlist. It runs a FastAPI web interface, APScheduler and SQLite in one container, which avoids a separate database or scheduler service on a NAS.

## Architecture overview

```text
Browser ──HTTP Basic Auth──> FastAPI UI/API ──> SQLite (/data/deal-finder.db)
                                  │
                      APScheduler (09:00 and 17:00, Amsterdam time)
                                  │
                    SerpAPI Google Shopping search
                           │              │
                         history       SMTP / Firebase Cloud Messaging
```

Each watch uses a supported country and currency. SerpAPI receives the two-letter country code as `gl` (not its `location` parameter). Only the lowest offer within the watch's target range is retained. Accepted results are recorded in price history, and the lowest current result links directly to the retailer. Updating an item automatically starts a new check.

## Project structure

```text
deal-finder/
├── app/
│   ├── main.py             # API, data model, scheduler, SerpAPI integration
│   ├── notifications.py    # SMTP and Firebase Cloud Messaging delivery
│   └── static/             # browser UI
├── data/                   # persisted SQLite database (created at runtime)
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── requirements-dev.txt
├── tests/                  # pytest suite; provider calls are mocked
├── AGENTS.md               # contributor architecture and behaviour reference
└── schema.sql              # reference schema; app applies it on startup
```

## Deploy on Synology Container Manager

1. Copy this entire `deal-finder` directory to a shared folder, for example `/volume1/docker/deal-finder`.
2. In that directory, copy `.env.example` to `.env`; set a strong `APP_PASSWORD` and your `SERPAPI_API_KEY`. Keep `.env` private.
3. Configure at least one notification method below. Email can remain unconfigured if you initially use only Android notifications, and vice versa.
4. In Container Manager, open **Project** → **Create**, select the folder, choose **Build new image**, and select `docker-compose.yml`. Start the project. Container Manager builds the official multi-architecture Python image for the NAS CPU (ARM64 or x86_64).
5. Open `http://NAS-IP:8321`. Sign in with `APP_USERNAME` and `APP_PASSWORD` from `.env`.
6. Add a watch: **Gaming Laptop**, `800`–`1200`, choose a country and currency (the defaults are **The Netherlands** and **EUR**), then an email destination. Select **Check now** to test.

For a reverse proxy, create a Synology reverse-proxy rule to host port 8321 and terminate HTTPS there. Do not expose plain HTTP or the service port directly to the internet. The service runs as an unprivileged user and has `no-new-privileges` enabled.

## Configuration guide

### Search provider

This application deliberately uses the supported SerpAPI Google Shopping endpoint rather than scraping retailer pages. It is substantially more reliable for a scheduled NAS job and avoids parsing browser markup. Create a SerpAPI account/key and place it in `SERPAPI_API_KEY`. Searches run at 09:00 and 17:00 in `SCHEDULER_TIMEZONE` (default: `Europe/Amsterdam`).

The country field is a dropdown of supported countries and defaults to **The Netherlands** (`NL`); currency is a dropdown and defaults to **EUR**. The app sends `gl=<country code>` to SerpAPI and deliberately does not send `location`, because SerpAPI rejects values such as `NL` for that parameter.

### Email

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USERNAME`, and `SMTP_PASSWORD` in `.env`. For common providers, use an app password rather than your account password. `SMTP_STARTTLS=true` uses encrypted STARTTLS (the normal port is 587). Each item's notification target is the recipient email address.

### Android push notifications

The Android app that receives the alert must obtain an FCM registration token and display notifications. In Firebase Console, create/select the Firebase project, create a service-account JSON key, and save it as `firebase-service-account.json` alongside the compose file. Uncomment its volume line in `docker-compose.yml`; the default `.env` path is already correct. In the watch form choose **Android push (FCM)** and paste that device's current registration token as the notification target. The token can rotate; replace it in the watch when the Android app reports a new one.

## Verification and operations

1. Check Container Manager logs for `Deal Finder started` and visit `/health` (it returns JSON without authentication).
2. Log in, create an email watch with a broad price range, then press **Check now**. The row should show `matched` or `ok`, its last-check time, the lowest accepted price, and a direct retailer link. The History dialog shows price records and a chart for the past month by default (with 3-month and one-year options).
3. With valid SMTP or FCM configuration, verify the received notification contains the item, price, retailer, country, and deal link. The first accepted price establishes a baseline. Later notifications are sent only for a strictly lower price, or as a weekly reminder when the accepted price has not changed for seven days.
4. Restart the project. The item and history remain because `./data` is mounted to `/data`.

Useful status meanings: `ok` = search completed but no result met its price rule; `matched` = matching price found; `search_error` = provider/network/configuration failure; `notify_error` = search succeeded but delivery failed. Error detail is shown under the status and in container logs. The scheduler continues after a failed item check.

## API

The UI uses the same authenticated API: `GET/POST /api/items`, `PUT/DELETE /api/items/{id}`, `POST /api/items/{id}/check`, and `GET /api/items/{id}/history?days=30`. History defaults to the past 30 days; `days` accepts 1–3650. `GET /api/options` returns the available countries and currencies. Interactive API documentation is at `/docs` after signing in.

## Price and notification rules

- Only the lowest valid HTTP(S) offer within the configured minimum/maximum range is retained. An offer below the minimum does not become the current price or comparison baseline.
- Every accepted lowest result is stored in price history. The overview shows the target range plus the current lowest price, retailer, and a link on the item name.
- The first accepted result is a baseline, not a notification. A subsequent accepted result alerts only when its price is strictly lower than the previous accepted result.
- If an accepted numeric price remains unchanged for seven days, the next successful check refreshes the displayed offer and sends a reminder. A failed reminder delivery is retried on the next scheduled check.

## Development

Use Python 3.12 to match the container image:

```bash
python3.12 -m venv venv
venv/bin/python -m pip install -r requirements-dev.txt
venv/bin/python -m pytest
```

The suite uses temporary SQLite databases and mocked external providers. It enforces at least 90% coverage.
