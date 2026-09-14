"""Service principal auth for the Fabric and Power BI REST APIs.

Acquires app-only tokens via MSAL client credentials, cached per scope until
shortly before expiry. Two scopes are in play: the Fabric API and the Power BI
API. They are separate audiences and their tokens are not interchangeable, so
the cache is keyed by scope rather than holding a single token.
"""

import time

import msal

from common.config import settings

# scope -> {"token": str, "expires_at": float}
_cache: dict[str, dict] = {}


def get_token(scope: str | None = None) -> str:
    """Return a valid bearer token for `scope`, refreshing near expiry.

    Defaults to the Fabric API scope so existing callers need no change.
    """
    scope = scope or settings.fabric_scope
    entry = _cache.get(scope)

    if entry and time.time() < entry["expires_at"] - 300:
        return entry["token"]

    app = msal.ConfidentialClientApplication(
        client_id=settings.fabric_client_id,
        client_credential=settings.fabric_client_secret,
        authority=f"https://login.microsoftonline.com/{settings.fabric_tenant_id}",
    )

    result = app.acquire_token_for_client(scopes=[scope])

    if "access_token" not in result:
        raise RuntimeError(
            f"Token acquisition failed for {scope}: {result.get('error')} — "
            f"{result.get('error_description')}"
        )

    _cache[scope] = {
        "token": result["access_token"],
        "expires_at": time.time() + result.get("expires_in", 3600),
    }
    return result["access_token"]


def auth_headers(scope: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {get_token(scope)}"}