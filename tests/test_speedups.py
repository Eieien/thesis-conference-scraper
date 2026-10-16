"""The pipeline's speed-ups: unchanged sites skip the LLM, detail pages are cached longer."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.pipeline import enrich
from app.pipeline.enrich import _apply_outcome, _Job, _read_and_extract, fingerprint
from app.scrapers.http import Fetcher


class CountingExtractor:
    def __init__(self):
        self.calls = 0

    async def extract_conference(self, text, *, url=None, hint=None):
        self.calls += 1
        raise AssertionError("the LLM must not be asked about an unchanged page")


def _job(previous, skip=True):
    return _Job(1, None, "hint", ["### page\nRegistration fee: USD 300"], previous, skip)


def test_unchanged_pages_skip_the_llm(monkeypatch):
    text = "### page\nRegistration fee: USD 300"
    extractor = CountingExtractor()
    out = asyncio.run(_read_and_extract(_job(fingerprint(text)), None, extractor, Settings()))
    assert out.unchanged and extractor.calls == 0
    assert out.fingerprint == fingerprint(text)


def test_changed_pages_or_no_skip_ask_the_llm():
    extractor = CountingExtractor()
    for job in (_job("an older fingerprint"), _job(None), _job(fingerprint("x"), skip=False)):
        with contextlib.suppress(AssertionError):
            asyncio.run(_read_and_extract(job, None, extractor, Settings()))
    assert extractor.calls == 3


def test_an_unchanged_read_keeps_status_and_note():
    class Conf:
        status, review_note, enriched_at, read_fingerprint = (
            "needs_review",
            "fees not published yet",
            None,
            "abc",
        )

    conf = Conf()
    result = _apply_outcome(None, conf, enrich._Outcome(unchanged=True, fingerprint="abc"))
    assert result == "unchanged"
    assert (conf.status, conf.review_note) == ("needs_review", "fees not published yet")
    assert conf.enriched_at is not None


def test_cache_for_raises_the_age_limit_only_inside_the_block():
    f = Fetcher(Settings(cache_ttl_hours=72))
    assert f._ttl == timedelta(hours=72)
    with f.cache_for(timedelta(days=7)):
        assert f._ttl == timedelta(days=7)
    assert f._ttl == timedelta(hours=72)
    # never shorter than the normal limit
    with f.cache_for(timedelta(hours=1)):
        assert f._ttl == timedelta(hours=72)
    assert datetime.now(UTC)  # keep the import used
