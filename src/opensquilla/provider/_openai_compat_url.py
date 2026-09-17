"""Internal URL helpers for OpenAI-compatible providers."""

from __future__ import annotations

import re

# Some OpenAI-compatible API roots carry a non-integer version segment before
# an adapter namespace.  Gemini's documented compatibility root is
# ``/v1beta/openai``: appending our canonical ``/v1`` again produces the
# nonexistent ``/v1beta/openai/v1/chat/completions`` endpoint.  Treat these
# roots exactly like the existing ``/v1`` ... ``/vN`` forms.
_VERSIONED_BASE_URL_RE = re.compile(
    r"/v\d+(?:(?:alpha|beta)\d*)?(?:/openai)?$",
)


def _versioned_api_url(base_url: str, path: str) -> str:
    """Join a canonical ``/v1/...`` path to an API root without duplication."""

    base = base_url.rstrip("/")
    if path.startswith("/v1/") and _VERSIONED_BASE_URL_RE.search(base):
        return f"{base}{path[3:]}"
    return f"{base}{path}"
