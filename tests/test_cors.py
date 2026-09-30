from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _preflight(origin: str, method: str = "GET"):
    return client.options(
        "/health", headers={"Origin": origin, "Access-Control-Request-Method": method}
    )


def test_a_local_frontend_may_read():
    r = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert _preflight("http://127.0.0.1:5173").status_code == 200


def test_other_sites_and_writes_are_not_allowed():
    r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    assert _preflight("https://evil.example").status_code == 400
    # A web page may read the data, but not start scraping runs.
    assert _preflight("http://localhost:3000", "POST").status_code == 400
