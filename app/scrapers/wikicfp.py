"""WikiCFP (public).

Category pages (/cfp/call?conference=<category>&page=N) list open CFPs first, then an
"Expired CFPs" section in descending deadline order. After ~20 pages the site keeps returning
the same stale page, so we stop when a page brings no new events or deadlines fall past the cutoff.
"""

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from app.pipeline.normalize import parse_date, parse_date_range, split_location
from app.schemas import ListingData
from app.scrapers.base import BrowsePage, SourceAdapter
from app.scrapers.http import Fetcher

BASE_URL = "http://www.wikicfp.com"
LIST_URL = BASE_URL + "/cfp/call?conference={category}&page={page}"

_EVENT_HREF = re.compile(r"event\.showcfp\?eventid=\d+")
_YEAR_SUFFIX = re.compile(r"\s+(?:19|20)\d{2}$")

# Detail-page <th> label -> ListingData field
_DETAIL_DATE_FIELDS = {
    "abstract registration due": "abstract_deadline",
    "submission deadline": "paper_deadline",
    "notification due": "notification_date",
    "final version due": "camera_ready_deadline",
}


@dataclass
class CategoryRow:
    listing: ListingData
    deadline: date | None
    expired: bool
    undated: bool  # "When: N/A" -> journals and special issues, not conferences


def _event_id(href: str) -> str:
    return parse_qs(urlsplit(href).query)["eventid"][0]


def parse_category_page(html: str) -> list[CategoryRow]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[CategoryRow] = []
    expired = False
    trs = soup.find_all("tr")
    for i, tr in enumerate(trs):
        cells = tr.find_all("td", recursive=False)
        if len(cells) == 1 and cells[0].get_text(strip=True) == "Expired CFPs":
            expired = True
            continue
        if not cells or cells[0].get("rowspan") != "2":
            continue
        link = cells[0].find("a", href=_EVENT_HREF)
        if link is None or i + 1 >= len(trs) or len(cells) < 2:
            continue
        detail = [td.get_text(" ", strip=True) for td in trs[i + 1].find_all("td", recursive=False)]
        if len(detail) < 3:
            continue
        when, where, deadline_text = detail[:3]
        start, end = parse_date_range(when)
        city, country = split_location(where)
        acronym_with_year = " ".join(link.get_text(" ", strip=True).split())
        rows.append(
            CategoryRow(
                listing=ListingData(
                    source_id=_event_id(link["href"]),
                    url=urljoin(BASE_URL, link["href"]),
                    acronym=_YEAR_SUFFIX.sub("", acronym_with_year) or acronym_with_year,
                    name=" ".join(cells[1].get_text(" ", strip=True).split()),
                    start_date=start,
                    end_date=end,
                    location_raw=None if where.upper() == "N/A" else where,
                    city=city,
                    country=country,
                    paper_deadline=parse_date(deadline_text),
                    extra={"wikicfp_title": acronym_with_year},
                ),
                deadline=parse_date(deadline_text),
                expired=expired,
                undated=when.upper() == "N/A",
            )
        )
    return rows


def parse_event_page(html: str, listing: ListingData) -> ListingData:
    soup = BeautifulSoup(html, "lxml")
    data = listing.model_dump()

    for th in soup.find_all("th"):
        label = th.get_text(" ", strip=True).lower()
        td = th.find_next_sibling("td")
        if td is None:
            continue
        value = td.get_text(" ", strip=True)
        if label == "when":
            data["start_date"], data["end_date"] = parse_date_range(value)
        elif label == "where" and value.upper() != "N/A":
            data["location_raw"] = value
            data["city"], data["country"] = split_location(value)
        elif label in _DETAIL_DATE_FIELDS:
            # Machine-readable date if present, else the visible text.
            span = td.find("span", attrs={"property": "v:startDate"})
            data[_DETAIL_DATE_FIELDS[label]] = parse_date(
                span["content"][:10] if span and span.get("content") else value
            )

    for text_node in soup.find_all(string=re.compile(r"^\s*Link:\s*$")):
        a = text_node.find_next("a", href=True)
        if a and not a["href"].startswith("/"):
            data["homepage"] = a["href"].strip()
            break

    cfp = soup.find("div", class_="cfp")
    if cfp:
        data["description"] = cfp.get_text("\n", strip=True)[:8000]

    categories = []
    for a in soup.find_all("a", href=re.compile(r"call\?conference=")):
        if a.find_parent("h5"):
            categories.append(a.get_text(strip=True))
    if categories:
        data["categories"] = categories
    return ListingData(**data)


class WikiCfpAdapter(SourceAdapter):
    name = "wikicfp"
    label = "WikiCFP"
    has_details = True
    notes = "Public. Walks each configured category until listings repeat or get too old."

    async def collect(
        self, fetcher: Fetcher, *, max_pages: int | None = None
    ) -> AsyncIterator[ListingData]:
        page_limit = max_pages or self.settings.wikicfp_max_pages
        # Oct-Nov events rarely close submissions more than ~12 months before they start.
        oldest_deadline = self.settings.window_start - timedelta(days=365)
        seen: set[str] = set()

        for category in self.settings.wikicfp_categories:
            for page in range(1, page_limit + 1):
                url = LIST_URL.format(category=quote(category), page=page)
                result = await fetcher.get(url)
                rows = parse_category_page(result.text)
                new_rows = [r for r in rows if r.listing.source_id not in seen]
                if not new_rows:
                    break
                for row in new_rows:
                    seen.add(row.listing.source_id)
                    if row.undated:
                        continue
                    row.listing.categories = [category]
                    yield row.listing
                expired_deadlines = [r.deadline for r in rows if r.expired and r.deadline]
                if expired_deadlines and max(expired_deadlines) < oldest_deadline:
                    break

    async def fetch_detail(self, fetcher: Fetcher, listing: ListingData) -> ListingData:
        if not listing.url:
            return listing
        result = await fetcher.get(listing.url)
        return parse_event_page(result.text, listing)

    async def browse_page(
        self, fetcher: Fetcher, *, page: int = 1, category: str | None = None
    ) -> BrowsePage:
        category = category or self.settings.wikicfp_categories[0]
        link = LIST_URL.format(category=quote(category), page=page)
        rows = parse_category_page((await fetcher.get(link)).text)
        listings = []
        for row in rows:
            if row.undated:
                continue
            row.listing.categories = [category]
            listings.append(row.listing)
        return BrowsePage(link=link, listings=listings, has_next=bool(rows))
