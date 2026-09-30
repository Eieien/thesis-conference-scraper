"""IEEE Conference Search (conferences.ieee.org), read through a real browser.

conferences.ieee.org answers plain HTTP clients with HTTP 418 "Request Rejected" (robots.txt is
404, so nothing is disallowed). The user chose to read it anyway through a real browser
(Playwright + stealth, like the EDAS login): the search page is opened at a human pace and the
results it receives are read as the page receives them. IEEE's website sends its own key with
those requests; this adapter never reads, stores or reuses that key, it only watches the page.

The search runs in the address bar (`/conferences/search?q=*&region=…&field_of_interest=…&pos=N`,
10 results a page; `pos` is the PAGE number, so pos=10 gives results 100-109, and a `pos` past the
last page is clamped to it). The page gets its results as JSON:
    {"entity": {"totalResults": N, "results": [{"eventId", "eventTitle", "startDate", "endDate",
     "location": {"city", "region", "country"}, "about", "sponsors", "eventFormat", ...}]}}
There is no working date-range filter, so each search is sorted by date, the first page on or
after the window is found by halving the offset range, and pages are read until past the window.

Kept small on purpose: a few regions and one field of interest (`ieee_regions`,
`ieee_fields_of_interest`) and at least `ieee_delay_seconds` between page loads.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import date
from urllib.parse import urlencode

from app.schemas import ListingData
from app.scrapers.base import SourceAdapter
from app.scrapers.http import Fetcher

log = logging.getLogger(__name__)

SEARCH_URL = "https://conferences.ieee.org/conferences_events/conferences/search?"
DETAILS_URL = "https://conferences.ieee.org/conferences_events/conferences/conferencedetails/{id}"
PAGE_SIZE = 10
MAX_PAGES_PER_SEARCH = 60

DEFAULT_REGIONS = [
    "Region10-Asia and Pacific",
    # The Middle East is in Region 8 with Europe and Africa; app/pipeline/geo.py counts it as Asia.
    "Region08-Europe,Middle East,Africa",
]
DEFAULT_FIELDS = ["Computing and Processing"]

_ACRONYM = re.compile(r"\(([A-Za-z0-9][A-Za-z0-9&\-/ ]{1,24})\)\s*$")


def search_url(region: str, field: str, pos: int) -> str:
    params = {
        "q": "*",
        "subsequent_q": "",
        "date": "all",
        "from": "",
        "to": "",
        "region": region,
        "country": "all",
        "pos": pos,
        "sortorder": "asc",
        "sponsor": "",
        "sponsor_type": "all",
        "state": "all",
        "field_of_interest": field,
        "sortfield": "dates",
        "searchmode": "advanced",
        "virtualflag": "B",
        "virtualConfReadOnly": "B",
        "eventformat": "",
    }
    return SEARCH_URL + urlencode(params)


def parse_result(r: dict) -> ListingData:
    """One search result (the JSON the page receives) as a listing."""
    title = " ".join((r.get("eventTitle") or "").split())
    m = _ACRONYM.search(title)
    loc = r.get("location") or {}
    city = (loc.get("city") or "").strip() or None
    region = (loc.get("region") or "").strip()
    country = (loc.get("country") or "").strip() or None
    raw = ", ".join(x for x in (city, region, country) if x) or None
    return ListingData(
        source_id=str(r["eventId"]),
        name=title,
        acronym=m.group(1).strip() if m else None,
        url=DETAILS_URL.format(id=r["eventId"]),
        start_date=date.fromisoformat(r["startDate"]) if r.get("startDate") else None,
        end_date=date.fromisoformat(r["endDate"]) if r.get("endDate") else None,
        location_raw=raw,
        city=city,
        country=country,
        categories=[t.strip() for t in (r.get("about") or "").split(";") if t.strip()],
        extra={
            "ieee_event_id": r["eventId"],
            "sponsors": r.get("sponsors"),
            "event_format": r.get("eventFormat"),
            "virtual": r.get("isvirtual") == "Y",
        },
    )


def parse_response(body: dict) -> tuple[int, list[dict]]:
    entity = body.get("entity") or {}
    return int(entity.get("totalResults") or 0), list(entity.get("results") or [])


class IeeeAdapter(SourceAdapter):
    name = "ieee"
    label = "IEEE Conference Search"
    notes = (
        "Read through a real browser at a human pace (the site rejects plain HTTP clients); "
        "the user chose this. Asia-Pacific and Middle East regions, Computing and Processing."
    )

    async def collect(
        self, fetcher: Fetcher, *, max_pages: int | None = None
    ) -> AsyncIterator[ListingData]:
        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        regions = getattr(self.settings, "ieee_regions", None) or DEFAULT_REGIONS
        fields = getattr(self.settings, "ieee_fields_of_interest", None) or DEFAULT_FIELDS
        delay = float(getattr(self.settings, "ieee_delay_seconds", 10.0))
        cap = max_pages or MAX_PAGES_PER_SEARCH
        start, end = self.settings.window_start, self.settings.window_end
        seen: set[str] = set()

        async with Stealth().use_async(async_playwright()) as p:
            browser = await p.chromium.launch(headless=True)
            page = await (
                await browser.new_context(viewport={"width": 1366, "height": 1000}, locale="en-US")
            ).new_page()
            last_load = 0.0

            async def load(url: str) -> tuple[int, list[dict]]:
                """Open one search page and return what the page itself received."""
                nonlocal last_load
                wait = last_load + delay - asyncio.get_running_loop().time()
                if wait > 0:
                    await asyncio.sleep(wait)
                bodies: list[dict] = []

                async def on_response(resp):
                    if "searchfacet" not in resp.url:
                        return
                    try:
                        body = await resp.json()
                    except Exception:
                        return
                    if (body.get("entity") or {}).get("results") is not None:
                        bodies.append(body)

                page.on("response", on_response)
                try:
                    # The results response can race the listener on a fresh page: one retry.
                    for attempt in range(2):
                        if attempt:
                            await asyncio.sleep(delay)
                        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                        for _ in range(30):  # up to 15 s for the results to arrive
                            if bodies:
                                break
                            await page.wait_for_timeout(500)
                        if bodies:
                            break
                finally:
                    page.remove_listener("response", on_response)
                    last_load = asyncio.get_running_loop().time()
                if not bodies:
                    raise RuntimeError(f"IEEE search returned no results data for {url}")
                return parse_response(bodies[-1])

            try:
                for region in regions:
                    for field in fields:
                        total, first = await load(search_url(region, field, 0))
                        log.info("IEEE %s / %s: %s upcoming", region, field, total)
                        # Find the first page that reaches the window by halving the page range.
                        # `pos` is a page number (pos=10 -> results 100-109), not an offset.
                        last_page = max(0, (total - 1) // PAGE_SIZE)
                        lo, hi = 0, last_page
                        pages_read = 1
                        while hi - lo > 1 and pages_read < cap:
                            mid = (lo + hi) // 2
                            _, rows = await load(search_url(region, field, mid))
                            pages_read += 1
                            # An empty page is past the last result: treat it as after the window.
                            last = rows[-1]["startDate"][:10] if rows else "9999-12-31"
                            if date.fromisoformat(last) < start:
                                lo = mid
                            else:
                                hi = mid
                        # Read forward from there until the results pass the window.
                        pos = lo
                        while pages_read < cap:
                            _, rows = await load(search_url(region, field, pos))
                            pages_read += 1
                            if not rows:
                                break
                            for r in rows:
                                listing = parse_result(r)
                                if listing.source_id in seen:
                                    continue
                                seen.add(listing.source_id)
                                yield listing
                            if date.fromisoformat(rows[0]["startDate"][:10]) > end:
                                break
                            pos += 1
                            if pos > last_page:
                                break
            finally:
                await browser.close()


# ---------- detail pages: the official website, scope, venue ----------


def _as_url(raw: str | None) -> str | None:
    """IEEE stores websites loosely ("iccmn.in/", "www.x.org", "https://..."); make them links."""
    raw = (raw or "").strip()
    if not raw or " " in raw or "." not in raw:
        return None
    return raw if re.match(r"https?://", raw, re.I) else f"https://{raw.lstrip('/')}"


def parse_detail(body: dict) -> dict:
    """The fields a detail page adds, from the JSON the page receives (`entity.eventDetail`)."""
    d = ((body or {}).get("entity") or {}).get("eventDetail") or {}
    venue = ", ".join(p.strip() for p in (d.get("venues") or "").split(",") if p.strip())
    return {
        "event_id": d.get("eventId"),
        "homepage": _as_url(d.get("url")),
        "description": " ".join((d.get("scope") or "").split()) or None,
        "venue": venue or None,
        "contact": (d.get("eventContact") or "").strip() or None,
        "keywords": [k.strip() for k in (d.get("keywords") or "").split(",") if k.strip()],
        "call_for_papers": d.get("callForPapers"),
    }


async def read_details(
    event_ids: list[int], *, delay: float, stop_after_empty: int = 2
) -> AsyncIterator[tuple[int, dict | None]]:
    """Open each conference's IEEE page in one real-browser session; yield what it received.

    Yields (event_id, detail or None). Stops early after `stop_after_empty` pages in a row that
    receive no data, which is how IEEE's throttling shows (the page loads, its data never comes).
    """
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    empty_in_a_row = 0
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True)
        page = await (
            await browser.new_context(viewport={"width": 1366, "height": 1000}, locale="en-US")
        ).new_page()
        try:
            for i, event_id in enumerate(event_ids):
                if i:
                    await asyncio.sleep(delay)
                bodies: list[dict] = []

                async def on_response(resp, bodies=bodies):
                    if "conf/details" not in resp.url:
                        return
                    try:
                        bodies.append(await resp.json())
                    except Exception:
                        return

                page.on("response", on_response)
                try:
                    url = DETAILS_URL.format(id=event_id)
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    for _ in range(30):  # up to 15 s for the data to arrive
                        if bodies:
                            break
                        await page.wait_for_timeout(500)
                except Exception as e:  # one slow page is not a reason to stop
                    log.warning("IEEE detail %s did not load: %r", event_id, e)
                finally:
                    page.remove_listener("response", on_response)
                detail = parse_detail(bodies[-1]) if bodies else None
                empty_in_a_row = 0 if detail and detail["event_id"] else empty_in_a_row + 1
                yield event_id, detail
                if empty_in_a_row >= stop_after_empty:
                    log.warning("IEEE stopped sending detail data (throttled?); stopping here")
                    return
        finally:
            await browser.close()
