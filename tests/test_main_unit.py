from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("raw_price", "expected"),
    [
        ("$1,299.99", Decimal("1299.99")),
        ("€1.299,99", Decimal("1299.99")),
        ("19,95 EUR", Decimal("19.95")),
        ("1,299", Decimal("1299.00")),
        (42, Decimal("42.00")),
    ],
)
def test_parse_price_supports_common_price_formats(main_module, raw_price, expected):
    assert main_module.parse_price(raw_price) == expected


def test_parse_price_returns_none_for_missing_or_non_numeric_values(main_module):
    assert main_module.parse_price(None) is None
    assert main_module.parse_price("price unavailable") is None


def test_item_input_trims_fields_and_normalizes_currency(main_module, item_payload):
    item_payload.update(name="  Backpack  ", region="  NL ", currency=" eur ", notification_target="  a@b.cd ")

    item = main_module.ItemInput(**item_payload)

    assert item.name == "Backpack"
    assert item.region == "NL"
    assert item.currency == "EUR"
    assert item.notification_target == "a@b.cd"


def test_item_input_rejects_blank_required_text(main_module, item_payload):
    item_payload["name"] = "   "

    with pytest.raises(ValidationError):
        main_module.ItemInput(**item_payload)


def test_item_input_rejects_inverted_price_range(main_module, item_payload):
    item_payload.update(min_price="1200", max_price="800")

    with pytest.raises(main_module.HTTPException, match="Minimum price"):
        main_module.ItemInput(**item_payload).validate_price_range()


def test_as_utc_marks_sqlite_naive_timestamp_as_utc(main_module):
    naive = datetime(2026, 1, 2, 3, 4, 5)

    assert main_module.as_utc(naive) == naive.replace(tzinfo=timezone.utc)
    assert main_module.as_utc(None) is None


def test_as_utc_preserves_aware_timestamp(main_module):
    aware = datetime(2026, 1, 2, tzinfo=timezone.utc)

    assert main_module.as_utc(aware) is aware


def test_is_eligible_honors_both_price_bounds(main_module):
    item = main_module.WatchItem(min_price=Decimal("10"), max_price=Decimal("20"))

    assert main_module.is_eligible(item, main_module.SearchResult(title="a", retailer="b", price=Decimal("10"), currency="USD", deal_url="https://x.test"))
    assert not main_module.is_eligible(item, main_module.SearchResult(title="a", retailer="b", price=Decimal("9.99"), currency="USD", deal_url="https://x.test"))
    assert not main_module.is_eligible(item, main_module.SearchResult(title="a", retailer="b", price=Decimal("20.01"), currency="USD", deal_url="https://x.test"))


def test_is_eligible_accepts_any_price_when_bounds_are_empty(main_module):
    item = main_module.WatchItem(min_price=None, max_price=None)
    offer = main_module.SearchResult(title="a", retailer="b", price=Decimal("999"), currency="USD", deal_url="https://x.test")

    assert main_module.is_eligible(item, offer)


def test_serialize_item_emits_timezone_aware_dates(main_module):
    item = main_module.WatchItem(
        id=1, name="Boots", region="NL", currency="EUR", notification_method="email", notification_target="a@b.cd",
        enabled=True, last_status="never", created_at=datetime(2026, 1, 2), updated_at=datetime(2026, 1, 2),
    )

    serialized = main_module.serialize_item(item)

    assert serialized.created_at.tzinfo == timezone.utc
    assert serialized.updated_at.tzinfo == timezone.utc


def test_require_basic_auth_rejects_missing_credentials(main_module):
    from starlette.requests import Request

    request = Request({"type": "http", "path": "/api/items", "headers": []})

    with pytest.raises(main_module.HTTPException) as error:
        main_module.require_basic_auth(request)

    assert error.value.status_code == 401


def test_require_basic_auth_accepts_valid_credentials(main_module):
    from starlette.requests import Request

    request = Request({"type": "http", "path": "/api/items", "headers": [(b"authorization", b"Basic dGVzdGVyOnRlc3QtcGFzc3dvcmQ=")]})

    assert main_module.require_basic_auth(request) is None
