import logging
import traceback
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionLocal
from app.models import RawListing, ScrapeRun
from app.pipeline.filter import in_window
from app.schemas import ListingData
from app.scrapers.registry import get_adapter

log = logging.getLogger(__name__)

_LISTING_FIELDS = [f for f in ListingData.model_fields if f != "source_id"]


def upsert_listing(db: Session, source: str, data: ListingData) -> bool:
    """Insert or update one listing. Returns True if it was new."""
    row = db.scalar(
        select(RawListing).where(
            RawListing.source == source, RawListing.source_id == data.source_id
        )
    )
    created = row is None
    if created:
        row = RawListing(source=source, source_id=data.source_id)
        db.add(row)
    for field in _LISTING_FIELDS:
        value = getattr(data, field)
        # Don't let a thinner re-scrape wipe fields a detail fetch filled earlier.
        if value not in (None, [], {}) or created:
            setattr(row, field, value)
    row.last_seen_at = datetime.now(UTC)
    return created


async def collect_listings(
    settings: Settings,
    source: str,
    *,
    max_pages: int | None,
    fetch_details: bool,
    limit: int | None = None,
):
    """Yield listings from one source; detail pages are fetched only for in-window events."""
    adapter = get_adapter(source)(settings)
    async with adapter.make_fetcher() as fetcher:
        count = 0
        async for listing in adapter.collect(fetcher, max_pages=max_pages):
            maybe_relevant = listing.start_date is None or in_window(listing.start_date, settings)
            if fetch_details and adapter.has_details and maybe_relevant:
                listing = await adapter.fetch_detail(fetcher, listing)
            yield listing
            count += 1
            if limit and count >= limit:
                return


async def run_collect(
    settings: Settings, run_id: int, source: str, *, max_pages: int | None, fetch_details: bool
) -> None:
    stats: Counter = Counter()
    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        try:
            async for listing in collect_listings(
                settings, source, max_pages=max_pages, fetch_details=fetch_details
            ):
                stats["seen"] += 1
                stats["new" if upsert_listing(db, source, listing) else "updated"] += 1
                if listing.start_date and in_window(listing.start_date, settings):
                    stats["in_window"] += 1
                db.commit()
                run.stats = dict(stats)
            run.status = "succeeded"
        except Exception as e:  # recorded on the run so the API can show it
            log.exception("collect %s failed", source)
            db.rollback()
            run.status = "failed"
            run.error = f"{e!r}\n{traceback.format_exc()[-2000:]}"
        run.stats = dict(stats)
        run.finished_at = datetime.now(UTC)
        db.commit()
