from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db import get_db
from app.models import Conference, RawListing, ScrapeRun
from app.pipeline.export import export_conferences
from app.pipeline.fx import rates_info
from app.pipeline.geo import CONTINENTS, continent_of
from app.schemas import ConferenceDetailOut, ConferenceOut, ListingOut, RunOut

router = APIRouter(tags=["3. data"])

_COMPACT = Query(
    False,
    description="Leave out the long text fields (description, field_sources) that lists don't show",
)


class DateRange:
    """Date filters shared by /conferences and the export. Bounds are inclusive."""

    def __init__(
        self,
        starts_from: date | None = Query(None, description="Conference starts on or after"),
        starts_to: date | None = Query(None, description="Conference starts on or before"),
        abstract_from: date | None = Query(None, description="Abstract deadline on or after"),
        abstract_to: date | None = Query(None, description="Abstract deadline on or before"),
        has_abstract: bool | None = Query(
            None, description="Only conferences that list (true) or don't list (false) one"
        ),
    ):
        self.conditions = []
        if starts_from:
            self.conditions.append(Conference.start_date >= starts_from)
        if starts_to:
            self.conditions.append(Conference.start_date <= starts_to)
        if abstract_from:
            self.conditions.append(Conference.abstract_deadline >= abstract_from)
        if abstract_to:
            self.conditions.append(Conference.abstract_deadline <= abstract_to)
        if has_abstract is not None:
            column = Conference.abstract_deadline
            self.conditions.append(column.isnot(None) if has_abstract else column.is_(None))


_LISTING_HEAVY = {"description"}
_CONFERENCE_HEAVY = {"description", "field_sources", "history"}  # lists get history_years


def _compact(rows, model, heavy: set[str]) -> JSONResponse:
    # Returning a response directly skips response_model, which would demand the dropped fields.
    return JSONResponse(
        jsonable_encoder([model.model_validate(r).model_dump(exclude=heavy) for r in rows])
    )


@router.get("/runs", response_model=list[RunOut])
def list_runs(limit: int = 20, db: Session = Depends(get_db)):
    return db.scalars(select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(limit)).all()


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: Session = Depends(get_db)):
    if (run := db.get(ScrapeRun, run_id)) is None:
        raise HTTPException(404)
    return run


@router.get("/listings", response_model=list[ListingOut])
def list_listings(
    source: str | None = None,
    in_window: bool | None = None,
    is_cs: bool | None = None,
    q: str | None = Query(None, description="substring match on acronym or name"),
    limit: int = Query(50, le=500),
    offset: int = 0,
    compact: bool = _COMPACT,
    db: Session = Depends(get_db),
):
    stmt = select(RawListing).order_by(RawListing.start_date, RawListing.id)
    if source:
        stmt = stmt.where(RawListing.source == source)
    if in_window is not None:
        stmt = stmt.where(RawListing.in_window.is_(in_window))
    if is_cs is not None:
        stmt = stmt.where(RawListing.is_cs.is_(is_cs))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(RawListing.name.ilike(like) | RawListing.acronym.ilike(like))
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return _compact(rows, ListingOut, _LISTING_HEAVY) if compact else rows


@router.get("/listings/{listing_id}")
def get_listing(listing_id: int, db: Session = Depends(get_db)):
    """Full listing, including the source-specific `extra` blob."""
    if (row := db.get(RawListing, listing_id)) is None:
        raise HTTPException(404)
    return {**ListingOut.model_validate(row).model_dump(), "extra": row.extra}


@router.get("/conferences", response_model=list[ConferenceOut])
def list_conferences(
    status: str | None = None,
    has_fees: bool | None = None,
    continent: str | None = Query(
        None, description=f"One of {', '.join(CONTINENTS)}, or Unknown for places not recognised"
    ),
    source: str | None = Query(None, description="Only conferences collected from this source"),
    limit: int = Query(100, le=1000),
    offset: int = 0,
    compact: bool = _COMPACT,
    dates: DateRange = Depends(),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Conference)
        # listings for each row's `sources`, fees for its headline fees
        .options(selectinload(Conference.listings), selectinload(Conference.fees))
        .where(*dates.conditions)
        .order_by(Conference.start_date, Conference.id)
    )
    if source:
        stmt = stmt.where(Conference.listings.any(RawListing.source == source))
    if status:
        stmt = stmt.where(Conference.status == status)
    if has_fees is not None:
        stmt = stmt.where(
            Conference.fee_min.isnot(None) if has_fees else Conference.fee_min.is_(None)
        )
    if continent is None:
        rows = db.scalars(stmt.limit(limit).offset(offset)).all()
        return _compact(rows, ConferenceOut, _CONFERENCE_HEAVY) if compact else rows
    # Continent comes from the stored country's spelling, so it is filtered here, not in SQL.
    wanted = continent.casefold()
    if wanted != "unknown" and wanted not in {c.casefold() for c in CONTINENTS}:
        raise HTTPException(
            422, f"Unknown continent '{continent}'. Use one of {CONTINENTS} or Unknown"
        )
    rows = [
        c
        for c in db.scalars(stmt).all()
        if (continent_of(c.country) or "Unknown").casefold() == wanted
    ]
    rows = rows[offset : offset + limit]
    return _compact(rows, ConferenceOut, _CONFERENCE_HEAVY) if compact else rows


@router.get("/conferences/export")
def export(
    fmt: str = Query("csv", pattern="^(csv|xlsx)$"),
    continent: str | None = Query(
        None, description="A continent, or `all`. Default: the project scope (SCOPE_CONTINENTS)"
    ),
    source: str | None = Query(None, description="Only conferences collected from this source"),
    dates: DateRange = Depends(),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Spreadsheet of conferences with fees in the original currency, USD and PHP."""
    continents = (
        None if continent == "all" else [continent] if continent else settings.scope_continents
    )
    path = export_conferences(
        db, settings, fmt, continents=continents, source=source, where=dates.conditions
    )
    return FileResponse(path, filename=path.name)


@router.get("/conferences/{conference_id}", response_model=ConferenceDetailOut)
def get_conference(conference_id: int, db: Session = Depends(get_db)):
    conf = db.scalar(
        select(Conference)
        .where(Conference.id == conference_id)
        .options(selectinload(Conference.fees), selectinload(Conference.listings))
    )
    if conf is None:
        raise HTTPException(404)
    return conf


@router.get("/summary")
def summary(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    def count(stmt):
        return db.scalar(stmt) or 0

    by_source = db.execute(
        select(RawListing.source, func.count()).group_by(RawListing.source)
    ).all()
    by_status = db.execute(
        select(Conference.status, func.count()).group_by(Conference.status)
    ).all()
    conferences_by_source = db.execute(
        select(RawListing.source, func.count(func.distinct(RawListing.conference_id)))
        .where(RawListing.conference_id.isnot(None))
        .group_by(RawListing.source)
    ).all()
    by_continent: dict[str, int] = {}
    in_scope = in_scope_with_fees = 0
    for country, has_fees, n in db.execute(
        select(Conference.country, Conference.fee_min.isnot(None), func.count()).group_by(
            Conference.country, Conference.fee_min.isnot(None)
        )
    ):
        key = continent_of(country) or "Unknown"
        by_continent[key] = by_continent.get(key, 0) + n
        if key in settings.scope_continents:
            in_scope += n
            in_scope_with_fees += n if has_fees else 0
    return {
        "listings_by_source": dict(by_source),
        "listings_in_window": count(
            select(func.count()).select_from(RawListing).where(RawListing.in_window.is_(True))
        ),
        "listings_in_window_cs": count(
            select(func.count())
            .select_from(RawListing)
            .where(RawListing.in_window.is_(True), RawListing.is_cs.is_(True))
        ),
        "conferences_by_status": dict(by_status),
        "conferences_by_continent": by_continent,
        "conferences_by_source": dict(conferences_by_source),
        "scope": {
            "continents": settings.scope_continents,
            "conferences": in_scope,
            "conferences_with_fees": in_scope_with_fees,
        },
        "exchange_rates": rates_info(settings),
        "conferences_with_fees": count(
            select(func.count()).select_from(Conference).where(Conference.fee_min.isnot(None))
        ),
    }
