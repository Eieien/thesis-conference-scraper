"""ConferenceIndex (conferenceindex.org): a noisy aggregator, used for coverage only.

robots.txt is `User-agent: *` / `Disallow:` (empty), so everything is allowed. Listing pages are
plain server-rendered HTML:

- `/conferences/<discipline>` (e.g. `computer-science`) and `/conferences/<discipline>/<country>`
  (e.g. `computer-science/japan`, slugs as on `/locations`). There is no date filter; rows are in
  date order from today, grouped in one card per month ("October, 2026"), 500 rows per page,
  `?page=N` with a `rel="next"` link. Each row is "Oct 05 <a href=/event/<slug>>Name (ACR)</a> -
  City, Country". The slug is unique per listing (repeats get a `-1` suffix) and is the source ID.
- `/event/<slug>` adds a schema.org Event JSON-LD block (start/end date, homepage `url`, place),
  a "Short Name", deadlines ("Final Submission", "Notification", ...) and the description.
  There are no fee fields.

The site lists the same generic-name event in many cities ("International Conference on Computer
Science and Information Technology" in Tbilisi, Beijing, Paris, ...). Each keeps its own slug;
merge decides what is the same conference.
"""

import json
import re
from collections.abc import AsyncIterator
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from app.pipeline.normalize import parse_date, parse_date_range, split_location
from app.schemas import ListingData
from app.scrapers.base import BrowsePage, SourceAdapter
from app.scrapers.http import Fetcher

BASE_URL = "https://conferenceindex.org"
LIST_URL = BASE_URL + "/conferences/{path}"

DEFAULT_DISCIPLINES = ["computer-science"]
# Country slugs (from /locations) of every Asian country the site has, using the same idea of
# "Asia" as app/pipeline/geo.py (so Georgia, Turkey and the Gulf states are included).
# Override with a `conferenceindex_countries` setting; an empty list reads the worldwide page.
ASIA_COUNTRIES = [
    "azerbaijan",
    "bahrain",
    "bangladesh",
    "cambodia",
    "china",
    "georgia",
    "hong-kong",
    "india",
    "indonesia",
    "iraq",
    "israel",
    "japan",
    "kuwait",
    "laos",
    "macao",
    "malaysia",
    "maldives",
    "myanmar",
    "nepal",
    "oman",
    "pakistan",
    "philippines",
    "qatar",
    "republic-of-korea",
    "saudi-arabia",
    "singapore",
    "sri-lanka",
    "taiwan",
    "thailand",
    "turkey",
    "united-arab-emirates",
    "vietnam",
]
# Safety net per listing; paging normally stops at the date window or when no new IDs appear.
MAX_PAGES_PER_LIST = 10

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_MONTH_HEADER = re.compile(r"([A-Za-z]+),\s*(\d{4})")
_ROW_DAY = re.compile(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})\b")
_ACRONYM_TAIL = re.compile(r"\s*\(([^()]{1,40})\)\s*$")

# Detail-page "Label: value" -> ListingData field (labels seen on real pages, lowercased).
_DEADLINE_FIELDS = {
    "abstract submission": "abstract_deadline",
    "abstract deadline": "abstract_deadline",
    "final submission": "paper_deadline",
    "paper submission": "paper_deadline",
    "full paper submission": "paper_deadline",
    "submission deadline": "paper_deadline",
    "notification": "notification_date",
    "camera ready": "camera_ready_deadline",
    "camera-ready": "camera_ready_deadline",
    "registration": "registration_deadline",
    "registration deadline": "registration_deadline",
}


def disciplines(settings) -> list[str]:
    return list(getattr(settings, "conferenceindex_disciplines", None) or DEFAULT_DISCIPLINES)


def countries(settings) -> list[str]:
    value = getattr(settings, "conferenceindex_countries", None)
    return list(ASIA_COUNTRIES if value is None else value)


def list_paths(settings) -> list[str]:
    """`computer-science/japan`, ... ; just the discipline when no countries are configured."""
    cs = countries(settings)
    return [f"{d}/{c}" if c else d for d in disciplines(settings) for c in (cs or [""])]


def list_url(path: str, page: int = 1) -> str:
    url = LIST_URL.format(path=path)
    return url if page <= 1 else f"{url}?page={page}"


def split_name(title: str) -> tuple[str, str | None]:
    """'International Conference on AI (ICAI)' -> ('International Conference on AI', 'ICAI')."""
    title = " ".join(title.split())
    m = _ACRONYM_TAIL.search(title)
    if not m:
        return title, None
    name = title[: m.start()].strip()
    return (name or title), m.group(1).strip()


def _strip_tracking(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _row_date(text: str, month: int | None, year: int | None) -> date | None:
    m = _ROW_DAY.match(text.strip())
    if not m or year is None:
        return None
    row_month = _MONTHS.get(m.group(1).lower(), month)
    try:
        return date(year, row_month or month or 1, int(m.group(2)))
    except (TypeError, ValueError):
        return None


def parse_list_page(html: str) -> list[ListingData]:
    """Rows of a /conferences/... listing page, in page order."""
    soup = BeautifulSoup(html, "lxml")
    container = soup.find(id="eventList")
    if container is None:
        return []
    out: list[ListingData] = []
    for card in container.find_all("div", class_="card"):
        header = card.find(class_="card-header")
        hm = _MONTH_HEADER.search(header.get_text(" ", strip=True)) if header else None
        month = _MONTHS.get(hm.group(1)[:3].lower()) if hm else None
        year = int(hm.group(2)) if hm else None
        for li in card.find_all("li"):
            link = li.find("a", href=re.compile(r"/event/[^/?#]+$"))
            if link is None:
                continue
            slug = link["href"].rstrip("/").rsplit("/", 1)[-1]
            title = link.get("title") or link.get_text(" ", strip=True)
            name, acronym = split_name(title)
            before = "".join(
                s for s in link.previous_siblings if isinstance(s, str)
            ).strip()  # "Oct 05"
            after = "".join(s for s in link.next_siblings if isinstance(s, str))
            where = " ".join(after.strip().lstrip("-").split()) or None
            city, country = split_location(where)
            out.append(
                ListingData(
                    source_id=slug,
                    url=link["href"]
                    if link["href"].startswith("http")
                    else BASE_URL + link["href"],
                    name=name,
                    acronym=acronym,
                    start_date=_row_date(before, month, year),  # end date: detail page
                    location_raw=where,
                    city=city,
                    country=country,
                    extra={"conferenceindex_title": " ".join(title.split())},
                )
            )
    return out


def has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    return soup.find("a", rel="next") is not None


def _event_jsonld(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Event":
            return data
    return {}


def _iso_date(value) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _detail_fields(detail: Tag) -> dict[str, Tag]:
    """'Short Name: <strong>ICCAC</strong>' items of the detail card, keyed by lowercased label."""
    fields: dict[str, Tag] = {}
    ul = detail.find("ul", class_="list-unstyled")
    if ul is None:
        return fields
    for li in ul.find_all("li", recursive=False):
        text = li.get_text(" ", strip=True)
        label, sep, _ = text.partition(":")
        if sep:
            fields[label.strip().lower()] = li
    return fields


def _field_value(li: Tag) -> str:
    return li.get_text(" ", strip=True).partition(":")[2].strip()


def parse_event_page(html: str, listing: ListingData) -> ListingData:
    soup = BeautifulSoup(html, "lxml")
    detail = soup.find(id="eventDetail")
    if detail is None:
        return listing
    data = listing.model_dump()
    extra = dict(data["extra"])
    ld = _event_jsonld(soup)
    fields = _detail_fields(detail)

    if ld.get("name") and not data.get("name"):
        data["name"] = ld["name"]
    if "short name" in fields:
        data["acronym"] = _field_value(fields["short name"]) or data.get("acronym")

    start, end = _iso_date(ld.get("startDate")), _iso_date(ld.get("endDate"))
    if not start and "date" in fields:
        start, end = parse_date_range(_field_value(fields["date"]))
    if start:
        data["start_date"], data["end_date"] = start, end or start

    homepage = ld.get("url")
    if not homepage and "website url" in fields:
        a = fields["website url"].find("a", href=True)
        homepage = a["href"] if a else None
    data["homepage"] = _strip_tracking(homepage) or data.get("homepage")

    place = (ld.get("location") or {}).get("name") if isinstance(ld.get("location"), dict) else None
    if not place and "location" in fields:
        place = _field_value(fields["location"])
    if place:
        data["location_raw"] = place
        data["city"], data["country"] = split_location(place)
    if "venue" in fields:
        extra["venue"] = _field_value(fields["venue"])
    if "organization" in fields:
        extra["organizer"] = _field_value(fields["organization"])
    if "program url" in fields:
        a = fields["program url"].find("a", href=True)
        if a:
            extra["program_url"] = _strip_tracking(a["href"])

    other_dates: dict[str, str] = {}
    for label, li in fields.items():
        if label in _DEADLINE_FIELDS:
            parsed = parse_date(_field_value(li))
            if parsed:
                data[_DEADLINE_FIELDS[label]] = parsed
        elif any(w in label for w in ("deadline", "submission", "notification")):
            other_dates[label] = _field_value(li)
    if other_dates:
        extra["other_dates"] = other_dates

    tags = []
    tags_li = next((li for label, li in fields.items() if label.endswith("tags")), None)
    if tags_li is not None:
        tags = [" ".join(a.get_text(" ", strip=True).split()) for a in tags_li.find_all("a")]
    if tags:
        data["categories"] = [t for t in tags if t]

    parts = []
    for pane_id in ("event-description", "event-tracks"):
        pane = detail.find(id=pane_id)
        if pane is not None:
            parts.append(pane.get_text("\n", strip=True))
    text = "\n\n".join(p for p in parts if p)
    if text:
        data["description"] = text[:8000]

    m = re.search(r"/volunteer-applications/create/(\d+)|/event-logo/(\d+)\.", html)
    if m:
        extra["conferenceindex_id"] = m.group(1) or m.group(2)
    data["extra"] = extra
    return ListingData(**data)


class ConferenceIndexAdapter(SourceAdapter):
    name = "conferenceindex"
    label = "ConferenceIndex"
    has_details = True
    notes = (
        "Noisy aggregator: use for coverage only, never as the trusted source for a field. "
        "Reads /conferences/<discipline>/<country> for Asian countries; paging stops past the "
        "date window. Detail pages add end date, homepage and deadlines. No fees."
    )

    async def _read_list(
        self, fetcher: Fetcher, path: str, seen: set[str], page_limit: int
    ) -> AsyncIterator[ListingData]:
        for page in range(1, page_limit + 1):
            html = (await fetcher.get(list_url(path, page))).text
            rows = parse_list_page(html)
            new_rows = [r for r in rows if r.source_id not in seen]
            if not new_rows:
                return  # empty page or the same rows again: paging is over
            for row in new_rows:
                seen.add(row.source_id)
                yield row
            # Rows are in date order, so once a page reaches past the window, later pages can't
            # hold anything in it.
            last = max((r.start_date for r in rows if r.start_date), default=None)
            if last and last > self.settings.window_end:
                return
            if not has_next_page(html):
                return

    async def collect(
        self, fetcher: Fetcher, *, max_pages: int | None = None
    ) -> AsyncIterator[ListingData]:
        page_limit = max_pages or MAX_PAGES_PER_LIST
        seen: set[str] = set()
        for path in list_paths(self.settings):
            async for row in self._read_list(fetcher, path, seen, page_limit):
                row.extra["conferenceindex_list"] = path
                yield row

    async def fetch_detail(self, fetcher: Fetcher, listing: ListingData) -> ListingData:
        if not listing.url:
            return listing
        result = await fetcher.get(listing.url)
        return parse_event_page(result.text, listing)

    async def browse_page(
        self, fetcher: Fetcher, *, page: int = 1, category: str | None = None
    ) -> BrowsePage:
        """`page` N reads the Nth configured list (discipline/country); `category` may name a
        list path directly, e.g. `computer-science/japan`."""
        paths = list_paths(self.settings)
        if category and category.strip():
            path, has_next = category.strip().strip("/"), False
        elif 1 <= page <= len(paths):
            path, has_next = paths[page - 1], page < len(paths)
        else:
            return BrowsePage(link=BASE_URL + "/conferences", listings=[], has_next=False)
        link = list_url(path)
        listings = parse_list_page((await fetcher.get(link)).text)
        return BrowsePage(link=link, listings=listings, has_next=has_next)
