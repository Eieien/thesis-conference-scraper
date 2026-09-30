from app.scrapers.base import SourceAdapter
from app.scrapers.conferenceindex import ConferenceIndexAdapter
from app.scrapers.easychair import EasyChairAdapter
from app.scrapers.edas import EdasAdapter
from app.scrapers.ieee import IeeeAdapter
from app.scrapers.ieeecs import IeeeCsAdapter
from app.scrapers.pending import (
    AcmAdapter,
    SpringerAdapter,
)
from app.scrapers.wikicfp import WikiCfpAdapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    cls.name: cls
    for cls in (
        EdasAdapter,
        WikiCfpAdapter,
        IeeeAdapter,
        AcmAdapter,
        EasyChairAdapter,
        IeeeCsAdapter,
        SpringerAdapter,
        ConferenceIndexAdapter,
    )
}

# When sources disagree on a field, the earlier source wins (homepage data overrides all).
SOURCE_PRIORITY = [
    "edas",
    "ieee",
    "ieeecs",
    "acm",
    "springer",
    "wikicfp",
    "easychair",
    "conferenceindex",
]


def get_adapter(name: str) -> type[SourceAdapter]:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise KeyError(f"Unknown source '{name}'. Known: {', '.join(ADAPTERS)}") from None
