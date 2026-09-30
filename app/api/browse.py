"""Browse conference names straight from a source's listing links (nothing is saved)."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.scrapers.edas import EdasSessionExpired
from app.scrapers.http import FetchError
from app.scrapers.registry import ADAPTERS

router = APIRouter(prefix="/browse", tags=["0. browse"])


class ConferenceName(BaseModel):
    acronym: str | None
    name: str
    link: str | None  # the conference's page on the source


class BrowsedPage(BaseModel):
    page: int
    link: str  # the listing page these names came from
    count: int


class BrowseOut(BaseModel):
    source: str
    category: str | None
    pages: list[BrowsedPage]
    count: int
    next_page: int | None
    conferences: list[ConferenceName]


@router.get("/{source}", response_model=BrowseOut)
async def browse_names(
    source: str,
    page: int = Query(1, ge=1, description="First listing page to read"),
    pages: int = Query(1, ge=1, le=10, description="How many pages to read from `page` on"),
    category: str | None = Query(
        None, description="WikiCFP category, e.g. 'machine learning'. Defaults to the first one."
    ),
    settings: Settings = Depends(get_settings),
):
    """Conference names from the source's listing pages. Only listing pages are fetched."""
    if source not in ADAPTERS:
        raise HTTPException(404, f"Unknown source '{source}'. Known: {', '.join(ADAPTERS)}")
    adapter_cls = ADAPTERS[source]
    if not adapter_cls.implemented:
        raise HTTPException(501, f"{adapter_cls.label}: {adapter_cls.notes}")
    adapter = adapter_cls(settings)

    if source == "wikicfp":
        category = category or settings.wikicfp_categories[0]
    try:
        fetcher = adapter.make_fetcher()
    except FetchError as e:  # e.g. no saved EDAS session
        raise HTTPException(400, str(e)) from e

    seen: set[str] = set()
    names: list[ConferenceName] = []
    fetched: list[BrowsedPage] = []
    next_page: int | None = None
    async with fetcher:
        for current in range(page, page + pages):
            try:
                result = await adapter.browse_page(fetcher, page=current, category=category)
            except EdasSessionExpired as e:
                raise HTTPException(401, str(e)) from e
            except FetchError as e:
                raise HTTPException(502, str(e)) from e
            new = [row for row in result.listings if row.source_id not in seen]
            fetched.append(BrowsedPage(page=current, link=result.link, count=len(new)))
            for row in new:
                seen.add(row.source_id)
                names.append(ConferenceName(acronym=row.acronym, name=row.name, link=row.url))
            # WikiCFP repeats its last page forever; a page with nothing new means the end.
            if not result.has_next or (result.listings and not new):
                next_page = None
                break
            next_page = current + 1

    return BrowseOut(
        source=source,
        category=category,
        pages=fetched,
        count=len(names),
        next_page=next_page,
        conferences=names,
    )
