"""ConferenceIndex parsers and paging, against saved real pages (network faked).

All fixtures are unmodified pages fetched 2026-09-23: the worldwide computer-science listing
(page 1 of 8), the Japan and Philippines computer-science listings, and three event pages.
"""

import asyncio
from datetime import date
from pathlib import Path

from app.config import Settings
from app.schemas import ListingData
from app.scrapers.conferenceindex import (
    ASIA_COUNTRIES,
    ConferenceIndexAdapter,
    has_next_page,
    list_paths,
    list_url,
    parse_event_page,
    parse_list_page,
    split_name,
)
from app.scrapers.http import FetchResult

FIXTURES = Path(__file__).parent / "fixtures"
WINDOW = (date(2026, 10, 1), date(2026, 11, 30))


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def _by_id(name: str) -> dict[str, ListingData]:
    return {r.source_id: r for r in parse_list_page(_read(name))}


def _in_window(rows) -> list[ListingData]:
    return [r for r in rows if r.start_date and WINDOW[0] <= r.start_date <= WINDOW[1]]


def test_split_name():
    assert split_name("International Conference on Artificial Intelligence (ICAI)") == (
        "International Conference on Artificial Intelligence",
        "ICAI",
    )
    assert split_name("flutterCon") == ("flutterCon", None)


def test_worldwide_listing_rows():
    rows = _by_id("conferenceindex_cs.html")
    assert len(rows) == 500  # one page holds 500 rows
    assert has_next_page(_read("conferenceindex_cs.html"))
    iccac = rows[
        "international-conference-on-cybersecurity-and-advanced-computing-iccac-2026-october-singapore-sg"
    ]
    assert iccac.name == "International Conference on Cybersecurity and Advanced Computing"
    assert iccac.acronym == "ICCAC"
    assert iccac.start_date == date(2026, 10, 2)
    assert iccac.end_date is None  # only the event page has it
    assert (iccac.city, iccac.country) == ("Singapore", "Singapore")
    assert iccac.url == (
        "https://conferenceindex.org/event/"
        "international-conference-on-cybersecurity-and-advanced-computing-iccac-2026-october-singapore-sg"
    )
    # Page 1 already runs from late September into December, so it covers the whole window.
    assert len(_in_window(rows.values())) == 421
    assert max(r.start_date for r in rows.values()) > WINDOW[1]


def test_generic_names_keep_their_own_ids():
    rows = parse_list_page(_read("conferenceindex_cs.html"))
    iccsit = [r for r in rows if r.acronym == "ICCSIT"]
    assert len(iccsit) > 3
    assert len({r.source_id for r in iccsit}) == len(iccsit)
    # The same event twice in one city: the site adds "-1" to the second slug.
    bangkok = [r.source_id for r in rows if r.acronym == "ICSIE" and r.city == "Bangkok"]
    assert len(bangkok) == 2 and any(s.endswith("-1") for s in bangkok)


def test_country_listings():
    japan = parse_list_page(_read("conferenceindex_cs_japan.html"))
    assert len(japan) == 217 and not has_next_page(_read("conferenceindex_cs_japan.html"))
    assert {r.country for r in japan} == {"Japan"}
    assert len(_in_window(japan)) == 31
    # Months and years come from the card headers: the list runs into 2028.
    assert max(r.start_date for r in japan).year == 2028

    ph = parse_list_page(_read("conferenceindex_cs_philippines.html"))
    assert len(ph) == 12
    assert [(r.acronym, r.start_date) for r in _in_window(ph)] == [("ICRAET", date(2026, 10, 27))]


def test_event_page_single_day():
    out = parse_event_page(
        _read("conferenceindex_event_iccac_singapore.html"),
        ListingData(source_id="iccac", name="", acronym=None),
    )
    assert out.name == "International Conference on Cybersecurity and Advanced Computing"
    assert out.acronym == "ICCAC"
    assert out.start_date == out.end_date == date(2026, 10, 2)
    assert out.homepage == "https://iitr.org.in/Conference/153987/ICCAC/"  # no utm_ tracking
    assert (out.city, out.country) == ("Singapore", "Singapore")
    assert out.extra["venue"] == "Village Hotel Changi"
    assert out.extra["conferenceindex_id"] == "1852432"
    assert "computer science" in out.categories
    assert out.description and out.description.startswith("International Conference on Cyber")
    assert out.paper_deadline is None


def test_event_page_deadlines():
    japan = _by_id("conferenceindex_cs_japan.html")
    base = japan[
        "international-conference-on-information-network-and-computer-communications"
        "-incc-2026-november-niigata-jp-1"
    ]
    incc = parse_event_page(_read("conferenceindex_event_incc_niigata.html"), base)
    assert incc.source_id == base.source_id
    assert (incc.start_date, incc.end_date) == (date(2026, 11, 28), date(2026, 11, 30))
    assert incc.homepage == "https://www.incc.net/"
    assert incc.paper_deadline == date(2026, 10, 15)  # "Final Submission"
    assert incc.extra["program_url"] == "https://www.incc.net/program.html"
    assert "Internet of Things" in incc.description  # the Tracks tab is kept

    base = japan[
        "international-conference-on-computing-and-information-technology-iccit-2026-october-tokyo-jp"
    ]
    iccit = parse_event_page(_read("conferenceindex_event_iccit_tokyo.html"), base)
    assert (iccit.start_date, iccit.end_date) == (date(2026, 10, 5), date(2026, 10, 6))
    assert iccit.paper_deadline == date(2026, 9, 7)
    assert iccit.notification_date == date(2026, 9, 16)
    assert iccit.homepage.startswith("https://waset.org/")
    assert iccit.extra["organizer"] == "World Academy of Science, Engineering and Technology"


def test_list_paths():
    assert list_paths(Settings())[0] == "computer-science/" + ASIA_COUNTRIES[0]
    assert len(list_paths(Settings())) == len(ASIA_COUNTRIES)
    s = Settings()
    object.__setattr__(s, "conferenceindex_countries", [])
    assert list_paths(s) == ["computer-science"]
    assert list_url("computer-science", 2).endswith("/conferences/computer-science?page=2")


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


def _settings(countries: list[str], **kw) -> Settings:
    kw.setdefault("window_start", WINDOW[0])
    kw.setdefault("window_end", WINDOW[1])
    s = Settings(**kw)
    object.__setattr__(s, "conferenceindex_countries", countries)
    return s


def test_collect_stops_past_the_window():
    # Page 1 reaches December, past the window end: page 2 is never requested.
    url = list_url("computer-science")
    fetcher = _FakeFetcher({url: _read("conferenceindex_cs.html")})
    out = asyncio.run(_collect(ConferenceIndexAdapter(_settings([])), fetcher))
    assert len(out) == 500
    assert fetcher.calls == [url]
    assert all(x.extra["conferenceindex_list"] == "computer-science" for x in out)


def test_collect_stops_when_no_new_ids():
    # A window far in the future forces paging; page 2 repeats page 1, so paging stops there.
    page = _read("conferenceindex_cs.html")
    urls = [list_url("computer-science", n) for n in (1, 2, 3)]
    fetcher = _FakeFetcher(dict.fromkeys(urls, page))
    adapter = ConferenceIndexAdapter(_settings([], window_end=date(2030, 1, 1)))
    out = asyncio.run(_collect(adapter, fetcher))
    assert len(out) == 500
    assert fetcher.calls == urls[:2]


def test_collect_walks_countries_and_dedupes():
    jp, ph = _read("conferenceindex_cs_japan.html"), _read("conferenceindex_cs_philippines.html")
    fetcher = _FakeFetcher(
        {
            list_url("computer-science/japan"): jp,
            list_url("computer-science/philippines"): ph,
            list_url("computer-science/hong-kong"): jp,  # same rows again: nothing new
        }
    )
    adapter = ConferenceIndexAdapter(_settings(["japan", "philippines", "hong-kong"]))
    out = asyncio.run(_collect(adapter, fetcher))
    assert len(out) == 217 + 12
    assert len({x.source_id for x in out}) == len(out)
    assert len(fetcher.calls) == 3  # no rel=next on these pages


def test_browse_page():
    fetcher = _FakeFetcher(
        {list_url("computer-science/japan"): _read("conferenceindex_cs_japan.html")}
    )
    adapter = ConferenceIndexAdapter(_settings(["japan", "philippines"]))
    first = asyncio.run(adapter.browse_page(fetcher, page=1))
    assert first.has_next and len(first.listings) == 217
    by_name = asyncio.run(adapter.browse_page(fetcher, category="computer-science/japan"))
    assert not by_name.has_next and len(by_name.listings) == 217
    beyond = asyncio.run(adapter.browse_page(fetcher, page=3))
    assert beyond.listings == [] and not beyond.has_next
