"""EasyChair Smart CFP (public, easychair.org/cfp).

robots.txt only blocks /statistics/, /slides/, /help/ and /account/ for generic agents, so /cfp/ is
allowed. Each area page (/cfp/area?area=N, e.g. 1 = Computing, 18 = Technology) holds *every* open
CFP of that area in one HTML table; the "pages" in the browser are client-side only, so one area =
one request. Rows give acronym, name, location, submission deadline, start date (machine-readable
`data-key` epoch ms on area pages) and topic tags. The detail page (/cfp/<slug>) adds the end date,
homepage, abstract deadline and the CFP text. EasyChair has no structured fee field; fees, when
organisers mention them at all, are only in the free text (kept in `description`).
"""

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.pipeline.normalize import parse_date, parse_date_range, split_location
from app.schemas import ListingData
from app.scrapers.base import BrowsePage, SourceAdapter
from app.scrapers.http import Fetcher

BASE_URL = "https://easychair.org"
AREA_URL = BASE_URL + "/cfp/area?area={area}"

# Area id -> label, as listed on /cfp/area. Override with a `easychair_areas` setting (list of ids).
AREAS = {
    1: "Computing",
    18: "Technology",
    2: "Engineering",
    13: "Genomics and Bioinformatics",
    19: "Mathematics and Statistics",
}
DEFAULT_AREAS = [1, 18]

_CFP_HREF = re.compile(r"^/cfp/[^/?#]+$")
_YEAR_SUFFIX = re.compile(r"[\s'’_\-]*(?:20\d{2}|['’]?2\d)$")
_ORDINAL_PREFIX = re.compile(r"^\d+(?:st|nd|rd|th)\s+", re.I)
# "Boca Raton, FL, United States, November 2-4, 2026": the date tail starts at a month name.
_DATE_TAIL = re.compile(
    r"(?:^|,\s*)((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b.*\d{4})\s*$"
)

# Detail-page date_table label -> ListingData field (labels seen on real pages, lowercased).
_DETAIL_DATE_FIELDS = {
    "abstract registration deadline": "abstract_deadline",
    "submission deadline": "paper_deadline",
    "notification": "notification_date",
    "notification of acceptance": "notification_date",
    "camera-ready deadline": "camera_ready_deadline",
    "camera ready deadline": "camera_ready_deadline",
    "registration deadline": "registration_deadline",
}


@dataclass
class AreaRow:
    listing: ListingData
    undated: bool  # no start date: journals, awards, standing calls


def area_ids(settings) -> list[int]:
    return list(getattr(settings, "easychair_areas", None) or DEFAULT_AREAS)


def _cell_date(td: Tag) -> date | None:
    key = td.get("data-key")
    if key and str(key).isdigit():
        return datetime.fromtimestamp(int(key) / 1000, UTC).date()
    return parse_date(td.get_text(" ", strip=True))


def _clean_acronym(raw: str) -> str:
    s = _ORDINAL_PREFIX.sub("", raw).strip()
    return _YEAR_SUFFIX.sub("", s).strip() or raw


def _cfp_table(soup: BeautifulSoup) -> Tag | None:
    for table in soup.find_all("table", class_="ct_table"):
        thead = table.find("thead")
        if thead and any(th.get_text(strip=True) == "Acronym" for th in thead.find_all("th")):
            return table
    return None


def parse_list_page(html: str) -> list[AreaRow]:
    """Parse the CFP table of an area page, a topic page or the /cfp home page."""
    soup = BeautifulSoup(html, "lxml")
    table = _cfp_table(soup)
    if table is None:
        return []
    rows: list[AreaRow] = []
    for tbody in table.find_all("tbody", recursive=False):
        for tr in tbody.find_all("tr", recursive=False):
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 5:
                continue
            link = cells[0].find("a", href=_CFP_HREF)
            if link is None:
                continue
            slug = link["href"].rsplit("/", 1)[-1]
            title = " ".join(link.get_text(" ", strip=True).split())
            where = " ".join(cells[2].get_text(" ", strip=True).split()) or None
            city, country = split_location(where)
            start = _cell_date(cells[4])
            topics = []
            if len(cells) > 5:
                topics = [a.get_text(" ", strip=True) for a in cells[5].find_all("a")]
            rows.append(
                AreaRow(
                    listing=ListingData(
                        source_id=slug,
                        url=urljoin(BASE_URL, link["href"]),
                        acronym=_clean_acronym(title),
                        name=" ".join(cells[1].get_text(" ", strip=True).split()) or title,
                        start_date=start,  # end date only on the detail page
                        location_raw=where,
                        city=city,
                        country=country,
                        paper_deadline=_cell_date(cells[3]),
                        categories=[t for t in topics if t],
                        extra={"easychair_title": title},
                    ),
                    undated=start is None,
                )
            )
    return rows


def split_date_place(text: str) -> tuple[str | None, str | None]:
    """'Aarhus, Denmark, November 11-12, 2026' -> ('Aarhus, Denmark', 'November 11-12, 2026')."""
    text = " ".join(text.split())
    m = _DATE_TAIL.search(text)
    if not m:
        return (text or None), None
    place = text[: m.start()].strip(" ,")
    return (place or None), m.group(1)


def parse_cfp_page(html: str, listing: ListingData) -> ListingData:
    soup = BeautifulSoup(html, "lxml")
    data = listing.model_dump()
    cfp = soup.find("div", id="cfp")
    if cfp is None:
        return listing

    title = cfp.find(id="cfptitle")
    if title:
        full_title = title.get_text(" ", strip=True)
        _, _, name = full_title.partition(":")
        if name.strip():
            data["extra"] = {**data["extra"], "easychair_full_title": full_title}
            if not data.get("name"):
                data["name"] = name.strip()

    dateplace = cfp.find(id="cfpdateplace")
    if dateplace:
        place, when = split_date_place(dateplace.get_text(" ", strip=True))
        start, end = parse_date_range(when)
        if start:
            data["start_date"], data["end_date"] = start, end or start
        if place:
            data["location_raw"] = place
            data["city"], data["country"] = split_location(place)

    table = cfp.find("table", class_="date_table")
    if table:
        for tr in table.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 2:
                continue
            label = tds[0].get_text(" ", strip=True).lower()
            value = tds[1].get_text(" ", strip=True)
            if label == "conference web page":
                a = tds[1].find("a", href=True)
                data["homepage"] = (a["href"] if a else value).strip() or None
            elif label == "submission link":
                data["extra"] = {**data["extra"], "submission_link": value}
            elif label in _DETAIL_DATE_FIELDS:
                parsed = parse_date(value)
                if parsed:
                    data[_DETAIL_DATE_FIELDS[label]] = parsed

    # The CFP text: everything except the title and the key-dates table.
    for t in cfp.find_all("table", class_=("title_table", "date_table")):
        t.decompose()
    text = cfp.get_text("\n", strip=True)
    if text:
        data["description"] = text[:8000]
    return ListingData(**data)


class EasyChairAdapter(SourceAdapter):
    name = "easychair"
    label = "EasyChair CFP"
    has_details = True
    notes = (
        "Public. One request per area page (all open CFPs of that area are in one table); "
        "detail pages add end date, homepage and deadlines. No structured fees."
    )

    async def collect(
        self, fetcher: Fetcher, *, max_pages: int | None = None
    ) -> AsyncIterator[ListingData]:
        areas = area_ids(self.settings)
        if max_pages:
            areas = areas[:max_pages]
        seen: set[str] = set()
        for area in areas:
            result = await fetcher.get(AREA_URL.format(area=area))
            rows = parse_list_page(result.text)
            new_rows = [r for r in rows if r.listing.source_id not in seen]
            if not new_rows:
                continue  # an area page is complete in itself; nothing new means nothing to add
            label = AREAS.get(area, f"area {area}")
            for row in new_rows:
                seen.add(row.listing.source_id)
                if row.undated:
                    continue
                row.listing.extra["easychair_area"] = label
                yield row.listing

    async def fetch_detail(self, fetcher: Fetcher, listing: ListingData) -> ListingData:
        if not listing.url:
            return listing
        result = await fetcher.get(listing.url)
        return parse_cfp_page(result.text, listing)

    async def browse_page(
        self, fetcher: Fetcher, *, page: int = 1, category: str | None = None
    ) -> BrowsePage:
        """`page` N reads the Nth configured area; `category` may name an area id instead."""
        areas = area_ids(self.settings)
        if category and category.strip().isdigit():
            area, has_next = int(category), False
        elif 1 <= page <= len(areas):
            area, has_next = areas[page - 1], page < len(areas)
        else:
            return BrowsePage(link=BASE_URL + "/cfp/area", listings=[], has_next=False)
        link = AREA_URL.format(area=area)
        rows = parse_list_page((await fetcher.get(link)).text)
        listings = [r.listing for r in rows if not r.undated]
        return BrowsePage(link=link, listings=listings, has_next=has_next and bool(rows))
