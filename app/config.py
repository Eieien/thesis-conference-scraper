from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    database_url: str = f"sqlite:///{(DATA_DIR / 'scraper.db').as_posix()}"
    data_dir: Path = DATA_DIR

    # The continents the project works on. Other conferences stay in the database, but enrich
    # and export default to these, and the visualizer opens on them.
    scope_continents: list[str] = ["Asia"]

    # Conferences whose start date falls inside this window are kept.
    window_start: date = date(2026, 10, 1)
    window_end: date = date(2026, 11, 30)

    # HTTP politeness
    user_agent: str = "Mozilla/5.0 (compatible; ConferenceResearchBot/0.1)"
    request_delay_seconds: float = 3.0
    edas_delay_seconds: float = 4.0
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    cache_ttl_hours: int = 72
    respect_robots: bool = True
    max_concurrent_domains: int = 8

    # Gemini extraction
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash-lite"
    gemini_fallback_model: str | None = "gemini-2.5-flash"
    gemini_min_interval_seconds: float = 4.0  # ~15 requests/min, safe for the free tier
    # Tried in order when the main model is overloaded (503) or out of free quota (429).
    # Free-tier limits and load are per model, so another model usually still answers.
    gemini_backup_models: list[str] = []
    gemini_max_input_chars: int = 48_000

    # Groq (free plan): used only when every Gemini model is busy or out of quota. Free models
    # get 1,000 requests a day and 8,000 tokens a minute each, so pages are condensed first.
    groq_api_key: SecretStr | None = None
    groq_models: list[str] = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
    groq_tokens_per_minute: int = 7_500  # a little under the free plan's 8,000
    groq_max_input_chars: int = 14_000

    # EDAS (requires a logged-in session saved by scripts/edas_login.py)
    edas_base_url: str = "https://edas.info"
    edas_list_url: str = "https://edas.info/listConferencesRegister.php"
    edas_state_path: Path = DATA_DIR / "edas_state.json"
    # edas.info/robots.txt disallows everything outside /doc/ and /web/. The user chose to
    # scrape it anyway, so EDAS fetches skip the check by default (delays still apply).
    edas_respect_robots: bool = False
    # Optional: lets scripts/edas_login.py fill in the login form. Only that script reads these.
    edas_username: str | None = None
    edas_password: SecretStr | None = None

    # WikiCFP
    wikicfp_categories: list[str] = [
        "computer science",
        "artificial intelligence",
        "machine learning",
        "computer networks",
        "software engineering",
        "security",
        "data mining",
        "internet of things",
        "computer vision",
        "cloud computing",
        "robotics",
        "signal processing",
        # Added 2026-09-29 (97 new Asian conferences with the IEEE and EasyChair additions).
        "information technology",
        "big data",
        "wireless",
        "hci",
        "databases",
        "embedded systems",
        "blockchain",
        "multimedia",
        "information systems",
        "networking",
    ]
    wikicfp_max_pages: int = 25

    # EasyChair Smart CFP area ids (/cfp/area): 1 = Computing, 18 = Technology
    easychair_areas: list[int] = [1, 18, 2]  # Computing, Technology, Engineering

    # ConferenceIndex topic pages, read per Asian country (None = every Asian country page the
    # site has; [] = the worldwide page, which needs ~3x the detail requests).
    conferenceindex_disciplines: list[str] = ["computer-science"]
    conferenceindex_countries: list[str] | None = None

    # IEEE Conference Search, read through a real browser (see app/scrapers/ieee.py). Keep the
    # searches few and slow: IEEE rejects bots, and a block would also hit the user's own visits.
    ieee_regions: list[str] = ["Region10-Asia and Pacific", "Region08-Europe,Middle East,Africa"]
    # Exact names from IEEE's search facets; the CS/tech ones of its 16 subjects.
    ieee_fields_of_interest: list[str] = [
        "Computing and Processing",
        "Communication, Networking and Broadcast Technologies",
        "Signal Processing and Analysis",
        "Robotics and Control Systems",
        "Components, Circuits, Devices and Systems",
    ]
    ieee_delay_seconds: float = 10.0
    # Recovering sites that reject the crawler, in a real browser (app/pipeline/recover.py).
    recover_delay_seconds: float = 6.0

    # Homepage enrichment
    # Homepage plus up to 7 linked pages (registration, fees, important dates, ...).
    enrich_max_pages_per_site: int = 8
    # Conferences enriched at once. Each site keeps its own delay and LLM calls stay spaced by
    # gemini_min_interval_seconds, so this only overlaps the waiting.
    enrich_concurrency: int = 6

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def recon_dir(self) -> Path:
        return self.data_dir / "recon"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.cache_dir, self.recon_dir, self.export_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
