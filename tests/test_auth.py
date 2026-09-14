"""Auth tests that need no network and no real credentials."""

import time

from common import auth
from common.config import settings


def setup_function():
    """Each test starts with an empty cache."""
    auth._cache.clear()


def test_cached_token_is_reused(monkeypatch):
    auth._cache[settings.fabric_scope] = {
        "token": "cached-value",
        "expires_at": time.time() + 3600,
    }

    def fail(*args, **kwargs):
        raise AssertionError("MSAL should not be called when cache is valid")

    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", fail)
    assert auth.get_token() == "cached-value"


def test_expiring_token_is_refreshed(monkeypatch):
    auth._cache[settings.fabric_scope] = {
        "token": "stale-value",
        "expires_at": time.time() + 60,  # inside the 300s margin
    }

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def acquire_token_for_client(self, scopes):
            return {"access_token": "fresh-value", "expires_in": 3600}

    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", FakeApp)
    assert auth.get_token() == "fresh-value"


def test_scopes_do_not_share_a_token(monkeypatch):
    """A Fabric token must never be handed to a Power BI call, or vice versa."""

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def acquire_token_for_client(self, scopes):
            return {"access_token": f"token-for-{scopes[0]}", "expires_in": 3600}

    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", FakeApp)

    fabric = auth.get_token(settings.fabric_scope)
    powerbi = auth.get_token(settings.powerbi_scope)

    assert fabric != powerbi
    assert settings.fabric_scope in fabric
    assert settings.powerbi_scope in powerbi


def test_default_scope_is_fabric(monkeypatch):
    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def acquire_token_for_client(self, scopes):
            return {"access_token": f"token-for-{scopes[0]}", "expires_in": 3600}

    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", FakeApp)
    assert settings.fabric_scope in auth.get_token()