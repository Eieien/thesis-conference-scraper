from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.config import get_settings
from app.pipeline.fee_kinds import classify, headline_fees
from app.pipeline.fx import convert
from app.pipeline.geo import continent_of
from app.pipeline.quality import organizer_flag

# ---------- Adapter output ----------


class ListingData(BaseModel):
    """What a source adapter yields for one conference."""

    source_id: str
    name: str
    url: str | None = None
    acronym: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    location_raw: str | None = None
    city: str | None = None
    country: str | None = None
    homepage: str | None = None
    description: str | None = None
    abstract_deadline: date | None = None
    paper_deadline: date | None = None
    notification_date: date | None = None
    camera_ready_deadline: date | None = None
    registration_deadline: date | None = None
    categories: list[str] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)


# ---------- LLM structured output ----------
# No defaults on purpose: Gemini fills every field, null when unknown.


class FeeItem(BaseModel):
    category: str = Field(
        description="Attendee type, e.g. 'IEEE member author', 'Student', 'Listener'"
    )
    tier: str | None = Field(description="early | regular | late | onsite, or null")
    amount: float
    currency: str | None = Field(description="ISO 4217 code, e.g. USD, EUR, IDR")
    notes: str | None


class ExtractedPage(BaseModel):
    name: str | None
    acronym: str | None
    start_date: str | None = Field(description="YYYY-MM-DD")
    end_date: str | None = Field(description="YYYY-MM-DD")
    city: str | None
    country: str | None
    description: str | None = Field(description="1-3 sentences on the conference scope")
    abstract_deadline: str | None = Field(description="YYYY-MM-DD")
    paper_deadline: str | None = Field(description="YYYY-MM-DD")
    notification_date: str | None = Field(description="YYYY-MM-DD")
    camera_ready_deadline: str | None = Field(description="YYYY-MM-DD")
    registration_deadline: str | None = Field(description="YYYY-MM-DD")
    registration_url: str | None
    fees_published: bool
    fees: list[FeeItem]
    notes: str | None


class TopicVerdict(BaseModel):
    is_cs: bool = Field(description="True if the event is about computer science or technology")
    reason: str


# ---------- API output ----------


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RunOut(ORMModel):
    id: int
    kind: str
    source: str | None
    status: str
    params: dict
    stats: dict
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class ListingOut(ORMModel):
    id: int
    source: str
    source_id: str
    url: str | None
    acronym: str | None
    name: str
    start_date: date | None
    end_date: date | None
    location_raw: str | None
    city: str | None
    country: str | None
    homepage: str | None
    description: str | None
    abstract_deadline: date | None
    paper_deadline: date | None
    categories: list
    in_window: bool | None
    is_cs: bool | None
    topic_reason: str | None
    conference_id: int | None


class FeeOut(ORMModel):
    id: int
    category: str
    tier: str | None
    amount: float
    currency: str | None
    notes: str | None
    source: str
    source_url: str | None
    # app/pipeline/validate.py: verified | not_found | implausible (None = not checked yet)
    check_status: str | None = None
    check_note: str | None = None
    evidence: str | None = None

    @computed_field
    @property
    def kind(self) -> dict:
        """who / student / member / mode / period / local, read from the text (fee_kinds.py)."""
        return classify(self.category, self.tier, self.notes).__dict__

    @computed_field
    @property
    def amount_usd(self) -> float | None:
        """In US dollars at today's rate (app/pipeline/fx.py); None when it can't be converted."""
        return convert(self.amount, self.currency, "USD", get_settings())

    @computed_field
    @property
    def amount_php(self) -> float | None:
        """In Philippine pesos at today's rate."""
        return convert(self.amount, self.currency, "PHP", get_settings())


class HeadlineFee(BaseModel):
    """One fee picked to stand for a conference (app/pipeline/fee_kinds.py), with conversions."""

    amount: float
    currency: str | None
    category: str
    tier: str | None
    amount_usd: float | None
    amount_php: float | None


def _headline(fee) -> HeadlineFee | None:
    if fee is None:
        return None
    s = get_settings()
    return HeadlineFee(
        amount=fee.amount,
        currency=fee.currency,
        category=fee.category,
        tier=fee.tier,
        amount_usd=convert(fee.amount, fee.currency, "USD", s),
        amount_php=convert(fee.amount, fee.currency, "PHP", s),
    )


class ConferenceOut(ORMModel):
    id: int
    acronym: str | None
    name: str
    start_date: date | None
    end_date: date | None
    city: str | None
    country: str | None
    description: str | None
    abstract_deadline: date | None
    paper_deadline: date | None
    notification_date: date | None
    camera_ready_deadline: date | None
    registration_deadline: date | None
    homepage: str | None
    registration_url: str | None
    fee_min: float | None
    fee_max: float | None
    currency: str | None
    status: str
    review_note: str | None
    field_sources: dict
    sources: list[str] = []  # where it was collected from, e.g. ["easychair", "wikicfp"]
    # Earlier editions with published proceedings (app/pipeline/history.py); None = not checked.
    history: dict | None = None
    # Online attendance (app/pipeline/attendance.py): {mode, evidence, url, checked_at}.
    attendance: dict | None = None

    @computed_field
    @property
    def online(self) -> str | None:
        """'online option', 'online only' or 'in person only'; None when nothing says."""
        return (self.attendance or {}).get("mode")

    @computed_field
    @property
    def history_label(self) -> str | None:
        """'has history' or 'no history found'; None when not checked yet. Not a verdict on
        whether the conference is predatory: a first edition has no history either."""
        if self.history is None:
            return None
        return "has history" if self.history.get("has_history") else "no history found"

    @computed_field
    @property
    def history_years(self) -> list[int]:
        """Years of the earlier editions found, newest first (empty when none or unchecked)."""
        return [e["year"] for e in (self.history or {}).get("editions", [])]

    # Read for the headline fees; only the detail view sends them (ConferenceDetailOut).
    fees: list[FeeOut] = Field(default_factory=list, exclude=True)

    def _headlines(self):
        s = get_settings()
        return headline_fees(self.fees, lambda f: convert(f.amount, f.currency, "USD", s))

    @computed_field
    @property
    def standard_fee(self) -> HeadlineFee | None:
        """What a regular, non-member, international author pays in person (fee_kinds.py)."""
        return _headline(self._headlines()[0])

    @computed_field
    @property
    def student_fee(self) -> HeadlineFee | None:
        """The lowest student rate open to international participants."""
        return _headline(self._headlines()[1])

    @computed_field
    @property
    def fee_min_usd(self) -> float | None:
        return convert(self.fee_min, self.currency, "USD", get_settings())

    @computed_field
    @property
    def fee_max_usd(self) -> float | None:
        return convert(self.fee_max, self.currency, "USD", get_settings())

    @computed_field
    @property
    def fee_min_php(self) -> float | None:
        return convert(self.fee_min, self.currency, "PHP", get_settings())

    @computed_field
    @property
    def fee_max_php(self) -> float | None:
        return convert(self.fee_max, self.currency, "PHP", get_settings())

    @computed_field
    @property
    def continent(self) -> str | None:
        """Derived from `country`; None when the country is missing or not recognised."""
        return continent_of(self.country)

    @computed_field
    @property
    def organizer_flag(self) -> str | None:
        """A warning about the organizer (app/pipeline/quality.py), e.g. WASET; None if none."""
        return organizer_flag(self.homepage)


class ConferenceDetailOut(ConferenceOut):
    fees: list[FeeOut] = Field(default_factory=list)  # sent in full here
    listings: list[ListingOut]
