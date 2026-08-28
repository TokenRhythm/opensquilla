"""Lightweight token-count estimates for tool-result projection telemetry."""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(0, len(text) // 4)
