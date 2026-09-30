from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.extract import ExtractorNotConfigured
from app.models import ScrapeRun
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
    run = ScrapeRun(
        kind="enrich",
        params={
            "conference_ids": conference_ids,
            "limit": limit,
            "only_missing": only_missing,
            "continent": continent,
            "retry_no_fees": retry_no_fees,
        },
    )
    db.add(run)
    db.commit()
    background.add_task(
        run_enrich,
        settings,
        run.id,
        conference_ids=conference_ids,
        limit=limit,
        only_missing=only_missing,
        continent=continent,
        retry_no_fees=retry_no_fees,
    )
    return run


@router.post("/validate", response_model=RunOut, status_code=202)
async def validate(
    background: BackgroundTasks,
    conference_ids: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Re-check every stored fee against the pages it came from (no LLM, background)."""
    run = ScrapeRun(kind="validate", params={"conference_ids": conference_ids})
    db.add(run)
    db.commit()
    background.add_task(run_validate, settings, run.id, conference_ids=conference_ids)
    return run


@router.post("/history", response_model=RunOut, status_code=202)
async def history(
    background: BackgroundTasks,
    continent: str | None = Query("Asia", description="Continent to check, or 'all'"),
    only_unchecked: bool = Query(False, description="Skip conferences already checked"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Look up earlier editions with published proceedings in Crossref (background). Labels each
    conference "has history" or "no history found"; it does not judge whether it is predatory."""
    from app.pipeline.history import run_history

    scope = None if continent == "all" else continent
    run = ScrapeRun(
        kind="history", params={"continent": continent, "only_unchecked": only_unchecked}
    )
    db.add(run)
    db.commit()
    background.add_task(
        run_history, settings, run.id, continent=scope, only_unchecked=only_unchecked
    )
    return run


@router.post("/attendance", response_model=RunOut, status_code=202)
async def attendance(
    background: BackgroundTasks,
    continent: str | None = Query("Asia", description="Continent to check, or 'all'"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Decide whether each conference can be attended online, from its cached pages, fees and
    IEEE's event format (no LLM, background)."""
    from app.pipeline.attendance import run_attendance

    run = ScrapeRun(kind="attendance", params={"continent": continent})
    db.add(run)
    db.commit()
    scope = None if continent == "all" else continent
    background.add_task(run_attendance, settings, run.id, continent=scope)
    return run


@router.post("/recover", response_model=RunOut, status_code=202)
async def recover(
    background: BackgroundTasks,
    conference_ids: list[int] | None = Query(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Retry sites enrich could not read, with a real browser for ones that reject the crawler
    (robots.txt respected, predatory organizers skipped). Run enrich afterwards (background)."""
    from app.pipeline.recover import run_recover

    run = ScrapeRun(kind="recover", params={"conference_ids": conference_ids})
    db.add(run)
    db.commit()
    background.add_task(run_recover, settings, run.id, conference_ids=conference_ids)
    return run
