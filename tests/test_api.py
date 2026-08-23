from datetime import datetime


def test_health_is_public_and_returns_utc_timestamp(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["time"].endswith("+00:00")


def test_ui_requires_basic_authentication(client):
    response = client.get("/")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="Deal Finder"'


def test_ui_and_static_assets_are_served_to_authenticated_user(client, auth_headers):
    page = client.get("/", headers=auth_headers)
    stylesheet = client.get("/static/styles.css", headers=auth_headers)

    assert page.status_code == 200
    assert "Deal Finder" in page.text
    assert stylesheet.status_code == 200
    assert "--accent" in stylesheet.text


def test_missing_static_asset_returns_not_found(client, auth_headers):
    response = client.get("/static/missing.css", headers=auth_headers)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_create_list_update_and_delete_item_api(client, auth_headers, item_payload, main_module, monkeypatch):
    created = client.post("/api/items", headers=auth_headers, json=item_payload)

    assert created.status_code == 201
    item_id = created.json()["id"]
    assert created.json()["name"] == "Gaming Laptop"
    assert client.get("/api/items", headers=auth_headers).json()[0]["id"] == item_id

    scheduled = []

    def capture_task(coroutine):
        scheduled.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(main_module.asyncio, "create_task", capture_task)
    updated_payload = {**item_payload, "region": "NL", "notification_method": "android", "notification_target": "fcm-token"}
    updated = client.put(f"/api/items/{item_id}", headers=auth_headers, json=updated_payload)

    assert updated.status_code == 200
    assert updated.json()["region"] == "NL"
    assert updated.json()["last_notified_price"] is None
    assert len(scheduled) == 1

    deleted = client.delete(f"/api/items/{item_id}", headers=auth_headers)

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get("/api/items", headers=auth_headers).json() == []


def test_create_item_rejects_invalid_price_range(client, auth_headers, item_payload):
    item_payload.update(min_price="200", max_price="100")

    response = client.post("/api/items", headers=auth_headers, json=item_payload)

    assert response.status_code == 422
    assert response.json()["detail"] == "Minimum price must not exceed maximum price."


def test_item_endpoints_return_not_found_for_unknown_item(client, auth_headers, item_payload):
    assert client.put("/api/items/999", headers=auth_headers, json=item_payload).status_code == 404
    assert client.delete("/api/items/999", headers=auth_headers).status_code == 404
    assert client.get("/api/items/999/history", headers=auth_headers).status_code == 404


def test_manual_check_returns_check_result(client, auth_headers, item_payload, main_module, monkeypatch):
    item_id = client.post("/api/items", headers=auth_headers, json=item_payload).json()["id"]

    async def checked(saved_id):
        assert saved_id == item_id
        return {"status": "ok", "offers": 3, "eligible": 0}

    monkeypatch.setattr(main_module, "check_item", checked)
    response = client.post(f"/api/items/{item_id}/check", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "offers": 3, "eligible": 0}


def test_price_history_returns_recorded_offer_with_utc_timestamp(client, auth_headers, item_payload, main_module):
    item_id = client.post("/api/items", headers=auth_headers, json=item_payload).json()["id"]
    with main_module.SessionLocal() as db:
        db.add(main_module.PriceHistory(item_id=item_id, title="Laptop", retailer="Shop", price="999.99", currency="USD", deal_url="https://shop.example/deal", found_at=datetime(2026, 1, 1)))
        db.commit()

    response = client.get(f"/api/items/{item_id}/history", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()[0]["price"] == "999.99"
    assert response.json()[0]["found_at"].endswith("Z") or response.json()[0]["found_at"].endswith("+00:00")
