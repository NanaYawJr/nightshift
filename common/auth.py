"""Service principal auth against the Fabric REST API.

Acquires an app-only token via MSAL client credentials and caches it in memory
until shortly before expiry. Every collector imports `get_token()` rather than
handling auth itself.
"""

import time

import msal

from common.config import settings

_cache: dict = {"token": None, "expires_at": 0.0}


def get_token() -> str:
    """Return a valid bearer token, refreshing if within 5 minutes of expiry."""
    if _cache["token"] and time.time() < _cache["expires_at"] - 300:
        return _cache["token"]

    app = msal.ConfidentialClientApplication(
        client_id=settings.fabric_client_id,
        client_credential=settings.fabric_client_secret,
        authority=f"https://login.microsoftonline.com/{settings.fabric_tenant_id}",
    )

    result = app.acquire_token_for_client(scopes=[settings.fabric_scope])

    if "access_token" not in result:
        raise RuntimeError(
            f"Token acquisition failed: {result.get('error')} — "
            f"{result.get('error_description')}"
        )

    _cache["token"] = result["access_token"]
    _cache["expires_at"] = time.time() + result.get("expires_in", 3600)
    return _cache["token"]


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_token()}"}
