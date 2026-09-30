import json
from datetime import date
from pathlib import Path

from app.scrapers.ieee import parse_response, parse_result, search_url

FIXTURE = Path(__file__).parent / "fixtures" / "ieee_searchfacet.json"


def test_search_results_become_listings():
    # A real response as the IEEE search page received it (2026-09-24), unmodified.
    total, rows = parse_response(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert total == 1904 and len(rows) == 10
    first = parse_result(rows[0])
    assert first.source_id == "70095"
    assert first.acronym == "ICOSICS"
    assert first.name.startswith("2026 International Conference on Secure IoT")
    assert (first.start_date, first.end_date) == (date(2026, 9, 23), date(2026, 9, 25))
    assert (first.city, first.country) == ("Bengaluru", "India")
    assert "Computing and Processing" in first.categories
    assert first.url.endswith("/conferencedetails/70095")


def test_every_result_parses():
    _, rows = parse_response(json.loads(FIXTURE.read_text(encoding="utf-8")))
    for r in rows:
        listing = parse_result(r)
        assert listing.source_id and listing.name and listing.start_date


def test_search_url_has_the_filters():
    url = search_url("Region10-Asia and Pacific", "Computing and Processing", 30)
    assert "region=Region10-Asia+and+Pacific" in url
    assert "field_of_interest=Computing+and+Processing" in url
    assert "pos=30" in url and "sortfield=dates" in url and "sortorder=asc" in url


def test_detail_page_gives_the_official_website():
    from app.scrapers.ieee import parse_detail

    # A real detail response as the IEEE page received it (2026-09-24), unmodified.
    fixture = Path(__file__).parent / "fixtures" / "ieee_details.json"
    d = parse_detail(json.loads(fixture.read_text(encoding="utf-8")))
    assert d["event_id"] == 71606
    assert d["homepage"] == "https://iccmn.in/"  # IEEE stored it as "iccmn.in/"
    assert d["description"].startswith("COMPUTER SCIENCE, COMMUNICATION ENGINEERING")
    assert "Belagavi" in d["venue"]
    assert d["call_for_papers"] == "2026-05-05"


def test_websites_become_links():
    from app.scrapers.ieee import _as_url

    assert _as_url("www.example.org") == "https://www.example.org"
    assert _as_url("http://example.org/2026") == "http://example.org/2026"
    assert _as_url("") is None
    assert _as_url("TBA") is None


def test_detail_fills_homepage_and_requeues_for_enrich():
    from app.models import Conference, RawListing
    from app.pipeline.ieee_details import NO_HOMEPAGE_NOTE, apply_detail

    conf = Conference(name="X", status="needs_review", review_note=NO_HOMEPAGE_NOTE)
    row = RawListing(source="ieee", source_id="71606", name="X", url="https://ieee.example/71606")
    detail = {"event_id": 71606, "homepage": "https://iccmn.in/", "description": "Scope text"}
    apply_detail(conf, row, detail)
    assert conf.homepage == row.homepage == "https://iccmn.in/"
    assert conf.field_sources["homepage"] == {"source": "ieee", "url": "https://ieee.example/71606"}
    assert (conf.status, conf.review_note) == ("collected", None)  # enrich will read it now
    assert conf.description == "Scope text"
