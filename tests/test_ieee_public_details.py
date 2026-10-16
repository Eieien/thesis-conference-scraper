import json
from pathlib import Path

from app.scrapers.ieee import is_detail_response, parse_detail

FIXTURE = Path(__file__).parent / "fixtures" / "ieee_public_details.json"


def test_both_detail_addresses_are_recognised():
    assert is_detail_response("https://conference-api.ieee.org/public/details?id=71525")
    assert is_detail_response(
        "https://conferences.ieee.org/conferences_events/server/conf/details?id=1"
    )
    assert not is_detail_response("https://conferences.ieee.org/conferences_events/main.bundle.js")
    assert not is_detail_response(
        "https://conferences.ieee.org/conferences_events/server/servicedisruption"
    )


def test_the_new_detail_data_parses():
    """Saved from conference-api.ieee.org on 2026-10-11 (ICMTS 2027)."""
    detail = parse_detail(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert detail["event_id"] == 71525
    assert detail["homepage"] == "https://icmts.net/"
    assert detail["venue"].startswith("Auditorium Sgorlon")
    assert "test structure design" in detail["keywords"]
