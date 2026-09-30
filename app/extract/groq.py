"""Groq (free plan) as a fallback extractor for when every Gemini model is busy or out of quota.

Groq's free plan gives each model 1,000 requests a day and only 8,000 tokens a minute (prompt and
answer together), so a call gets a condensed version of the page text (`condense`), and a
per-model token budget spaces the calls out. A 429 for the minute rests that model until Groq
says to retry; one for the day drops it for the process. When every model is dropped the batch
stops, like Gemini's quota.

The key is `GROQ_API_KEY` in .env (a SecretStr; never logged). The response headers of the free
plan show `x-ratelimit-limit-tokens: 8000`; a paid plan would show far more.
"""

import asyncio
import json
import logging
import re
import time
from collections import deque
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.extract.base import (
    ExtractorBusy,
    ExtractorNotConfigured,
    ExtractorQuotaUsed,
    ExtractorStop,
)
from app.extract.gemini import EXTRACT_PROMPT, TOPIC_PROMPT
from app.schemas import ExtractedPage, TopicVerdict

log = logging.getLogger(__name__)

API_URL = "https://api.groq.com/openai/v1/chat/completions"
# Room for the JSON answer and a little reasoning: a page with 24 fees needs about 2,000.
_ANSWER_TOKENS = 3000

# Lines worth keeping when a page is too long for one call.
_USEFUL = re.compile(
    r"fee|registration|register|early[- ]?bird|price|cost|USD|US\$|NT\$|\$|EUR|€|£|¥|₹|৳|₱|₩|฿|"
    # No "member"/"student" here: committee lists would crowd out the fee table, which is kept
    # whole anyway once one of its rows shows a price.
    r"RMB|CNY|IDR|INR|JPY|KRW|MYR|THB|SGD|PHP|TWD|BDT|PKR|\bRM\b|\bRp\b|"
    r"deadline|submission|abstract|notification|camera|"
    r"important dates|venue|location|held|20\d\d",
    re.I,
)


def condense(text: str, limit: int) -> str:
    """At most `limit` characters of `text`: the start (for the description), the section
    headers, and the lines about fees, deadlines and dates with a line of context either side.
    A table (a run of "a | b" lines) is kept whole when any of its lines matters, so a fee
    table never arrives with only its first row."""
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    keep: set[int] = set()
    i = 0
    while i < len(lines):
        if "|" in lines[i]:
            end = i
            while end + 1 < len(lines) and "|" in lines[end + 1]:
                end += 1
            # The table, or its title just above ("Registration Fees"), mentions something useful.
            if any(_USEFUL.search(lines[k]) for k in range(max(0, i - 2), end + 1)):
                keep.update(range(max(0, i - 2), end + 1))  # with the table's title
            i = end + 1
            continue
        if lines[i].startswith("### "):
            keep.add(i)
        elif _USEFUL.search(lines[i]):
            keep.update(range(max(0, i - 1), min(len(lines), i + 2)))
        i += 1
    head = text[: limit // 8]
    out, size = [head], len(head)
    for i in sorted(keep):
        line = lines[i]
        if size + len(line) + 1 > limit:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema in the form strict mode accepts: references inlined, every
    property required, no extra properties, no titles."""
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, list):
            return [fix(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return fix(defs[node["$ref"].split("/")[-1]])
        node = {k: fix(v) for k, v in node.items() if k not in ("title", "default", "$defs")}
        if node.get("type") == "object":
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        return node

    return fix(raw)


class UnusableAnswer(RuntimeError):
    """Every model answered, but none produced JSON matching the schema for this page (a very
    long fee table can overflow the answer). Fails this one conference, not the whole batch."""


class GroqExtractor:
    def __init__(self, settings: Settings) -> None:
        if not settings.groq_api_key:
            raise ExtractorNotConfigured("GROQ_API_KEY is not set in .env")
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=120)
        self._lock = asyncio.Lock()
        self._used: dict[str, deque[tuple[float, int]]] = {}  # model -> (time, tokens)
        self._spent: set[str] = set()
        self._rested_until: dict[str, float] = {}

    def _rotation(self) -> list[str]:
        now = time.monotonic()
        return [
            m
            for m in self.settings.groq_models
            if m not in self._spent and self._rested_until.get(m, 0) <= now
        ]

    async def _reserve(self, models: list[str], tokens: int) -> str:
        """The first of `models` with room for `tokens` more in its budget for the last minute,
        waiting for the one that frees up soonest when none has. Each model has its own budget,
        so spreading calls over all of them is what makes Groq fast enough."""
        budget = self.settings.groq_tokens_per_minute
        async with self._lock:
            while True:
                now = time.monotonic()
                soonest = 60.0
                for model in models:
                    used = self._used.setdefault(model, deque())
                    while used and now - used[0][0] > 60:
                        used.popleft()
                    if not used or sum(t for _, t in used) + tokens <= budget:
                        used.append((now, tokens))
                        return model
                    soonest = min(soonest, 60 - (now - used[0][0]))
                await asyncio.sleep(soonest + 0.5)

    async def _generate[T: BaseModel](self, prompt: str, schema: type[T]) -> T:
        # Rough count: about 3.5 characters a token, plus the answer.
        tokens = len(prompt) * 2 // 7 + _ANSWER_TOKENS
        last: Exception | None = None
        # A model resting for a per-minute limit is waited for: only a model out of its daily
        # quota is gone for good, and the batch stops only when every model is. (Raising while
        # one model merely rested for seconds stopped a whole run once.)
        for _round in range(4):
            if all(m in self._spent for m in self.settings.groq_models):
                raise ExtractorQuotaUsed("every Groq model is out of its daily quota", "groq")
            untried = self._rotation()
            if not untried:
                now = time.monotonic()
                wake = min(
                    self._rested_until.get(m, now)
                    for m in self.settings.groq_models
                    if m not in self._spent
                )
                await asyncio.sleep(min(max(wake - now, 0) + 0.5, 300))
                continue
            only_unusable = True  # this round, every model answered but none usefully
            while untried:
                model = await self._reserve(untried, tokens)
                untried.remove(model)
                try:
                    return await self._call(model, prompt, schema)
                except ExtractorQuotaUsed as e:
                    self._spent.add(model)
                    log.warning("Groq %s is out of its daily quota; moving on", model)
                    last, only_unusable = e, False
                except ExtractorBusy as e:
                    self._rested_until[model] = time.monotonic() + e.rest_seconds
                    log.warning("Groq %s is busy; resting it %ss", model, e.rest_seconds)
                    last, only_unusable = e, False
                except (ValidationError, json.JSONDecodeError) as e:
                    log.warning("Groq %s gave an unusable answer (%s); trying the next", model, e)
                    last = UnusableAnswer(f"Groq {model} gave an unusable answer: {e}")
                except UnusableAnswer as e:
                    log.warning("Groq %s: %s; trying the next", model, e)
                    last = e
            if only_unusable and isinstance(last, UnusableAnswer):
                raise last  # asking again won't help this page
        if all(m in self._spent for m in self.settings.groq_models):
            raise ExtractorQuotaUsed("every Groq model is out of its daily quota", "groq")
        raise last or ExtractorBusy("every Groq model is resting", rest_seconds=60)

    async def _call[T: BaseModel](self, model: str, prompt: str, schema: type[T]) -> T:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_completion_tokens": _ANSWER_TOKENS,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": strict_schema(schema),
                },
            },
        }
        if model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "low"  # reasoning tokens count against the minute budget
        key = self.settings.groq_api_key.get_secret_value()
        try:
            resp = await self._client.post(
                API_URL, json=body, headers={"Authorization": f"Bearer {key}"}
            )
        except httpx.TransportError as e:
            raise ExtractorBusy(f"Groq {model} did not answer: {e!r}", rest_seconds=120) from e
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"] or ""
            return schema.model_validate_json(content)

        message = _error_message(resp)
        if resp.status_code == 429:
            if re.search(r"per day|\(RPD\)|\(TPD\)", message, re.I):
                raise ExtractorQuotaUsed(f"Groq quota used up for {model}: {message}", model)
            retry = float(resp.headers.get("retry-after") or 30)
            raise ExtractorBusy(f"Groq {model} hit a per-minute limit", rest_seconds=int(retry) + 1)
        if resp.status_code in (401, 403):
            raise ExtractorStop(f"Groq refused the key ({resp.status_code}): {message}")
        if resp.status_code == 413:  # the request alone is over the minute budget
            raise ExtractorBusy(f"Groq {model}: request too large ({message})", rest_seconds=0)
        if resp.status_code >= 500 or resp.status_code == 498:  # 498: flex capacity
            raise ExtractorBusy(f"Groq {model} is unavailable ({resp.status_code})")
        if resp.status_code == 400 and "json" in message.lower():
            # The model could not produce JSON matching the schema; another model may.
            raise UnusableAnswer(f"Groq {model} could not follow the schema: {message}")
        raise ExtractorStop(f"Groq error {resp.status_code} for {model}: {message}")

    async def extract_conference(
        self, text: str, *, url: str | None = None, hint: str | None = None
    ) -> ExtractedPage:
        prompt = EXTRACT_PROMPT.format(
            hint=f"\nEdition to extract: {hint}" if hint else "",
            url=url or "unknown",
            text=condense(text, self.settings.groq_max_input_chars),
        )
        return await self._generate(prompt, ExtractedPage)

    async def classify_topic(self, text: str) -> TopicVerdict:
        return await self._generate(TOPIC_PROMPT.format(text=text[:4000]), TopicVerdict)


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", resp.text))[:300]
    except ValueError:
        return resp.text[:300]


class FallbackExtractor:
    """Gemini first; Groq when Gemini fails for any reason (busy, out of quota, a server error
    or an answer that doesn't fit the schema). A batch only stops when Groq stops too."""

    def __init__(self, primary: Any, backup: GroqExtractor) -> None:
        self.primary, self.backup = primary, backup

    async def extract_conference(
        self, text: str, *, url: str | None = None, hint: str | None = None
    ) -> ExtractedPage:
        try:
            return await self.primary.extract_conference(text, url=url, hint=hint)
        except Exception as e:
            log.info("Gemini failed (%s); asking Groq", str(e)[:120])
            return await self.backup.extract_conference(text, url=url, hint=hint)

    async def classify_topic(self, text: str) -> TopicVerdict:
        try:
            return await self.primary.classify_topic(text)
        except Exception:
            return await self.backup.classify_topic(text)
