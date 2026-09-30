"""Heuristic fee-table parsing (no LLM). Used first; Gemini handles whatever this misses."""

import re
from collections import Counter

from bs4 import BeautifulSoup

from app.pipeline.normalize import normalize_currency, parse_money
from app.schemas import FeeItem

_TIER_WORDS = {
    "early": "early",
    "advance": "early",
    "regular": "regular",
    "standard": "regular",
    "normal": "regular",
    "late": "late",
    "on-site": "onsite",
    "onsite": "onsite",
    "on site": "onsite",
}
_CURRENCY_HINT = re.compile(r"(US\$|\$|€|£|¥|₹|₱|₩|\bRp\b|\bRM\b|\b[A-Z]{3}\b)")
# Header words that mark a price column, and ones that mark a number that is not a fee
# (EDAS tables carry "Cancellation fee", "Covers how many papers?" and "Covers extra pages?").
_MONEY_HEADER = re.compile(
    r"amount|fee|price|cost|rate|registration|early|regular|late|on-?site|member|student|"
    r"\b(usd|eur|gbp|jpy|cny|inr|idr|myr|sgd|krw|aud|cad|php)\b|[$€£¥₹₱₩]",
    re.IGNORECASE,
)
_NOT_FEE_HEADER = re.compile(
    r"cancel|refund|covers|how many|pages?\b|papers?\b|description|date|deadline|available|"
    r"option|^register$|^no\.?$|^#$|quantity|qty",
    re.IGNORECASE,
)
_NOTES_HEADER = re.compile(r"description|details|remarks?|notes?", re.IGNORECASE)


def _tier(text: str) -> str | None:
    low = text.lower()
    for word, tier in _TIER_WORDS.items():
        if word in low:
            return tier
    return None


def _currency_in(text: str) -> str | None:
    for m in _CURRENCY_HINT.finditer(text):
        if code := normalize_currency(m.group(1)):
            return code
    return None


def fees_from_tables(html: str) -> list[FeeItem]:
    soup = BeautifulSoup(html, "lxml")
    fees: list[FeeItem] = []
    for table in soup.find_all("table"):
        if table.find("table"):  # layout wrapper; inner tables are visited on their own
            continue
        rows = [
            [" ".join(c.get_text(" ", strip=True).split()) for c in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")
        ]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2:
            continue
        header = rows[0]
        money_cols = _money_columns(header)
        notes_col = next((i for i, h in enumerate(header) if _NOTES_HEADER.search(h)), None)
        table_currency = _currency_in(" ".join(header)) or _currency_in(
            table.get_text(" ", strip=True)[:300]
        )
        for row in rows[1:]:
            label = row[0]
            if not label or parse_money(label)[0] is not None and _currency_in(label):
                continue
            for col, cell in enumerate(row[1:], start=1):
                if money_cols is not None and col not in money_cols:
                    continue
                if not re.search(r"\d", cell):
                    continue
                # A sentence that happens to contain a year is not a price.
                if len(cell) > 30 and not _currency_in(cell):
                    continue
                amount, currency = parse_money(cell)
                col_label = header[col] if col < len(header) else ""
                currency = currency or _currency_in(col_label) or table_currency
                if amount is None or currency is None or amount <= 0:
                    continue
                fees.append(
                    FeeItem(
                        category=label[:255],
                        tier=_tier(col_label) or _tier(label),
                        amount=amount,
                        currency=currency,
                        notes=_notes(row, notes_col) or col_label or None,
                    )
                )
    return fees


def _money_columns(header: list[str]) -> set[int] | None:
    """Columns whose header names a price. None when the header says nothing either way."""
    if not any(_MONEY_HEADER.search(h) or _NOT_FEE_HEADER.search(h) for h in header[1:]):
        return None
    return {
        i
        for i, h in enumerate(header)
        if i > 0 and _MONEY_HEADER.search(h) and not _NOT_FEE_HEADER.search(h)
    }


def _notes(row: list[str], col: int | None) -> str | None:
    if col is None or col >= len(row) or col == 0:
        return None
    return row[col][:255] or None


def summarize_fees(
    amounts: list[tuple[float, str | None]],
) -> tuple[float | None, float | None, str | None]:
    """(min, max, currency) using the most common currency so mixed currencies aren't compared."""
    if not amounts:
        return None, None, None
    currency = Counter(c for _, c in amounts).most_common(1)[0][0]
    values = [a for a, c in amounts if c == currency]
    return min(values), max(values), currency
