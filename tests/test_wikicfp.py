from datetime import date
from pathlib import Path

from app.schemas import ListingData
from app.scrapers.wikicfp import parse_category_page, parse_event_page

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def test_open_category_page():
    rows = parse_category_page(_read("wikicfp_category_open.html"))
    assert len(rows) == 20
    assert not any(r.expired for r in rows)
    assert next(r for r in rows if r.listing.acronym == "HIIJ").undated  # a journal
    cei = next(r.listing for r in rows if r.listing.acronym == "CEI")
    assert cei.source_id == "202993"
    assert cei.start_date == date(2026, 11, 20)
    assert cei.end_date == date(2026, 11, 22)
    assert cei.country == "China"
    assert cei.paper_deadline == date(2026, 9, 24)


def test_expired_section_is_flagged():
    rows = parse_category_page(_read("wikicfp_category_expired.html"))
    assert rows and all(r.expired for r in rows)


def test_event_page_details():
    base = ListingData(source_id="202993", name="CEI", url="http://www.wikicfp.com/x")
    out = parse_event_page(_read("wikicfp_event.html"), base)
    assert out.homepage == "https://ais.cn/u/Mvqeuu"
    assert out.start_date == date(2026, 11, 20)
    assert out.paper_deadline == date(2026, 9, 24)
    assert out.camera_ready_deadline == date(2026, 10, 21)
    assert "computer science" in out.categories
    assert out.description and "Nanning" in out.description
