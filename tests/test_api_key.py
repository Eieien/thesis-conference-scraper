import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app

client = TestClient(app)


@pytest.fixture
def keyed():
    app.dependency_overrides[get_settings] = lambda: Settings(api_keys=["old-key", "new-key"])
    yield
    app.dependency_overrides.clear()


def test_no_keys_configured_means_no_check():
    assert client.get("/runs").status_code == 200


def test_missing_or_wrong_key_is_401(keyed):
    for headers in ({}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}):
        r = client.get("/runs", headers=headers)
        assert r.status_code == 401
        assert "X-API-Key" in r.json()["detail"]


def test_any_configured_key_works(keyed):
    for key in ("old-key", "new-key"):
        assert client.get("/runs", headers={"X-API-Key": key}).status_code == 200


def test_key_in_query_string_is_not_accepted(keyed):
    assert client.get("/runs", params={"api_key": "new-key"}).status_code == 401


def test_scraping_routes_need_the_key_too(keyed):
    r = client.post("/tools/parse-dates", json={"text": "December 14-20, 2026"})
    assert r.status_code == 401


def test_health_stays_open(keyed):
    assert client.get("/health").status_code == 200
