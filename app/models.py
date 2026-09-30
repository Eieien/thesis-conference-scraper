from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class ScrapeRun(Base):
    """One execution of a pipeline step (collect / enrich), tracked for the API."""

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # collect | enrich
    source: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|succeeded|failed
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FetchedPage(Base):
    """HTTP cache index. Bodies live on disk under data/cache/."""

    __tablename__ = "fetched_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    final_url: Mapped[str] = mapped_column(String(2048))
    status_code: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(128))
    body_path: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conference(Base):
    """Canonical, de-duplicated conference built from one or more raw listings."""

    __tablename__ = "conferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    acronym: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date, index=True)
    end_date: Mapped[date | None] = mapped_column(Date)
    city: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(128))
    location_raw: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)

    abstract_deadline: Mapped[date | None] = mapped_column(Date)
    paper_deadline: Mapped[date | None] = mapped_column(Date)
    notification_date: Mapped[date | None] = mapped_column(Date)
    camera_ready_deadline: Mapped[date | None] = mapped_column(Date)
    registration_deadline: Mapped[date | None] = mapped_column(Date)

    homepage: Mapped[str | None] = mapped_column(String(2048))
    registration_url: Mapped[str | None] = mapped_column(String(2048))

    fee_min: Mapped[float | None] = mapped_column(Float)
    fee_max: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8))

    # field name -> {"source": "...", "url": "..."}
    field_sources: Mapped[dict] = mapped_column(JSON, default=dict)
    # collected | enriched | needs_review | enrich_failed
    status: Mapped[str] = mapped_column(String(24), default="collected", index=True)
    review_note: Mapped[str | None] = mapped_column(Text)
    # When enrich last read this conference's pages (None = never).
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Earlier editions with published proceedings (app/pipeline/history.py); None = not checked.
    history: Mapped[dict | None] = mapped_column(JSON)
    # Can people attend or present online? (app/pipeline/attendance.py); None = unknown.
    attendance: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    listings: Mapped[list["RawListing"]] = relationship(back_populates="conference")
    fees: Mapped[list["Fee"]] = relationship(
        back_populates="conference", cascade="all, delete-orphan"
    )

    @property
    def sources(self) -> list[str]:
        """The sources this conference was collected from, e.g. ["easychair", "wikicfp"]."""
        return sorted({row.source for row in self.listings})


class RawListing(Base):
    """A conference exactly as one source reported it."""

    __tablename__ = "raw_listings"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_raw_listings_source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(String(2048))

    acronym: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date, index=True)
    end_date: Mapped[date | None] = mapped_column(Date)
    location_raw: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(128))
    homepage: Mapped[str | None] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(Text)

    abstract_deadline: Mapped[date | None] = mapped_column(Date)
    paper_deadline: Mapped[date | None] = mapped_column(Date)
    notification_date: Mapped[date | None] = mapped_column(Date)
    camera_ready_deadline: Mapped[date | None] = mapped_column(Date)
    registration_deadline: Mapped[date | None] = mapped_column(Date)

    categories: Mapped[list] = mapped_column(JSON, default=list)
    # Source-specific data (e.g. EDAS register URL, parsed fee rows, page text for the LLM).
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    in_window: Mapped[bool | None] = mapped_column(Boolean, index=True)
    is_cs: Mapped[bool | None] = mapped_column(Boolean, index=True)
    topic_reason: Mapped[str | None] = mapped_column(String(255))

    conference_id: Mapped[int | None] = mapped_column(
        ForeignKey("conferences.id", ondelete="SET NULL"), index=True
    )
    conference: Mapped[Conference | None] = relationship(back_populates="listings")

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Fee(Base):
    """One registration price (attendee category x tier)."""

    __tablename__ = "fees"

    id: Mapped[int] = mapped_column(primary_key=True)
    conference_id: Mapped[int] = mapped_column(
        ForeignKey("conferences.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(255))  # e.g. "IEEE member - author"
    tier: Mapped[str | None] = mapped_column(String(32))  # early | regular | late | onsite
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8))
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))  # edas | homepage | ...
    source_url: Mapped[str | None] = mapped_column(String(2048))
    # app/pipeline/validate.py: verified | not_found | implausible | duplicate (None = not checked)
    check_status: Mapped[str | None] = mapped_column(String(16), index=True)
    check_note: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)  # page snippet the amount was found in

    conference: Mapped[Conference] = relationship(back_populates="fees")
