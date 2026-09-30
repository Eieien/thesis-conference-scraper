"""Publication history: did earlier editions of this conference publish proceedings?

A series with past proceedings on record (IEEE Xplore, ACM, Springer LNCS, ...) has a history.
This is one signal among several, so the result is only a label: "has history" or "no history
found". It says nothing about being predatory: a first edition has no history, and some mills
publish too.

Source: Crossref (api.crossref.org), the DOI registry, free and built for machine access.
DBLP would be the natural source for computer science, but its robots.txt disallows all crawlers
and it answers with a bot challenge, so it is not used.

One query per conference: its name and acronym, limited to works published before this year.
An item counts as an earlier edition when one of its names (the event name, a container title,
or the title of a proceedings volume) matches the conference:
- same acronym (from the parentheses, "... (TSSA)") and a similar name, or
- nearly the same name, when either has no acronym.
Both are needed for acronyms because different series share one: "ICCSSE" is both "Computer
Science and Software Engineering" and "Control Science and Systems Engineering".

Common names are reused, so a match only counts when it fits (all found on the first run):
- the words must agree, not just the letters ("Bioethics" is not "Biometrics", both "ICB");
- edition numbers must fit: a "1st" edition has no history, and a past "4th" can't come before
  a current "2nd";
- the history must be recent: the last edition within RECENT_YEARS. Older namesakes (a 2008-2013
  series with the 2026 event's name) are kept as evidence but don't count.
Conferences run by a flagged organizer (app/pipeline/quality.py, WASET) are not checked: their
events copy the names of real series ("Machine Learning and Cybernetics"), so any match would be
the real series' history, not theirs.
"""

import json
import logging
import re
import traceback
from collections import Counter
from datetime import UTC, date, datetime
from urllib.parse import urlencode

from rapidfuzz import fuzz
from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import Conference, ScrapeRun
from app.pipeline.geo import continent_of
from app.pipeline.merge import acronym_key, core_name
from app.pipeline.quality import organizer_flag
from app.scrapers.http import Fetcher, FetchError

log = logging.getLogger(__name__)

API = "https://api.crossref.org/works"
FIELDS = "DOI,container-title,published,publisher,event,type,title"
ACRONYM_NAME_MIN = 80  # name similarity needed when the acronyms agree
ACRONYM_WORDS_MIN = 0.6  # and the share of words the two names have in common
NAME_ONLY_MIN = 97  # without an acronym to agree on, the name must be nearly the same
RECENT_YEARS = 4  # the last earlier edition must be this recent to count as a history
MAX_EDITIONS = 12

_WORD_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10,
}  # fmt: skip
_ORDINAL = re.compile(r"\b(\d{1,3})(?:st|nd|rd|th)\b|\b(" + "|".join(_WORD_ORDINALS) + r")\b", re.I)

_PAREN_ACRONYM = re.compile(r"\(([A-Za-z][A-Za-z0-9&'\-/ ]{1,20})\)")
# ACM writes it in front: "ICIMH 2022: 2022 The 4th International Conference on ..."
_LEADING_ACRONYM = re.compile(r"^([A-Za-z][A-Za-z0-9&\-]{1,15})\s*'?(?:19|20)?\d{2}\s*:")
# Tags some sources glue to the front of a name: "IEEE--2026 The 8th ...", "ACM--..."
_NAME_TAG = re.compile(r"^\s*(?:IEEE|ACM|EI|SCOPUS)\s*-+\s*", re.I)
_VOLUME_TYPES = {"proceedings", "book", "edited-book"}


def search_name(conf: Conference) -> str:
    """The conference name as a search phrase: no tags, brackets, years or edition numbers."""
    name = _NAME_TAG.sub("", conf.name or "")
    name = re.sub(r"\(.*?\)", " ", name)
    name = re.sub(r"\b(?:19|20)\d{2}\b|\b\d+(?:st|nd|rd|th)\b|\bthe\b", " ", name, flags=re.I)
    return " ".join(name.split())


def query_url(conf: Conference, field: str = "bibliographic") -> str:
    """The Crossref search for earlier editions, published before this year. `bibliographic`
    searches everything (name plus acronym); `container-title` searches only the titles of
    proceedings, the second try when the first finds nothing (it found ACM's ICIMH, which the
    first search buried under journal articles)."""
    year = (conf.start_date or date.today()).year
    name = search_name(conf)
    terms = name if field == "container-title" else f"{name} {acronym_key(conf.acronym) or ''}"
    params = {
        f"query.{field}": " ".join(terms.split()),
        "filter": f"until-pub-date:{year - 1}-12-31",
        "rows": 20,
        "select": FIELDS,
    }
    return f"{API}?{urlencode(params)}"


def _names(item: dict) -> list[str]:
    names = [(item.get("event") or {}).get("name") or ""]
    names += item.get("container-title") or []
    if item.get("type") in _VOLUME_TYPES:
        names += item.get("title") or []
    return [n for n in names if n]


def _year(item: dict) -> int | None:
    parts = (item.get("published") or {}).get("date-parts") or [[None]]
    return parts[0][0] if parts and parts[0] else None


def ordinal(name: str | None) -> int | None:
    """The edition number in a name: "2026 5th ..." -> 5, "Second International ..." -> 2."""
    m = _ORDINAL.search(name or "")
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else _WORD_ORDINALS[m.group(2).lower()]


def _words(name: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", core_name(name)) if len(w) > 2}


def _matches(conf_name: str, conf_key: str | None, candidate: str) -> bool:
    similarity = fuzz.token_sort_ratio(core_name(conf_name), core_name(candidate))
    found = _PAREN_ACRONYM.search(candidate) or _LEADING_ACRONYM.search(candidate)
    other_key = acronym_key(found.group(1)) if found else None
    if conf_key and other_key:
        a, b = _words(conf_name), _words(candidate)
        shared = len(a & b) / max(len(a | b), 1)
        return (
            conf_key == other_key and similarity >= ACRONYM_NAME_MIN and shared >= ACRONYM_WORDS_MIN
        )
    return similarity >= NAME_ONLY_MIN and len(_words(conf_name)) >= 3


def editions(conf: Conference, body: dict) -> list[dict]:
    """Earlier editions found in a Crossref answer: one entry per year, newest first."""
    this_year = (conf.start_date or date.today()).year
    key = acronym_key(conf.acronym)
    edition = ordinal(conf.name)
    if edition == 1:
        return []  # a first edition has no earlier ones, whatever shares its name
    by_year: dict[int, dict] = {}
    for item in (body.get("message") or {}).get("items") or []:
        year = _year(item)
        if not year or year >= this_year:
            continue
        name = next((n for n in _names(item) if _matches(conf.name, key, n)), None)
        earlier = ordinal(name) if name else None
        if name and edition and earlier and earlier >= edition:
            continue  # a past "4th" can't come before this "2nd": another series
        if name and year not in by_year:
            by_year[year] = {
                "year": year,
                "name": name,
                "publisher": item.get("publisher"),
                "doi": item.get("DOI"),
            }
    return [by_year[y] for y in sorted(by_year, reverse=True)][:MAX_EDITIONS]


def history_record(conf: Conference, body: dict, url: str, extra: dict | None = None) -> dict:
    """The stored result. `extra` is a second Crossref answer to search as well."""
    found = editions(conf, body)
    if extra is not None:
        more = {e["year"]: e for e in editions(conf, extra)}
        more.update({e["year"]: e for e in found})
        found = [more[y] for y in sorted(more, reverse=True)][:MAX_EDITIONS]
    this_year = (conf.start_date or date.today()).year
    recent = bool(found) and found[0]["year"] >= this_year - RECENT_YEARS
    note = None
    if found and not recent:
        note = (
            f"only editions up to {found[0]['year']} were found: probably another series with "
            "the same name"
        )
    return {
        "has_history": recent,
        "note": note,
        "editions": found,
        "publishers": sorted({e["publisher"] for e in found if e.get("publisher")}),
        "source": "crossref",
        "query": url,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


async def run_history(
    settings: Settings,
    run_id: int,
    *,
    continent: str | None = "Asia",
    only_unchecked: bool = False,
) -> None:
    stats: Counter = Counter()
    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        try:
            confs = [
                c
                for c in db.scalars(select(Conference).order_by(Conference.start_date))
                if (continent is None or (continent_of(c.country) or "Unknown") == continent)
                and not (only_unchecked and c.history)
            ]
            flagged = [c for c in confs if organizer_flag(c.homepage)]
            for c in flagged:
                c.history = None  # not checked: its name copies a real series (see above)
            stats["skipped: flagged organizer"] = len(flagged)
            confs = [c for c in confs if not organizer_flag(c.homepage)]
            stats["to_check"] = len(confs)
            async with Fetcher(settings) as fetcher:
                for conf in confs:
                    url = query_url(conf)
                    try:
                        body = json.loads((await fetcher.get(url)).text)
                    except (FetchError, ValueError) as e:
                        log.info("history: %s failed: %s", conf.id, e)
                        stats["lookup failed"] += 1
                        continue
                    extra = None
                    if not history_record(conf, body, url)["has_history"]:
                        second = query_url(conf, "container-title")
                        try:
                            extra = json.loads((await fetcher.get(second)).text)
                        except (FetchError, ValueError) as e:
                            log.info("history: %s second query failed: %s", conf.id, e)
                    conf.history = history_record(conf, body, url, extra)
                    stats["has history" if conf.history["has_history"] else "no history found"] += 1
                    run.stats = dict(stats)
                    db.commit()
            run.status = "succeeded"
        except Exception as e:
            log.exception("history run failed")
            db.rollback()
            run.status = "failed"
            run.error = f"{e!r}\n{traceback.format_exc()[-2000:]}"
        run.stats = dict(stats)
        run.finished_at = datetime.now(UTC)
        db.commit()
