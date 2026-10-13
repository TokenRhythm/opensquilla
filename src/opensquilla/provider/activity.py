"""Small, safe identifiers for public provider activity and durable replay."""

from __future__ import annotations

import re

_UNSAFE_MODEL_CHARACTERS = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)


def safe_activity_model(value: object) -> str | None:
    """Bound an observed model label while preserving configured Unicode aliases."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 256
        or _UNSAFE_MODEL_CHARACTERS.search(normalized)
    ):
        return None
    return normalized
