"""What kind of fee a fee row is, from its free-text category, tier and notes (no LLM).

Sites write the same thing in thousands of ways ("Offline Student Author Non-IEEE Member Indian",
"Regular - IEEE member", "Listener/Attendee"; 2,617 distinct categories on 2026-09-29), so fees
couldn't be compared across conferences. Each fee gets a few fixed fields instead:

- who: author (registers a paper; the default when nothing says otherwise), attendee (listener,
  observer, no paper) or extra (an add-on: extra paper or page, banquet, tour, book chapter)
- student: True when the rate is for students
- member: True (society member), False (non-member), None (not said)
- mode: onsite, virtual or None
- period: early, regular, late or None
- local: True for rates limited to the host country or region ("Indian", "domestic", "SAARC"),
  False for international or foreign rates, None when not said

From these, `headline_fees` picks two figures per conference: the standard fee (what a regular,
non-member, international author pays to attend in person) and the lowest student fee. Derived
when read, like continents; nothing is stored.
"""

import re
from dataclasses import dataclass

_EXTRA = re.compile(
    r"extra|additional|\badd[- ]?on|banquet|dinner|gala|tour|excursion|accompany|companion|"
    r"per page|page charge|over[- ]?length|book chapter|proceedings copy|visa|hotel|t-?shirt|"
    r"workshop only|tutorial only|lunch",
    re.I,
)
_ATTENDEE = re.compile(
    r"listener|attendee|attend(?:ance)? only|observer|audience|visitor|participant only|"
    r"non[- ]?author|non[- ]?presenter|without (?:a )?paper|no paper|guest",
    re.I,
)
_STUDENT = re.compile(
    r"student|\bug\b|\bpg\b|ph\.?d|master'?s?|masters|undergrad|postgrad|research scholar|"
    r"trainee|resident",
    re.I,
)
_NON_MEMBER = re.compile(
    r"non[- ]?(?:ieee|acm|iapr|isas|[a-z]{2,6})?[- /]*members?|nonmember", re.I
)
# "Non IEEE Academician" is a non-member rate even without the word "member"; a bare society
# name ("Student IEEE", beside a "Student Non-IEEE" column) is the member rate.
_NON_SOCIETY = re.compile(r"\bnon[- ]?(?:ieee|acm|iapr)\b", re.I)
_MEMBER = re.compile(r"members?\b|\bieee\b|\bacm\b", re.I)
_VIRTUAL = re.compile(r"virtual|online|remote|zoom|video", re.I)
_ONSITE = re.compile(r"on[- ]?site|in[- ]person|physical|offline|face[- ]to[- ]face|live", re.I)
_EARLY = re.compile(r"early|advance|batch 1|\bbefore\b|first round", re.I)
_LATE = re.compile(r"\blate\b|\bafter\b|batch 2|last", re.I)
_REGULAR = re.compile(r"regular|standard|normal|full[- ]rate", re.I)
_LOCAL = re.compile(
    r"\blocal\b|domestic|\bnational\b|resident of|indian|saarc|mainland|pakistani|bangladeshi|"
    r"indonesian|malaysian|thai\b|vietnamese|filipino|nepali|sri lankan|in india|from india",
    re.I,
)
_FOREIGN = re.compile(r"international|foreign|overseas|outside|abroad|global", re.I)


@dataclass(frozen=True)
class FeeKind:
    who: str  # author | attendee | extra
    student: bool
    member: bool | None
    mode: str | None  # onsite | virtual
    period: str | None  # early | regular | late
    local: bool | None


def classify(category: str | None, tier: str | None = None, notes: str | None = None) -> FeeKind:
    cat = category or ""
    tier_text = tier or ""
    text = f"{cat} {tier_text} {notes or ''}"

    if _EXTRA.search(cat) and not re.search(r"registration", cat, re.I):
        who = "extra"
    # "Non-Author & Non-Presenter" says attendee, although it contains "author".
    elif _ATTENDEE.search(cat) and not re.search(
        r"(?<!non-)(?<!non )\b(?:author|presenter)|with paper", cat, re.I
    ):
        who = "attendee"
    else:
        who = "author"  # plain "IEEE Member" or "Regular" rows are the paper registration

    if _NON_MEMBER.search(text) or _NON_SOCIETY.search(text):
        member: bool | None = False
    elif _MEMBER.search(text):
        member = True
    else:
        member = None

    virtual, onsite = bool(_VIRTUAL.search(text)), bool(_ONSITE.search(text))
    mode = "virtual" if virtual and not onsite else "onsite" if onsite and not virtual else None

    # "Onsite" as a tier is the last registration period (at the desk), as well as in person.
    tier_l = tier_text.strip().lower()
    if tier_l in ("onsite", "on-site"):
        period: str | None = "late"
    elif _EARLY.search(tier_text) or (not tier_text and _EARLY.search(text)):
        period = "early"
    elif _LATE.search(tier_text) or (not tier_text and _LATE.search(text)):
        period = "late"
    elif _REGULAR.search(text):
        period = "regular"
    else:
        period = None

    local = True if _LOCAL.search(cat) else False if _FOREIGN.search(cat) else None
    return FeeKind(who, bool(_STUDENT.search(cat)), member, mode, period, local)


# ---------- the two headline figures ----------


def _standard_score(k: FeeKind) -> int | None:
    """Higher is closer to "regular, non-member, international author, in person"; None means
    the fee can't stand for that at all."""
    if k.who != "author" or k.student or k.local is True:
        return None
    score = 0
    score += {False: 4, None: 2, True: 0}[k.member]
    score += {"onsite": 3, None: 2, "virtual": 0}[k.mode]
    score += {"regular": 3, None: 2, "late": 1, "early": 1}[k.period]
    score += 1 if k.local is False else 0
    return score


def headline_fees(fees, usd_of) -> tuple[object | None, object | None]:
    """(standard fee, lowest student fee) among `fees` that count (verified or not checked).
    `usd_of(fee)` converts a fee to US dollars (None when it can't), to compare currencies."""
    counted = [f for f in fees if getattr(f, "check_status", None) in (None, "verified")]
    kinds = [(f, classify(f.category, f.tier, f.notes)) for f in counted]

    standard = None
    best = None
    for f, k in kinds:
        score = _standard_score(k)
        if score is None:
            continue
        # Same score: the higher price (a regular rate over a discounted one).
        key = (score, usd_of(f) or 0)
        if best is None or key > best:
            best, standard = key, f

    # A student presenting a paper is the case that matters; listener rates only when a
    # conference lists no student author rate.
    students = [
        (k.who != "author", usd_of(f), f)
        for f, k in kinds
        if k.student and k.who != "extra" and k.local is not True and usd_of(f) is not None
    ]
    student = min(students, key=lambda t: (t[0], t[1]))[2] if students else None
    return standard, student
