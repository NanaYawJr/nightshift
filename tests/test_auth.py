"""Auth tests that need no network and no real credentials.

The fixture supplies dummy credentials on purpose. Without them these tests
pass on a machine that has a filled .env and fail on a fresh clone — the suite
would be testing the developer's environment rather than the code.
"""

import time

import pytest

from common import auth
from common.config import settings


class FakeApp:
    """Stands in for MSAL's ConfidentialClientApplication."""

    def __init__(self, **kwargs):
        pass

    def acquire_token_for_client(self, scopes):
        return {"access_token": f"token-for-{scopes[0]}", "expires_in": 3600}


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    """Each test starts with an empty cache, outside Fabric, with credentials."""
    auth._cache.clear()
    monkeypatch.setattr(auth, "in_fabric", lambda: False)

    # Dummy, never sent anywhere — the MSAL client is always faked below.
    monkeypatch.setattr(settings, "fabric_tenant_id", "tenant-test")
    monkeypatch.setattr(settings, "fabric_client_id", "client-test")
    monkeypatch.setattr(settings, "fabric_client_secret", "secret-test")

    yield
    auth._cache.clear()


# --- Caching ---------------------------------------------------------------


def test_cached_token_is_reused(monkeypatch):
    auth._cache[settings.fabric_scope] = {
        "token": "cached-value",
        "expires_at": time.time() + 3600,
    }

    def fail(*args, **kwargs):
        raise AssertionError("MSAL should not be called when cache is valid")

    monkeypatch.setattr(auth, "msal", type("M", (), {"ConfidentialClientApplication": fail}))

    assert auth.get_token() == "cached-value"


def test_expiring_token_is_refreshed(monkeypatch):
    auth._cache[settings.fabric_scope] = {
        "token": "stale-value",
        "expires_at": time.time() + 60,  # inside the 300s margin
    }

    monkeypatch.setattr(
        auth, "msal", type("M", (), {"ConfidentialClientApplication": FakeApp})
    )

    assert "stale-value" not in auth.get_token()


def test_scopes_do_not_share_a_token(monkeypatch):
    """A Fabric token must never be handed to a Power BI call, or vice versa."""
    monkeypatch.setattr(
        auth, "msal", type("M", (), {"ConfidentialClientApplication": FakeApp})
    )

    fabric = auth.get_token(settings.fabric_scope)
    powerbi = auth.get_token(settings.powerbi_scope)

    assert fabric != powerbi
    assert settings.fabric_scope in fabric
    assert settings.powerbi_scope in powerbi


def test_default_scope_is_fabric(monkeypatch):
    monkeypatch.setattr(
        auth, "msal", type("M", (), {"ConfidentialClientApplication": FakeApp})
    )

    assert settings.fabric_scope in auth.get_token()


def test_missing_msal_outside_fabric_is_a_clear_error(monkeypatch):
    """Rather than an AttributeError on None, which says nothing useful."""
    monkeypatch.setattr(auth, "msal", None)

    with pytest.raises(RuntimeError, match="not a Fabric notebook"):
        auth.get_token()


def test_blank_credentials_outside_fabric_name_themselves(monkeypatch):
    """Credentials are optional in config because Fabric does not need them.
    Locally, a blank one produces an authentication error that says nothing, so
    the check happens here instead and names what is missing."""
    monkeypatch.setattr(
        auth, "msal", type("M", (), {"ConfidentialClientApplication": FakeApp})
    )
    monkeypatch.setattr(settings, "fabric_client_secret", "")

    with pytest.raises(RuntimeError, match="FABRIC_CLIENT_SECRET"):
        auth.get_token()


# --- Fabric path -----------------------------------------------------------


class FakeCredentials:
    def __init__(self, log: list):
        self.log = log

    def getToken(self, audience):
        self.log.append(audience)
        return f"fabric-token-{audience}"


def test_in_fabric_uses_the_notebook_identity(monkeypatch):
    """No secret, no service principal. The whole reason this path exists."""
    log: list[str] = []

    fake_notebookutils = type("N", (), {"credentials": FakeCredentials(log)})
    monkeypatch.setitem(__import__("sys").modules, "notebookutils", fake_notebookutils)
    monkeypatch.setattr(auth, "in_fabric", lambda: True)

    token = auth.get_token()

    assert token == "fabric-token-pbi"
    assert log == ["pbi"]


def test_in_fabric_never_touches_msal(monkeypatch):
    """A Fabric runtime may not have msal installed at all."""
    log: list[str] = []

    fake_notebookutils = type("N", (), {"credentials": FakeCredentials(log)})
    monkeypatch.setitem(__import__("sys").modules, "notebookutils", fake_notebookutils)
    monkeypatch.setattr(auth, "in_fabric", lambda: True)
    monkeypatch.setattr(auth, "msal", None)

    assert auth.get_token() == "fabric-token-pbi"


def test_in_fabric_needs_no_credentials(monkeypatch):
    """The point of D-029: nothing to fill in, nothing to rotate."""
    log: list[str] = []

    fake_notebookutils = type("N", (), {"credentials": FakeCredentials(log)})
    monkeypatch.setitem(__import__("sys").modules, "notebookutils", fake_notebookutils)
    monkeypatch.setattr(auth, "in_fabric", lambda: True)
    monkeypatch.setattr(settings, "fabric_tenant_id", "")
    monkeypatch.setattr(settings, "fabric_client_id", "")
    monkeypatch.setattr(settings, "fabric_client_secret", "")

    assert auth.get_token() == "fabric-token-pbi"


def test_both_scopes_map_to_the_pbi_audience(monkeypatch):
    """Fabric and Power BI REST both accept it, so one token serves both."""
    log: list[str] = []

    fake_notebookutils = type("N", (), {"credentials": FakeCredentials(log)})
    monkeypatch.setitem(__import__("sys").modules, "notebookutils", fake_notebookutils)
    monkeypatch.setattr(auth, "in_fabric", lambda: True)

    auth.get_token(settings.fabric_scope)
    auth.get_token(settings.powerbi_scope)

    assert log == ["pbi", "pbi"]