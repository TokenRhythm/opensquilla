"""Shared helpers for hidden meta preflight confirmation protocol text."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

PREFLIGHT_CONFIRMED_RE = re.compile(
    r"\s*<!--\s*opensquilla:meta_preflight_confirmed=1\s*-->\s*",
    re.IGNORECASE,
)
PREFLIGHT_RUN_ID_RE = re.compile(
    r"\s*<!--\s*opensquilla:meta_preflight_run_id=([A-Za-z0-9:_-]+)\s*-->\s*",
    re.IGNORECASE,
)
PREFLIGHT_FIELDS_RE = re.compile(
    r"\s*<!--\s*opensquilla:meta_preflight_fields=([A-Za-z0-9_\-=]+)\s*-->\s*",
    re.IGNORECASE,
)
_PREFLIGHT_CONFIRMED_FIELDS_BLOCK_RE = re.compile(
    r"(?:\r?\n){0,2}Confirmed request fields:[ \t]*(?:\r?\n[ \t]*-[^\r\n]*)*",
    re.IGNORECASE,
)
_PREFLIGHT_ANY_MARKER_RE = re.compile(
    r"\s*<!--\s*opensquilla:meta_preflight_[^>]*-->\s*",
    re.IGNORECASE,
)


def decode_preflight_fields(value: str) -> dict[str, Any]:
    if not value or len(value) > 16_384:
        return {}
    padded = value + ("=" * (-len(value) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): item
        for key, item in payload.items()
        if key is not None and str(key).strip()
    }


def display_text_from_preflight_confirmation(user_message: str) -> str | None:
    """Return a user-visible text for hidden meta preflight confirmations."""

    if not isinstance(user_message, str) or not PREFLIGHT_CONFIRMED_RE.search(
        user_message
    ):
        return None
    return strip_preflight_confirmation_protocol_text(user_message)


def strip_preflight_confirmation_protocol_text(text: str) -> str | None:
    """Remove meta preflight protocol blocks from user-visible text."""

    if not isinstance(text, str) or not _PREFLIGHT_ANY_MARKER_RE.search(text):
        return None
    clean = _PREFLIGHT_CONFIRMED_FIELDS_BLOCK_RE.sub("\n", text)
    clean = PREFLIGHT_CONFIRMED_RE.sub("\n", clean)
    clean = PREFLIGHT_RUN_ID_RE.sub("\n", clean)
    clean = PREFLIGHT_FIELDS_RE.sub("\n", clean)
    clean = _PREFLIGHT_ANY_MARKER_RE.sub("\n", clean)
    paragraphs = [
        "\n".join(line.rstrip() for line in part.strip().splitlines()).strip()
        for part in re.split(r"(?:\r?\n){2,}", clean)
        if part.strip()
    ]
    if len(paragraphs) >= 2 and paragraphs[1].startswith(paragraphs[0]):
        paragraphs = paragraphs[1:]
    return "\n\n".join(paragraphs).strip()
