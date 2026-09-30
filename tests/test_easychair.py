"""EasyChair Smart CFP parsers and paging, against saved real pages (network faked).

easychair_area_computing.html and easychair_home.html are real pages with most table rows cut
out (the originals are 0.5-0.9 MB); the detail pages are unmodified.
"""

import asyncio
from datetime import date
from pathlib import Path

from app.config import Settings
from app.scrapers.easychair import (
    AREA_URL,
    EasyChairAdapter,
    parse_cfp_page,
    parse_list_page,
    split_date_place,
)
from app.scrapers.http import FetchResult

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def _by_id(name: str) -> dict:
    return {r.listing.source_id: r for r in parse_list_page(_read(name))}


def test_area_page_rows():
    rows = _by_id("easychair_area_computing.html")
    assert len(rows) == 33
    nordsec = rows["nordsec26"].listing
    assert nordsec.acronym == "Nordsec"
    assert nordsec.name == "31st Nordic Conference on Secure IT Systems"
    assert nordsec.url == "https://easychair.org/cfp/nordsec26"
    assert nordsec.start_date == date(2026, 11, 11)  # from the data-key epoch
    assert nordsec.end_date is None  # only the detail page has it
    assert nordsec.paper_deadline == date(2026, 8, 17)
    assert (nordsec.city, nordsec.country) == ("Aarhus", "Denmark")
    assert "security" in nordsec.categories
    assert rows["accml-26"].listing.acronym == "AccML"  # "9th AccML"
    assert rows["ICTAI2026"].listing.paper_deadline == date(2026, 7, 22)
    undated = [r for r in rows.values() if r.undated]
    assert len(undated) == 1 and undated[0].listing.start_date is None


def test_home_page_uses_short_dates():
    rows = _by_id("easychair_home.html")
    assert len(rows) == 23
    qccs = rows["qccs2026"].listing
    assert qccs.start_date == date(2026, 11, 11)
    assert qccs.paper_deadline == date(2026, 10, 25)
    assert qccs.country == "United States"
    assert rows["herit-ai-2026"].undated


def test_split_date_place():
    assert split_date_place("Boca Raton, FL, United States, November 2-4, 2026") == (
        "Boca Raton, FL, United States",
        "November 2-4, 2026",
    )
    assert split_date_place("Pontianak, Indonesia, November 12, 2026") == (
        "Pontianak, Indonesia",
        "November 12, 2026",
    )
    assert split_date_place("Online") == ("Online", None)


def test_detail_page_ictai():
    base = _by_id("easychair_area_computing.html")["ICTAI2026"].listing
    out = parse_cfp_page(_read("easychair_cfp_ictai2026.html"), base)
    assert out.start_date == date(2026, 11, 2)
    assert out.end_date == date(2026, 11, 4)
    assert out.homepage == "https://ictai.computer.org/2026/"
    assert out.paper_deadline == date(2026, 7, 22)
    assert out.location_raw == "Boca Raton, FL, United States"
    assert out.extra["submission_link"] == "https://easychair.org/conferences/?conf=ictai2026"
    assert out.description and "Camera ready paper" in out.description
    assert "Conference web page" not in out.description  # key-dates table is left out


def test_detail_page_deadlines():
    rows = _by_id("easychair_area_computing.html")
    nordsec = parse_cfp_page(_read("easychair_cfp_nordsec26.html"), rows["nordsec26"].listing)
    assert nordsec.end_date == date(2026, 11, 12)
    assert nordsec.abstract_deadline == date(2026, 8, 17)
    assert nordsec.homepage == "http://nordsec2026.dk/"

    base = _by_id("easychair_home.html")["icosin-2026"].listing
    icosin = parse_cfp_page(_read("easychair_cfp_icosin2026.html"), base)
    assert icosin.abstract_deadline == date(2026, 10, 11)
    assert icosin.paper_deadline == date(2026, 10, 12)
    assert icosin.start_date == icosin.end_date == date(2026, 11, 12)


class _FakeFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def get(self, url: str, **_) -> FetchResult:
        self.calls.append(url)
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            content_type="text/html",
            content=self.pages[url].encode("utf-8"),
            from_cache=False,
        )


async def _collect(adapter, fetcher, **kw):
    return [x async for x in adapter.collect(fetcher, **kw)]


def test_collect_dedupes_across_areas_and_stops():
    area_html = _read("easychair_area_computing.html")
    fetcher = _FakeFetcher({AREA_URL.format(area=a): area_html for a in (1, 18)})
    adapter = EasyChairAdapter(Settings(easychair_areas=[1, 18]))  # the two saved area pages
    out = asyncio.run(_collect(adapter, fetcher))
    # Same page for both areas: the second brings no new IDs, so nothing is yielded twice.
    assert len(out) == 32  # 33 rows minus the undated one
    assert len({x.source_id for x in out}) == len(out)
    assert fetcher.calls == [AREA_URL.format(area=1), AREA_URL.format(area=18)]
    assert all(x.extra["easychair_area"] == "Computing" for x in out)

    fetcher = _FakeFetcher({AREA_URL.format(area=1): area_html})
    assert len(asyncio.run(_collect(adapter, fetcher, max_pages=1))) == 32
    assert fetcher.calls == [AREA_URL.format(area=1)]


def test_browse_page_walks_areas():
    area_html = _read("easychair_area_computing.html")
    fetcher = _FakeFetcher({AREA_URL.format(area=a): area_html for a in (1, 18)})
    adapter = EasyChairAdapter(Settings(easychair_areas=[1, 18]))  # the two saved area pages
    first = asyncio.run(adapter.browse_page(fetcher, page=1))
    assert first.has_next and len(first.listings) == 32
    last = asyncio.run(adapter.browse_page(fetcher, page=2))
    assert not last.has_next
    beyond = asyncio.run(adapter.browse_page(fetcher, page=3))
    assert beyond.listings == [] and not beyond.has_next
