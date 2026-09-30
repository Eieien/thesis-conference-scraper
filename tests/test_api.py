from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_sources_listed():
    names = {s["name"] for s in client.get("/sources").json()}
    assert {"edas", "wikicfp", "ieee"} <= names


def test_unimplemented_source_returns_501():
    assert client.post("/sources/acm/preview").status_code == 501


def test_parse_dates_tool():
    r = client.post("/tools/parse-dates", json={"text": "December 14-20, 2026"})
    assert r.json() == {"start": "2026-12-14", "end": "2026-12-20"}


def test_unknown_continent_is_rejected():
    assert client.get("/conferences", params={"continent": "Atlantis"}).status_code == 422


def test_compact_lists_leave_out_long_text():
    for path in ("/conferences", "/listings"):
        full = client.get(path, params={"limit": 1}).json()
        slim = client.get(path, params={"limit": 1, "compact": True}).json()
        assert len(full) == len(slim)
        for row in slim:
            assert "description" not in row
            assert "field_sources" not in row
            assert "name" in row and "start_date" in row


def test_source_filter_and_counts():
    counts = client.get("/summary").json()["conferences_by_source"]
    for source, n in counts.items():
        rows = client.get("/conferences", params={"source": source, "limit": 1000, "compact": True})
        assert len(rows.json()) == n
        assert all(source in r["sources"] for r in rows.json())


def test_fees_come_in_usd_and_php_when_rates_are_cached():
    from app.config import get_settings
    from app.pipeline.fx import load_rates

    rows = client.get(
        "/conferences", params={"has_fees": True, "limit": 20, "compact": True}
    ).json()
    if not rows or load_rates(get_settings()) is None:
        return  # an empty database or no cached rates: nothing to convert
    for r in rows:
        assert set(r) >= {"fee_min_usd", "fee_max_usd", "fee_min_php", "fee_max_php"}
        if r["currency"] == "USD":
            assert r["fee_min_usd"] == r["fee_min"]
            assert r["fee_min_php"] > r["fee_min"] * 20  # a peso is worth well under a dollar


def test_summary_reports_scope():
    scope = client.get("/summary").json()["scope"]
    assert scope["continents"] == ["Asia"]
    assert scope["conferences_with_fees"] <= scope["conferences"]


def test_summary_listing_counts_are_listing_rows():
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import RawListing

    s = client.get("/summary").json()
    with SessionLocal() as db:
        rows = db.scalar(select(func.count()).select_from(RawListing))
    # Once overwritten by the per-source conference counts; they must stay listing rows.
    assert sum(s["listings_by_source"].values()) == rows


def test_date_range_filters():
    params = {"limit": 1000, "compact": True}
    rows = client.get(
        "/conferences", params={**params, "starts_from": "2026-11-01", "starts_to": "2026-11-15"}
    ).json()
    assert all("2026-11-01" <= r["start_date"] <= "2026-11-15" for r in rows)
    rows = client.get(
        "/conferences",
        params={**params, "abstract_from": "2026-09-01", "abstract_to": "2026-09-30"},
    ).json()
    assert all("2026-09-01" <= r["abstract_deadline"] <= "2026-09-30" for r in rows)
    rows = client.get("/conferences", params={**params, "has_abstract": False}).json()
    assert all(r["abstract_deadline"] is None for r in rows)
    bad = client.get("/conferences", params={"starts_from": "not-a-date"})
    assert bad.status_code == 422
