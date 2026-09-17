"""Layer-neutral agent identifier normalization."""

from __future__ import annotations

import re
from functools import lru_cache

_INVALID_CHARS = re.compile(r"[^a-z0-9_-]")
_LEADING_TRAILING_DASHES = re.compile(r"^-+|-+$")


def normalize_id_segment(value: str, max_len: int = 64) -> str:
    """Normalize an identifier segment for stable keys and paths."""

    normalized = value.strip().lower()
    normalized = _INVALID_CHARS.sub("-", normalized)
    normalized = _LEADING_TRAILING_DASHES.sub("", normalized)
    return normalized[:max_len] if normalized else "default"


@lru_cache(maxsize=512)
def normalize_agent_id(agent_id: str | None) -> str:
    """Return the canonical runtime agent id.

    ``default`` was historically used by Web/RPC/CLI entrypoints as a
    no-agent sentinel. Treat it as an alias for the real default agent,
    ``main``, so sessions, workspaces, and memory stores do not split.
    """
    raw = str(agent_id or "").strip()
    if not raw or raw.lower() == "default":
        return "main"
    normalized = normalize_id_segment(raw)
    return "main" if normalized == "default" else normalized


__all__ = ["normalize_agent_id", "normalize_id_segment"]
