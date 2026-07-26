"""Session key construction — deterministic from agent/channel/account/peer info."""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache

# Prototype-poisoning keys blocked for account_id normalization
_POISONING_KEYS = frozenset({"__proto__", "constructor", "prototype", "hasownproperty"})

_INVALID_CHARS = re.compile(r"[^a-z0-9_-]")
_LEADING_TRAILING_DASHES = re.compile(r"^-+|-+$")


class DmScope(StrEnum):
    MAIN = "main"
    PER_PEER = "per_peer"
    PER_CHANNEL_PEER = "per_channel_peer"
    PER_ACCOUNT_CHANNEL_PEER = "per_account_channel_peer"


class PeerKind(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    CHANNEL = "channel"


def _normalize_id(value: str, max_len: int = 64) -> str:
    """Normalize an id segment: lowercase, replace invalid chars, strip dashes, cap length."""
    v = value.strip().lower()
    v = _INVALID_CHARS.sub("-", v)
    v = _LEADING_TRAILING_DASHES.sub("", v)
    return v[:max_len] if v else "default"


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
    normalized = _normalize_id(raw)
    return "main" if normalized == "default" else normalized


@lru_cache(maxsize=512)
def normalize_account_id(account_id: str) -> str:
    lower = account_id.strip().lower()
    if lower in _POISONING_KEYS:
        return "default"
    return _normalize_id(account_id) or "default"


def build_main_key(agent_id: str = "main") -> str:
    """Return the main session key for an agent."""
    return f"agent:{normalize_agent_id(agent_id)}:main"


def build_webchat_key(agent_id: str = "main") -> str:
    """Return the canonical WebChat default session key for an agent."""
    return f"agent:{normalize_agent_id(agent_id)}:webchat:default"


def canonicalize_session_key(session_key: str | None) -> str:
    """Normalize legacy session-key aliases without changing conversation scope."""
    key = str(session_key or "").strip()
    if not key:
        return ""
    if key == "webchat:default":
        return build_webchat_key()
    if key.startswith("subagent:agent:"):
        return f"subagent:{canonicalize_session_key(key[len('subagent:') :])}"
    if key.startswith("agent:"):
        parts = key.split(":")
        if len(parts) >= 2:
            parts[1] = normalize_agent_id(parts[1])
            return ":".join(parts)
    return key


def build_direct_key(
    agent_id: str,
    peer_id: str,
    channel: str | None = None,
    account_id: str | None = None,
    dm_scope: DmScope = DmScope.MAIN,
) -> str:
    """Build a DM session key depending on dm_scope."""
    aid = normalize_agent_id(agent_id)
    match dm_scope:
        case DmScope.MAIN:
            return f"agent:{aid}:main"
        case DmScope.PER_PEER:
            return f"agent:{aid}:direct:{peer_id}"
        case DmScope.PER_CHANNEL_PEER:
            ch = channel or "unknown"
            return f"agent:{aid}:{ch}:direct:{peer_id}"
        case DmScope.PER_ACCOUNT_CHANNEL_PEER:
            ch = channel or "unknown"
            acc = normalize_account_id(account_id or "default")
            return f"agent:{aid}:{ch}:{acc}:direct:{peer_id}"


def build_group_key(agent_id: str, channel: str, peer_id: str) -> str:
    aid = normalize_agent_id(agent_id)
    return f"agent:{aid}:{channel}:group:{peer_id}"


def build_group_sender_key(
    agent_id: str,
    channel: str,
    peer_id: str,
    sender_id: str,
) -> str:
    """Build a group session key isolated to one sender in one room."""
    return f"{build_group_key(agent_id, channel, peer_id)}:sender:{sender_id}"


def build_channel_key(agent_id: str, channel: str, peer_id: str) -> str:
    aid = normalize_agent_id(agent_id)
    return f"agent:{aid}:{channel}:channel:{peer_id}"


def build_thread_key(base_key: str, thread_id: str, channel_hint: str | None = None) -> str:
    """Append thread suffix; uses :topic: for telegram."""
    marker = "topic" if channel_hint == "telegram" else "thread"
    return f"{base_key}:{marker}:{thread_id}"


def parse_thread_suffix(key: str) -> tuple[str, str | None]:
    """Split key into (base_key, thread_id). Returns (key, None) if no suffix."""
    for marker in (":thread:", ":topic:"):
        idx = key.rfind(marker)
        if idx != -1:
            return key[:idx], key[idx + len(marker) :]
    return key, None


def build_subagent_key(base_key: str) -> str:
    return f"subagent:{base_key}"


def build_subagent_session_key(agent_id: str, run_id: str) -> str:
    """Build the canonical agent-scoped subagent session key."""
    aid = normalize_agent_id(agent_id)
    rid = _normalize_id(run_id)
    return f"agent:{aid}:subagent:{rid}"


def is_subagent_key(session_key: str) -> bool:
    """Return True for canonical and legacy subagent session keys."""
    key = session_key.strip().lower()
    return key.startswith("subagent:") or bool(re.match(r"^agent:[^:]+:subagent:[^:]+$", key))


def allows_private_memory_prompt_injection(session_key: str | None) -> bool:
    """Return whether automatic private memory may be injected into a prompt."""
    key = canonicalize_session_key(session_key)
    if not key:
        return True

    key_lower = key.lower()
    if is_subagent_key(key_lower) or key_lower.startswith("cron:"):
        return False

    chat_type = derive_chat_type(key_lower)
    if chat_type in {"group", "channel"}:
        return False
    if chat_type in {"direct", "dm"}:
        return True

    # Preserve main/private CLI and webchat behavior, while denying unknown
    # shared-looking legacy keys that contain channel/guild/group markers.
    shared_markers = (":group", "group:", ":channel", "channel:", ":guild", "guild:")
    if any(marker in key_lower for marker in shared_markers):
        return False
    return True


def build_cron_key(name: str, run_id: str) -> str:
    return f"cron:{name}:run:{run_id}"


def parse_agent_id(session_key: str) -> str:
    """Extract agent_id from a session key string. Fallback 'main'.

    'agent:ops:discord:group:123'       -> 'ops'
    'subagent:agent:ops:...'            -> 'ops'
    'cron:foo:run:abc'                  -> 'main'
    """
    key = session_key.strip()
    # subagent:agent:ops:... -> strip prefix, then parse as agent key
    if key.startswith("subagent:agent:"):
        key = key[len("subagent:") :]
    if key.startswith("agent:"):
        parts = key.split(":")
        candidate = parts[1] if len(parts) >= 2 else ""
        return normalize_agent_id(candidate) if candidate else "main"
    return "main"


def derive_chat_type(session_key: str) -> str:
    """Derive chat type from session key tokens."""
    from opensquilla.session.models import ChatType

    key = session_key.lower()
    # Discord legacy pattern
    if re.match(r"^agent:[^:]+:discord:(?:[^:]+:)?guild-[^:]+:channel-[^:]+", key):
        return ChatType.CHANNEL
    for token in ("group", "channel", "direct", "dm"):
        if f":{token}:" in key or key.endswith(f":{token}"):
            if token in ("direct", "dm"):
                return ChatType.DIRECT
            return token  # "group" or "channel"
    return ChatType.UNKNOWN
