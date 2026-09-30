from typing import Protocol

from app.config import Settings
from app.schemas import ExtractedPage, TopicVerdict


class ExtractorNotConfigured(RuntimeError):
    pass


class ExtractorStop(RuntimeError):
    """The LLM refuses all further work: quota used up, or a billing/permission problem.

    Batch jobs stop on this instead of failing every remaining item, so nothing keeps calling an
    API that may start charging or has run out of free quota.
    """


class ExtractorBusy(ExtractorStop):
    """The LLM service is overloaded (HTTP 503) or rate-limited for now; try again later."""

    def __init__(self, message: str, rest_seconds: int = 300):
        super().__init__(message)
        self.rest_seconds = rest_seconds


class ExtractorQuotaUsed(ExtractorStop):
    """One model's quota is used up (HTTP 429). Other models may still have some."""

    def __init__(self, message: str, model: str):
        super().__init__(message)
        self.model = model


class Extractor(Protocol):
    async def extract_conference(
        self, text: str, *, url: str | None = None, hint: str | None = None
    ) -> ExtractedPage: ...

    async def classify_topic(self, text: str) -> TopicVerdict: ...


_instance: Extractor | None = None


def get_extractor(settings: Settings) -> Extractor:
    """Shared extractor so the Gemini rate limiter applies across all callers."""
    global _instance
    if _instance is None:
        from app.extract.gemini import GeminiExtractor

        if settings.groq_api_key:
            from app.extract.groq import FallbackExtractor, GroqExtractor

            # With Groq behind it, Gemini skips a busy model at once instead of retrying it.
            gemini = GeminiExtractor(settings, always_quick=True)
            _instance = FallbackExtractor(gemini, GroqExtractor(settings))
        else:
            _instance = GeminiExtractor(settings)
    return _instance
