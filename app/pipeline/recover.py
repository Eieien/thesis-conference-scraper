"""Recover conference sites that enrich could not read ("no homepage and no source page text").

Why sites fail (Asia, 2026-09-25, 102 of them): WASET's HTTP 403 (50, not recovered: see
app/pipeline/quality.py), no website known (11), refused or silent connections (17),
robots.txt disallowing crawlers (9, respected), dead pages (404/500, 6), and other sites
answering the crawler with 403/418 (4).

For each one this step, one site at a time:
1. fetches the homepage again (a temporary outage has often passed);
2. when the site still rejects the crawler (403, 418, 429, 5xx, refused connection) and its
   robots.txt allows reading, opens it in a real browser (Playwright + stealth, like the IEEE
   adapter; the user chose this for sites that block bots), with at least
   `recover_delay_seconds` between page loads, reading the homepage and its most relevant
   linked pages (registration, fees, dates);
3. saves what it read into the normal page cache (`Fetcher.store`), and puts the conference
   back to `collected`, so the next enrich run reads it like any other site.
"""

import asyncio
import logging
import traceback
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import Conference, ScrapeRun
from app.pipeline.enrich import score_links
from app.pipeline.geo import continent_of
from app.pipeline.quality import organizer_flag
from app.scrapers.html_clean import extract_links, html_to_text
from app.scrapers.http import Fetcher, FetchError, RobotsDisallowed

log = logging.getLogger(__name__)

UNREADABLE_NOTE = "no homepage and no source page text"
MIN_TEXT = 300  # characters of text a readable page has at least


def unreadable(db, settings: Settings, conference_ids: list[int] | None = None):
    query = select(Conference).where(Conference.review_note == UNREADABLE_NOTE)
    if conference_ids:
        query = query.where(Conference.id.in_(conference_ids))
    return [
        c
        for c in db.scalars(query).all()
        if (continent_of(c.country) or "Unknown") in settings.scope_continents
    ]


async def _render_site(page, fetcher: Fetcher, homepage: str, delay: float, pages: int) -> int:
    """Open the homepage and its best linked pages in the browser; cache what reads as a page.
    Returns how many pages were saved."""
    saved = 0
    await page.goto(homepage, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(3000)  # let scripts fill the page
    html = await page.content()
    if len(html_to_text(html)) < MIN_TEXT:
        return 0
    fetcher.store(homepage, page.url, html)
    saved += 1
    links = score_links(extract_links(html, page.url), homepage)
    for _, url, _ in links[: pages - 1]:
        if url.lower().endswith(".pdf") or not await fetcher.allowed(url):
            continue
        await asyncio.sleep(delay)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(2000)
            html = await page.content()
        except Exception as e:
            log.info("recover: %s did not load: %r", url, e)
            continue
        if len(html_to_text(html)) >= MIN_TEXT:
            fetcher.store(url, page.url, html)
            saved += 1
    return saved


async def run_recover(
    settings: Settings, run_id: int, *, conference_ids: list[int] | None = None
) -> None:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    stats: Counter = Counter()
    delay = float(settings.recover_delay_seconds)
    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        try:
            todo = unreadable(db, settings, conference_ids)
            stats["to_check"] = len(todo)
            async with (
                Fetcher(settings) as fetcher,
                Stealth().use_async(async_playwright()) as p,
            ):
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 1366, "height": 1000}, locale="en-US"
                )
                page = await context.new_page()
                try:
                    for conf in todo:
                        outcome = await _recover_one(conf, fetcher, page, delay, settings)
                        stats[outcome] += 1
                        if outcome in ("reachable again", "read with a browser"):
                            conf.status, conf.review_note = "collected", None
                        run.stats = dict(stats)
                        db.commit()
                finally:
                    await browser.close()
            run.status = "succeeded"
        except Exception as e:
            log.exception("recover run failed")
            db.rollback()
            run.status = "failed"
            run.error = f"{e!r}\n{traceback.format_exc()[-2000:]}"
        run.stats = dict(stats)
        run.finished_at = datetime.now(UTC)
        db.commit()


async def _recover_one(conf, fetcher: Fetcher, page, delay: float, settings: Settings) -> str:
    """What happened to one site, as a short label for the run's stats."""
    if not conf.homepage:
        return "no website known"
    if organizer_flag(conf.homepage):
        return "skipped: predatory organizer"
    try:
        result = await fetcher.get(conf.homepage, use_cache=False)
        if len(html_to_text(result.text)) >= MIN_TEXT:
            return "reachable again"
    except RobotsDisallowed:
        return "robots.txt disallows (respected)"
    except FetchError as e:
        if "HTTP 404" in str(e) or "HTTP 410" in str(e):
            return "page gone (404)"
        log.info("recover: %s failed plainly (%s); trying a browser", conf.homepage, e)
    if not await fetcher.allowed(conf.homepage):
        return "robots.txt disallows (respected)"
    try:
        saved = await _render_site(
            page, fetcher, conf.homepage, delay, settings.enrich_max_pages_per_site
        )
    except Exception as e:
        log.info("recover: browser could not open %s: %r", conf.homepage, e)
        return "unreachable even in a browser"
    finally:
        await asyncio.sleep(delay)
    return "read with a browser" if saved else "unreachable even in a browser"
