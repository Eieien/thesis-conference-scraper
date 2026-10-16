import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import inspect

from app.api import browse, data, pipeline, sources, tools
from app.api.auth import require_api_key
from app.config import get_settings
from app.db import engine
from app.pipeline.fx import refresh_rates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    if not inspect(engine).has_table("conferences", schema=settings.database_schema):
        log.warning("Database has no tables yet. Run: uv run alembic upgrade head")
    # Runs a previous process left "running" can't finish now; close them so they neither
    # mislead the dashboard nor block new runs (app/api/run_guard.py). Only the writing server
    # does this: a read-only server shares the database with a live one.
    if not settings.read_only and inspect(engine).has_table("scrape_runs"):
        from app.api.run_guard import close_abandoned
        from app.db import SessionLocal

        with SessionLocal() as db:
            if closed := close_abandoned(db):
                log.info("closed %d runs left unfinished by a previous server", closed)
    # Today's exchange rates for the USD/PHP columns; never blocks startup.
    refresh = asyncio.create_task(refresh_rates(settings))
    yield
    refresh.cancel()


app = FastAPI(
    title="Conference Scraper",
    description=(
        "Test each step on its own: **browse** (conference names from listing links), "
        "**sources** (collect/preview), **pipeline** "
        "(filter -> merge -> enrich), **data** (browse/export), **tools** (single-step helpers)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)
# Lists of conferences and listings are large JSON; compressed they shrink about 5-10x.
app.add_middleware(GZipMiddleware, minimum_size=1000)
# Lets the frontend, served from another address, call the API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_origin_regex=get_settings().cors_origin_regex,
    allow_methods=["GET"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # the export's file name
)
# With API_KEYS set, every router needs the X-API-Key header; /health below stays open.
_keyed = [Depends(require_api_key)]
app.include_router(data.router, dependencies=_keyed)
if not get_settings().read_only:
    app.include_router(browse.router, dependencies=_keyed)
    app.include_router(sources.router, dependencies=_keyed)
    app.include_router(pipeline.router, dependencies=_keyed)
    app.include_router(tools.router, dependencies=_keyed)


@app.get("/health", tags=["meta"])
def health():
    s = get_settings()
    return {
        "ok": True,
        "read_only": s.read_only,
        "window": [s.window_start, s.window_end],
        "gemini_configured": bool(s.gemini_api_key),
        "edas_session_saved": s.edas_state_path.exists(),
    }
