"""Parsers for the messy date, location and money strings sources publish."""

import calendar
import re
from datetime import date

import dateparser
from price_parser import Price

_SEP = r"\s*(?:-|–|—|to|until)\s*"
_MONTH = r"([A-Za-z]{3,9})\.?"

# "December 14-20, 2026" / "Oct 21 - 23, 2026"
_RE_M_D_D_Y = re.compile(rf"{_MONTH}\s+(\d{{1,2}}){_SEP}(\d{{1,2}}),?\s+(\d{{4}})")
# "2-3 December 2026"
_RE_D_D_M_Y = re.compile(rf"(\d{{1,2}}){_SEP}(\d{{1,2}})\s+{_MONTH},?\s+(\d{{4}})")
# "Nov 30 - Dec 2, 2026"
_RE_M_D_M_D_Y = re.compile(rf"{_MONTH}\s+(\d{{1,2}}){_SEP}{_MONTH}\s+(\d{{1,2}}),?\s+(\d{{4}})")
# "30 Nov - 2 Dec 2026"
_RE_D_M_D_M_Y = re.compile(rf"(\d{{1,2}})\s+{_MONTH}{_SEP}(\d{{1,2}})\s+{_MONTH},?\s+(\d{{4}})")

_DATEPARSER_SETTINGS = {"REQUIRE_PARTS": ["day", "month", "year"], "PREFER_DAY_OF_MONTH": "first"}


_MONTHS = {abbr.lower(): i for i, abbr in enumerate(calendar.month_abbr) if abbr}


def _month(name: str) -> int | None:
    return _MONTHS.get(name.strip(".").lower()[:3])


def _mk(y: str, m: str, d: str) -> date | None:
    month = _month(m)
    if month is None:
        return None
    try:
        return date(int(y), month, int(d))
    except ValueError:
        return None


def parse_date(text: str | None) -> date | None:
    """Parse one date. Parenthesised text (e.g. an original, pre-extension deadline) is ignored."""
    if not text:
        return None
    cleaned = re.sub(r"\(.*?\)", "", text).strip()
    if not cleaned or cleaned.upper() in {"N/A", "TBA", "TBD"}:
        return None
    try:
        return date.fromisoformat(cleaned[:10])
    except ValueError:
        pass
    parsed = dateparser.parse(cleaned, languages=["en"], settings=_DATEPARSER_SETTINGS)
    return parsed.date() if parsed else None


def parse_date_range(text: str | None) -> tuple[date | None, date | None]:
    """Parse the date formats seen across sources into (start, end)."""
    if not text:
        return None, None
    t = " ".join(text.replace(" ", " ").split())
    if not t or t.upper() in {"N/A", "TBA", "TBD"}:
        return None, None

    if m := _RE_M_D_M_D_Y.search(t):
        m1, d1, m2, d2, y = m.groups()
        start, end = _mk(y, m1, d1), _mk(y, m2, d2)
        if start and end and end < start:  # "Dec 30 - Jan 2, 2027"
            start = _mk(str(int(y) - 1), m1, d1)
        return start, end
    if m := _RE_D_M_D_M_Y.search(t):
        d1, m1, d2, m2, y = m.groups()
        return _mk(y, m1, d1), _mk(y, m2, d2)
    if m := _RE_M_D_D_Y.search(t):
        mon, d1, d2, y = m.groups()
        return _mk(y, mon, d1), _mk(y, mon, d2)
    if m := _RE_D_D_M_Y.search(t):
        d1, d2, mon, y = m.groups()
        return _mk(y, mon, d1), _mk(y, mon, d2)

    parts = re.split(r"\s+(?:-|–|—|to)\s+", t, maxsplit=1)
    if len(parts) == 2:
        start, end = parse_date(parts[0]), parse_date(parts[1])
        if start or end:
            return start, end or start
    single = parse_date(t)
    return single, single


def split_location(text: str | None) -> tuple[str | None, str | None]:
    """'Urayasu, Chiba, 〒 , Japan' -> ('Urayasu', 'Japan'). Country = last part, city = first."""
    if not text:
        return None, None
    parts = [p.strip() for p in text.split(",")]
    parts = [p for p in parts if re.search(r"[A-Za-z]", p)]
    if not parts or parts[0].upper() in {"N/A", "TBA", "TBD"}:
        return None, None
    if len(parts) == 1:
        if parts[0].lower() in {"online", "virtual", "hybrid"}:
            return parts[0].title(), None
        return parts[0], None
    return parts[0], parts[-1]


_SYMBOL_TO_ISO = {
    "US$": "USD",
    "USD": "USD",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₱": "PHP",
    "₩": "KRW",
    "RP": "IDR",
    "RM": "MYR",
    "A$": "AUD",
    "AU$": "AUD",
    "C$": "CAD",
    "CA$": "CAD",
    "S$": "SGD",
    "HK$": "HKD",
    "NT$": "TWD",
    "R$": "BRL",
    "RMB": "CNY",
    "CN¥": "CNY",
    "VND": "VND",
    "₫": "VND",
    "฿": "THB",
}
_ISO_RE = re.compile(r"\b([A-Z]{3})\b")
_KNOWN_ISO = set(
    "USD EUR GBP JPY CNY INR IDR MYR PHP KRW AUD CAD SGD HKD TWD BRL THB VND CHF SEK NOK DKK "
    "PLN CZK TRY AED SAR ZAR MXN NZD EGP PKR BDT LKR NGN KES MAD TND DZD".split()
)


def normalize_currency(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().upper()
    if key in _KNOWN_ISO:
        return key
    return _SYMBOL_TO_ISO.get(key) or _SYMBOL_TO_ISO.get(raw.strip())


def parse_money(text: str | None) -> tuple[float | None, str | None]:
    """'US$ 1,250.00' -> (1250.0, 'USD'); 'Rp 2.500.000' -> (2500000.0, 'IDR')."""
    if not text:
        return None, None
    price = Price.fromstring(text)
    amount = float(price.amount) if price.amount is not None else None
    currency = normalize_currency(price.currency)
    if currency is None:
        for code in _ISO_RE.findall(text.upper()):
            if code in _KNOWN_ISO:
                currency = code
                break
    # Indonesian-style thousand separators ("2.500.000") confuse price-parser.
    if currency == "IDR" and amount is not None and amount < 1000:
        digits = re.sub(r"[^\d]", "", text)
        if digits:
            amount = float(digits)
    return amount, currency
