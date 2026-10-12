"""Shared redaction helpers for memory-derived text."""

from __future__ import annotations

from opensquilla.safety.secret_redaction import redact_secret_text


def redact_memory_text(text: str) -> str:
    return redact_secret_text(text)
