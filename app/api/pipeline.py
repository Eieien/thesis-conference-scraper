from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.run_guard import IDEMPOTENCY_KEY, start_run
from app.config import Settings, get_settings
from app.db import get_db
from app.extract import ExtractorNotConfigured
from app.pipeline.enrich import run_enrich, run_validate
from app.pipeline.filter import run_filter
from app.pipeline.geo import CONTINENTS
from app.pipeline.merge import run_merge
from app.schemas import RunOut

router = APIRouter(prefix="/pipeline", tags=["2. pipeline"])


@router.post("/filter")
async def filter_listings(
    use_llm: bool = Query(False, description="Ask Gemini about listings keywords can't decide"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Mark listings in the date window and classify CS/tech vs not."""
    try:
        return await run_filter(db, settings, use_llm=use_llm)
    except ExtractorNotConfigured as e:
        raise HTTPException(400, str(e)) from e


@router.post("/merge")
def merge(db: Session = Depends(get_db)):
    """Group in-window CS listings from all sources into de-duplicated conferences."""
    return run_merge(db)


@router.post("/enrich", response_model=RunOut, status_code=202)
async def enrich(
    background: BackgroundTasks,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    conference_ids: list[int] | None = Query(None),
    limit: int | None = Query(None, description="Try a few first, e.g. 5"),
    only_missing: bool = True,
    continent: str | None = Query(
        None,
        description=f"Only one continent: {', '.join(CONTINENTS)}. Default: the project scope "
        "(SCOPE_CONTINENTS, Asia). Pass `all` for every continent.",
    ),
    retry_no_fees: bool = Query(
        False, description="Also re-read conferences that ended without fees last time"
    ),
    skip_unchanged: bool = Query(
        True,
        description="Don't ask the LLM again when a conference's pages haven't changed since "
        "its last read. Turn off after changing the prompt or the extractor.",
    ),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Crawl homepages + use Gemini to fill fees, deadlines and descriptions (background)."""
    if not settings.gemini_api_key:
        raise HTTPException(400, "GEMINI_API_KEY is not set in .env")
    if continent is None and not conference_ids and len(settings.scope_continents) == 1:
        continent = settings.scope_continents[0]
    if continent == "all":
        continent = None
    run, new = start_run(
        db,
        response,
        kind="enrich",
        params={
            "conference_ids": conference_ids,
            "limit": limit,
            "only_missing": only_missing,
            "continent": continent,
            "retry_no_fees": retry_no_fees,
            "skip_unchanged": skip_unchanged,
        },
        key=idempotency_key,
    )
    if new:
        background.add_task(
            run_enrich,
            settings,
            run.id,
            conference_ids=conference_ids,
            limit=limit,
            only_missing=only_missing,
            continent=continent,
            retry_no_fees=retry_no_fees,
            skip_unchanged=skip_unchanged,
        )
    return run


@router.post("/validate", response_model=RunOut, status_code=202)
async def validate(
    background: BackgroundTasks,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    conference_ids: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Re-check every stored fee against the pages it came from (no LLM, background)."""
    run, new = start_run(
        db,
        response,
        kind="validate",
        params={"conference_ids": conference_ids},
        key=idempotency_key,
    )
    if new:
        background.add_task(run_validate, settings, run.id, conference_ids=conference_ids)
    return run


@router.post("/history", response_model=RunOut, status_code=202)
async def history(
    background: BackgroundTasks,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    continent: str | None = Query("Asia", description="Continent to check, or 'all'"),
    only_unchecked: bool = Query(False, description="Skip conferences already checked"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Look up earlier editions with published proceedings in Crossref (background). Labels each
    conference "has history" or "no history found"; it does not judge whether it is predatory."""
    from app.pipeline.history import run_history

    scope = None if continent == "all" else continent
    run, new = start_run(
        db,
        response,
        kind="history",
        params={"continent": continent, "only_unchecked": only_unchecked},
        key=idempotency_key,
    )
    if new:
        background.add_task(
            run_history, settings, run.id, continent=scope, only_unchecked=only_unchecked
        )
    return run


@router.post("/attendance", response_model=RunOut, status_code=202)
async def attendance(
    background: BackgroundTasks,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    continent: str | None = Query("Asia", description="Continent to check, or 'all'"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Decide whether each conference can be attended online, from its cached pages, fees and
    IEEE's event format (no LLM, background)."""
    from app.pipeline.attendance import run_attendance

    run, new = start_run(
        db, response, kind="attendance", params={"continent": continent}, key=idempotency_key
    )
    if new:
        scope = None if continent == "all" else continent
        background.add_task(run_attendance, settings, run.id, continent=scope)
    return run


@router.post("/recover", response_model=RunOut, status_code=202)
async def recover(
    background: BackgroundTasks,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    conference_ids: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Retry sites enrich could not read, with a real browser for ones that reject the crawler
    (robots.txt respected, predatory organizers skipped). Run enrich afterwards (background)."""
    from app.pipeline.recover import run_recover

    run, new = start_run(
        db, response, kind="recover", params={"conference_ids": conference_ids}, key=idempotency_key
    )
    if new:
        background.add_task(run_recover, settings, run.id, conference_ids=conference_ids)
    return run


@router.post("/update", response_model=RunOut, status_code=202)
async def update(
    background: BackgroundTasks,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    sources: list[str] | None = Query(None, description="Default: every working source"),
    continent: str | None = Query(
        None, description="Default: the public site's continents (PUBLIC_CONTINENTS); 'all' for all"
    ),
    retry_no_fees: bool = Query(
        False, description="Also re-read conferences that had no fees (unchanged sites are free)"
    ),
    export: bool = Query(True, description="Write the MariaDB file for phpMyAdmin at the end"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """The whole pipeline in one go: collect every source at once, then filter, merge, IEEE
    websites, enrich, publication history and the MariaDB export, each starting when the last
    ends (background). Each step also shows as its own run."""
    from app.pipeline.update import STEPS, run_update

    run, new = start_run(
        db,
        response,
        kind="update",
        params={
            "sources": sources,
            "continent": continent,
            "retry_no_fees": retry_no_fees,
            "steps": STEPS,
            "current": "starting",
        },
        key=idempotency_key,
    )
    if new:
        background.add_task(
            run_update,
            settings,
            run.id,
            sources=sources,
            continent=continent,
            retry_no_fees=retry_no_fees,
            export=export,
        )
    return run
