"""The one place a model is called.

Every provider worth using on a free tier speaks the OpenAI chat-completions
shape, so there is one function here rather than an SDK per provider. Swapping
provider is two settings — `model_base_url` and `model_name` — and nothing else
in the project changes.

Deliberately thin. No retries on anything except rate limiting, no streaming, no
tool-calling protocol. The diagnostician builds its own evidence pack before it
calls, so the model is asked exactly once per incident and the loop stays
inspectable. Tool-calling is in ARCHITECTURE.md as the next step; it is not here
yet and the docs overstate what exists.

Rate limiting is expected, not exceptional. Free tiers are the whole point of
the cost constraint, and `ModelUnavailable` is what the deterministic fallback
catches.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from common.config import settings

# Free tiers answer slowly under load. Long enough to ride that out, short
# enough that a scheduled run cannot hang for the whole hour.
TIMEOUT_SECONDS = 90

# One retry only. A second 429 means the window is genuinely exhausted and the
# fallback is the right answer, not a longer sleep inside a notebook cell.
RATE_LIMIT_PAUSE_SECONDS = 20


class ModelUnavailable(RuntimeError):
    """The model could not be reached, or refused. Caller falls back."""


def complete(prompt: str, system: str = "", temperature: float = 0.0) -> str:
    """Send one prompt, return the text of the reply.

    Temperature defaults to zero because this is a diagnostic tool and the same
    evidence should produce the same account twice. Variability here would make
    the benchmark unrepeatable and the agent untrustworthy for no gain.
    """
    if not settings.model_api_key:
        raise ModelUnavailable(
            "MODEL_API_KEY is not set. Add it to .env for a local run, or to the "
            "notebook's environment for a Fabric run."
        )

    if not settings.model_name:
        raise ModelUnavailable("MODEL_NAME is not set — see .env.example.")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": settings.model_name,
        "messages": messages,
        "temperature": temperature,
    }

    try:
        return _post(payload)
    except _RateLimited:
        time.sleep(RATE_LIMIT_PAUSE_SECONDS)

        try:
            return _post(payload)
        except _RateLimited as exc:
            raise ModelUnavailable(
                "rate limited twice — free-tier window is exhausted"
            ) from exc


class _RateLimited(Exception):
    """Internal: a 429. Separate from ModelUnavailable so `complete` can retry
    once without the caller's fallback being triggered prematurely."""


def _post(payload: dict) -> str:
    request = urllib.request.Request(
        f"{settings.model_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {settings.model_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]

        if exc.code == 429:
            raise _RateLimited(detail) from exc

        # The provider's own words, not a summary. A wrong model name comes
        # back as a 400 or 404 naming the models that do exist, and that
        # message is far more useful than anything written here could be.
        raise ModelUnavailable(f"HTTP {exc.code} from model API: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ModelUnavailable(f"could not reach model API: {exc.reason}") from exc

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ModelUnavailable(f"unexpected response shape: {body}") from exc