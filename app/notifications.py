"""Notification backends. Email uses SMTP; Android uses Firebase Cloud Messaging."""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from firebase_admin import credentials, initialize_app, messaging

logger = logging.getLogger("deal-finder.notifications")
_firebase_ready = False


class NotificationError(RuntimeError):
    pass


def _message(item, offer) -> tuple[str, str]:
    subject = f"Deal Finder: {item.name} for {offer.currency} {offer.price}"
    body = (
        f"A matching deal was found.\n\nItem: {item.name}\nPrice: {offer.currency} {offer.price}\n"
        f"Retailer: {offer.retailer}\nLocation: {item.region}\nLink: {offer.deal_url}\n"
    )
    return subject, body


def _send_email(target: str, subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST")
    sender = os.getenv("SMTP_FROM")
    if not host or not sender:
        raise NotificationError("SMTP_HOST and SMTP_FROM must be configured for email alerts")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = target
    message["Subject"] = subject
    message.set_content(body)
    port = int(os.getenv("SMTP_PORT", "587"))
    username, password = os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD")
    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            if os.getenv("SMTP_STARTTLS", "true").lower() == "true":
                server.starttls()
                server.ehlo()
            if username and password:
                server.login(username, password)
            server.send_message(message)
    except OSError as exc:
        raise NotificationError(f"Email delivery failed: {exc}") from exc


def _firebase_app() -> None:
    global _firebase_ready
    if _firebase_ready:
        return
    filename = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE", "")
    if not filename or not Path(filename).is_file():
        raise NotificationError("Mount Firebase service-account JSON and set FIREBASE_SERVICE_ACCOUNT_FILE")
    try:
        initialize_app(credentials.Certificate(filename))
        _firebase_ready = True
    except Exception as exc:
        raise NotificationError(f"Firebase setup failed: {exc}") from exc


def _send_android(token: str, subject: str, body: str, link: str) -> None:
    _firebase_app()
    try:
        message = messaging.Message(notification=messaging.Notification(title=subject, body=body[:500]),
                                    data={"deal_url": link}, token=token)
        messaging.send(message)
    except Exception as exc:
        raise NotificationError(f"Android push failed: {exc}") from exc


def send_notification(item, offer) -> None:
    subject, body = _message(item, offer)
    if item.notification_method == "email":
        _send_email(item.notification_target, subject, body)
    elif item.notification_method == "android":
        _send_android(item.notification_target, subject, body, offer.deal_url)
    else:
        raise NotificationError(f"Unknown notification method: {item.notification_method}")
