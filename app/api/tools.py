"""Single-step endpoints for inspecting sites and testing each building block in isolation."""

import hashlib
import re
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.extract import ExtractorNotConfigured, get_extractor
from app.pipeline.enrich import discover_pages
from app.pipeline.fees import fees_from_tables
from app.pipeline.filter import keyword_topic
from app.pipeline.fx import rates_info, refresh_rates
from app.pipeline.merge import acronym_key
from app.pipeline.normalize import parse_date_range, parse_money, split_location
from app.schemas import ExtractedPage, FeeItem, TopicVerdict
from app.scrapers.html_clean import html_to_text, page_title, pdf_to_text
from app.scrapers.http import Fetcher, FetchError, load_playwright_cookies

router = APIRouter(prefix="/tools", tags=["4. tools"])


class FetchIn(BaseModel):
    url: str
    use_edas_session: bool = False
    use_cache: bool = True


class TextIn(BaseModel):
    text: str


class ExtractIn(BaseModel):
    url: str | None = None
    text: str | None = None
    hint: str | None = None


def _recon_path(settings: Settings, url: str, suffix: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[-1])[:80]
    return settings.recon_dir / f"{slug}_{hashlib.sha1(url.encode()).hexdigest()[:8]}{suffix}"


def _fetcher(settings: Settings, use_edas_session: bool) -> Fetcher:
    if not use_edas_session:
        return Fetcher(settings)
    try:
        cookies = load_playwright_cookies(settings.edas_state_path)
    except FetchError as e:
        raise HTTPException(400, str(e)) from e
    return Fetcher(
        settings,
        cookies=cookies,
        delay_seconds=settings.edas_delay_seconds,
        cache_namespace="edas",
        respect_robots=settings.edas_respect_robots,
    )


@router.post("/fetch")
async def fetch(body: FetchIn, settings: Settings = Depends(get_settings)):
    """Fetch a URL (plain HTTP) and save the raw HTML to data/recon/ for inspecting markup."""
    async with _fetcher(settings, body.use_edas_session) as f:
        try:
            r = await f.get(body.url, use_cache=body.use_cache)
        except FetchError as e:
            raise HTTPException(502, str(e)) from e
    path = _recon_path(settings, body.url, ".pdf" if r.is_pdf else ".html")
    path.write_bytes(r.content)
    text = pdf_to_text(r.content) if r.is_pdf else html_to_text(r.text)
    return {
        "status_code": r.status_code,
        "final_url": r.final_url,
        "from_cache": r.from_cache,
        "content_type": r.content_type,
        "title": None if r.is_pdf else page_title(r.text),
        "bytes": len(r.content),
        "saved_to": str(path),
        "text_preview": text[:3000],
    }


@router.post("/render")
async def render(body: FetchIn, settings: Settings = Depends(get_settings)):
    """Load a JavaScript-heavy page in headless Chromium and save the rendered HTML."""
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    state = str(settings.edas_state_path) if body.use_edas_session else None
    if state and not settings.edas_state_path.exists():
        raise HTTPException(400, "No saved EDAS session. Run: uv run python -m scripts.edas_login")
    # EDAS renders use stealth and Chromium's own user agent so the page sees a normal browser.
    manager = Stealth().use_async(async_playwright()) if state else async_playwright()
    async with manager as p:
        browser = await p.chromium.launch()
        user_agent = None if state else settings.user_agent
        context = await browser.new_context(storage_state=state, user_agent=user_agent)
        page = await context.new_page()
        await page.goto(body.url, wait_until="networkidle", timeout=60_000)
        html = await page.content()
        await browser.close()
    path = _recon_path(settings, body.url, ".rendered.html")
    path.write_text(html, encoding="utf-8")
    return {
        "title": page_title(html),
        "bytes": len(html),
        "saved_to": str(path),
        "text_preview": html_to_text(html)[:3000],
    }


@router.post("/discover-pages")
async def discover(body: FetchIn, settings: Settings = Depends(get_settings)):
    """Show which links on a homepage the enricher would follow, and their scores."""
    async with Fetcher(settings) as f:
        try:
            _, candidates = await discover_pages(f, body.url)
        except FetchError as e:
            raise HTTPException(502, str(e)) from e
    return [{"score": s, "url": u, "text": t} for s, u, t in candidates[:25]]


@router.post("/extract", response_model=ExtractedPage)
async def extract(body: ExtractIn, settings: Settings = Depends(get_settings)):
    """Run Gemini extraction on one URL or on pasted text. Nothing is saved."""
    if not body.text and not body.url:
        raise HTTPException(422, "Provide url or text")
    text = body.text
    if text is None:
        async with Fetcher(settings) as f:
            r = await f.get(body.url)
        text = pdf_to_text(r.content) if r.is_pdf else html_to_text(r.text)
    try:
        return await get_extractor(settings).extract_conference(text, url=body.url, hint=body.hint)
    except ExtractorNotConfigured as e:
        raise HTTPException(400, str(e)) from e


@router.post("/fees-from-html", response_model=list[FeeItem])
async def fees_from_html(body: FetchIn, settings: Settings = Depends(get_settings)):
    """Run the non-LLM fee-table heuristic on a page."""
    async with _fetcher(settings, body.use_edas_session) as f:
        r = await f.get(body.url, use_cache=body.use_cache)
    return fees_from_tables(r.text)


@router.post("/classify-topic")
async def classify_topic(
    body: TextIn, use_llm: bool = False, settings: Settings = Depends(get_settings)
):
    verdict, reason = keyword_topic(body.text)
    result: dict = {"keyword_verdict": verdict, "keyword_reason": reason}
    if use_llm:
        try:
            llm: TopicVerdict = await get_extractor(settings).classify_topic(body.text)
        except ExtractorNotConfigured as e:
            raise HTTPException(400, str(e)) from e
        result["llm"] = llm
    return result


@router.post("/refresh-rates")
async def refresh_exchange_rates(settings: Settings = Depends(get_settings)):
    """Fetch today's exchange rates now (they otherwise refresh once a day at startup)."""
    data = await refresh_rates(settings, force=True)
    if not data:
        raise HTTPException(502, "Could not fetch exchange rates; the previous ones are kept")
    return rates_info(settings)


@router.post("/parse-dates")
def parse_dates(body: TextIn) -> dict[str, date | None]:
    start, end = parse_date_range(body.text)
    return {"start": start, "end": end}


@router.post("/parse-location")
def parse_location(body: TextIn) -> dict[str, str | None]:
    city, country = split_location(body.text)
    return {"city": city, "country": country}


@router.post("/parse-money")
def parse_money_endpoint(body: TextIn) -> dict:
    amount, currency = parse_money(body.text)
    return {"amount": amount, "currency": currency}


@router.post("/acronym-key")
def acronym_key_endpoint(body: TextIn) -> dict:
    """The key used to match the same conference across sources."""
    return {"key": acronym_key(body.text)}
