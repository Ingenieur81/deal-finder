from types import SimpleNamespace

import pytest


@pytest.fixture
def notifications_module():
    import app.notifications as notifications

    return notifications


@pytest.fixture
def item_and_offer():
    item = SimpleNamespace(name="Backpack", region="NL", notification_method="email", notification_target="alerts@example.test")
    offer = SimpleNamespace(currency="EUR", price="49.99", retailer="Shop", deal_url="https://shop.example/deal")
    return item, offer


def test_message_contains_the_deal_details(notifications_module, item_and_offer):
    subject, body = notifications_module._message(*item_and_offer)

    assert subject == "Deal Finder: Backpack for EUR 49.99"
    assert "Retailer: Shop" in body
    assert "https://shop.example/deal" in body


def test_send_email_requires_server_configuration(notifications_module, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)

    with pytest.raises(notifications_module.NotificationError, match="SMTP_HOST"):
        notifications_module._send_email("a@b.cd", "Subject", "Body")


def test_send_email_uses_starttls_login_and_recipient(notifications_module, monkeypatch):
    calls = []

    class FakeSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def ehlo(self):
            calls.append("ehlo")

        def starttls(self):
            calls.append("starttls")

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(("send", message["To"], message["Subject"]))

    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_FROM", "from@example.test")
    monkeypatch.setenv("SMTP_USERNAME", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "pass")
    monkeypatch.setattr(notifications_module.smtplib, "SMTP", lambda *_args, **_kwargs: FakeSMTP())

    notifications_module._send_email("to@example.test", "Subject", "Body")

    assert "starttls" in calls
    assert ("login", "user", "pass") in calls
    assert ("send", "to@example.test", "Subject") in calls


def test_send_email_wraps_smtp_errors(notifications_module, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_FROM", "from@example.test")
    monkeypatch.setattr(notifications_module.smtplib, "SMTP", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("down")))

    with pytest.raises(notifications_module.NotificationError, match="Email delivery failed"):
        notifications_module._send_email("to@example.test", "Subject", "Body")


def test_firebase_app_requires_mounted_service_account(notifications_module, monkeypatch, tmp_path):
    monkeypatch.setattr(notifications_module, "_firebase_ready", False)
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT_FILE", str(tmp_path / "missing.json"))

    with pytest.raises(notifications_module.NotificationError, match="Mount Firebase"):
        notifications_module._firebase_app()


def test_firebase_app_initializes_once_with_service_account(notifications_module, monkeypatch, tmp_path):
    service_account = tmp_path / "service-account.json"
    service_account.write_text("{}")
    initialized = []
    monkeypatch.setattr(notifications_module, "_firebase_ready", False)
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT_FILE", str(service_account))
    monkeypatch.setattr(notifications_module.credentials, "Certificate", lambda path: ("certificate", path))
    monkeypatch.setattr(notifications_module, "initialize_app", lambda credential: initialized.append(credential))

    notifications_module._firebase_app()
    notifications_module._firebase_app()

    assert initialized == [("certificate", str(service_account))]
    assert notifications_module._firebase_ready is True


def test_send_android_builds_message_and_sends_token(notifications_module, monkeypatch, item_and_offer):
    sent = []
    monkeypatch.setattr(notifications_module, "_firebase_app", lambda: None)
    monkeypatch.setattr(notifications_module.messaging, "Message", lambda **kwargs: kwargs)
    monkeypatch.setattr(notifications_module.messaging, "Notification", lambda **kwargs: kwargs)
    monkeypatch.setattr(notifications_module.messaging, "send", lambda message: sent.append(message))

    notifications_module._send_android("device-token", "Subject", "Body", "https://shop.example/deal")

    assert sent[0]["token"] == "device-token"
    assert sent[0]["data"] == {"deal_url": "https://shop.example/deal"}


def test_send_android_wraps_firebase_delivery_error(notifications_module, monkeypatch):
    monkeypatch.setattr(notifications_module, "_firebase_app", lambda: None)
    monkeypatch.setattr(notifications_module.messaging, "Message", lambda **kwargs: kwargs)
    monkeypatch.setattr(notifications_module.messaging, "Notification", lambda **kwargs: kwargs)
    monkeypatch.setattr(notifications_module.messaging, "send", lambda _message: (_ for _ in ()).throw(RuntimeError("offline")))

    with pytest.raises(notifications_module.NotificationError, match="Android push failed"):
        notifications_module._send_android("device-token", "Subject", "Body", "https://shop.example/deal")


def test_send_notification_dispatches_to_selected_backend(notifications_module, monkeypatch, item_and_offer):
    item, offer = item_and_offer
    calls = []
    monkeypatch.setattr(notifications_module, "_send_email", lambda *args: calls.append(("email", args)))
    monkeypatch.setattr(notifications_module, "_send_android", lambda *args: calls.append(("android", args)))

    notifications_module.send_notification(item, offer)
    item.notification_method = "android"
    item.notification_target = "token"
    notifications_module.send_notification(item, offer)

    assert [call[0] for call in calls] == ["email", "android"]


def test_send_notification_rejects_unknown_method(notifications_module, item_and_offer):
    item, offer = item_and_offer
    item.notification_method = "sms"

    with pytest.raises(notifications_module.NotificationError, match="Unknown notification"):
        notifications_module.send_notification(item, offer)
