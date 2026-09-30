import asyncio
import time

import pytest

from app.config import Settings
from app.extract.base import ExtractorBusy, ExtractorQuotaUsed
from app.extract.groq import FallbackExtractor, GroqExtractor, condense, strict_schema
from app.schemas import ExtractedPage


def test_condense_keeps_fee_and_deadline_lines():
    filler = "\n".join(f"Committee member number {i} from some university" for i in range(2000))
    text = (
        "Intro about the conference.\n"
        + filler
        + "\nRegistration fee: USD 350 for students\nPaper submission deadline: 1 Oct 2026\n"
        + filler
    )
    short = condense(text, 4000)
    assert len(short) <= 4000
    assert "USD 350" in short
    assert "1 Oct 2026" in short
    assert short.startswith("Intro about the conference.")
    assert condense("short text", 4000) == "short text"


def test_strict_schema_inlines_refs_and_requires_everything():
    schema = strict_schema(ExtractedPage)
    assert "$defs" not in str(schema) and "$ref" not in str(schema)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    fee = schema["properties"]["fees"]["items"]
    assert fee["additionalProperties"] is False
    assert "amount" in fee["required"]


def _page() -> ExtractedPage:
    return ExtractedPage.model_validate(
        {k: None for k in ExtractedPage.model_fields} | {"fees_published": False, "fees": []}
    )


class _Busy:
    async def extract_conference(self, text, *, url=None, hint=None):
        raise ExtractorBusy("every Gemini model is overloaded")


def test_fallback_asks_groq_when_gemini_is_busy(monkeypatch):
    groq = GroqExtractor(Settings(groq_api_key="x"))
    calls = []

    async def fake_generate(prompt, schema):
        calls.append(prompt)
        return _page()

    monkeypatch.setattr(groq, "_generate", fake_generate)
    result = asyncio.run(FallbackExtractor(_Busy(), groq).extract_conference("text"))
    assert result.fees == [] and len(calls) == 1


def test_groq_drops_a_model_out_of_daily_quota_and_uses_the_next(monkeypatch):
    groq = GroqExtractor(
        Settings(groq_api_key="x", groq_models=["a", "b"], groq_tokens_per_minute=10**9)
    )
    tried = []

    async def fake_call(model, prompt, schema):
        tried.append(model)
        if model == "a":
            raise ExtractorQuotaUsed("Rate limit reached ... tokens per day (TPD)", model)
        return _page()

    monkeypatch.setattr(groq, "_call", fake_call)
    asyncio.run(groq.extract_conference("text"))
    asyncio.run(groq.extract_conference("text"))
    assert tried == ["a", "b", "b"]  # "a" is not asked again

    async def spent(model, prompt, schema):
        raise ExtractorQuotaUsed("per day", model)

    monkeypatch.setattr(groq, "_call", spent)
    with pytest.raises(ExtractorQuotaUsed):
        asyncio.run(groq.extract_conference("text"))


def test_calls_spread_over_models_when_one_has_no_budget_left(monkeypatch):
    groq = GroqExtractor(
        Settings(groq_api_key="x", groq_models=["a", "b"], groq_tokens_per_minute=3000)
    )
    used = []

    async def fake_call(model, prompt, schema):
        used.append(model)
        return _page()

    monkeypatch.setattr(groq, "_call", fake_call)
    asyncio.run(groq.extract_conference("text"))  # ~2,000 tokens: "a" is now nearly full
    asyncio.run(groq.extract_conference("text"))  # goes to "b" instead of waiting a minute
    assert used == ["a", "b"]


def test_a_briefly_resting_model_is_waited_for_not_a_reason_to_stop(monkeypatch):
    # The real failure: "b" rested a few seconds for a per-minute limit while "a" ran out for the
    # day, and the whole batch stopped although "b" still had quota.
    groq = GroqExtractor(
        Settings(groq_api_key="x", groq_models=["a", "b"], groq_tokens_per_minute=10**9)
    )
    groq._rested_until["b"] = time.monotonic() + 0.2
    tried = []

    async def fake_call(model, prompt, schema):
        tried.append(model)
        if model == "a":
            raise ExtractorQuotaUsed("tokens per day (TPD)", model)
        return _page()

    monkeypatch.setattr(groq, "_call", fake_call)
    asyncio.run(groq.extract_conference("text"))
    assert tried == ["a", "b"]


def test_condense_keeps_a_whole_fee_table():
    filler = "\n".join(f"Committee member number {i} from some university" for i in range(2000))
    table = "Registration Fees\nCategory | Early | Late\n" + "\n".join(
        f"Group {i} | {100 + i} | {200 + i}" for i in range(12)
    )
    short = condense(filler + "\n" + table + "\n" + filler, 6000)
    assert "Group 11 | 111 | 211" in short  # rows without fee words survive with their table


def test_a_page_no_model_can_answer_fails_alone_after_one_pass(monkeypatch):
    # Real case: a long fee table overflowed the answer on every model, and the batch stopped.
    from app.extract.groq import UnusableAnswer

    groq = GroqExtractor(
        Settings(groq_api_key="x", groq_models=["a", "b"], groq_tokens_per_minute=10**9)
    )
    tried = []

    async def truncated(model, prompt, schema):
        tried.append(model)
        raise UnusableAnswer("max completion tokens reached")

    monkeypatch.setattr(groq, "_call", truncated)
    with pytest.raises(UnusableAnswer):  # not ExtractorStop: enrich skips only this page
        asyncio.run(groq.extract_conference("text"))
    assert tried == ["a", "b"]
    from app.extract.base import ExtractorStop

    assert not issubclass(UnusableAnswer, ExtractorStop)
