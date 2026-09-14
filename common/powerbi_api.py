"""Thin wrapper over the Power BI REST API.

Separate from `fabric_api` because the two differ in ways that would make one
client messy: different host, different token audience, and different paging.
Fabric pages with a continuationToken; Power BI list endpoints mostly return
everything at once, with $top where a limit is needed.
"""

import time
from typing import Any

import requests

from common.auth import auth_headers
from common.config import settings

TIMEOUT = 60
MAX_RETRIES = 4


def _request(method: str, path: str, **kwargs) -> requests.Response:
    url = path if path.startswith("http") else f"{settings.powerbi_api_base}{path}"
    headers = auth_headers(settings.powerbi_scope)

    for attempt in range(MAX_RETRIES):
        response = requests.request(
            method, url, headers=headers, timeout=TIMEOUT, **kwargs
        )

        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", 2**attempt))
            time.sleep(wait)
            continue

        response.raise_for_status()
        return response

    raise RuntimeError(f"Throttled after {MAX_RETRIES} attempts: {url}")


def get(path: str, **kwargs) -> dict[str, Any]:
    return _request("GET", path, **kwargs).json()


def get_list(path: str, **kwargs) -> list[dict[str, Any]]:
    """List endpoints wrap their results in a `value` array."""
    return get(path, **kwargs).get("value", [])