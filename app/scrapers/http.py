"""Polite async HTTP client: per-domain rate limit, robots.txt, retries, on-disk cache."""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from protego import Protego
from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import FetchedPage

log = logging.getLogger(__name__)

_TEXT_TYPES = ("text/", "application/xhtml", "application/json", "application/xml")


class FetchError(Exception):
    pass


class RobotsDisallowed(FetchError):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content_type: str | None
    content: bytes
    from_cache: bool

    @property
    def is_text(self) -> bool:
        return self.content_type is None or self.content_type.startswith(_TEXT_TYPES)

    @property
    def is_pdf(self) -> bool:
        return (
            bool(self.content_type and "pdf" in self.content_type) or self.content[:5] == b"%PDF-"
        )

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def load_playwright_cookies(state_path: Path) -> httpx.Cookies:
    """Turn a Playwright storage_state file into httpx cookies."""
    if not state_path.exists():
        raise FetchError(
            f"No saved session at {state_path}. Run: uv run python -m scripts.edas_login"
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    cookies = httpx.Cookies()
    for c in state.get("cookies", []):
        cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
    return cookies


class Fetcher:
    def __init__(
        self,
        settings: Settings,
        *,
        cookies: httpx.Cookies | None = None,
        delay_seconds: float | None = None,
        cache_namespace: str = "public",
        respect_robots: bool | None = None,
    ) -> None:
        self.settings = settings
        self.delay = settings.request_delay_seconds if delay_seconds is None else delay_seconds
        self._cookies = cookies
        # Keeps logged-in pages from being served to anonymous fetches and vice versa.
        self._ns = cache_namespace
        self.respect_robots = settings.respect_robots if respect_robots is None else respect_robots
        self._client: httpx.AsyncClient | None = None
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._domain_last: dict[str, float] = {}
        # Protego follows Google's robots.txt rules: wildcards (* and $) and the most specific
        # rule wins. The standard library's parser ignores both and applies rules in file order.
        self._robots: dict[str, Protego | None] = {}
        # Domains that refused or never answered a connection; later requests fail at once.
        self._unreachable: set[str] = set()
        self._global = asyncio.Semaphore(settings.max_concurrent_domains)

    async def __aenter__(self) -> "Fetcher":
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.settings.request_timeout_seconds,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            },
            cookies=self._cookies,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    # ---------- public ----------

    async def get(
        self, url: str, *, use_cache: bool = True, respect_robots: bool | None = None
    ) -> FetchResult:
        if use_cache and (cached := self._read_cache(url)):
            return cached
        check_robots = self.respect_robots if respect_robots is None else respect_robots
        if check_robots and not await self._robots_allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        result = await self._throttled_get(url)
        self._write_cache(result)
        return result

    async def allowed(self, url: str) -> bool:
        """Whether robots.txt lets this crawler read `url` (always true when robots are off)."""
        return not self.respect_robots or await self._robots_allowed(url)

    def store(self, url: str, final_url: str, html: str) -> None:
        """Put a page read some other way (a real browser, see app/pipeline/recover.py) into
        the cache, so the normal pipeline reads it like any fetched page."""
        self._write_cache(
            FetchResult(
                url=url,
                final_url=final_url,
                status_code=200,
                content_type="text/html; charset=utf-8",
                content=html.encode("utf-8"),
                from_cache=False,
            )
        )

    # ---------- internals ----------

    def _cache_key(self, url: str) -> str:
        return f"{self._ns}:{url}"

    def _read_cache(self, url: str) -> FetchResult | None:
        with SessionLocal() as db:
            row = db.scalar(select(FetchedPage).where(FetchedPage.url == self._cache_key(url)))
        if row is None:
            return None
        age = datetime.now(UTC) - row.fetched_at.replace(tzinfo=UTC)
        path = self.settings.cache_dir / row.body_path
        if age > timedelta(hours=self.settings.cache_ttl_hours) or not path.exists():
            return None
        return FetchResult(
            url=url,
            final_url=row.final_url,
            status_code=row.status_code,
            content_type=row.content_type,
            content=path.read_bytes(),
            from_cache=True,
        )

    def _write_cache(self, r: FetchResult) -> None:
        if r.status_code >= 400:
            return
        key = self._cache_key(r.url)
        digest = hashlib.sha256(key.encode()).hexdigest()
        body_path = f"{digest[:2]}/{digest}.bin"
        path = self.settings.cache_dir / body_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
        with SessionLocal() as db:
            row = db.scalar(select(FetchedPage).where(FetchedPage.url == key))
            if row is None:
                row = FetchedPage(url=key)
                db.add(row)
            row.final_url = r.final_url
            row.status_code = r.status_code
            row.content_type = r.content_type
            row.body_path = body_path
            row.sha256 = hashlib.sha256(r.content).hexdigest()
            row.fetched_at = datetime.now(UTC)
            db.commit()

    async def _robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: Protego | None = None
            try:
                resp = await self._throttled_get(f"{origin}/robots.txt", raise_for_status=False)
                if resp.status_code < 400:  # no robots.txt -> everything allowed
                    parser = Protego.parse(resp.text)
            except FetchError:
                parser = None
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(url, self.settings.user_agent)

    async def _throttled_get(self, url: str, *, raise_for_status: bool = True) -> FetchResult:
        assert self._client is not None, "use 'async with Fetcher(...)'"
        domain = urlsplit(url).netloc
        if domain in self._unreachable:
            raise FetchError(f"{domain} did not accept a connection earlier; skipping {url}")
        lock = self._domain_locks.setdefault(domain, asyncio.Lock())
        # Domain lock first, so requests queued for one slow domain don't hold global slots.
        async with lock, self._global:
            last_error: Exception | None = None
            for attempt in range(self.settings.max_retries + 1):
                wait = self._domain_last.get(domain, 0) + self.delay - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    resp = await self._client.get(url)
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    # A dead host rarely comes back within seconds: one retry, then give up on
                    # the whole domain for this run instead of waiting out every retry again.
                    self._domain_last[domain] = time.monotonic()
                    if attempt >= 1:
                        self._unreachable.add(domain)
                        raise FetchError(f"{domain} does not accept connections: {e!r}") from e
                    last_error = e
                    await asyncio.sleep(self.delay)
                    continue
                except httpx.HTTPError as e:
                    last_error = e
                    self._domain_last[domain] = time.monotonic()
                    await asyncio.sleep(self.delay * 2**attempt)
                    continue
                finally:
                    self._domain_last[domain] = time.monotonic()

                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = resp.headers.get("Retry-After", "")
                    backoff = (
                        float(retry_after) if retry_after.isdigit() else self.delay * 2**attempt
                    )
                    last_error = FetchError(f"HTTP {resp.status_code} for {url}")
                    log.warning("%s; retrying in %.0fs", last_error, backoff)
                    await asyncio.sleep(backoff)
                    continue
                if raise_for_status and resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code} for {url}")
                return FetchResult(
                    url=url,
                    final_url=str(resp.url),
                    status_code=resp.status_code,
                    content_type=resp.headers.get("content-type", "").split(";")[0] or None,
                    content=resp.content,
                    from_cache=False,
                )
            raise FetchError(f"giving up on {url}: {last_error}")
