import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import inspect

from app.api import browse, data, pipeline, sources, tools
from app.config import get_settings
from app.db import engine
from app.pipeline.fx import refresh_rates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    if not inspect(engine).has_table("conferences"):
        log.warning("Database has no tables yet. Run: uv run alembic upgrade head")
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
app.include_router(browse.router)
app.include_router(sources.router)
app.include_router(pipeline.router)
app.include_router(data.router)
app.include_router(tools.router)


@app.get("/health", tags=["meta"])
def health():
    s = get_settings()
    return {
        "ok": True,
        "window": [s.window_start, s.window_end],
        "gemini_configured": bool(s.gemini_api_key),
        "edas_session_saved": s.edas_state_path.exists(),
    }
