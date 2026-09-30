from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import ClassVar

from app.config import Settings
from app.schemas import ListingData
from app.scrapers.http import Fetcher


@dataclass
class BrowsePage:
    link: str  # the listing page that was fetched
    listings: list[ListingData]
    has_next: bool


class SourceAdapter(ABC):
    """One conference source. Subclasses yield ListingData from the source's listing pages."""

    name: ClassVar[str]
    label: ClassVar[str]
    implemented: ClassVar[bool] = True
    requires_login: ClassVar[bool] = False
    has_details: ClassVar[bool] = False
    notes: ClassVar[str] = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def make_fetcher(self) -> Fetcher:
        return Fetcher(self.settings)

    @abstractmethod
    def collect(
        self, fetcher: Fetcher, *, max_pages: int | None = None
    ) -> AsyncIterator[ListingData]:
        """Yield listings from the source's index pages."""

    async def fetch_detail(self, fetcher: Fetcher, listing: ListingData) -> ListingData:
        """Fill in fields only available on the per-conference page. Default: no-op."""
        return listing

    async def browse_page(
        self, fetcher: Fetcher, *, page: int = 1, category: str | None = None
    ) -> BrowsePage:
        """Fetch one listing page only (no detail pages). Used to check names quickly."""
        raise NotImplementedError(f"Browsing isn't supported for {self.label} yet.")


class NotImplementedAdapter(SourceAdapter):
    """Placeholder for a source whose markup hasn't been inspected yet."""

    implemented = False

    async def collect(self, fetcher: Fetcher, *, max_pages: int | None = None):
        raise NotImplementedError(
            f"The {self.label} adapter isn't built yet. {self.notes} "
            "Use POST /tools/fetch or /tools/render to save sample pages to data/recon/ first."
        )
        yield  # pragma: no cover  (makes this an async generator)
