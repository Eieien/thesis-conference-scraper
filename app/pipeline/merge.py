"""Cross-source de-duplication: raw listings -> canonical conferences."""

import re
from collections import Counter
from datetime import date

from rapidfuzz import fuzz
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Conference, Fee, RawListing
from app.pipeline.fees import summarize_fees
from app.pipeline.validate import DUPLICATE, VERIFIED, check_fees
from app.scrapers.registry import SOURCE_PRIORITY

MERGE_FIELDS = [
    "acronym",
    "name",
    "start_date",
    "end_date",
    "city",
    "country",
    "location_raw",
    "description",
    "homepage",
    "abstract_deadline",
    "paper_deadline",
    "notification_date",
    "camera_ready_deadline",
    "registration_deadline",
]
MAX_DATE_GAP_DAYS = 7
NAME_MATCH_THRESHOLD = 90

# Words some sources put in front of an acronym: "IEEE CAMA", "Ei/Scopus-AI2A" (an indexing
# claim used by some organizers). They are not part of the event's name.
_NOISE_WORDS = re.compile(r"\b(?:IEEE|ACM|IFIP|SPRINGER|IEEE/ACM|ACM/IEEE|EI|SCOPUS)\b", re.I)
_ORDINAL = re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.I)
_YEAR = re.compile(r"(?:19|20)\d{2}|'\d{2}\b")
_BOILERPLATE = re.compile(
    r"\b(?:the|of|on|and|for|in|international|conference|symposium|workshop|annual|"
    r"ieee|acm|\d+(?:st|nd|rd|th)|(?:19|20)\d{2})\b",
    re.I,
)


def acronym_key(acronym: str | None) -> str | None:
    """'19th IEEE MCSoC-2026' -> 'MCSOC', "ATC'26" -> 'ATC', '2026 2nd ICAIFI' -> 'ICAIFI'."""
    if not acronym:
        return None
    s = _NOISE_WORDS.sub(" ", acronym)
    s = _ORDINAL.sub(" ", s)
    s = _YEAR.sub(" ", s)
    s = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    return s or None


def _close(a: date | None, b: date | None) -> bool:
    return a is not None and b is not None and abs((a - b).days) <= MAX_DATE_GAP_DAYS


def _priority(source: str) -> int:
    return SOURCE_PRIORITY.index(source) if source in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


def core_name(name: str | None) -> str:
    """Name without the words every conference shares, so generic titles don't look alike."""
    without_parens = re.sub(r"\(.*?\)", " ", name or "")
    return " ".join(_BOILERPLATE.sub(" ", without_parens).split()).lower()


def _places_conflict(a: RawListing, b: Conference) -> bool:
    ca, cb = (a.country or "").strip().lower(), (b.country or "").strip().lower()
    return bool(ca and cb and ca != cb)


def find_match(listing: RawListing, conferences: list[Conference]) -> Conference | None:
    key = acronym_key(listing.acronym)
    for conf in conferences:
        if key and key == acronym_key(conf.acronym) and _close(listing.start_date, conf.start_date):
            return conf
    # Name fallback, for sources that give no acronym or format it differently.
    for conf in conferences:
        other_key = acronym_key(conf.acronym)
        if key and other_key and key != other_key:
            continue  # two different known acronyms: different events (often co-located)
        if not _close(listing.start_date, conf.start_date) or _places_conflict(listing, conf):
            continue
        similarity = fuzz.token_sort_ratio(core_name(listing.name), core_name(conf.name))
        if similarity >= NAME_MATCH_THRESHOLD:
            return conf
    return None


def apply_listing_fields(conf: Conference) -> None:
    """Fill each field from the highest-priority listing that has it. Homepage values stay."""
    listings = sorted(conf.listings, key=lambda row: _priority(row.source))
    sources = dict(conf.field_sources or {})
    for field in MERGE_FIELDS:
        if sources.get(field, {}).get("source") == "homepage":
            continue
        for row in listings:
            value = getattr(row, field)
            if value:
                setattr(conf, field, value)
                sources[field] = {"source": row.source, "url": row.url}
                break
    conf.field_sources = sources


def sync_source_fees(db: Session, conf: Conference) -> None:
    """Copy fee rows parsed by source adapters (e.g. EDAS register page) into the fees table."""
    for row in conf.listings:
        fees = (row.extra or {}).get("fees")
        if not fees:
            continue
        db.execute(delete(Fee).where(Fee.conference_id == conf.id, Fee.source == row.source))
        page_text = (row.extra or {}).get("register_page_text") or ""
        checks = check_fees(fees, page_text) if page_text else [None] * len(fees)
        for f, check in zip(fees, checks, strict=True):
            if check is not None and check.status == DUPLICATE:
                continue
            db.add(
                Fee(
                    conference_id=conf.id,
                    category=f["category"],
                    tier=f.get("tier"),
                    amount=f["amount"],
                    currency=f.get("currency"),
                    notes=f.get("notes"),
                    source=row.source,
                    source_url=(row.extra or {}).get("register_self_url") or row.url,
                    check_status=check.status if check else None,
                    check_note=check.note if check else None,
                    evidence=check.evidence if check else None,
                )
            )
    db.flush()
    db.refresh(conf)
    refresh_fee_summary(conf)


def refresh_fee_summary(conf: Conference) -> None:
    # Fees that failed validation stay visible but don't count; unchecked ones (None) still do.
    conf.fee_min, conf.fee_max, conf.currency = summarize_fees(
        [(f.amount, f.currency) for f in conf.fees if f.check_status in (None, VERIFIED)]
    )


def run_merge(db: Session) -> dict:
    stats: Counter = Counter()
    conferences = list(db.scalars(select(Conference)))
    pending = db.scalars(
        select(RawListing).where(
            RawListing.in_window.is_(True),
            RawListing.is_cs.isnot(False),  # keep ambiguous ones; they can be reviewed later
            RawListing.conference_id.is_(None),
        )
    ).all()

    for listing in sorted(pending, key=lambda row: _priority(row.source)):
        conf = find_match(listing, conferences)
        if conf is None:
            conf = Conference(
                name=listing.name, acronym=listing.acronym, start_date=listing.start_date
            )
            db.add(conf)
            db.flush()
            conferences.append(conf)
            stats["created"] += 1
        else:
            stats["matched"] += 1
        listing.conference = conf

    db.flush()
    for conf in conferences:
        apply_listing_fields(conf)
        sync_source_fees(db, conf)
    db.commit()
    stats["combined_duplicates"] = run_dedupe(db)["pairs"]
    stats["conferences_total"] = len(conferences) - stats["combined_duplicates"]
    return dict(stats)


# ---------- combining duplicates that already exist ----------
#
# `run_merge` only places new listings, so two conferences created before a matching rule
# improved (or from listings merged in different runs) stay apart. This pass finds such pairs
# among existing conferences and folds one into the other: listings, missing fields and fees.

DEDUPE_NAME_THRESHOLD = 92


def _keys_compatible(a: str | None, b: str | None) -> bool:
    """Missing, equal, or one a prefix of the other ("ICETC" / "ICETCCOM")."""
    return not a or not b or a.startswith(b) or b.startswith(a)


def _same_event(a: Conference, b: Conference) -> bool:
    if not _close(a.start_date, b.start_date):
        return False
    ca, cb = (a.country or "").strip().lower(), (b.country or "").strip().lower()
    if ca and cb and ca != cb:
        return False
    ka, kb = acronym_key(a.acronym), acronym_key(b.acronym)
    if ka and kb and ka == kb:
        return True
    if not _keys_compatible(ka, kb):
        return False  # two different acronyms: often co-located events at one venue
    return fuzz.token_sort_ratio(core_name(a.name), core_name(b.name)) >= DEDUPE_NAME_THRESHOLD


def _weight(c: Conference) -> tuple:
    """Which of two duplicates to keep: the one with fees, read from its homepage, more sources."""
    from_homepage = sum(
        1 for v in (c.field_sources or {}).values() if v.get("source") == "homepage"
    )
    return (c.fee_min is not None, from_homepage, len(c.listings), -c.id)


def find_duplicates(conferences: list[Conference]) -> list[tuple[Conference, Conference]]:
    """(keep, drop) pairs. Each conference appears in at most one pair per pass."""
    by_start = sorted((c for c in conferences if c.start_date), key=lambda c: c.start_date)
    used: set[int] = set()
    pairs = []
    for i, a in enumerate(by_start):
        if a.id in used:
            continue
        for b in by_start[i + 1 :]:
            if (b.start_date - a.start_date).days > MAX_DATE_GAP_DAYS:
                break
            if b.id in used or not _same_event(a, b):
                continue
            keep, drop = (a, b) if _weight(a) >= _weight(b) else (b, a)
            pairs.append((keep, drop))
            used.update((a.id, b.id))
            break
    return pairs


def combine(db: Session, keep: Conference, drop: Conference) -> None:
    """Fold `drop` into `keep`: its listings, any field `keep` lacks, and its fees when `keep`
    has none that count. Then delete `drop`."""
    sources = dict(keep.field_sources or {})
    other = drop.field_sources or {}
    for field in [*MERGE_FIELDS, "registration_url"]:
        if not getattr(keep, field) and getattr(drop, field):
            setattr(keep, field, getattr(drop, field))
            if field in other:
                sources[field] = other[field]
    keep.field_sources = sources
    for row in list(drop.listings):
        row.conference = keep
    if keep.fee_min is None and drop.fee_min is not None:
        for fee in list(drop.fees):
            fee.conference = keep
        keep.status, keep.review_note = drop.status, drop.review_note
    if drop.enriched_at and (not keep.enriched_at or drop.enriched_at > keep.enriched_at):
        keep.enriched_at = drop.enriched_at
    db.flush()
    db.refresh(drop)
    db.delete(drop)  # its remaining fees (keep had its own) go with it
    db.flush()
    db.refresh(keep)
    refresh_fee_summary(keep)


def run_dedupe(db: Session, *, dry_run: bool = False) -> dict:
    conferences = list(db.scalars(select(Conference)))
    pairs = find_duplicates(conferences)
    stats = {"pairs": len(pairs), "combined": [f"{d.id} -> {k.id}" for k, d in pairs]}
    if not dry_run:
        for keep, drop in pairs:
            combine(db, keep, drop)
        db.commit()
    return stats
