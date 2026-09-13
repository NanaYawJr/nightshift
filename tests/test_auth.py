"""Auth tests that need no network and no real credentials."""

import time

from common import auth


def test_cached_token_is_reused(monkeypatch):
    auth._cache["token"] = "cached-value"
    auth._cache["expires_at"] = time.time() + 3600

    def fail(*args, **kwargs):
        raise AssertionError("MSAL should not be called when cache is valid")

    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", fail)
    assert auth.get_token() == "cached-value"


def test_expiring_token_is_refreshed(monkeypatch):
    auth._cache["token"] = "stale-value"
    auth._cache["expires_at"] = time.time() + 60  # inside the 300s margin

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def acquire_token_for_client(self, scopes):
            return {"access_token": "fresh-value", "expires_in": 3600}

    monkeypatch.setattr(auth.msal, "ConfidentialClientApplication", FakeApp)
    assert auth.get_token() == "fresh-value"
