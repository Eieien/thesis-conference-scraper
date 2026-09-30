"""Currency conversion to US dollars and Philippine pesos.

Rates come from ExchangeRate-API's free open-access endpoint (no key; the terms ask for an
attribution link wherever converted prices are shown). They are fetched at most once a day and
cached in data/fx_rates.json, so reading a conversion never touches the network. When no rates
are available yet, conversions return None rather than a guess.
"""

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.pipeline.normalize import normalize_currency

log = logging.getLogger(__name__)

RATES_URL = "https://open.er-api.com/v6/latest/USD"
ATTRIBUTION = {"name": "ExchangeRate-API", "url": "https://www.exchangerate-api.com"}
MAX_AGE_SECONDS = 24 * 3600
TARGETS = ("USD", "PHP")

_memo: dict[str, object] = {"mtime": None, "data": None}


def _path(settings: Settings) -> Path:
    return settings.data_dir / "fx_rates.json"


def load_rates(settings: Settings) -> dict | None:
    """The cached rates file ({"rates": {code: units per USD}, "date": ..., ...}) or None."""
    path = _path(settings)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    if _memo["mtime"] != mtime:
        _memo["data"] = json.loads(path.read_text(encoding="utf-8"))
        _memo["mtime"] = mtime
    return _memo["data"]  # type: ignore[return-value]


def rates_are_stale(settings: Settings) -> bool:
    path = _path(settings)
    return not path.exists() or time.time() - path.stat().st_mtime > MAX_AGE_SECONDS


async def refresh_rates(settings: Settings, *, force: bool = False) -> dict | None:
    """Fetch today's rates if the cache is older than a day (or `force`). Returns the rates."""
    if not force and not rates_are_stale(settings):
        return load_rates(settings)
    from app.scrapers.http import Fetcher, FetchError  # late: http imports models

    try:
        async with Fetcher(settings) as fetcher:
            result = await fetcher.get(RATES_URL, use_cache=False)
        body = json.loads(result.text)
        if body.get("result") != "success" or "PHP" not in body.get("rates", {}):
            raise ValueError(f"unexpected response: {str(body)[:200]}")
    except (FetchError, ValueError) as e:
        log.warning("could not refresh exchange rates: %s", e)
        return load_rates(settings)
    data = {
        "base": "USD",
        "rates": body["rates"],
        "date": body.get("time_last_update_utc"),
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": ATTRIBUTION,
    }
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def convert(
    amount: float | None, currency: str | None, to: str, settings: Settings
) -> float | None:
    """`amount` in `currency` expressed in `to` (USD or PHP), rounded to 2 decimals."""
    if amount is None or not currency:
        return None
    data = load_rates(settings)
    if not data:
        return None
    rates: dict[str, float] = data["rates"]
    code = normalize_currency(currency) or currency.upper()
    if code not in rates or to not in rates or not rates[code]:
        return None
    return round(amount / rates[code] * rates[to], 2)


def rates_info(settings: Settings) -> dict | None:
    """What the UI shows next to converted prices: date and attribution."""
    data = load_rates(settings)
    if not data:
        return None
    return {"date": data.get("date"), "fetched_at": data.get("fetched_at"), **ATTRIBUTION}
