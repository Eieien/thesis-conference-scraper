"""IEEE Computer Society calls for papers (www.computer.org/conferences/cfp-...).

robots.txt allows `/` but disallows `/api/`, `/_next/`, `/search`, `/wp-json/` and every URL with
a query string (`/*?*`). The /conferences/calendar page fills its list from the disallowed API
with JavaScript, so it is not used. Instead:

- `/sitemap_index.xml` lists `/sitemap/0.xml` ... `/sitemap/4.xml` (about 4,970 URLs with
  `lastmod`). About 58 are CFP pages, `/conferences/cfp-<name>[-<year>]`; a few older ones are
  `/conferences/<name>-cfp` or `/conferences/<name>-<year>`. The sitemap has nothing but URLs.
- Slugs with a year are kept only when a year touches the date window (`cfp-ieee-host-2027` and
  `cfp-ieee-nca-2025` are skipped unread). Slugs without a year (`cfp-ieee-issre`) are reused
  for each new edition, so they are read when the sitemap `lastmod` is less than a year before
  the window; some still describe last year's edition (`cfp-ieee-itnac` is ITNAC 2025).
- Each CFP page is server-rendered: an `<h1>` "Call for Submissions: IEEE SUSTAIN 2026", usually
  an `<h4>` "29 November - 2 December 2026 | Dhahran, Saudi Arabia", and a prose block whose first
  paragraph is "Conference dates: <dates> | <place>", then the full name (often), a description,
  a "Learn More" link to the conference site, "Topics" and boilerplate. Deadlines are rare: the
  SWEBOK Summit page has a "Key Dates" list, HOST/WACV say "The contribution deadlines are
  19 Aug 2025 (first round) and 1 Dec 2025 (second round)". No page states fees.
- Pages under /conferences that aren't events (a call for hosting proposals, a mentoring session)
  have no "Conference dates" line and are skipped.

The project's `Fetcher` checks robots.txt with the standard-library parser, which reads rules in
order without wildcards: computer.org's leading `Allow: /` makes it allow every path. So this
adapter guards its own URLs (`is_allowed_url`): only plain www.computer.org paths, never a query.
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from app.pipeline.normalize import parse_date, parse_date_range, split_location
from app.schemas import ListingData
from app.scrapers.base import BrowsePage, SourceAdapter
from app.scrapers.http import Fetcher, RobotsDisallowed

HOST = "www.computer.org"
BASE_URL = f"https://{HOST}"
SITEMAP_INDEX_URL = BASE_URL + "/sitemap_index.xml"

# Paths computer.org's robots.txt disallows (besides any query string).
_DISALLOWED_PREFIXES = ("/api/", "/_next/", "/search", "/wp-json/", "/private/", "/admin/")
# How old a year-less CFP page may be (sitemap lastmod, days before the window start) to be read.
DEFAULT_MAX_AGE_DAYS = 365
# Safety net on pages read per run (the sitemap currently yields about 35).
MAX_PAGES = 60

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_SLUG_OK = re.compile(r"^(?:cfp-[a-z0-9-]+|[a-z0-9-]+-cfp|[a-z0-9-]*[a-z]-?20\d{2}(?:-20\d{2})?)$")
_YEAR = re.compile(r"20\d{2}")
_TITLE_PREFIX = re.compile(r"^(?:closed:\s*)?call\s+for\s+[a-z ]+?:\s*", re.I)
_TITLE_YEAR = re.compile(r"\s*\b20\d{2}(?:\s*[-–]\s*20\d{2})?\s*$")
_DATES_LABEL = re.compile(r"^\s*conference\s+dates?\s*:\s*", re.I)
_NAME_WORDS = re.compile(r"\b(conference|symposium|workshop|summit|congress|forum|week)\b", re.I)
_ROUNDS = re.compile(
    r"deadlines?\s+(?:are|is)\s+(.+?\(first round\).*?\(second round\))", re.I | re.S
)
_ROUND_DATE = re.compile(r"(\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4})\s*\(\s*(\w+)\s+round\s*\)", re.I)

# "Key Dates" labels -> ListingData field; the first rule that matches a label wins.
_KEY_DATE_RULES = [
    ("notification", "notification_date"),
    ("camera", "camera_ready_deadline"),
    ("registration", "registration_deadline"),
    ("abstract", "abstract_deadline"),
    ("submission", "paper_deadline"),
    ("paper", "paper_deadline"),
    ("due", "paper_deadline"),
]


@dataclass
class SitemapEntry:
    url: str
    lastmod: date | None


def is_allowed_url(url: str) -> bool:
    """Only plain www.computer.org paths that robots.txt allows (no query, no API paths)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != HOST or parts.query or "?" in url:
        return False
    return not parts.path.startswith(_DISALLOWED_PREFIXES)


def _lastmod(text: str | None) -> date | None:
    if not text or len(text) < 10:
        return None
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def parse_sitemap_index(xml: str) -> list[str]:
    root = ET.fromstring(xml.encode("utf-8"))
    locs = [(el.text or "").strip() for el in root.findall("sm:sitemap/sm:loc", _NS)]
    return [u for u in locs if u and is_allowed_url(u)]


def parse_sitemap(xml: str) -> list[SitemapEntry]:
    root = ET.fromstring(xml.encode("utf-8"))
    out: list[SitemapEntry] = []
    for url in root.findall("sm:url", _NS):
        loc = (url.findtext("sm:loc", default="", namespaces=_NS) or "").strip()
        if loc:
            out.append(SitemapEntry(loc, _lastmod(url.findtext("sm:lastmod", namespaces=_NS))))
    return out


def cfp_slug(url: str) -> str | None:
    """'https://www.computer.org/conferences/cfp-ieee-sustain' -> 'cfp-ieee-sustain'."""
    if not is_allowed_url(url):
        return None
    path = urlsplit(url).path.rstrip("/")
    head, _, slug = path.rpartition("/")
    if head != "/conferences" or not _SLUG_OK.match(slug):
        return None
    return slug


def slug_years(slug: str) -> list[int]:
    """'cfp-ieee-cai-2028-2029' -> [2028, 2029]; 'cfp-ieee-issre' -> []."""
    return [int(y) for y in _YEAR.findall(slug)]


def wanted(entry: SitemapEntry, window_start: date, window_end: date, max_age_days: int) -> bool:
    """Whether a sitemap URL is worth a request for this date window (decided from the URL)."""
    slug = cfp_slug(entry.url)
    if slug is None:
        return False
    years = slug_years(slug)
    if years:
        return min(years) <= window_end.year and max(years) >= window_start.year
    if entry.lastmod is None:
        return True
    return entry.lastmod >= window_start - timedelta(days=max_age_days)


def select_pages(
    entries: list[SitemapEntry], window_start: date, window_end: date, max_age_days: int
) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for e in entries:
        url = e.url.rstrip("/")
        if url not in seen and wanted(e, window_start, window_end, max_age_days):
            seen.add(url)
            out.append(url)
    return sorted(out)


def split_title(title: str) -> str | None:
    """'Call for Submissions: IEEE COMPSAC 2026' -> 'COMPSAC'."""
    t = _TITLE_PREFIX.sub("", " ".join(title.split()))
    t = _TITLE_YEAR.sub("", t).strip()
    t = re.sub(r"^IEEE(?:/CVF)?\s+", "", t).strip()
    return t or None


def _dates_place(text: str) -> tuple[date | None, date | None, str | None]:
    """'29 November - 2 December 2026 | Dhahran, Saudi Arabia' -> (start, end, place)."""
    text = _DATES_LABEL.sub("", " ".join(text.split()))
    when, sep, place = text.partition("|")
    when = when.replace("&", "-")  # "1 & 2 October 2026"
    start, end = parse_date_range(when.strip())
    return start, (end or start) if start else None, (place.strip() or None) if sep else None


def _article(soup: BeautifulSoup) -> Tag | None:
    # The page has two article.general-post-content blocks; the CFP is the one with the <h1>.
    h1 = soup.find("h1")
    return h1.find_parent("article") if h1 else None


def _full_name(paragraphs: list[str]) -> str | None:
    """The short paragraph after 'Conference dates' that names the conference, if there is one."""
    for text in paragraphs[:2]:
        if len(text) <= 160 and _NAME_WORDS.search(text) and not text.endswith("."):
            name = re.sub(r"^The\s+", "", text).strip()
            return name or None
    return None


def _key_dates(prose: Tag) -> tuple[dict[str, date], dict[str, str]]:
    """'Extended abstract submission due: 3 Nov 2025 (extended)' lines -> fields, raw texts."""
    fields: dict[str, date] = {}
    raw: dict[str, str] = {}
    for line in prose.get_text("\n", strip=True).splitlines():
        label, sep, value = line.partition(":")
        label = label.strip().lower()
        if not sep or len(label) > 60 or label.startswith("conference date"):
            continue
        parsed = parse_date(re.sub(r"\(.*?\)", "", value).strip())
        if parsed is None:
            continue
        raw[label] = value.strip()
        for word, field in _KEY_DATE_RULES:
            if word in label:
                fields.setdefault(field, parsed)
                break
    text = prose.get_text(" ", strip=True)
    m = _ROUNDS.search(text)
    if m:
        rounds = {r.lower(): parse_date(d) for d, r in _ROUND_DATE.findall(m.group(1))}
        rounds = {k: v for k, v in rounds.items() if v}
        if rounds:
            raw["contribution deadlines"] = " ".join(m.group(1).split())
            fields.setdefault("paper_deadline", max(rounds.values()))  # the last round
    return fields, raw


def _homepage(prose: Tag) -> str | None:
    for a in prose.find_all("a", href=True):
        if "learn more" not in a.get_text(" ", strip=True).lower():
            continue
        href = a["href"].strip()
        host = urlsplit(href).netloc.lower()
        # HOST's "Learn More" is a mailing-list sign-up on join.computer.org, not a website.
        if href.startswith("http") and host != "join.computer.org":
            return href
    return None


def parse_cfp_page(html: str, url: str) -> ListingData | None:
    """One CFP page -> ListingData; None when the page isn't a dated event."""
    soup = BeautifulSoup(html, "lxml")
    article = _article(soup)
    if article is None:
        return None
    title = " ".join(article.find("h1").get_text(" ", strip=True).split())
    prose = article.find("div", class_="prose")
    paragraphs = (
        [
            " ".join(p.get_text(" ", strip=True).split())
            for p in prose.find_all("p", recursive=False)
        ]
        if prose
        else []
    )
    paragraphs = [p for p in paragraphs if p]

    # Dates and place: the "Conference dates:" paragraph is fuller than the <h4> ("New York
    # City, NY, USA" vs "NYC, NY"), but on SWEBOK only the <h4> has the "dates | place" form.
    dates_idx = next((i for i, p in enumerate(paragraphs) if _DATES_LABEL.match(p)), None)
    candidates = [paragraphs[dates_idx]] if dates_idx is not None else []
    h4 = article.find("h4")
    if h4 is not None:
        candidates.append(h4.get_text(" ", strip=True))
    if not candidates:
        return None  # a proposal call or a side event, not a conference CFP
    start = end = place = None
    for text in candidates:
        s, e, p = _dates_place(text)
        if s and (p or start is None):
            start, end, place = s, e, p
        if start and place:
            break
    if start is None:
        return None

    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    acronym = split_title(title)
    after = paragraphs[dates_idx + 1 :] if dates_idx is not None else paragraphs
    name = _full_name(after) or _TITLE_PREFIX.sub("", title).strip() or title
    city, country = split_location(place)
    extra: dict = {"ieeecs_title": title}
    data: dict = {}
    description = None
    if prose is not None:
        dates, raw_dates = _key_dates(prose)
        data.update(dates)
        if raw_dates:
            extra["other_dates"] = raw_dates
        description = prose.get_text("\n", strip=True)[:8000] or None
    return ListingData(
        source_id=slug,
        url=url,
        name=name,
        acronym=acronym,
        start_date=start,
        end_date=end,
        location_raw=place,
        city=city,
        country=country,
        homepage=_homepage(prose) if prose is not None else None,
        description=description,
        extra=extra,
        **data,
    )


class IeeeCsAdapter(SourceAdapter):
    name = "ieeecs"
    label = "IEEE Computer Society CFPs"
    notes = (
        "Public. Reads computer.org's sitemaps (6 requests), then each /conferences/cfp-... page "
        "whose slug year touches the window (or year-less pages updated in the last year): "
        "about 35 pages. Dates, place, homepage; deadlines only where the page lists them. "
        "No fees."
    )

    def _max_age_days(self) -> int:
        return int(getattr(self.settings, "ieeecs_max_age_days", None) or DEFAULT_MAX_AGE_DAYS)

    async def _get(self, fetcher: Fetcher, url: str) -> str:
        if not is_allowed_url(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        return (await fetcher.get(url)).text

    async def cfp_urls(self, fetcher: Fetcher) -> list[str]:
        entries: list[SitemapEntry] = []
        for sitemap in parse_sitemap_index(await self._get(fetcher, SITEMAP_INDEX_URL)):
            entries.extend(parse_sitemap(await self._get(fetcher, sitemap)))
        s = self.settings
        return select_pages(entries, s.window_start, s.window_end, self._max_age_days())

    async def collect(
        self, fetcher: Fetcher, *, max_pages: int | None = None
    ) -> AsyncIterator[ListingData]:
        """`max_pages` caps the CFP pages read (the sitemaps are always read)."""
        urls = (await self.cfp_urls(fetcher))[: max_pages or MAX_PAGES]
        for url in urls:
            listing = parse_cfp_page(await self._get(fetcher, url), url)
            if listing is not None:
                yield listing

    async def browse_page(
        self, fetcher: Fetcher, *, page: int = 1, category: str | None = None
    ) -> BrowsePage:
        """The CFP pages the sitemap offers, named by slug; nothing but the sitemaps is read."""
        if page != 1:
            return BrowsePage(link=SITEMAP_INDEX_URL, listings=[], has_next=False)
        listings = [
            ListingData(source_id=slug, url=url, name=slug)
            for url in await self.cfp_urls(fetcher)
            if (slug := cfp_slug(url))
        ]
        return BrowsePage(link=SITEMAP_INDEX_URL, listings=listings, has_next=False)
