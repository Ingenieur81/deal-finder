import asyncio
from decimal import Decimal

import httpx
import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://serpapi.com/search.json?api_key=secret")
            raise httpx.HTTPStatusError("credential should not leak", request=request, response=httpx.Response(self.status_code))

    def json(self):
        return self.payload


class FakeAsyncClient:
    def __init__(self, response, captured):
        self.response = response
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url, params):
        self.captured.update(url=url, params=params)
        return self.response


def make_item(main_module, **overrides):
    values = {
        "name": "Backpack", "region": "NL", "currency": "EUR", "notification_method": "email",
        "notification_target": "alerts@example.test", "enabled": True, "last_status": "never",
        "min_price": None, "max_price": None,
    }
    values.update(overrides)
    return main_module.WatchItem(**values)


def test_search_serpapi_parses_offers_filters_unsafe_links_and_targets_iso_region(main_module, monkeypatch):
    captured = {}
    payload = {"shopping_results": [
        {"title": "Valid", "source": "Shop", "extracted_price": "€1.299,99", "link": "https://shop.example/deal"},
        {"title": "Unsafe", "source": "Shop", "extracted_price": "€9,99", "link": "javascript:alert(1)"},
    ]}
    monkeypatch.setattr(main_module.httpx, "AsyncClient", lambda **_: FakeAsyncClient(FakeResponse(payload), captured))

    offers = asyncio.run(main_module.search_serpapi(make_item(main_module, region="NL")))

    assert captured["params"]["gl"] == "nl"
    assert "location" not in captured["params"]
    assert "The Netherlands" in captured["params"]["q"]
    assert len(offers) == 1
    assert offers[0].price == Decimal("1299.99")
    assert offers[0].deal_url == "https://shop.example/deal"


def test_search_serpapi_returns_only_the_lowest_valid_offer(main_module, monkeypatch):
    captured = {}
    payload = {"shopping_results": [
        {"title": "Expensive", "source": "Shop A", "extracted_price": "€99,99", "link": "https://a.example/deal"},
        {"title": "Lowest", "source": "Shop B", "extracted_price": "€49,99", "link": "https://b.example/deal"},
    ]}
    monkeypatch.setattr(main_module.httpx, "AsyncClient", lambda **_: FakeAsyncClient(FakeResponse(payload), captured))

    offers = asyncio.run(main_module.search_serpapi(make_item(main_module)))

    assert [offer.price for offer in offers] == [Decimal("49.99")]
    assert offers[0].retailer == "Shop B"


def test_search_serpapi_sends_normalized_country_code(main_module, monkeypatch, item_payload):
    captured = {}
    monkeypatch.setattr(main_module.httpx, "AsyncClient", lambda **_: FakeAsyncClient(FakeResponse({"shopping_results": []}), captured))
    item_payload["region"] = "The Netherlands"
    normalized_region = main_module.ItemInput(**item_payload).region

    assert asyncio.run(main_module.search_serpapi(make_item(main_module, region=normalized_region))) == []
    assert captured["params"]["gl"] == "nl"


def test_search_serpapi_rejects_missing_api_key(main_module, monkeypatch):
    monkeypatch.setattr(main_module, "SERPAPI_API_KEY", "")

    with pytest.raises(RuntimeError, match="SERPAPI_API_KEY"):
        asyncio.run(main_module.search_serpapi(make_item(main_module)))


def test_search_serpapi_reports_provider_error_payload(main_module, monkeypatch):
    captured = {}
    monkeypatch.setattr(main_module.httpx, "AsyncClient", lambda **_: FakeAsyncClient(FakeResponse({"error": "quota exhausted"}), captured))

    with pytest.raises(RuntimeError, match="quota exhausted"):
        asyncio.run(main_module.search_serpapi(make_item(main_module)))


def test_search_serpapi_redacts_api_key_from_http_failure(main_module, monkeypatch):
    captured = {}
    monkeypatch.setattr(main_module.httpx, "AsyncClient", lambda **_: FakeAsyncClient(FakeResponse({"error": "Invalid API key"}, 401), captured))

    with pytest.raises(RuntimeError, match="HTTP 401: Invalid API key") as error:
        asyncio.run(main_module.search_serpapi(make_item(main_module)))

    assert "secret" not in str(error.value)


def test_check_item_skips_disabled_item(main_module):
    with main_module.SessionLocal() as db:
        item = make_item(main_module, enabled=False)
        db.add(item)
        db.commit()

    assert asyncio.run(main_module.check_item(item.id)) == {"status": "skipped"}


def test_check_item_persists_offers_and_notifies_changed_best_price(main_module, monkeypatch):
    with main_module.SessionLocal() as db:
        item = make_item(main_module, min_price=Decimal("10"), max_price=Decimal("20"))
        db.add(item)
        db.commit()
        item_id = item.id
        db.add(main_module.PriceHistory(item_id=item_id, title="Backpack", retailer="Old Shop", price=Decimal("20"), currency="EUR", deal_url="https://old.example/deal"))
        db.commit()

    offer = main_module.SearchResult(title="Backpack", retailer="Shop", price=Decimal("15"), currency="EUR", deal_url="https://shop.example/deal")
    notified = []

    async def search(_):
        return [offer]

    async def run_in_thread(function, *args):
        notified.append(args)
        return function(*args)

    monkeypatch.setattr(main_module, "search_serpapi", search)
    monkeypatch.setattr(main_module.asyncio, "to_thread", run_in_thread)
    monkeypatch.setattr(main_module, "send_notification", lambda *_: None)

    result = asyncio.run(main_module.check_item(item_id))

    with main_module.SessionLocal() as db:
        saved = db.get(main_module.WatchItem, item_id)
        history = list(db.scalars(main_module.select(main_module.PriceHistory)))
    assert result == {"status": "matched", "offers": 1, "eligible": 1}
    assert saved.last_notified_price == Decimal("15.00")
    assert len(history) == 2
    assert len(notified) == 1


def test_check_item_records_notification_failure_without_losing_search(main_module, monkeypatch):
    with main_module.SessionLocal() as db:
        item = make_item(main_module)
        db.add(item)
        db.commit()
        item_id = item.id
        db.add(main_module.PriceHistory(item_id=item_id, title="Backpack", retailer="Old Shop", price=Decimal("20"), currency="EUR", deal_url="https://old.example/deal"))
        db.commit()

    offer = main_module.SearchResult(title="Backpack", retailer="Shop", price=Decimal("15"), currency="EUR", deal_url="https://shop.example/deal")

    async def search(_):
        return [offer]

    async def fail_notification(*_):
        raise main_module.NotificationError("SMTP failed")

    monkeypatch.setattr(main_module, "search_serpapi", search)
    monkeypatch.setattr(main_module.asyncio, "to_thread", fail_notification)

    result = asyncio.run(main_module.check_item(item_id))

    assert result["status"] == "notify_error"
    with main_module.SessionLocal() as db:
        assert db.get(main_module.WatchItem, item_id).last_error == "SMTP failed"


def test_check_item_records_search_error(main_module, monkeypatch):
    with main_module.SessionLocal() as db:
        item = make_item(main_module)
        db.add(item)
        db.commit()
        item_id = item.id

    async def fail_search(_):
        raise RuntimeError("provider offline")

    monkeypatch.setattr(main_module, "search_serpapi", fail_search)

    result = asyncio.run(main_module.check_item(item_id))

    assert result == {"status": "search_error", "error": "provider offline"}
    with main_module.SessionLocal() as db:
        assert db.get(main_module.WatchItem, item_id).last_status == "search_error"


def test_check_item_marks_successful_search_without_matches_as_ok(main_module, monkeypatch):
    with main_module.SessionLocal() as db:
        item = make_item(main_module, min_price=Decimal("100"))
        db.add(item)
        db.commit()
        item_id = item.id

    async def search(_):
        return [main_module.SearchResult(title="Backpack", retailer="Shop", price=Decimal("50"), currency="EUR", deal_url="https://shop.example/deal")]

    monkeypatch.setattr(main_module, "search_serpapi", search)

    assert asyncio.run(main_module.check_item(item_id)) == {"status": "ok", "offers": 1, "eligible": 0}
    with main_module.SessionLocal() as db:
        assert db.get(main_module.WatchItem, item_id).current_price == Decimal("50.00")


def test_check_item_does_not_notify_for_first_observed_price(main_module, monkeypatch):
    with main_module.SessionLocal() as db:
        item = make_item(main_module)
        db.add(item)
        db.commit()
        item_id = item.id

    async def search(_):
        return [main_module.SearchResult(title="Backpack", retailer="Shop", price=Decimal("15"), currency="EUR", deal_url="https://shop.example/deal")]

    async def unexpected_notification(*_):
        raise AssertionError("First observed price must not notify")

    monkeypatch.setattr(main_module, "search_serpapi", search)
    monkeypatch.setattr(main_module.asyncio, "to_thread", unexpected_notification)

    assert asyncio.run(main_module.check_item(item_id)) == {"status": "matched", "offers": 1, "eligible": 1}


def test_run_all_checks_processes_each_enabled_item_once(main_module, monkeypatch):
    with main_module.SessionLocal() as db:
        db.add_all([make_item(main_module, name="One"), make_item(main_module, name="Two")])
        db.commit()

    checked = []

    async def check(item_id):
        checked.append(item_id)

    async def no_wait(_):
        return None

    monkeypatch.setattr(main_module, "check_item", check)
    monkeypatch.setattr(main_module.asyncio, "sleep", no_wait)

    asyncio.run(main_module.run_all_checks())

    assert len(checked) == 2
