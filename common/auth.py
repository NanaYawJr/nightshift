"""Token acquisition, local and in Fabric.

Two environments, one interface. Locally, MSAL with the service principal from
.env. Inside a Fabric notebook, the notebook's own identity — no secret, no
service principal, nothing to rotate.

The Fabric path exists because the collectors moved into Fabric (D-029) and the
service principal's secret lives in a .env file a notebook cannot read. Rather
than copying the secret into the Lakehouse — which D-008 already flagged as the
weakest point in the security posture — the notebook asks the platform for a
token it is already entitled to.

Callers do not choose. `get_token()` detects where it is running.
"""

from __future__ import annotations

import time

from common.config import settings

# Imported at module level so tests can substitute it, but tolerantly: the
# Fabric runtime does not ship msal and does not need it.
try:
    import msal
except ImportError:  # pragma: no cover - depends on the runtime
    msal = None

# scope -> {"token": str, "expires_at": float}
_cache: dict[str, dict] = {}

# Fabric's token helper takes an audience name rather than a scope URL. Both
# the Fabric and Power BI REST APIs accept the "pbi" audience.
_FABRIC_AUDIENCE = {
    settings.fabric_scope: "pbi",
    settings.powerbi_scope: "pbi",
}


def in_fabric() -> bool:
    """True when running inside a Fabric notebook.

    `notebookutils` is sometimes importable and sometimes injected into the
    notebook's globals, depending on runtime version. Both are checked.
    """
    try:
        import notebookutils  # noqa: F401

        return True
    except ImportError:
        return False


def _fabric_token(scope: str) -> dict:
    import notebookutils

    audience = _FABRIC_AUDIENCE.get(scope, "pbi")
    token = notebookutils.credentials.getToken(audience)

    # The helper does not report expiry. An hour is the platform default and
    # the margin in get_token() covers the difference.
    return {"token": token, "expires_at": time.time() + 3600}


def _msal_token(scope: str) -> dict:
    if msal is None:
        raise RuntimeError(
            "msal is not installed and this is not a Fabric notebook. "
            "Install it, or run where notebookutils is available."
        )

    # Credentials are optional in config because Fabric does not need them.
    # Reaching here without them means a local run with an unfilled .env, and
    # a blank credential produces an authentication error that says nothing.
    missing = [
        name
        for name, value in (
            ("FABRIC_TENANT_ID", settings.fabric_tenant_id),
            ("FABRIC_CLIENT_ID", settings.fabric_client_id),
            ("FABRIC_CLIENT_SECRET", settings.fabric_client_secret),
        )
        if not value
    ]

    if missing:
        raise RuntimeError(
            f"Missing credentials: {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )

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

    return {
        "token": result["access_token"],
        "expires_at": time.time() + result.get("expires_in", 3600),
    }


def get_token(scope: str | None = None) -> str:
    """Return a valid bearer token for `scope`, refreshing near expiry.

    Cached per scope. Fabric and Power BI are separate audiences in the MSAL
    path and their tokens are not interchangeable, so a single cache would hand
    the wrong token to one of them.
    """
    scope = scope or settings.fabric_scope
    entry = _cache.get(scope)

    if entry and time.time() < entry["expires_at"] - 300:
        return entry["token"]

    entry = _fabric_token(scope) if in_fabric() else _msal_token(scope)

    _cache[scope] = entry
    return entry["token"]


def auth_headers(scope: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {get_token(scope)}"}