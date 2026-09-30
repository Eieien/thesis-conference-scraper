import asyncio

import httpx
import pytest

from app.config import Settings
from app.scrapers.http import Fetcher, FetchError


def test_dead_host_is_given_up_after_one_retry():
    attempts: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        raise httpx.ConnectTimeout("no answer", request=request)

    async def run():
        settings = Settings(request_delay_seconds=0, max_retries=3, respect_robots=False)
        async with Fetcher(settings) as f:
            f._client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
            with pytest.raises(FetchError):
                await f.get("https://dead.example/", use_cache=False)
            assert len(attempts) == 2  # first try and one retry, not four
            # Later requests to the same domain fail at once, without touching the network.
            with pytest.raises(FetchError, match="did not accept a connection"):
                await f.get("https://dead.example/fees", use_cache=False)
            assert len(attempts) == 2

    asyncio.run(run())
