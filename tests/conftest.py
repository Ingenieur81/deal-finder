import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def main_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reload the application against a fresh SQLite database for each test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("APP_USERNAME", "tester")
    monkeypatch.setenv("APP_PASSWORD", "test-password")
    monkeypatch.setenv("SEARCH_INTERVAL_MINUTES", "60")
    monkeypatch.setenv("SERPAPI_API_KEY", "test-serp-key")

    import app.main as main

    if main.scheduler.running:
        main.scheduler.shutdown(wait=False)
    main = importlib.reload(main)
    main.DATA_DIR.mkdir(parents=True, exist_ok=True)
    main.Base.metadata.create_all(main.engine)
    yield main
    if main.scheduler.running:
        main.scheduler.shutdown(wait=False)
    main.engine.dispose()


@pytest.fixture
def client(main_module):
    with TestClient(main_module.app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers():
    return {"Authorization": "Basic dGVzdGVyOnRlc3QtcGFzc3dvcmQ="}


@pytest.fixture
def item_payload():
    return {
        "name": "Gaming Laptop",
        "min_price": "800.00",
        "max_price": "1200.00",
        "region": "US",
        "currency": "USD",
        "notification_method": "email",
        "notification_target": "alerts@example.test",
        "enabled": True,
    }
