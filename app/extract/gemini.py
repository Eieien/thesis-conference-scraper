import asyncio
import logging
import re
import time

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.extract.base import (
    ExtractorBusy,
    ExtractorNotConfigured,
    ExtractorQuotaUsed,
    ExtractorStop,
)
from app.schemas import ExtractedPage, TopicVerdict

log = logging.getLogger(__name__)

_BILLING_WORDS = re.compile(r"billing|payment|prepay|credit|permission|api key|api_key", re.I)


def stop_reason(e: errors.APIError) -> str | None:
    """Why no further Gemini call should be made, or None when a retry or fallback may help."""
    message = f"{e.status or ''} {e.message or ''}"
    if e.code == 429:
        return f"Gemini quota used up (429 {e.status}): {e.message}"
    if e.code in (401, 403) or (e.code == 400 and _BILLING_WORDS.search(message)):
        return f"Gemini refused the key ({e.code} {e.status}): {e.message}"
    return None


EXTRACT_PROMPT = """You extract facts about ONE academic conference from the text of its web pages.

Rules:
- Use only information stated in the text. Use null for anything not stated. Never guess.
- All dates as YYYY-MM-DD. If a deadline was extended, use the latest date.
- abstract_deadline = abstract submission/registration deadline. paper_deadline = full paper
  submission deadline. If only one submission deadline is given, put it in paper_deadline.
- fees: one item per registration price (each attendee category x early/regular/late/onsite tier).
  amount is a plain number. currency is an ISO 4217 code (USD, EUR, IDR, JPY, ...).
  Leave out add-ons (extra pages, banquet tickets, extra papers) unless they are the only prices;
  mention them in notes instead.
- Fee tables are written as "cell | cell" rows; the first row names the columns, and a column
  name like "IEEE Member / Onsite" joins several header levels. For each price cell, category =
  the row's labels (e.g. "Student, Non-member") plus the column's membership/mode, and tier =
  the column's period (early/regular/late/onsite) if any. Read every row of the table.
- A "-" or empty cell means that option is not offered: no fee item. "Free" or 0 is not a fee
  item either; mention it in notes.
- Only report a fee with a currency written on the page (a symbol such as $, ৳, ₹, RM, or a
  code). A number without any currency or fee context is not a fee.
- fees_published = false when the text says fees are not announced yet, or has no fee information.
- description: 1-3 plain sentences on what the conference covers, based on the text.
- The pages may include old editions of the conference. Only report data for the edition
  described below.
{hint}
Source URL: {url}

--- PAGE TEXT ---
{text}
"""

TOPIC_PROMPT = """Is this event a computer science or technology conference (computing, AI,
software, data, networks, security, electronics, telecommunications, robotics, information
systems, etc.)?
Answer is_cs=false for events mainly about medicine, business, law, education, social sciences or
pure natural sciences unless technology is the main focus.

{text}
"""

_FEE_WORDS = re.compile(r"registration fee|fees?\b|US\$|USD|EUR|€|\bRp\b|early bird", re.I)


class GeminiExtractor:
    def __init__(self, settings: Settings, *, always_quick: bool = False) -> None:
        if not settings.gemini_api_key:
            raise ExtractorNotConfigured("GEMINI_API_KEY is not set in .env")
        self.settings = settings
        # True when another extractor stands behind this one: never wait out a busy model.
        self.always_quick = always_quick
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        # Models whose free quota ran out in this process; they are not called again.
        self._spent_models: set[str] = set()
        # Models that were overloaded or hit a per-minute limit: rested until this time.
        self._rested_until: dict[str, float] = {}

    def _rotation(self) -> list[str]:
        """The main model, then the backups, minus the spent and the resting ones."""
        now = time.monotonic()
        models = dict.fromkeys([self.settings.gemini_model, *self.settings.gemini_backup_models])
        return [
            m for m in models if m not in self._spent_models and self._rested_until.get(m, 0) <= now
        ]

    async def _generate_any[T: BaseModel](self, prompt: str, schema: type[T]) -> T:
        """Ask the first model in the rotation that can answer. A busy model rests for a few
        minutes and one out of daily quota is dropped; the batch only stops when none is left."""
        rotation = self._rotation()
        if not rotation:
            raise ExtractorBusy("every Gemini model in the rotation is overloaded or out of quota")
        last: ExtractorStop | None = None
        for i, model in enumerate(rotation):
            has_next = i < len(rotation) - 1
            try:
                # With another model to try, don't sit through retries on this one.
                quick = has_next or self.always_quick
                return await self._generate(model, prompt, schema, quick=quick)
            except ExtractorQuotaUsed as e:
                self._spent_models.add(model)
                log.warning("%s is out of free quota; moving on", model)
                last = e
            except ExtractorBusy as e:
                self._rested_until[model] = time.monotonic() + e.rest_seconds
                log.warning("%s is busy; resting it %ss and moving on", model, e.rest_seconds)
                last = e
        assert last is not None
        raise last

    async def _generate[T: BaseModel](
        self, model: str, prompt: str, schema: type[T], *, quick: bool = False
    ) -> T:
        """One model, with retries. `quick` gives up on the first overload or rate limit, for
        when another model can take the call instead."""
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0,
        )
        for attempt in range(4):
            async with self._lock:
                wait = (
                    self._last_call + self.settings.gemini_min_interval_seconds - time.monotonic()
                )
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = time.monotonic()
            try:
                # A call cut off by sleep or a dropped connection can wait forever otherwise.
                resp = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=model, contents=prompt, config=config
                    ),
                    timeout=90,
                )
            except (httpx.TransportError, TimeoutError) as e:
                # A dropped or silent connection is how an overloaded service often looks.
                if quick:
                    raise ExtractorBusy(f"{model} did not answer: {e!r}", rest_seconds=120) from e
                if attempt < 3:
                    log.warning("Gemini connection problem (%s): %r; retrying", model, e)
                    await asyncio.sleep(15 * 2**attempt)
                    continue
                raise ExtractorBusy(f"{model} did not answer after retries: {e!r}") from e
            except errors.APIError as e:
                # A per-day quota won't come back in a minute, so don't wait for it.
                daily = e.code == 429 and "PerDay" in str(e)
                if e.code == 429 and not daily and quick:
                    raise ExtractorBusy(f"{model} hit a per-minute limit", rest_seconds=60) from e
                if e.code == 503 and quick:
                    raise ExtractorBusy(f"{model} is overloaded (503): {e.message}") from e
                if e.code in (429, 500, 503) and attempt < 3 and not daily:
                    backoff = 15 * 2**attempt
                    log.warning("Gemini %s (%s); retrying in %ss", e.code, model, backoff)
                    await asyncio.sleep(backoff)
                    continue
                if e.code == 503:
                    # Still overloaded after every retry: each further call would fail the same
                    # way, so stop the batch and let it be run again later.
                    raise ExtractorBusy(
                        f"Gemini is overloaded (503) for {model}: {e.message}"
                    ) from e
                if reason := stop_reason(e):
                    if e.code == 429:
                        raise ExtractorQuotaUsed(reason, model) from e
                    raise ExtractorStop(reason) from e
                raise
            if isinstance(resp.parsed, schema):
                return resp.parsed
            return schema.model_validate_json(resp.text or "")
        raise RuntimeError("unreachable")

    async def extract_conference(
        self, text: str, *, url: str | None = None, hint: str | None = None
    ) -> ExtractedPage:
        prompt = EXTRACT_PROMPT.format(
            hint=f"\nEdition to extract: {hint}" if hint else "",
            url=url or "unknown",
            text=text[: self.settings.gemini_max_input_chars],
        )
        fallback = self._usable_fallback()
        try:
            result = await self._generate_any(prompt, ExtractedPage)
        except (ValidationError, errors.APIError) as e:
            if not fallback:
                raise
            log.info("Primary model failed (%s); using %s", e, fallback)
            return await self._fallback(fallback, prompt, reraise=e)

        # Page clearly talks about fees but the small model returned none: escalate once.
        if fallback and not result.fees and _FEE_WORDS.search(text):
            better = await self._fallback(fallback, prompt)
            if better is not None and better.fees:
                return better
        return result

    def _usable_fallback(self) -> str | None:
        fallback = self.settings.gemini_fallback_model
        if not fallback or fallback in self._spent_models:
            return None
        return None if self._rested_until.get(fallback, 0) > time.monotonic() else fallback

    async def _fallback(
        self, model: str, prompt: str, reraise: Exception | None = None
    ) -> ExtractedPage | None:
        """Call the fallback model. If it is out of quota or busy, skip it (drop it, or rest it
        a few minutes) and carry on with the primary model's answer, or re-raise the primary's
        error when there is no answer. The fallback is optional, so it never stops a batch."""
        try:
            # quick: a busy fallback is skipped at once rather than waited for.
            return await self._generate(model, prompt, ExtractedPage, quick=True)
        except ExtractorQuotaUsed as e:
            self._spent_models.add(model)
            log.warning("Fallback model %s is out of quota; continuing without it", model)
            cause: Exception = e
        except ExtractorBusy as e:
            self._rested_until[model] = time.monotonic() + e.rest_seconds
            log.warning("Fallback model %s is busy; skipping it for %ss", model, e.rest_seconds)
            cause = e
        if reraise is not None:
            raise reraise from cause
        return None

    async def classify_topic(self, text: str) -> TopicVerdict:
        prompt = TOPIC_PROMPT.format(text=text[:4000])
        return await self._generate_any(prompt, TopicVerdict)
