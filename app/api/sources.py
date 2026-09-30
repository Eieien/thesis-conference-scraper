from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import ScrapeRun
from app.pipeline.collect import collect_listings, run_collect
from app.pipeline.ieee_details import run_ieee_details
from app.schemas import ListingData, RunOut
from app.scrapers.registry import ADAPTERS

router = APIRouter(prefix="/sources", tags=["1. sources"])


def _check_source(name: str) -> None:
    if name not in ADAPTERS:
        raise HTTPException(404, f"Unknown source '{name}'. Known: {', '.join(ADAPTERS)}")
    if not ADAPTERS[name].implemented:
        raise HTTPException(501, f"{ADAPTERS[name].label}: {ADAPTERS[name].notes}")


@router.get("")
def list_sources(settings: Settings = Depends(get_settings)):
    return [
        {
            "name": cls.name,
            "label": cls.label,
            "implemented": cls.implemented,
            "requires_login": cls.requires_login,
            "session_saved": settings.edas_state_path.exists() if cls.requires_login else None,
            "notes": cls.notes,
        }
        for cls in ADAPTERS.values()
    ]


@router.post("/{name}/preview", response_model=list[ListingData])
async def preview(
    name: str,
    limit: int = Query(10, le=100),
    max_pages: int = Query(1, le=5),
    fetch_details: bool = False,
    settings: Settings = Depends(get_settings),
):
    """Run an adapter and return what it parses, without writing to the database."""
    _check_source(name)
    out = []
    async for listing in collect_listings(
        settings, name, max_pages=max_pages, fetch_details=fetch_details, limit=limit
    ):
        out.append(listing)
    return out


@router.post("/{name}/collect", response_model=RunOut, status_code=202)
async def collect(
    name: str,
    background: BackgroundTasks,
    max_pages: int | None = None,
    fetch_details: bool = True,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Scrape a source into raw_listings in the background. Poll GET /runs/{id}."""
    _check_source(name)
    run = ScrapeRun(
        kind="collect",
        source=name,
        params={"max_pages": max_pages, "fetch_details": fetch_details},
    )
    db.add(run)
    db.commit()
    background.add_task(
        run_collect, settings, run.id, name, max_pages=max_pages, fetch_details=fetch_details
    )
    return run


@router.post("/ieee/details", response_model=RunOut, status_code=202)
async def ieee_details(
    background: BackgroundTasks,
    batch_size: int = Query(60, ge=1, le=200),
    rest_seconds: int = Query(900, ge=0, description="Pause between batches, to avoid throttling"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Open the IEEE page of in-scope conferences that have no homepage, through a real browser,
    and save their official website (background). Stops by itself if IEEE starts throttling."""
    run = ScrapeRun(
        kind="ieee_details",
        source="ieee",
        params={"batch_size": batch_size, "rest_seconds": rest_seconds},
    )
    db.add(run)
    db.commit()
    background.add_task(
        run_ieee_details, settings, run.id, batch_size=batch_size, rest_seconds=rest_seconds
    )
    return run
