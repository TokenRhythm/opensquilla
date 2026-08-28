"""Backward-compatible OpenRouter application attribution facade."""

from __future__ import annotations

from .app_attribution import (
    OPENSQUILLA_APP_REFERER,
    OPENSQUILLA_APP_TITLE,
    is_provider_app_host,
    provider_app_headers,
)

OPENROUTER_APP_REFERER = OPENSQUILLA_APP_REFERER
OPENROUTER_APP_TITLE = OPENSQUILLA_APP_TITLE


def is_openrouter_url(url: str | None) -> bool:
    """Return whether a URL points at OpenRouter's hosted API."""
    return is_provider_app_host(url, "openrouter.ai")


def openrouter_app_headers(url: str | None) -> dict[str, str]:
    """Return attribution headers only for real OpenRouter API URLs."""
    if not is_openrouter_url(url):
        return {}
    return provider_app_headers(url)
