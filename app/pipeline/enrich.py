"""Homepage enrichment: find registration/dates pages, have the LLM extract fees and deadlines."""

import asyncio
import logging
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionLocal
from app.extract import Extractor, ExtractorStop, get_extractor
from app.models import Conference, Fee, ScrapeRun
from app.pipeline.attendance import record as attendance_record
from app.pipeline.geo import continent_of
from app.pipeline.merge import refresh_fee_summary
from app.pipeline.normalize import normalize_currency
from app.pipeline.validate import (
    DUPLICATE,
    VERIFIED,
    FeeCheck,
    check_fees,
    fee_mentions,
    summary_note,
)
from app.schemas import ExtractedPage
from app.scrapers.html_clean import extract_links, html_to_text, pdf_to_text
from app.scrapers.http import Fetcher, FetchError

log = logging.getLogger(__name__)

_LINK_KEYWORDS = {
    "registration": 6,
    "register": 5,
    "fee": 6,
    "fees": 6,
    "payment": 3,
    "price": 3,
    "important dates": 5,
    "important-dates": 5,
    "dates": 3,
    "deadline": 4,
    "call for papers": 3,
    "cfp": 3,
    "submission": 2,
    "about": 1,
    "attend": 2,
}
# Homepage-extracted dates further than this from the listing's dates are probably an old edition.
EDITION_TOLERANCE_DAYS = 45

EXTRACTED_DATE_FIELDS = [
    "start_date",
    "end_date",
    "abstract_deadline",
    "paper_deadline",
    "notification_date",
    "camera_ready_deadline",
    "registration_deadline",
]
EXTRACTED_TEXT_FIELDS = ["city", "country", "description", "registration_url"]


def _same_site(a: str, b: str) -> bool:
    ha, hb = urlsplit(a).netloc.lower(), urlsplit(b).netloc.lower()
    root = lambda h: ".".join(h.split(".")[-2:])  # noqa: E731
    return root(ha) == root(hb)


def score_links(links: list[tuple[str, str]], homepage: str) -> list[tuple[int, str, str]]:
    scored = []
    for url, text in links:
        if not _same_site(url, homepage) and not url.lower().endswith(".pdf"):
            continue
        hay = f"{text} {urlsplit(url).path}".lower()
        score = sum(w for kw, w in _LINK_KEYWORDS.items() if kw in hay)
        if score:
            scored.append((score, url, text))
    return sorted(scored, key=lambda s: -s[0])


async def discover_pages(fetcher: Fetcher, homepage: str) -> tuple[str, list[tuple[int, str, str]]]:
    result = await fetcher.get(homepage)
    return result.text, score_links(extract_links(result.text, result.final_url), homepage)


async def _page_text(fetcher: Fetcher, url: str) -> str:
    result = await fetcher.get(url)
    if result.is_pdf:
        return pdf_to_text(result.content)
    return html_to_text(result.text)


def _to_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def edition_mismatch(conf: Conference, extracted: ExtractedPage) -> str | None:
    start = _to_date(extracted.start_date)
    if conf.start_date and start and abs((start - conf.start_date).days) > EDITION_TOLERANCE_DAYS:
        return f"homepage dates {start} don't match listing {conf.start_date} (old edition?)"
    return None


def apply_extraction(
    db: Session, conf: Conference, extracted: ExtractedPage, source_url: str, text: str = ""
) -> list[FeeCheck]:
    """Write the extracted fields and fees. Each fee is checked against `text`, the pages the
    LLM read (app/pipeline/validate.py). Returns the checks."""
    sources = dict(conf.field_sources or {})
    for field in EXTRACTED_DATE_FIELDS:
        if value := _to_date(getattr(extracted, field)):
            setattr(conf, field, value)
            sources[field] = {"source": "homepage", "url": source_url}
    for field in EXTRACTED_TEXT_FIELDS:
        if value := getattr(extracted, field):
            setattr(conf, field, value)
            sources[field] = {"source": "homepage", "url": source_url}
    conf.field_sources = sources

    checks: list[FeeCheck] = []
    if extracted.fees:
        rows = [
            {
                "category": f.category[:255],
                "tier": f.tier,
                "amount": f.amount,
                "currency": normalize_currency(f.currency) or (f.currency or "").upper() or None,
                "notes": f.notes,
            }
            for f in extracted.fees
        ]
        checks = check_fees(rows, text)
        db.execute(delete(Fee).where(Fee.conference_id == conf.id, Fee.source == "homepage"))
        for row, check in zip(rows, checks, strict=True):
            if check.status == DUPLICATE:
                continue
            db.add(
                Fee(
                    **row,
                    conference_id=conf.id,
                    source="homepage",
                    source_url=conf.registration_url or source_url,
                    check_status=check.status,
                    check_note=check.note,
                    evidence=check.evidence,
                )
            )
        db.flush()
        db.refresh(conf)
    refresh_fee_summary(conf)
    return checks


@dataclass
class _Job:
    """What one conference's enrichment needs, read from the database up front, so the network
    part can run concurrently without touching the shared session."""

    conference_id: int
    homepage: str | None
    hint: str
    sections: list[str]


@dataclass
class _Outcome:
    extracted: ExtractedPage | None = None
    note: str | None = None  # set when there was nothing to read
    text: str = ""  # what the LLM read; fees are validated against it


def _job_for(conf: Conference) -> _Job:
    sections: list[str] = []
    # Source pages that already carry fee/deadline text (EDAS register page, WikiCFP CFP text).
    for row in conf.listings:
        extra = row.extra or {}
        for key in ("register_page_text", "edas_page_text"):
            if extra.get(key):
                sections.append(f"### {row.source} {key} ({row.url})\n{extra[key]}")
        if row.source == "wikicfp" and row.description:
            sections.append(f"### wikicfp call for papers ({row.url})\n{row.description}")
    hint = f"{conf.acronym or ''} {conf.name} starting {conf.start_date or 'unknown date'}"
    return _Job(conf.id, conf.homepage, hint, sections)


async def _gather_text(job: _Job, fetcher: Fetcher, settings: Settings) -> str | None:
    """Source-page text plus the homepage and its most relevant linked pages. None when there is
    nothing to read. Used by enrich (for the LLM) and by validate (to re-check stored fees)."""
    sections = list(job.sections)
    if job.homepage:
        try:
            home_html, candidates = await discover_pages(fetcher, job.homepage)
            for _, url, _text in candidates[: settings.enrich_max_pages_per_site - 1]:
                try:
                    sections.insert(0, f"### page {url}\n{await _page_text(fetcher, url)}")
                except FetchError as e:
                    log.info("skip %s: %s", url, e)
            sections.append(f"### homepage {job.homepage}\n{html_to_text(home_html)}")
        except FetchError as e:
            log.info("homepage %s failed: %s", job.homepage, e)

    if not sections:
        return None
    # Give each section a fair share so one huge page can't crowd out the fee page.
    budget = settings.gemini_max_input_chars // len(sections)
    return "\n\n".join(s[:budget] for s in sections)


async def _read_and_extract(
    job: _Job, fetcher: Fetcher, extractor: Extractor, settings: Settings
) -> _Outcome:
    """The slow part: crawl the homepage and ask the LLM. No database access."""
    text = await _gather_text(job, fetcher, settings)
    if text is None:
        return _Outcome(note="no homepage and no source page text")
    extracted = await extractor.extract_conference(text, url=job.homepage, hint=job.hint)
    return _Outcome(extracted=extracted, text=text)


def _apply_outcome(db: Session, conf: Conference, outcome: _Outcome) -> str:
    """The quick part: write the result. Returns the resulting status."""
    conf.enriched_at = datetime.now(UTC)
    extracted = outcome.extracted
    if extracted is None:
        conf.status, conf.review_note = "needs_review", outcome.note
        return conf.status
    if note := edition_mismatch(conf, extracted):
        conf.status, conf.review_note = "needs_review", note
        return conf.status

    checks = apply_extraction(db, conf, extracted, conf.homepage or "source pages", outcome.text)
    _set_status(conf, checks, extracted.fees_published, outcome.text)
    conf.attendance = attendance_record(conf, outcome.text)
    return conf.status


MISSED_FEES_NOTE = "prices on the page were not extracted"


def _set_status(
    conf: Conference, checks: list[FeeCheck], fees_published: bool, text: str | None = None
) -> None:
    verified = any(f.check_status in (None, VERIFIED) for f in conf.fees)
    if verified:
        conf.status, conf.review_note = "enriched", summary_note(checks)
        return
    conf.status = "needs_review"
    if conf.fees:
        conf.review_note = "fees found, but none could be verified: " + (
            summary_note(checks) or "see the fee checks"
        )
    else:
        conf.review_note = "fees not published yet" if not fees_published else "fees not found"
    # No usable fee: check the page itself for prices the extraction missed (no LLM).
    if text and (mentions := fee_mentions(text, limit=1)):
        conf.review_note = f'{MISSED_FEES_NOTE}, e.g. "{mentions[0][:160]}"'


async def enrich_conference(
    db: Session, conf: Conference, fetcher: Fetcher, extractor: Extractor, settings: Settings
) -> str:
    """Enrich one conference start to finish. Returns the resulting status."""
    outcome = await _read_and_extract(_job_for(conf), fetcher, extractor, settings)
    return _apply_outcome(db, conf, outcome)


async def run_enrich(
    settings: Settings,
    run_id: int,
    *,
    conference_ids: list[int] | None,
    limit: int | None,
    only_missing: bool,
    continent: str | None = None,
    retry_no_fees: bool = False,
) -> None:
    """Enrich many conferences, several at a time (`enrich_concurrency`).

    `retry_no_fees` also takes conferences read before that ended without fees ("fees not
    published yet", "fees not found"), e.g. after raising `enrich_max_pages_per_site`.
    `continent` keeps only conferences whose country is on that continent.

    Only the network work runs concurrently: each site still gets its own polite delay from the
    shared Fetcher, and the extractor still spaces its LLM calls. Results are written to the
    database one at a time, in the order they finish.
    """
    stats: Counter = Counter()
    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        tasks: list[asyncio.Task] = []
        try:
            extractor = get_extractor(settings)
            query = select(Conference).order_by(Conference.start_date)
            if conference_ids:
                query = query.where(Conference.id.in_(conference_ids))
            elif only_missing:
                wanted = Conference.status.in_(["collected", "enrich_failed"])
                if retry_no_fees:
                    wanted = wanted | (
                        (Conference.status == "needs_review")
                        & Conference.fee_min.is_(None)
                        & Conference.review_note.in_(["fees not published yet", "fees not found"])
                    )
                query = query.where(wanted)
            rows = db.scalars(query).all()
            if continent:
                rows = [c for c in rows if (continent_of(c.country) or "Unknown") == continent]
            if limit:
                rows = rows[:limit]
            conferences = {c.id: c for c in rows}
            jobs = [_job_for(c) for c in conferences.values()]

            gate = asyncio.Semaphore(max(1, settings.enrich_concurrency))
            async with Fetcher(settings) as fetcher:

                async def work(job: _Job) -> tuple[_Job, _Outcome | Exception]:
                    async with gate:
                        try:
                            return job, await _read_and_extract(job, fetcher, extractor, settings)
                        except ExtractorStop:
                            raise  # quota or billing: stop everything
                        except Exception as e:  # one bad site must not stop the run
                            return job, e

                tasks = [asyncio.create_task(work(j)) for j in jobs]
                for finished in asyncio.as_completed(tasks):
                    job, result = await finished
                    conf = conferences[job.conference_id]
                    if isinstance(result, Exception):
                        log.error("enrich %s failed: %r", conf.id, result)
                        conf.status, conf.review_note = "enrich_failed", repr(result)[:500]
                        status = "enrich_failed"
                    else:
                        try:
                            status = _apply_outcome(db, conf, result)
                        except Exception as e:
                            log.exception("saving enrich result for %s failed", conf.id)
                            db.rollback()
                            conf.status, conf.review_note = "enrich_failed", repr(e)[:500]
                            status = "enrich_failed"
                    stats[status] += 1
                    run.stats = dict(stats)
                    db.commit()
            run.status = "succeeded"
        except Exception as e:
            log.exception("enrich run failed")
            for t in tasks:
                t.cancel()
            db.rollback()
            run.status = "failed"
            run.error = f"{e!r}\n{traceback.format_exc()[-2000:]}"
        run.stats = dict(stats)
        run.finished_at = datetime.now(UTC)
        db.commit()


async def run_validate(
    settings: Settings, run_id: int, *, conference_ids: list[int] | None
) -> None:
    """Re-check stored fees against the pages they came from (app/pipeline/validate.py).

    No LLM: the page text is rebuilt the way enrich builds it, mostly from the fetch cache, and
    every stored fee is checked against it. Fees that fail stay visible but stop counting towards
    the conference's fee range.
    """
    stats: Counter = Counter()
    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        tasks: list[asyncio.Task] = []
        try:
            # Conferences with fees get them re-checked; those without get the missed-fee check.
            query = select(Conference).where(
                or_(
                    Conference.fees.any(),
                    Conference.review_note.like("fees not%"),
                    Conference.review_note.like(f"{MISSED_FEES_NOTE}%"),
                )
            )
            if conference_ids:
                query = query.where(Conference.id.in_(conference_ids))
            conferences = {c.id: c for c in db.scalars(query).all()}
            jobs = [_job_for(c) for c in conferences.values()]
            gate = asyncio.Semaphore(max(1, settings.enrich_concurrency))
            async with Fetcher(settings) as fetcher:

                async def work(job: _Job) -> tuple[_Job, str | None | Exception]:
                    async with gate:
                        try:
                            return job, await _gather_text(job, fetcher, settings)
                        except Exception as e:
                            return job, e

                tasks = [asyncio.create_task(work(j)) for j in jobs]
                for finished in asyncio.as_completed(tasks):
                    job, text = await finished
                    conf = conferences[job.conference_id]
                    if not isinstance(text, str):
                        stats["pages unavailable"] += 1
                        continue
                    fees = sorted(conf.fees, key=lambda f: f.id)
                    if not fees:
                        if mentions := fee_mentions(text, limit=1):
                            conf.status = "needs_review"
                            conf.review_note = f'{MISSED_FEES_NOTE}, e.g. "{mentions[0][:160]}"'
                            stats["no fees, but prices on the page"] += 1
                        else:
                            if (conf.review_note or "").startswith(MISSED_FEES_NOTE):
                                conf.review_note = "fees not published yet"  # flag no longer holds
                            stats["no fees, none on the page"] += 1
                        stats["conferences"] += 1
                        run.stats = dict(stats)
                        db.commit()
                        continue
                    checks = check_fees(
                        [
                            {
                                "category": f.category,
                                "tier": f.tier,
                                "amount": f.amount,
                                "currency": f.currency,
                            }
                            for f in fees
                        ],
                        text,
                    )
                    for fee, check in zip(fees, checks, strict=True):
                        if check.status == DUPLICATE:
                            db.delete(fee)
                        else:
                            fee.check_status = check.status
                            fee.check_note = check.note
                            fee.evidence = check.evidence
                        stats[f"fees {check.status}"] += 1
                    db.flush()
                    db.refresh(conf)
                    for f in conf.fees:  # the RMB/CNY fix, for fees saved before it
                        f.currency = normalize_currency(f.currency) or f.currency
                    refresh_fee_summary(conf)
                    if conf.status in ("enriched", "needs_review") and conf.fees:
                        _set_status(conf, checks, fees_published=True, text=text)
                    stats["conferences"] += 1
                    run.stats = dict(stats)
                    db.commit()
            run.status = "succeeded"
        except Exception as e:
            log.exception("validate run failed")
            for t in tasks:
                t.cancel()
            db.rollback()
            run.status = "failed"
            run.error = f"{e!r}\n{traceback.format_exc()[-2000:]}"
        run.stats = dict(stats)
        run.finished_at = datetime.now(UTC)
        db.commit()
