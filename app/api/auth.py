"""API keys. With API_KEYS set, every endpoint except /health needs an `X-API-Key` header.

Unset (the laptop), nothing is checked. /docs shows an "Authorize" button for the key.
"""

import hmac

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings

_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="The API key we gave you.")


def require_api_key(
    key: str | None = Security(_header), settings: Settings = Depends(get_settings)
) -> None:
    keys = [k.get_secret_value() for k in settings.api_keys]
    if not keys:
        return
    # compare_digest: the time taken doesn't reveal how much of a guess was right.
    if key and any(hmac.compare_digest(key.encode(), k.encode()) for k in keys):
        return
    raise HTTPException(
        401,
        "Missing or invalid API key. Send it in the X-API-Key header.",
        headers={"WWW-Authenticate": "APIKey"},
    )
