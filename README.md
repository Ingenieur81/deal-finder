# Deal Finder for Synology Container Manager

A low-footprint self-hosted price watchlist. It runs a FastAPI web interface, APScheduler and SQLite in one container, which avoids a separate database or scheduler service on a NAS.

## Architecture overview

```text
Browser ──HTTP Basic Auth──> FastAPI UI/API ──> SQLite (/data/deal-finder.db)
                                  │
                 APScheduler (09:00 and 17:00 local time)
                                  │
                    SerpAPI Google Shopping search
                           │              │
                         history       SMTP / Firebase Cloud Messaging
```

Searches use a country selected from the built-in catalog. Each check stores only the lowest available offer in price history and shows it beside the target price. A notification is sent only when a newly found, target-matching price is strictly lower than the previous recorded price. Updating an item automatically starts a new check.

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
└── schema.sql              # reference schema; app applies it on startup
```

## Deploy on Synology Container Manager

1. Copy this entire `deal-finder` directory to a shared folder, for example `/volume1/docker/deal-finder`.
2. In that directory, copy `.env.example` to `.env`; set a strong `APP_PASSWORD` and your `SERPAPI_API_KEY`. Keep `.env` private.
3. Configure at least one notification method below. Email can remain unconfigured if you initially use only Android notifications, and vice versa.
4. In Container Manager, open **Project** → **Create**, select the folder, choose **Build new image**, and select `docker-compose.yml`. Start the project. Container Manager builds the official multi-architecture Python image for the NAS CPU (ARM64 or x86_64).
5. Open `http://NAS-IP:8321`. Sign in with `APP_USERNAME` and `APP_PASSWORD` from `.env`.
6. Add a watch: **Gaming Laptop**, `800`–`1200`, **The Netherlands**, `EUR`, then an email destination. Select **Check now** to establish the initial reference price; a later lower price triggers the alert.

For a reverse proxy, create a Synology reverse-proxy rule to port 8321 and terminate HTTPS there. Do not expose plain HTTP or port 8321 directly to the internet. The service runs as an unprivileged user and has `no-new-privileges` enabled.

## Configuration guide

### Search provider

This application deliberately uses the supported SerpAPI Google Shopping endpoint rather than scraping retailer pages. It is substantially more reliable for a scheduled NAS job and avoids parsing browser markup. Create a SerpAPI account/key and place it in `SERPAPI_API_KEY`. Checks run daily at **09:00** and **17:00** in `SCHEDULER_TIMEZONE` (default: `Europe/Amsterdam`). The form provides the supported country catalog directly and defaults to **The Netherlands**; currencies are also selected from an ISO-currency dropdown and default to **EUR**.

### Email

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USERNAME`, and `SMTP_PASSWORD` in `.env`. For common providers, use an app password rather than your account password. `SMTP_STARTTLS=true` uses encrypted STARTTLS (the normal port is 587). Each item's notification target is the recipient email address.

### Android push notifications

The Android app that receives the alert must obtain an FCM registration token and display notifications. In Firebase Console, create/select the Firebase project, create a service-account JSON key, and save it as `firebase-service-account.json` alongside the compose file. Uncomment its volume line in `docker-compose.yml`; the default `.env` path is already correct. In the watch form choose **Android push (FCM)** and paste that device's current registration token as the notification target. The token can rotate; replace it in the watch when the Android app reports a new one.

## Verification and operations

1. Check Container Manager logs for `Deal Finder started` and visit `/health` (it returns JSON without authentication).
2. Log in, create an email watch with a broad price range, then press **Check now**. The row should show `matched` or `ok`, its last-check time, and the History dialog should list returned offers.
3. With valid SMTP or FCM configuration and a matching result, verify the received notification contains the item, price, retailer, location, and deal link.
4. Restart the project. The item and history remain because `./data` is mounted to `/data`.

Useful status meanings: `ok` = search completed but no result met its price rule; `matched` = matching price found; `search_error` = provider/network/configuration failure; `notify_error` = search succeeded but delivery failed. Error detail is shown under the status and in container logs. The scheduler continues after a failed item check.

## API

The UI uses the same authenticated API: `GET/POST /api/items`, `PUT/DELETE /api/items/{id}`, `POST /api/items/{id}/check`, and `GET /api/items/{id}/history`. Interactive API documentation is at `/docs` after signing in.
