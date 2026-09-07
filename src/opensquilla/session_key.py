"""Layer-neutral session-key canonicalization.

The key codec is shared by persistence, application modules, and transport
adapters.  Keeping this small pure helper at the package root prevents the
application layer from importing the large ``session`` implementation package.
"""

from __future__ import annotations

from opensquilla.agent_ids import normalize_agent_id


def canonicalize_session_key(session_key: str | None) -> str:
    """Normalize legacy session-key aliases without changing scope."""

    key = str(session_key or "").strip()
    if not key:
        return ""
    if key == "webchat:default":
        return f"agent:{normalize_agent_id('main')}:webchat:default"
    if key.startswith("subagent:agent:"):
        return f"subagent:{canonicalize_session_key(key[len('subagent:') :])}"
    if key.startswith("agent:"):
        parts = key.split(":")
        if len(parts) >= 2:
            parts[1] = normalize_agent_id(parts[1])
            return ":".join(parts)
    return key


def is_cron_session_key(session_key: str | None) -> bool:
    """Return whether a key belongs to the scheduler-owned Cron namespace."""

    parts = canonicalize_session_key(session_key).split(":")
    return len(parts) >= 2 and parts[0] == "cron" and bool(parts[1])


__all__ = ["canonicalize_session_key", "is_cron_session_key"]
