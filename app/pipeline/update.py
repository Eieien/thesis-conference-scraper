"""The whole pipeline in one run, each step starting when the previous one ends.

collect (every source at once) -> filter -> merge -> IEEE websites -> enrich -> history -> export
-> publish (Supabase: the public site's tables, then the full copy the DCISM API reads; skipped
without SUPABASE_DB_URL)

Sources are different websites, so they are collected in parallel: each site still gets its
own polite delay from its Fetcher, and SQLite's writes stay short (one listing per commit), so
the collectors take turns at the database. Every step is also a normal run of its own, so the
dashboard's Activity list shows each one live; this parent run says which step it is on.
A failed step (EDAS session expired, AI quota used up) is recorded and the chain goes on.
"""

import asyncio
import logging
import traceback
from datetime import UTC, datetime

from app.config import Settings
from app.db import SessionLocal
from app.models import ScrapeRun
from app.pipeline.collect import run_collect
from app.pipeline.enrich import run_enrich
from app.pipeline.filter import run_filter
from app.pipeline.history import run_history
from app.pipeline.ieee_details import run_ieee_details
from app.pipeline.merge import run_merge
from app.scrapers.registry import ADAPTERS

log = logging.getLogger(__name__)

STEPS = ["collect", "filter", "merge", "ieee websites", "enrich", "history", "export", "publish"]
IEEE_REST_SECONDS = 900  # between IEEE's search pages and its detail pages, and between batches


def _publish(settings: Settings) -> None:
    from app.pipeline.publish import public_data
    from scripts.sync_supabase import push, revalidate

    with SessionLocal() as db:
        data = public_data(db, settings, settings.public_continents)
    push(data, settings.supabase_db_url.get_secret_value(), init=False)
    log.info(revalidate(settings))

    # The public API on DCISM reads the private full copy (schema `scraper`).
    from scripts.copy_to_supabase import copy

    try:
        counts = copy(settings.supabase_db_url.get_secret_value())
    except SystemExit as e:  # the script's checks stop with SystemExit; keep the server up
        raise RuntimeError(str(e)) from None
    log.info("copied %d rows to Supabase's scraper schema", sum(counts.values()))


def _child(kind: str, source: str | None = None, params: dict | None = None) -> int:
    with SessionLocal() as db:
        run = ScrapeRun(kind=kind, source=source, params=params or {})
        db.add(run)
        db.commit()
        return run.id


async def run_update(
    settings: Settings,
    run_id: int,
    *,
    sources: list[str] | None = None,
    continent: str | None = None,
    retry_no_fees: bool = False,
    export: bool = True,
) -> None:
    sources = sources or [n for n, cls in ADAPTERS.items() if cls.implemented]
    # Enrich and history cover the public site's continents unless one is named ('all' = every).
    targets = (
        [None] if continent == "all" else [continent] if continent else settings.public_continents
    )

    def progress(step: str, done: int, **extra: int) -> None:
        with SessionLocal() as db:
            run = db.get(ScrapeRun, run_id)
            run.params = {**run.params, "current": step}  # a new dict: JSON columns need it
            run.stats = {**run.stats, "steps done": done, "of steps": len(STEPS), **extra}
            db.commit()

    try:
        progress("collect", 0)
        await asyncio.gather(
            *(
                run_collect(
                    settings, _child("collect", name), name, max_pages=None, fetch_details=True
                )
                for name in sources
            )
        )

        progress("filter", 1)
        with SessionLocal() as db:
            await run_filter(db, settings, use_llm=False)

        progress("merge", 2)
        with SessionLocal() as db:
            merged = run_merge(db)
        created = {"new conferences": merged.get("created", 0)}
        if "ieee" in sources:
            # IEEE throttled the website lookup when it began right after collecting (2026-10-11,
            # 49 s after a thousand search results). Rest as long as between its own batches.
            progress("ieee websites (resting first)", 3, **created)
            await asyncio.sleep(IEEE_REST_SECONDS)
            progress("ieee websites", 3, **created)
            await run_ieee_details(
                settings, _child("ieee_details", "ieee"), rest_seconds=IEEE_REST_SECONDS
            )

        progress("enrich", 4)
        for target in targets:
            await run_enrich(
                settings,
                _child("enrich", params={"continent": target, "retry_no_fees": retry_no_fees}),
                conference_ids=None,
                limit=None,
                only_missing=True,
                continent=target,
                retry_no_fees=retry_no_fees,
            )

        progress("history", 5)
        for target in targets:
            await run_history(
                settings,
                _child("history", params={"continent": target, "only_unchecked": True}),
                continent=target,
                only_unchecked=True,
            )

        progress("export", 6)
        if export:
            from scripts.export_mysql import export as export_mysql

            await asyncio.to_thread(export_mysql, settings.export_dir / "conference_data.sql.gz")

        # The public site (Supabase), only when its connection string is set in .env.
        progress("publish", 7)
        if settings.supabase_db_url:
            await asyncio.to_thread(_publish, settings)
        progress("done", 8)
        status, error = "succeeded", None
    except Exception as e:
        log.exception("update chain failed")
        status, error = "failed", f"{e!r}\n{traceback.format_exc()[-2000:]}"

    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        run.status, run.error, run.finished_at = status, error, datetime.now(UTC)
        db.commit()
