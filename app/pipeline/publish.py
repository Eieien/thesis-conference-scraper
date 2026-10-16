"""The public site's data: the conferences people may see, with every derived value worked out.

The website (web/, on Vercel) reads Supabase directly and runs no Python, so the values the API
derives on read (headline fees, USD/PHP, continent, history label, organizer flag) are computed
here with the same code (`ConferenceOut`) and stored as plain columns. Only a whitelist of
fields leaves this machine: no scraped page text, no fee evidence snippets, no review internals,
and the EDAS account's details are scrubbed from everything (app/pipeline/privacy.py).
"""

from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.models import Conference, RawListing
from app.pipeline.enrich import MISSED_FEES_NOTE
from app.pipeline.fx import rates_info
from app.pipeline.privacy import AccountScrubber, scrub_deep
from app.schemas import ConferenceOut, FeeOut

COUNTED = (None, "verified")  # fees that count towards the range; the others are not shown

SOURCE_NAMES = {
    "wikicfp": "WikiCFP",
    "easychair": "EasyChair",
    "ieee": "IEEE Conferences",
    "ieeecs": "IEEE Computer Society",
    "conferenceindex": "ConferenceIndex",
    "edas": "EDAS",
    "homepage": "Conference website",
}


def fee_status(status: str, note: str | None) -> str:
    """The plain-language label for where a conference's fees stand (docs/API.md's table)."""
    note = note or ""
    if status == "enriched":
        return "Fees found"
    if status == "collected":
        return "Not read yet"
    if note.startswith(("fees not published yet", "fees not found")):
        return "No fees posted yet"
    if note.startswith(MISSED_FEES_NOTE):
        return "Fees on the site, not read yet"
    if note.startswith("fees found, but none could be verified"):
        return "Fees unverified"
    if note.startswith("no homepage"):
        return "Website unreadable"
    if "don't match listing" in note:
        return "Check the edition"
    return "Not read yet"


def _without_person(url: str | None) -> str | None:
    """EDAS links carry the account's person ID as `p=`; keep the rest of the link."""
    if not url or "edas.info" not in url:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "p"]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _history(history: dict | None) -> dict | None:
    if not history:
        return None
    return {
        "has_history": bool(history.get("has_history")),
        "editions": [
            {k: e.get(k) for k in ("year", "name", "publisher", "doi")}
            for e in history.get("editions") or []
        ],
        "publishers": history.get("publishers") or [],
        "note": history.get("note"),
    }


def _attendance(attendance: dict | None) -> dict | None:
    if not attendance:
        return None
    return {
        "mode": attendance.get("mode"),
        "evidence": [
            {k: e.get(k) for k in ("source", "says", "text")}
            for e in attendance.get("evidence") or []
        ],
    }


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_summaries(conferences: list[dict], start: date, end: date) -> list[dict]:
    """One entry per week of the window, for the site's terrain: how many conferences start that
    week and the headline fee (US$) of each that has one. Small enough to ship with the metadata,
    so the terrain draws without downloading every conference."""
    weeks: dict[date, dict] = {}
    d = monday(start)
    while d <= end:
        weeks[d] = {"start": d.isoformat(), "total": 0, "fees_usd": [], "fees_php": []}
        d += timedelta(days=7)
    for c in conferences:
        if c["organizer_flag"] or not c["start_date"]:
            continue
        w = weeks.get(monday(c["start_date"]))
        if w is None:
            continue
        w["total"] += 1
        fee = c["standard_fee"]
        usd = fee["amount_usd"] if fee else c["fee_min_usd"]
        peso = fee["amount_php"] if fee else c["fee_min_php"]
        if usd is not None:
            w["fees_usd"].append(round(usd))
        if peso is not None:
            w["fees_php"].append(round(peso))
    return list(weeks.values())


def public_data(db: Session, settings: Settings, continents: list[str]) -> dict:
    """{"conferences": [...], "fees": [...], "meta": {...}} for the conferences on `continents`."""
    scrub = AccountScrubber(
        text
        for extra in db.scalars(select(RawListing.extra).where(RawListing.source == "edas"))
        for text in (extra or {}).values()
        if isinstance(text, str)
    )
    rows = db.scalars(
        select(Conference)
        .options(selectinload(Conference.listings), selectinload(Conference.fees))
        .order_by(Conference.start_date, Conference.id)
    ).all()

    conferences, fees = [], []
    for conf in rows:
        out = ConferenceOut.model_validate(conf)
        if (out.continent or "Unknown") not in continents:
            continue
        # A homepage can move a conference's start out of the window (RADECS: listed 1 Oct, its
        # site says 28 Sep). The site lists only what starts inside it.
        if conf.start_date and not (
            settings.window_start <= conf.start_date <= settings.window_end
        ):
            continue
        links = []
        for row in conf.listings:
            if row.source == "edas":
                links.append({"source": SOURCE_NAMES["edas"], "url": None})  # behind a login
            elif row.url:
                links.append({"source": SOURCE_NAMES.get(row.source, row.source), "url": row.url})
        standard, student = out.standard_fee, out.student_fee
        conferences.append(
            {
                "id": conf.id,
                "acronym": conf.acronym,
                "name": conf.name,
                "start_date": conf.start_date,
                "end_date": conf.end_date,
                "city": conf.city,
                "country": conf.country,
                "continent": out.continent or "Unknown",
                "description": conf.description,
                "abstract_deadline": conf.abstract_deadline,
                "paper_deadline": conf.paper_deadline,
                "notification_date": conf.notification_date,
                "camera_ready_deadline": conf.camera_ready_deadline,
                "registration_deadline": conf.registration_deadline,
                "homepage": conf.homepage,
                "registration_url": _without_person(conf.registration_url),
                "fee_min": conf.fee_min,
                "fee_max": conf.fee_max,
                "currency": conf.currency,
                "fee_min_usd": out.fee_min_usd,
                "fee_max_usd": out.fee_max_usd,
                "fee_min_php": out.fee_min_php,
                "fee_max_php": out.fee_max_php,
                "standard_fee": standard.model_dump() if standard else None,
                "student_fee": student.model_dump() if student else None,
                "standard_fee_usd": standard.amount_usd if standard else None,
                "fee_status": fee_status(conf.status, conf.review_note),
                "online": out.online,
                "attendance": _attendance(conf.attendance),
                "history_label": out.history_label,
                "history_years": out.history_years,
                "history": _history(conf.history),
                "organizer_flag": out.organizer_flag,
                "sources": sorted({SOURCE_NAMES.get(s, s) for s in out.sources}),
                "source_links": links,
            }
        )
        for fee in conf.fees:
            if fee.check_status not in COUNTED:
                continue
            f = FeeOut.model_validate(fee)
            fees.append(
                {
                    "id": fee.id,
                    "conference_id": conf.id,
                    "category": fee.category,
                    "tier": fee.tier,
                    "amount": fee.amount,
                    "currency": fee.currency,
                    "amount_usd": f.amount_usd,
                    "amount_php": f.amount_php,
                    "notes": fee.notes,
                    "source": SOURCE_NAMES.get(fee.source, fee.source),
                    "source_url": _without_person(fee.source_url),
                    "kind": f.kind,
                }
            )

    rates = rates_info(settings) or {}
    meta = {
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "window_start": settings.window_start.isoformat(),
        "window_end": settings.window_end.isoformat(),
        "continents": continents,
        # Counts leave out flagged organizers, which the site hides by default.
        "conferences": sum(1 for c in conferences if not c["organizer_flag"]),
        "conferences_with_fees": sum(
            1 for c in conferences if c["fee_min"] is not None and not c["organizer_flag"]
        ),
        "conferences_flagged": sum(1 for c in conferences if c["organizer_flag"]),
        "exchange_rates": rates,
        "weeks": week_summaries(conferences, settings.window_start, settings.window_end),
    }
    data = {"conferences": conferences, "fees": fees, "meta": meta}
    data = scrub_deep(data, scrub)
    return data
