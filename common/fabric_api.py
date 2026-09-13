"""Thin wrapper over the Fabric REST API.

Handles auth, pagination and throttling in one place so collectors stay
readable. Fabric paginates with a continuationToken and throttles with 429 plus
a Retry-After header; both are handled here rather than in every caller.
"""

import time
from typing import Any, Iterator

import requests

from common.auth import auth_headers
from common.config import settings

TIMEOUT = 60
MAX_RETRIES = 4


def _request(method: str, path: str, **kwargs) -> requests.Response:
    url = path if path.startswith("http") else f"{settings.fabric_api_base}{path}"

    for attempt in range(MAX_RETRIES):
        response = requests.request(
            method, url, headers=auth_headers(), timeout=TIMEOUT, **kwargs
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


def get_paged(path: str, key: str = "value") -> Iterator[dict[str, Any]]:
    """Yield every item across all pages of a list endpoint."""
    token: str | None = None

    while True:
        params = {"continuationToken": token} if token else None
        payload = _request("GET", path, params=params).json()

        yield from payload.get(key, [])

        token = payload.get("continuationToken")
        if not token:
            return
