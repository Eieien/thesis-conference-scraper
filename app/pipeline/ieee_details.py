"""Fill in the official website of conferences known only from IEEE's search.

IEEE's search results have no website link; each conference's IEEE page does. This step opens
those pages (slowly, through a real browser: see app/scrapers/ieee.py) only for conferences in the
project scope that have an IEEE listing and no homepage yet, and writes:

- the listing: `homepage`, `description` (IEEE's "scope"), and venue, contact, keywords and the
  call-for-papers date in `extra`;
- the conference: `homepage` (and `description` if it had none), recorded in `field_sources` as
  coming from IEEE with the IEEE page as the URL. A conference that enrich gave up on for lack of
  a homepage goes back to `collected`, so the next enrich run reads its website.

Pages are read in batches with a rest between them, and the run stops as soon as IEEE stops
sending data (throttling); running it again continues with the conferences still missing.
"""

import asyncio
import logging
import traceback
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.db import SessionLocal
from app.models import Conference, RawListing, ScrapeRun
from app.pipeline.geo import continent_of
from app.scrapers.ieee import read_details

log = logging.getLogger(__name__)

NO_HOMEPAGE_NOTE = "no homepage and no source page text"


def conferences_needing_details(db: Session, settings: Settings) -> list[Conference]:
    """In-scope conferences with an IEEE listing and no homepage, earliest first."""
    confs = db.scalars(
        select(Conference)
        .where(Conference.homepage.is_(None))
        .options(selectinload(Conference.listings))
        .order_by(Conference.start_date)
    ).all()
    return [
        c
        for c in confs
        if any(row.source == "ieee" for row in c.listings)
        and (continent_of(c.country) or "Unknown") in settings.scope_continents
    ]


def apply_detail(conf: Conference, row: RawListing, detail: dict) -> None:
    """Write one detail page's fields to its listing and conference."""
    extra = dict(row.extra or {})
    for key in ("venue", "contact", "keywords", "call_for_papers"):
        if detail.get(key):
            extra[key] = detail[key]
    row.extra = extra
    if detail.get("homepage"):
        row.homepage = detail["homepage"]
    if detail.get("description") and not row.description:
        row.description = detail["description"]

    sources = dict(conf.field_sources or {})
    if detail.get("homepage") and not conf.homepage:
        conf.homepage = detail["homepage"]
        sources["homepage"] = {"source": "ieee", "url": row.url}
        if conf.status == "needs_review" and conf.review_note == NO_HOMEPAGE_NOTE:
            conf.status, conf.review_note = "collected", None  # enrich can read it now
    if detail.get("description") and not conf.description:
        conf.description = detail["description"]
        sources["description"] = {"source": "ieee", "url": row.url}
    conf.field_sources = sources


async def run_ieee_details(
    settings: Settings, run_id: int, *, batch_size: int = 60, rest_seconds: int = 900
) -> None:
    stats: Counter = Counter()
    delay = float(getattr(settings, "ieee_delay_seconds", 10.0))
    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        try:
            by_event: dict[int, tuple[Conference, RawListing]] = {}
            for conf in conferences_needing_details(db, settings):
                row = next(r for r in conf.listings if r.source == "ieee")
                by_event[int(row.source_id)] = (conf, row)
            ids = list(by_event)
            stats["to_fetch"] = len(ids)
            throttled = False
            for start in range(0, len(ids), batch_size):
                if start:
                    run.stats = dict(stats)
                    db.commit()
                    log.info("IEEE details: resting %ss before the next batch", rest_seconds)
                    await asyncio.sleep(rest_seconds)
                batch = ids[start : start + batch_size]
                read = 0
                async for event_id, detail in read_details(batch, delay=delay):
                    read += 1
                    conf, row = by_event[event_id]
                    if not detail or not detail["event_id"]:
                        stats["no_data"] += 1
                    else:
                        apply_detail(conf, row, detail)
                        stats["with_website" if detail["homepage"] else "no_website_listed"] += 1
                    run.stats = dict(stats)
                    db.commit()
                if read < len(batch):
                    throttled = True
                    break
            if throttled:
                run.status = "failed"
                run.error = "IEEE stopped sending detail data (throttled). Run again later."
            else:
                run.status = "succeeded"
        except Exception as e:
            log.exception("ieee details run failed")
            db.rollback()
            run.status = "failed"
            run.error = f"{e!r}\n{traceback.format_exc()[-2000:]}"
        run.stats = dict(stats)
        run.finished_at = datetime.now(UTC)
        db.commit()
