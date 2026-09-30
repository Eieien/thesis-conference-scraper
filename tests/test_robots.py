from protego import Protego

UA = "Mozilla/5.0 (compatible; ConferenceResearchBot/0.1)"

# The start of www.computer.org/robots.txt as served on 2026-09-23. It opens with "Allow: /",
# which made the standard library's parser allow every path below.
COMPUTER_ORG = """User-agent: *
Allow: /
Disallow: /_next/
Disallow: /api/
Disallow: /search
Disallow: /*?*
"""


def test_wildcards_and_most_specific_rule_win():
    rp = Protego.parse(COMPUTER_ORG)
    assert rp.can_fetch("https://www.computer.org/conferences/cfp-ieee-issre", UA)
    assert not rp.can_fetch("https://www.computer.org/api/events", UA)
    assert not rp.can_fetch("https://www.computer.org/conferences/calendar?page=2", UA)
    assert not rp.can_fetch("https://www.computer.org/search", UA)


def test_fetcher_enforces_the_rules(monkeypatch):
    import asyncio

    import httpx
    import pytest

    from app.config import Settings
    from app.scrapers.http import Fetcher, RobotsDisallowed

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=COMPUTER_ORG)
        return httpx.Response(200, text="<html>ok</html>")

    async def run():
        settings = Settings(request_delay_seconds=0, respect_robots=True)
        # A test page must never land in the real page cache.
        monkeypatch.setattr(Fetcher, "_write_cache", lambda self, result: None)
        async with Fetcher(settings) as f:
            f._client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
            ok = await f.get("https://www.computer.org/conferences/cfp-ieee-issre", use_cache=False)
            assert ok.status_code == 200
            with pytest.raises(RobotsDisallowed):
                await f.get("https://www.computer.org/api/events", use_cache=False)
            with pytest.raises(RobotsDisallowed):
                await f.get("https://www.computer.org/conferences/calendar?x=1", use_cache=False)

    asyncio.run(run())
