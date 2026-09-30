"""Can people attend or present online? From evidence already collected (no LLM).

The answer, `mode`:
- "online option": hybrid, or online/virtual presentation or participation is offered
- "online only": the conference is fully virtual
- "in person only": the page says presentation or attendance must be in person
- None: nothing says either way

Evidence, strongest first:
1. an explicit statement on the conference's pages ("in-person presentation is mandatory",
   "no online presentation"): it wins over everything else;
2. a virtual attendance fee ("Online-Only - Student Non-Member", app/pipeline/fee_kinds.py):
   an online rate means online participation exists;
3. IEEE's own event format for the conference (hybrid / virtual / inperson);
4. wording on the pages ("online presentation", "virtual participation", "hybrid mode").
Each piece of evidence is kept, so the visualizer can show why.
"""

import logging
import re
import traceback
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import Conference, ScrapeRun
from app.pipeline.fee_kinds import classify
from app.pipeline.geo import continent_of

log = logging.getLogger(__name__)

ONLINE_OPTION = "online option"
ONLINE_ONLY = "online only"
IN_PERSON = "in person only"

_ONLINE = r"(?:online|virtual|remote|zoom)"
_POSITIVE = re.compile(
    rf"{_ONLINE}[- ](?:oral )?(?:presentations?|participation|participants?|attendance|attendees?|"
    # not "registration": paying online says nothing about attending online
    rf"mode|option|sessions?|delegates?|authors?|track)"
    rf"|(?:present|presented|presenting|attend|participate|join)\w*\s+(?:online|virtually|remotely)"
    rf"|hybrid[- ](?:mode|format|conference|event|model|symposium)|(?:in|as) a hybrid"
    rf"|both (?:online|virtual) and (?:on[- ]?site|in[- ]person|physical)"
    r"|(?:on[- ]?site|in[- ]person|physical)\W{0,3}(?:and|or|/|&|\+)\s*"
    r"(?:via\s+)?(?:online|virtual)",
    re.I,
)
# About the event itself, in the future or present tense: "fully online" alone also describes
# a side hackathon, and "was held ... online" a past edition (both found on the first run).
_EVENT = r"(?:conference|symposium|workshop|event|congress|summit|forum)"
_FULLY_ONLINE = re.compile(
    rf"fully (?:virtual|online) {_EVENT}|(?:virtual|online)[- ]only {_EVENT}"
    rf"|{_EVENT}(?: \w+){{0,3}} (?:will be|is) held (?:entirely |fully )?(?:online|virtually)\b"
    r"(?! and| or|/| &| \+)",
    re.I,
)
# "presented in person/Via Online", "in person or online": both are allowed.
_OR_ONLINE = re.compile(r"^\W{0,3}(?:/|or|and|&|\+)\s*(?:via\s+)?(?:online|virtual)", re.I)
_NEGATIVE = re.compile(
    rf"no {_ONLINE} (?:presentations?|options?|participation|attendance)"
    rf"|{_ONLINE} presentations? (?:is|are|will) not (?:be )?"
    r"(?:allowed|accepted|possible|available|permitted)"
    r"|(?:in[- ]person|on[- ]?site|physical) (?:presentation|attendance|participation) "
    r"(?:is |are )?(?:mandatory|compulsory|required)"
    r"|(?:must|should) be presented (?:in[- ]person|on[- ]?site|physically)"
    r"|(?:in[- ]person|on[- ]?site)[- ]only|fully (?:in[- ]person|on[- ]?site|physical)",
    re.I,
)


def _snippet(text: str, m: re.Match, radius: int = 80) -> str:
    return " ".join(text[max(0, m.start() - radius) : m.end() + radius].split())


def decide(
    text: str | None, fees: list, ieee_formats: list[str], url: str | None = None
) -> dict | None:
    """The attendance record for one conference, or None when nothing says either way."""
    evidence: list[dict] = []
    text = text or ""

    neg = next(
        (m for m in _NEGATIVE.finditer(text) if not _OR_ONLINE.match(text[m.end() : m.end() + 25])),
        None,
    )
    if neg:
        evidence.append({"source": "page", "says": "in person", "text": _snippet(text, neg)})
        return {"mode": IN_PERSON, "evidence": evidence, "url": url}

    virtual_fees = [f for f in fees if classify(f.category, f.tier, f.notes).mode == "virtual"]
    if virtual_fees:
        f = virtual_fees[0]
        evidence.append(
            {"source": "fee", "says": "online", "text": f"{f.category}: {f.amount:g} {f.currency}"}
        )
    for fmt in ieee_formats:
        evidence.append({"source": "ieee", "says": fmt, "text": f"IEEE lists the format as {fmt}"})
    if full := _FULLY_ONLINE.search(text):
        evidence.append({"source": "page", "says": "online only", "text": _snippet(text, full)})
    if pos := _POSITIVE.search(text):
        evidence.append({"source": "page", "says": "online", "text": _snippet(text, pos)})

    says = {e["says"] for e in evidence}
    if "online only" in says or ("virtual" in says and not virtual_fees and not pos):
        mode = ONLINE_ONLY
    elif virtual_fees or pos or "hybrid" in says or "virtual" in says:
        mode = ONLINE_OPTION
    elif "inperson" in says:
        mode = IN_PERSON  # IEEE's metadata only: weaker than a page statement
    else:
        return None
    return {"mode": mode, "evidence": evidence[:4], "url": url}


def ieee_formats(conf: Conference) -> list[str]:
    return sorted(
        {
            str((row.extra or {}).get("event_format")).lower()
            for row in conf.listings
            if row.source == "ieee" and (row.extra or {}).get("event_format")
        }
    )


def record(conf: Conference, text: str | None) -> dict | None:
    rec = decide(text, list(conf.fees), ieee_formats(conf), conf.homepage)
    if rec is not None:
        rec["checked_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return rec


async def run_attendance(
    settings: Settings, run_id: int, *, continent: str | None = "Asia"
) -> None:
    """Decide attendance for every conference in `continent` from cached pages (no LLM)."""
    from app.pipeline.enrich import _gather_text, _job_for
    from app.scrapers.http import Fetcher

    stats: Counter = Counter()
    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        try:
            confs = [
                c
                for c in db.scalars(select(Conference))
                if continent is None or (continent_of(c.country) or "Unknown") == continent
            ]
            stats["to_check"] = len(confs)
            async with Fetcher(settings) as fetcher:
                for conf in confs:
                    try:
                        text = await _gather_text(_job_for(conf), fetcher, settings)
                    except Exception as e:  # one unreadable site must not stop the run
                        log.info("attendance: %s pages unavailable: %r", conf.id, e)
                        text = None
                    conf.attendance = record(conf, text)
                    stats[(conf.attendance or {}).get("mode") or "unknown"] += 1
                    run.stats = dict(stats)
                    db.commit()
            run.status = "succeeded"
        except Exception as e:
            log.exception("attendance run failed")
            db.rollback()
            run.status = "failed"
            run.error = f"{e!r}\n{traceback.format_exc()[-2000:]}"
        run.stats = dict(stats)
        run.finished_at = datetime.now(UTC)
        db.commit()
