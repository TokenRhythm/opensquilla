"""Compatibility exports for bounded token estimation.

The implementation lives at the package root so provider admission can reuse
it without introducing a provider → session architecture edge.
"""

from __future__ import annotations

from opensquilla import token_estimation as _shared

TokenEstimateSource = _shared.TokenEstimateSource


def estimate_tokens_with_source(text: str) -> tuple[int, TokenEstimateSource]:
    """Return the shared bounded token estimate through the legacy module."""

    return _shared.estimate_tokens_with_source(text)


def estimate_tokens(text: str) -> int:
    """Estimate token count while keeping the historical integer-only API."""

    return estimate_tokens_with_source(text)[0]
