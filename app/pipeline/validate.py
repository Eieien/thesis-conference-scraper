"""Fee validation: is each price really on the page, and is it a plausible registration fee?

No LLM involved. Three checks per fee:
1. grounding   the amount appears in the text the fee was read from, in any common spelling
               ("1,250", "1.250,00", "1 250"); the surrounding snippet is kept as evidence.
2. plausibility converted to US dollars with rough built-in rates, a registration fee sits
               between MIN_USD and MAX_USD; "USD 2" (a "covers 2 papers" column) or a year read
               as a price fails.
3. duplicates  the same category, tier, amount and currency twice keeps one.

Only fees marked "verified" count towards a conference's fee range.

`fee_mentions` is the other direction: for a conference where no fee was extracted, it finds
prices written next to fee words in the page text, so a missed fee table gets flagged.
"""

import re
from dataclasses import dataclass

VERIFIED = "verified"
NOT_FOUND = "not_found"  # the amount is nowhere in the page text: likely invented
IMPLAUSIBLE = "implausible"  # outside the range of real registration fees
DUPLICATE = "duplicate"

MIN_USD = 3  # real student-listener fees (PKR 1,000) sit near US$4
MAX_USD = 6000

# Approximate US-dollar value of one unit, only for the plausibility range. Not for display.
USD_PER_UNIT = {
    "USD": 1, "EUR": 1.1, "GBP": 1.27, "CHF": 1.12, "CAD": 0.73, "AUD": 0.66, "NZD": 0.6,
    "JPY": 0.0068, "CNY": 0.14, "HKD": 0.13, "TWD": 0.031, "KRW": 0.00075, "SGD": 0.74,
    "MYR": 0.22, "IDR": 0.000063, "THB": 0.028, "VND": 0.00004, "PHP": 0.018, "INR": 0.012,
    "PKR": 0.0036, "BDT": 0.0085, "LKR": 0.0033, "NPR": 0.0075, "AED": 0.27, "SAR": 0.27,
    "QAR": 0.27, "OMR": 2.6, "BHD": 2.65, "KWD": 3.25, "JOD": 1.41, "ILS": 0.27, "TRY": 0.03,
    "KZT": 0.002, "UZS": 0.00008, "IRR": 0.000024, "IQD": 0.00076, "EGP": 0.02, "MAD": 0.1,
    "DZD": 0.0074, "TND": 0.32, "NGN": 0.00065, "ZAR": 0.055, "KES": 0.0078, "RUB": 0.011,
    "UAH": 0.024, "PLN": 0.25, "CZK": 0.044, "HUF": 0.0028, "RON": 0.22, "BGN": 0.56,
    "SEK": 0.095, "NOK": 0.094, "DKK": 0.15, "ISK": 0.0073, "RSD": 0.0094, "BRL": 0.18,
    "MXN": 0.055, "ARS": 0.001, "CLP": 0.0011, "COP": 0.00025, "PEN": 0.27, "UYU": 0.025,
}  # fmt: skip

_CURRENCY_MARKS = {
    "USD": ["$", "usd", "us$", "dollar"],
    "EUR": ["€", "eur", "euro"],
    "GBP": ["£", "gbp", "pound"],
    "JPY": ["¥", "jpy", "yen", "円"],
    "CNY": ["¥", "cny", "rmb", "yuan", "元"],
    "INR": ["₹", "inr", "rs", "rupee"],
    "IDR": ["rp", "idr", "rupiah"],
    "MYR": ["rm", "myr", "ringgit"],
    "KRW": ["₩", "krw", "won"],
    "PHP": ["₱", "php", "peso"],
    "THB": ["฿", "thb", "baht"],
    "VND": ["₫", "vnd", "dong"],
    "BDT": ["৳", "bdt", "tk", "taka"],
    "TWD": ["nt$", "twd"],
    "PKR": ["pkr", "rs"],
    "SGD": ["s$", "sgd"],
    "HKD": ["hk$", "hkd"],
    "LKR": ["lkr", "rs"],
    "NPR": ["npr", "rs"],
}


@dataclass
class FeeCheck:
    status: str
    note: str | None = None
    evidence: str | None = None


def _amount_pattern(amount: float) -> re.Pattern[str]:
    """Every usual way of writing `amount`: 1250, 1,250, 1.250, 1 250, optional ,00/.00 cents."""
    whole = int(round(amount)) if abs(amount - round(amount)) < 1e-9 else None
    if whole is None:
        # Real cents: 99.5 -> 99.50 / 99,50
        units, cents = f"{amount:.2f}".split(".")
        groups = _grouped(int(units))
        return re.compile(rf"(?<![\d.,]){groups}[.,]{cents}(?!\d)")
    groups = _grouped(whole)
    # Not part of a longer number, even one split over a line break ("202" then "6").
    return re.compile(rf"(?<![\d.,])(?<!\d\n){groups}(?:[.,]0{{1,2}}|,-)?(?![\d]|[.,]\d|\n\d)")


def _grouped(n: int) -> str:
    """Regex for n with any thousands separator (or none): 1250 -> 1[,.  ']?250."""
    digits = str(n)
    if len(digits) <= 3:
        return digits
    head = len(digits) % 3 or 3
    parts = [digits[:head]] + [digits[i : i + 3] for i in range(head, len(digits), 3)]
    return r"[,.   ']?".join(parts)


def _looks_like_year(amount: float, window: str) -> bool:
    if not (1990 <= amount <= 2040 and float(amount).is_integer()):
        return False
    months = r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{1,2}[/.-]"
    return re.search(months, window, re.IGNORECASE) is not None


def _snippet(text: str, start: int, end: int, radius: int = 70) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return " ".join(text[left:right].split())


def _currency_near(text: str, start: int, end: int, currency: str | None) -> bool:
    if not currency:
        return True
    window = text[max(0, start - 30) : end + 30].lower()
    marks = _CURRENCY_MARKS.get(currency, [currency.lower()])
    return any(m in window for m in marks)


def check_fee(amount: float, currency: str | None, text: str) -> FeeCheck:
    """Grounding and plausibility for one amount against the page text it came from."""
    rate = USD_PER_UNIT.get((currency or "").upper())
    if amount <= 0:
        return FeeCheck(IMPLAUSIBLE, "amount is zero or negative")
    if not currency:
        # Without a currency the range check can't run, and a bare number is most often not a
        # price at all (one was read from a year split over two lines).
        return FeeCheck(IMPLAUSIBLE, "no currency given, so this may not be a price")
    if rate is not None:
        usd = amount * rate
        if usd < MIN_USD:
            return FeeCheck(IMPLAUSIBLE, f"about US${usd:,.2f}, too low for a registration fee")
        if usd > MAX_USD:
            return FeeCheck(IMPLAUSIBLE, f"about US${usd:,.0f}, too high for a registration fee")

    matches = list(_amount_pattern(amount).finditer(text))
    if not matches:
        return FeeCheck(NOT_FOUND, "this amount does not appear on the pages that were read")
    # Prefer an occurrence with the currency written next to it.
    best = next((m for m in matches if _currency_near(text, m.start(), m.end(), currency)), None)
    chosen = best or matches[0]
    evidence = _snippet(text, chosen.start(), chosen.end())
    if all(_looks_like_year(amount, text[max(0, m.start() - 12) : m.end() + 12]) for m in matches):
        return FeeCheck(IMPLAUSIBLE, "this number only appears as part of a date", evidence)
    note = None if best else "amount found, but not next to its currency"
    return FeeCheck(VERIFIED, note, evidence)


def check_fees(fees: list[dict], text: str) -> list[FeeCheck]:
    """Check a list of fees ({category, tier, amount, currency}) read from `text`."""
    text = text.replace(" ", " ")
    seen: set[tuple] = set()
    out: list[FeeCheck] = []
    for f in fees:
        key = (
            (f.get("category") or "").strip().casefold(),
            f.get("tier"),
            round(float(f["amount"]), 2),
            f.get("currency"),
        )
        if key in seen:
            out.append(FeeCheck(DUPLICATE, "same category, tier and amount listed twice"))
            continue
        seen.add(key)
        out.append(check_fee(float(f["amount"]), f.get("currency"), text))
    return out


def summary_note(checks: list[FeeCheck]) -> str | None:
    """One line for the conference when some fees failed, e.g. '2 of 9 fees not on the page'."""
    counted = [c for c in checks if c.status != DUPLICATE]
    missing = sum(c.status == NOT_FOUND for c in counted)
    odd = sum(c.status == IMPLAUSIBLE for c in counted)
    parts = []
    if missing:
        parts.append(f"{missing} of {len(counted)} fees not found on the page")
    if odd:
        parts.append(f"{odd} of {len(counted)} fees implausible")
    return "; ".join(parts) or None


# ---------- fees the extraction missed ----------

# A price: a currency mark then a number of two or more digits, or a number then a code.
_MONEY = re.compile(
    r"(?:US\$|NT\$|S\$|HK\$|\$|€|£|¥|₹|৳|₱|₩|฿|\b(?:USD|EUR|GBP|JPY|CNY|RMB|INR|IDR|MYR|RM|Rp|"
    r"KRW|PHP|THB|VND|SGD|TWD|BDT|PKR|LKR|NPR|AED|SAR|QAR|KWD|BHD|OMR|JOD|TRY|KZT|Rs)\b\.?)"
    r"[ \t]?\d[\d,. ]*\d"
    r"|\b\d[\d,. ]*\d[ \t]?(?:(?:USD|EUR|GBP|JPY|CNY|RMB|INR|IDR|MYR|KRW|PHP|THB|VND|SGD|TWD|"
    r"BDT|PKR|AED|SAR|QAR|KWD|BHD|OMR|JOD|TRY|KZT)\b|元|円)",
    re.I,
)
_FEE_CONTEXT = re.compile(
    r"fee|registration|register|early[- ]?bird|regular|late|on-?site|student|member|author|"
    r"attendee|delegate|participant|listener|presenter|virtual|price|payment",
    re.I,
)
_NOT_A_FEE = re.compile(
    r"hotel|room|night|accommodation|award|prize|grant|exchange rate|1 usd|usd 1|= ?twd|"
    r"per page|extra page|additional page|/ ?page|over length|apc|article processing|"
    r"funding|million|billion|semester|tuition|ph\.?d|salary|scholarship|deposit|casino|slot",
    re.I,
)


def fee_mentions(text: str, limit: int = 3) -> list[str]:
    """Snippets where the text shows a price next to fee words ("Student | $ 660",
    "Registration fee: RM 1,200"). Empty when the page has no such prices. No LLM involved."""
    found: list[str] = []
    text = text.replace(" ", " ")
    for m in _MONEY.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line = text[line_start : line_end if line_end != -1 else len(text)]
        around = text[max(0, m.start() - 160) : m.end() + 60]
        if _NOT_A_FEE.search(line):
            continue
        if not (_FEE_CONTEXT.search(line) or _FEE_CONTEXT.search(around)):
            continue
        snippet = _snippet(text, m.start(), m.end())
        if not any(snippet in f or f in snippet for f in found):
            found.append(snippet)
        if len(found) >= limit:
            break
    return found
