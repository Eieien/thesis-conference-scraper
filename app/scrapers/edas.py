"""EDAS (requires login).

The list parser follows the "Conferences open for registration" table layout
(Conference | Name | Home page | Where & When | Register self | Register others).
It has NOT been verified against real HTML yet: save the page with
POST /tools/fetch {"url": "https://edas.info/listConferencesRegister.php", "use_edas_session": true}
and adjust the selectors below if needed.
"""

import re
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from app.pipeline.fees import fees_from_tables
from app.pipeline.normalize import parse_date_range, split_location
from app.schemas import ListingData
from app.scrapers.base import BrowsePage, SourceAdapter
from app.scrapers.html_clean import html_to_text
from app.scrapers.http import Fetcher, FetchError, load_playwright_cookies

_YEAR = re.compile(r"(?:^|\s|-)(?:19|20)\d{2}$")


class EdasSessionExpired(FetchError):
    pass


def _looks_logged_out(html: str) -> bool:
    # EDAS answers an expired session either with the login form or with a tiny page whose only
    # content is a JavaScript redirect to login.php (e.g. "First login from network location").
    if "login.php" in html and "redirect(" in html and len(html) < 2000:
        return True
    soup = BeautifulSoup(html, "lxml")
    return soup.find("input", attrs={"type": "password"}) is not None


def _query_id(href: str | None) -> str | None:
    if not href:
        return None
    qs = parse_qs(urlsplit(href).query)
    for key in ("c", "conference", "id"):
        if key in qs:
            return qs[key][0]
    return None


def parse_register_list(html: str, base_url: str) -> list[ListingData]:
    soup = BeautifulSoup(html, "lxml")
    table = None
    for t in soup.find_all("table"):
        head = t.get_text(" ", strip=True)[:500]
        if "Where" in head and "When" in head:
            table = t
            break
    if table is None:
        return []

    listings = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue
        conf_link = tds[0].find("a", href=True)
        acronym_raw = " ".join(tds[0].get_text(" ", strip=True).split())
        if not acronym_raw:
            continue
        home_link = tds[2].find("a", href=True)
        where_when = [line.strip() for line in tds[3].get_text("\n", strip=True).splitlines()]
        where_when = [line for line in where_when if line]
        location = where_when[0] if where_when else None
        start, end = parse_date_range(where_when[-1] if len(where_when) > 1 else None)
        city, country = split_location(location)

        register_self = tds[4].find("a", href=True) if len(tds) > 4 else None
        conf_url = urljoin(base_url, conf_link["href"]) if conf_link else None
        register_url = urljoin(base_url, register_self["href"]) if register_self else None

        listings.append(
            ListingData(
                source_id=_query_id(conf_url) or _query_id(register_url) or acronym_raw,
                url=conf_url,
                acronym=_YEAR.sub("", acronym_raw).strip() or acronym_raw,
                name=" ".join(tds[1].get_text(" ", strip=True).split()),
                start_date=start,
                end_date=end,
                location_raw=location,
                city=city,
                country=country,
                homepage=home_link["href"] if home_link else None,
                extra={"edas_title": acronym_raw, "register_self_url": register_url},
            )
        )
    return listings


class EdasAdapter(SourceAdapter):
    name = "edas"
    label = "EDAS"
    requires_login = True
    has_details = True
    notes = "Needs data/edas_state.json (run scripts/edas_login.py)."

    def make_fetcher(self) -> Fetcher:
        cookies = load_playwright_cookies(self.settings.edas_state_path)
        return Fetcher(
            self.settings,
            cookies=cookies,
            delay_seconds=self.settings.edas_delay_seconds,
            cache_namespace="edas",
            respect_robots=self.settings.edas_respect_robots,
        )

    async def collect(
        self, fetcher: Fetcher, *, max_pages: int | None = None
    ) -> AsyncIterator[ListingData]:
        result = await fetcher.get(self.settings.edas_list_url, use_cache=False)
        if _looks_logged_out(result.text):
            raise EdasSessionExpired("EDAS session expired. Re-run scripts/edas_login.py")
        for listing in parse_register_list(result.text, self.settings.edas_base_url):
            yield listing

    async def browse_page(
        self, fetcher: Fetcher, *, page: int = 1, category: str | None = None
    ) -> BrowsePage:
        # EDAS shows every open conference on one page.
        link = self.settings.edas_list_url
        if page > 1:
            return BrowsePage(link=link, listings=[], has_next=False)
        result = await fetcher.get(link, use_cache=False)
        if _looks_logged_out(result.text):
            raise EdasSessionExpired("EDAS session expired. Re-run scripts/edas_login.py")
        listings = parse_register_list(result.text, self.settings.edas_base_url)
        return BrowsePage(link=link, listings=listings, has_next=False)

    async def fetch_detail(self, fetcher: Fetcher, listing: ListingData) -> ListingData:
        extra = dict(listing.extra)
        if listing.url:
            page = await fetcher.get(listing.url)
            extra["edas_page_text"] = html_to_text(page.text)[:15_000]
        if register_url := extra.get("register_self_url"):
            page = await fetcher.get(register_url)
            if _looks_logged_out(page.text):
                raise EdasSessionExpired("EDAS session expired. Re-run scripts/edas_login.py")
            fees = fees_from_tables(page.text)
            extra["fees"] = [f.model_dump() for f in fees]
            # Kept for the LLM when the table heuristic finds nothing.
            extra["register_page_text"] = html_to_text(page.text)[:15_000]
        return listing.model_copy(update={"extra": extra})
