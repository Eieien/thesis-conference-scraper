from google.genai import errors

from app.extract.gemini import stop_reason


def _err(code: int, status: str, message: str) -> errors.APIError:
    return errors.APIError(code, {"error": {"code": code, "status": status, "message": message}})


def test_quota_and_billing_stop_the_run():
    assert stop_reason(_err(429, "RESOURCE_EXHAUSTED", "Quota exceeded for requests per day"))
    assert stop_reason(_err(403, "PERMISSION_DENIED", "API key not valid"))
    assert stop_reason(_err(400, "FAILED_PRECONDITION", "Billing account required"))


def test_ordinary_errors_do_not_stop():
    assert stop_reason(_err(400, "INVALID_ARGUMENT", "Request contains an invalid field")) is None
    assert stop_reason(_err(500, "INTERNAL", "Internal error")) is None


def test_spent_fallback_is_skipped_not_fatal(monkeypatch):
    import asyncio

    from app.config import Settings
    from app.extract.base import ExtractorQuotaUsed
    from app.extract.gemini import GeminiExtractor
    from app.schemas import ExtractedPage

    empty = dict.fromkeys(ExtractedPage.model_fields, None) | {"fees_published": False, "fees": []}
    calls: list[str] = []

    async def fake_generate(self, model, prompt, schema, *, quick=False):
        calls.append(model)
        if model == "fallback":
            raise ExtractorQuotaUsed("quota", model)
        return ExtractedPage(**empty)

    monkeypatch.setattr(GeminiExtractor, "_generate", fake_generate)
    ex = GeminiExtractor(
        Settings(gemini_api_key="x", gemini_model="primary", gemini_fallback_model="fallback")
    )
    # The page mentions fees and the primary found none, so the fallback is tried once...
    text = "Registration fee: see below"
    assert asyncio.run(ex.extract_conference(text)).fees == []
    assert asyncio.run(ex.extract_conference(text)).fees == []
    # ...and after its quota error it is never called again.
    assert calls == ["primary", "fallback", "primary"]


def test_persistent_overload_stops_the_batch(monkeypatch):
    import asyncio

    import pytest

    from app.config import Settings
    from app.extract.base import ExtractorBusy
    from app.extract.gemini import GeminiExtractor

    async def no_wait(_seconds):
        return None

    class Models:
        async def generate_content(self, **_):
            raise _err(503, "UNAVAILABLE", "This model is currently experiencing high demand.")

    monkeypatch.setattr("app.extract.gemini.asyncio.sleep", no_wait)
    ex = GeminiExtractor(Settings(gemini_api_key="x", gemini_min_interval_seconds=0))
    ex._client = type("C", (), {"aio": type("A", (), {"models": Models()})()})()
    with pytest.raises(ExtractorBusy):
        asyncio.run(ex.classify_topic("anything"))


def test_rotation_moves_past_busy_and_spent_models(monkeypatch):
    import asyncio

    from app.config import Settings
    from app.extract.base import ExtractorBusy, ExtractorQuotaUsed
    from app.extract.gemini import GeminiExtractor
    from app.schemas import TopicVerdict

    calls: list[str] = []

    async def fake_generate(self, model, prompt, schema, *, quick=False):
        calls.append(model)
        if model == "busy":
            raise ExtractorBusy("overloaded")
        if model == "spent":
            raise ExtractorQuotaUsed("quota", model)
        return TopicVerdict(is_cs=True, reason="ok")

    monkeypatch.setattr(GeminiExtractor, "_generate", fake_generate)
    ex = GeminiExtractor(
        Settings(gemini_api_key="x", gemini_model="busy", gemini_backup_models=["spent", "ok"])
    )
    assert asyncio.run(ex.classify_topic("a")).is_cs
    assert calls == ["busy", "spent", "ok"]
    # The busy one rests and the spent one is dropped, so the next call goes straight to "ok".
    calls.clear()
    asyncio.run(ex.classify_topic("b"))
    assert calls == ["ok"]


def test_rotation_stops_the_batch_when_every_model_is_out(monkeypatch):
    import asyncio

    import pytest

    from app.config import Settings
    from app.extract.base import ExtractorBusy, ExtractorStop
    from app.extract.gemini import GeminiExtractor

    async def always_busy(self, model, prompt, schema, *, quick=False):
        raise ExtractorBusy("overloaded")

    monkeypatch.setattr(GeminiExtractor, "_generate", always_busy)
    ex = GeminiExtractor(Settings(gemini_api_key="x", gemini_model="a", gemini_backup_models=["b"]))
    with pytest.raises(ExtractorStop):
        asyncio.run(ex.classify_topic("a"))
    with pytest.raises(ExtractorBusy, match="every Gemini model"):
        asyncio.run(ex.classify_topic("b"))  # both resting now: nothing to call


def test_dropped_connection_counts_as_busy(monkeypatch):
    import asyncio

    import httpx
    import pytest

    from app.config import Settings
    from app.extract.base import ExtractorBusy
    from app.extract.gemini import GeminiExtractor

    class Models:
        async def generate_content(self, **_):
            raise httpx.ReadError("")

    ex = GeminiExtractor(Settings(gemini_api_key="x", gemini_min_interval_seconds=0))
    ex._client = type("C", (), {"aio": type("A", (), {"models": Models()})()})()
    from app.schemas import TopicVerdict

    with pytest.raises(ExtractorBusy):
        asyncio.run(ex._generate("m", "p", TopicVerdict, quick=True))


def test_busy_fallback_is_skipped_not_fatal(monkeypatch):
    import asyncio

    from app.config import Settings
    from app.extract.base import ExtractorBusy
    from app.extract.gemini import GeminiExtractor
    from app.schemas import ExtractedPage

    empty = dict.fromkeys(ExtractedPage.model_fields, None) | {"fees_published": False, "fees": []}
    calls: list[str] = []

    async def fake_generate(self, model, prompt, schema, *, quick=False):
        calls.append(model)
        if model == "fallback":
            raise ExtractorBusy("overloaded")
        return ExtractedPage(**empty)

    monkeypatch.setattr(GeminiExtractor, "_generate", fake_generate)
    ex = GeminiExtractor(
        Settings(gemini_api_key="x", gemini_model="primary", gemini_fallback_model="fallback")
    )
    text = "Registration fee: see below"
    assert asyncio.run(ex.extract_conference(text)).fees == []  # no crash: primary's answer
    asyncio.run(ex.extract_conference(text))
    # The busy fallback rests, so the second conference doesn't try it again.
    assert calls == ["primary", "fallback", "primary"]
