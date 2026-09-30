"""IEEE Computer Society CFP pages, against saved real pages (network faked).

Fixtures were fetched 2026-09-23 with the project's user agent. The sitemap index is unmodified.
The five sitemaps are trimmed to their first five <url> blocks plus every /conferences/ block
(blocks themselves unchanged). The CFP pages had their <script>, <style>, <svg> and stylesheet /
preload <link> tags removed to keep them small; the markup the parser reads is unchanged.
"""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.scrapers.http import FetchResult, RobotsDisallowed
from app.scrapers.ieeecs import (
    SITEMAP_INDEX_URL,
    IeeeCsAdapter,
    SitemapEntry,
    cfp_slug,
    is_allowed_url,
    parse_cfp_page,
    parse_sitemap,
    parse_sitemap_index,
    select_pages,
    slug_years,
    split_title,
    wanted,
)

FIXTURES = Path(__file__).parent / "fixtures"
WINDOW = (date(2026, 10, 1), date(2026, 11, 30))
CONF = "https://www.computer.org/conferences/"
SITEMAPS = [f"https://www.computer.org/sitemap/{n}.xml" for n in range(5)]
PAGES = [
    "cfp-ieee-compsac-2026",
    "cfp-ieee-sustain",
    "cfp-ieee-isope-2026",
    "cfp-ieee-host-2026",
    "cfp-ieee-itnac",
    "swebok-summit-2026-cfp",
    "ismar2026",
    "icme-2026",
]


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _page(slug: str):
    return parse_cfp_page(_read(f"ieeecs_{slug}.html"), CONF + slug)


def _entries() -> list[SitemapEntry]:
    out: list[SitemapEntry] = []
    for n in range(5):
        out.extend(parse_sitemap(_read(f"ieeecs_sitemap_{n}.xml")))
    return out


def test_allowed_urls_follow_robots():
    assert is_allowed_url(CONF + "cfp-ieee-sustain")
    assert is_allowed_url("https://www.computer.org/sitemap/0.xml")
    assert not is_allowed_url(CONF + "calendar?page=2")  # Disallow: /*?*
    assert not is_allowed_url("https://www.computer.org/api/conferences")
    assert not is_allowed_url("https://www.computer.org/_next/data/x.json")
    assert not is_allowed_url("https://www.computer.org/search")
    assert not is_allowed_url("https://www.computer.org/wp-json/wp/v2/posts")
    assert not is_allowed_url("http://www.computer.org/conferences/cfp-ieee-sustain")
    assert not is_allowed_url("https://conferences.computer.org/IC2E/2026/")


def test_sitemaps():
    assert parse_sitemap_index(_read("ieeecs_sitemap_index.xml")) == SITEMAPS
    entries = _entries()
    sustain = next(e for e in entries if e.url.endswith("/cfp-ieee-sustain"))
    assert sustain.lastmod == date(2026, 7, 14)
    cfps = {cfp_slug(e.url) for e in entries} - {None}
    assert {"cfp-ieee-compsac-2026", "icme-2026", "swebok-summit-2026-cfp"} <= cfps
    assert cfp_slug(CONF + "cfp/ICDCS2020") is None  # old /conferences/cfp/<X> pages
    assert cfp_slug(CONF + "organize-a-conference") is None
    assert cfp_slug(CONF + "top-computer-science-events") is None


def test_page_selection_by_slug_year_and_lastmod():
    assert slug_years("cfp-ieee-cai-2028-2029") == [2028, 2029]
    old = date(2025, 5, 30)
    assert not wanted(SitemapEntry(CONF + "cfp-ieee-host-2027", None), *WINDOW, 365)
    assert not wanted(SitemapEntry(CONF + "cfp-ieee-cai-2028-2029", None), *WINDOW, 365)
    assert wanted(SitemapEntry(CONF + "cfp-ieee-cai-2026-2027", None), *WINDOW, 365)
    assert not wanted(SitemapEntry(CONF + "cfp-ieee-bigdata", old), *WINDOW, 365)
    assert wanted(SitemapEntry(CONF + "cfp-ieee-bigdata", None), *WINDOW, 365)

    urls = select_pages(_entries(), *WINDOW, 365)
    slugs = {u.rsplit("/", 1)[-1] for u in urls}
    assert len(urls) == 35  # out of 196 /conferences/ URLs in the sitemaps
    assert set(PAGES) <= slugs
    for skipped in ("cfp-ieee-host-2027", "cfp-ieee-nca-2025", "cfp-coins-2025", "compsac-2025"):
        assert skipped not in slugs
    assert "cfp-ieee-bigdata" not in slugs  # year-less, last modified 2025-05-30
    assert all(is_allowed_url(u) for u in urls)


def test_split_title():
    assert split_title("Call for Submissions: IEEE COMPSAC 2026") == "COMPSAC"
    assert split_title("Call for Submissions: IEEE APWG eCrime 2025") == "APWG eCrime"
    assert split_title("Call for Submissions: IEEE DCOSS-IoT") == "DCOSS-IoT"
    assert split_title("Call for Presentations: IEEE SWEBOK Summit 2026") == "SWEBOK Summit"


def test_page_with_h4_and_full_name():
    out = _page("cfp-ieee-compsac-2026")
    assert out.source_id == "cfp-ieee-compsac-2026"
    assert out.url == CONF + "cfp-ieee-compsac-2026"
    assert out.acronym == "COMPSAC"
    assert out.name == "IEEE Signature Conference on Computers, Software, & Applications"
    assert (out.start_date, out.end_date) == (date(2026, 7, 7), date(2026, 7, 10))
    assert (out.location_raw, out.city, out.country) == ("Madrid, Spain", "Madrid", "Spain")
    assert out.homepage == "https://ieeecompsac.computer.org/2026/call-for-papers/"
    assert out.paper_deadline is None
    assert out.description and "Agentic AI" in out.description
    assert out.extra["ieeecs_title"] == "Call for Submissions: IEEE COMPSAC 2026"


def test_year_less_slug_in_window_in_asia():
    out = _page("cfp-ieee-sustain")
    assert out.acronym == "SUSTAIN"
    assert out.name == "IEEE SUSTAIN 2026"  # no short name paragraph: the title stands in
    assert (out.start_date, out.end_date) == (date(2026, 11, 29), date(2026, 12, 2))
    assert (out.city, out.country) == ("Dhahran", "Saudi Arabia")
    assert out.homepage == "https://sustainconf.com/en"


def test_ampersand_dates_and_fuller_place():
    out = _page("cfp-ieee-isope-2026")
    assert (out.start_date, out.end_date) == (date(2026, 10, 1), date(2026, 10, 2))
    # The paragraph says "New York City, NY, USA"; the <h4> only "NYC, NY".
    assert out.location_raw == "New York City, NY, USA"
    assert out.country == "USA"


def test_contribution_rounds_and_signup_link():
    out = _page("cfp-ieee-host-2026")
    assert out.name == "IEEE International Symposium on Hardware Oriented Security and Trust (HOST)"
    assert out.paper_deadline == date(2025, 12, 1)  # the second (last) round
    assert out.extra["other_dates"] == {
        "contribution deadlines": "19 Aug 2025 (first round) and 1 Dec 2025 (second round)"
    }
    assert out.homepage is None  # "Learn More" is a join.computer.org mailing-list form


def test_key_dates_list():
    out = _page("swebok-summit-2026-cfp")
    assert out.acronym == "SWEBOK Summit"
    # "Conference Date: 18 April 2026 - Co-located with ..." has no place; the <h4> has both.
    assert out.start_date == out.end_date == date(2026, 4, 18)
    assert out.country == "Brazil"
    assert out.abstract_deadline == date(2025, 11, 3)
    assert out.notification_date == date(2025, 12, 1)
    assert out.camera_ready_deadline == date(2025, 12, 22)
    assert out.paper_deadline == date(2026, 1, 5)  # "Talk summary submission due"
    assert "talk summary acceptance notification" in out.extra["other_dates"]


def test_old_edition_on_year_less_slug():
    out = _page("cfp-ieee-itnac")
    assert out.acronym == "ITNAC"
    assert out.start_date == date(2025, 11, 26)  # last year's edition; the filter drops it


def test_non_event_pages_are_skipped():
    assert _page("icme-2026") is None  # a call for hosting proposals
    assert _page("ismar2026") is None  # a mentoring session, no conference dates


class _FakeFetcher:
    def __init__(self, pages: dict[str, str], default: str | None = None) -> None:
        self.pages = pages
        self.default = default
        self.calls: list[str] = []

    async def get(self, url: str, **_) -> FetchResult:
        self.calls.append(url)
        body = self.pages.get(url, self.default)
        if body is None:
            raise AssertionError(f"unexpected request: {url}")
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            content_type="text/html",
            content=body.encode("utf-8"),
            from_cache=False,
        )


def _site() -> dict[str, str]:
    pages = {SITEMAP_INDEX_URL: _read("ieeecs_sitemap_index.xml")}
    pages.update({u: _read(f"ieeecs_sitemap_{n}.xml") for n, u in enumerate(SITEMAPS)})
    pages.update({CONF + s: _read(f"ieeecs_{s}.html") for s in PAGES})
    return pages


def _settings(**kw) -> Settings:
    kw.setdefault("window_start", WINDOW[0])
    kw.setdefault("window_end", WINDOW[1])
    return Settings(**kw)


async def _collect(adapter, fetcher, **kw):
    return [x async for x in adapter.collect(fetcher, **kw)]


def test_collect():
    # Pages without a fixture get the ISMAR mentoring page, which is not an event.
    fetcher = _FakeFetcher(_site(), default=_read("ieeecs_ismar2026.html"))
    out = asyncio.run(_collect(IeeeCsAdapter(_settings()), fetcher))
    assert fetcher.calls[:6] == [SITEMAP_INDEX_URL, *SITEMAPS]
    assert len(fetcher.calls) == 6 + 35
    assert all("?" not in u for u in fetcher.calls)
    by_id = {x.source_id: x for x in out}
    assert set(by_id) == set(PAGES) - {"ismar2026", "icme-2026"}
    in_window = [x for x in out if WINDOW[0] <= x.start_date <= WINDOW[1]]
    assert sorted(x.acronym for x in in_window) == ["ISoPE", "SUSTAIN"]


def test_collect_max_pages():
    fetcher = _FakeFetcher(_site(), default=_read("ieeecs_ismar2026.html"))
    asyncio.run(_collect(IeeeCsAdapter(_settings()), fetcher, max_pages=3))
    assert len(fetcher.calls) == 6 + 3


def test_disallowed_sitemap_is_never_requested():
    index = _read("ieeecs_sitemap_index.xml").replace(
        "https://www.computer.org/sitemap/0.xml", "https://www.computer.org/sitemap/0.xml?page=1"
    )
    assert parse_sitemap_index(index) == SITEMAPS[1:]
    adapter = IeeeCsAdapter(_settings())
    with pytest.raises(RobotsDisallowed):
        asyncio.run(adapter._get(_FakeFetcher({}), "https://www.computer.org/api/events"))


def test_browse_page_reads_only_sitemaps():
    fetcher = _FakeFetcher(_site())
    page = asyncio.run(IeeeCsAdapter(_settings()).browse_page(fetcher))
    assert len(page.listings) == 35 and not page.has_next
    assert len(fetcher.calls) == 6
    assert "cfp-ieee-sustain" in {x.source_id for x in page.listings}
    empty = asyncio.run(IeeeCsAdapter(_settings()).browse_page(fetcher, page=2))
    assert empty.listings == [] and not empty.has_next
