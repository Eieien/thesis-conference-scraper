"""Date-window and CS/tech topic filtering."""

import re
from collections import Counter
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import RawListing

_POSITIVE = [
    r"comput\w*",
    r"software",
    r"artificial intelligence",
    r"\bai\b",
    r"machine learning",
    r"deep learning",
    r"neural",
    r"\bdata\b",
    r"big data",
    r"data science",
    r"network\w*",
    r"wireless",
    r"telecommunication\w*",
    r"communication\w* (?:technology|systems|engineering)",
    r"cyber\w*",
    r"security",
    r"informatic\w*",
    r"information (?:technology|systems|science)",
    r"\bict\b",
    r"internet",
    r"\biot\b",
    r"cloud",
    r"edge computing",
    r"robot\w*",
    r"automation",
    r"signal processing",
    r"embedded",
    r"\bvlsi\b",
    r"electronic\w*",
    r"semiconductor",
    r"digital",
    r"algorithm\w*",
    r"computer vision",
    r"image processing",
    r"pattern recognition",
    r"natural language",
    r"\bnlp\b",
    r"blockchain",
    r"quantum",
    r"high performance",
    r"parallel",
    r"distributed",
    r"database\w*",
    r"\bweb\b",
    r"multimedia",
    r"graphics",
    r"human[- ]computer",
    r"\bhci\b",
    r"antenna\w*",
    r"microwave",
    r"intelligent",
    r"smart",
    r"autonomous",
    r"\bsoc\b",
    r"multicore",
    r"neuromorphic",
    r"technology",
    r"engineering",
]
_NEGATIVE = [
    r"medic\w*",
    r"nursing",
    r"clinical",
    r"surgery",
    r"\blaw\b",
    r"legal",
    r"economic\w*",
    r"finance",
    r"accounting",
    r"marketing",
    r"management",
    r"tourism",
    r"agricultur\w*",
    r"linguistic\w*",
    r"literature",
    r"theolog\w*",
    r"psycholog\w*",
    r"sociolog\w*",
    r"education(?!al technology)",
    r"humanities",
    r"veterinar\w*",
    r"pharma\w*",
]
_POS_RE = [re.compile(p, re.I) for p in _POSITIVE]
_NEG_RE = [re.compile(p, re.I) for p in _NEGATIVE]


def in_window(start: date | None, settings: Settings) -> bool:
    return start is not None and settings.window_start <= start <= settings.window_end


def keyword_topic(text: str) -> tuple[bool | None, str]:
    """(verdict, reason). None means ambiguous: ask the LLM or review by hand."""
    pos = sorted({p.pattern for p in _POS_RE if p.search(text)})
    neg = sorted({p.pattern for p in _NEG_RE if p.search(text)})
    if pos and len(pos) > len(neg):
        return True, f"keywords: {', '.join(pos[:4])}"
    if neg and not pos:
        return False, f"non-CS keywords: {', '.join(neg[:4])}"
    return None, f"ambiguous (pos={len(pos)}, neg={len(neg)})"


def listing_topic_text(row: RawListing) -> str:
    return " ".join(
        filter(
            None,
            [row.acronym, row.name, " ".join(row.categories or []), (row.description or "")[:1500]],
        )
    )


async def run_filter(db: Session, settings: Settings, *, use_llm: bool) -> dict:
    extractor = None
    if use_llm:
        from app.extract import get_extractor

        extractor = get_extractor(settings)

    stats: Counter = Counter()
    for row in db.scalars(select(RawListing)):
        row.in_window = in_window(row.start_date, settings)
        stats["in_window" if row.in_window else "out_of_window"] += 1
        if not row.in_window:
            continue
        verdict, reason = keyword_topic(listing_topic_text(row))
        if verdict is None and extractor is not None:
            result = await extractor.classify_topic(listing_topic_text(row))
            verdict, reason = result.is_cs, f"llm: {result.reason}"[:255]
            stats["llm_calls"] += 1
        row.is_cs, row.topic_reason = verdict, reason
        stats[{True: "cs", False: "not_cs", None: "ambiguous"}[verdict]] += 1
    db.commit()
    return dict(stats)
