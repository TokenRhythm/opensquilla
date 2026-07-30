"""UI-facing session view normalization for gateway session lists."""

from __future__ import annotations

import json
import re
from typing import Any

from opensquilla.session.keys import derive_chat_type, parse_agent_id

_CHANNEL_SURFACES = frozenset(
    {
        "slack",
        "discord",
        "feishu",
        "dingtalk",
        "wecom",
        "qq",
        "matrix",
        "telegram",
    }
)
_TIME_PREFIX_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+\-]\d{2}:\d{2} "
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"[A-Za-z0-9_+\-/]+\]\n"
)


def build_session_view_item(
    session: Any,
    *,
    entry_count: int,
    task_rows: list[Any],
    now_ms: int,
    transcript_title: str | None = None,
    channel_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return additive Web UI contract fields for one session row.

    ``channel_types`` maps configured channel NAMES (lowercased) to their
    platform type. Operators name channels freely ("飞书", "slack-eng"), so
    surface classification by the type vocabulary alone would misfile every
    custom-named channel's sessions as unknown.
    """

    key = str(getattr(session, "session_key", "") or "")
    origin = getattr(session, "origin", None)
    origin_map = origin if isinstance(origin, dict) else {}
    effective_agent_id = _effective_agent_id(session, key)
    surface = _surface(session, key, origin_map, channel_types)
    session_kind = _session_kind(session, key, surface, origin_map)
    conversation_kind = _conversation_kind(session, key, session_kind)
    run_status = _run_status(task_rows)
    last_activity_at = _last_activity_at(session, task_rows, now_ms)

    return {
        "sessionId": getattr(session, "session_id", None),
        "effectiveAgentId": effective_agent_id,
        "sessionKind": session_kind,
        "surface": surface,
        "conversationKind": conversation_kind,
        "thread": _thread(session, key),
        "title": _title(
            session,
            key,
            effective_agent_id,
            session_kind,
            surface,
            transcript_title,
        ),
        "subtitle": _subtitle(
            session,
            key,
            effective_agent_id,
            session_kind,
            surface,
            conversation_kind,
        ),
        "groupLabel": _group_label(session_kind, surface),
        "updatedAt": last_activity_at,
        "lastActivityAt": last_activity_at,
        "messageCount": entry_count,
        "runStatus": run_status,
        "interactive": _interactive(session_kind, surface),
        "channelContext": _channel_context(session, surface),
        "parent": _parent(session, origin_map),
        "cron": _cron(session, key, origin_map),
    }


def _effective_agent_id(session: Any, key: str) -> str:
    parsed = parse_agent_id(key)
    stored = _display(getattr(session, "agent_id", None)) or "main"
    if parsed != "main":
        return parsed
    return stored


def _surface(
    session: Any,
    key: str,
    origin: dict[str, Any],
    channel_types: dict[str, str] | None = None,
) -> str:
    last_channel = _lower(getattr(session, "last_channel", None))
    channel = _lower(getattr(session, "channel", None))
    origin_kind = _lower(origin.get("kind"))

    named = channel_types or {}
    # The configured name→type map may carry plugin types outside the pinned
    # surface enum; those must degrade to the fall-through checks (ending in
    # "unknown") rather than widen the contract's closed union.
    mapped = named.get(last_channel) or named.get(channel)
    if mapped in _CHANNEL_SURFACES:
        return mapped
    if last_channel in _CHANNEL_SURFACES:
        return last_channel
    if channel in _CHANNEL_SURFACES:
        return channel
    if origin_kind in _CHANNEL_SURFACES:
        return origin_kind
    key_surface = _surface_from_key(key)
    if key_surface in _CHANNEL_SURFACES:
        return key_surface
    if ":webchat:" in key:
        return "webchat"
    if ":cli:" in key or ":standalone:" in key:
        return "cli"
    if ":subagent:" in key or key.startswith("subagent:"):
        return "subagent"
    if key.startswith("cron:") or origin_kind == "cron":
        return "cron"
    return "unknown"


def _session_kind(session: Any, key: str, surface: str, origin: dict[str, Any]) -> str:
    if surface in _CHANNEL_SURFACES:
        return "channel"
    if surface in {"webchat", "cli", "tui", "mcp"}:
        return "chat"
    if surface == "subagent":
        return "task"
    if surface == "cron":
        cron_meta = origin.get("cron")
        if isinstance(cron_meta, dict) and cron_meta.get("targetSessionKey") == key:
            channel = _lower(getattr(session, "last_channel", None))
            if channel in _CHANNEL_SURFACES:
                return "channel"
        return "cron"
    if _is_main_agent_chat_key(key) or _is_direct_agent_chat_key(key):
        return "chat"
    if getattr(session, "parent_session_key", None):
        return "task"
    return "unknown"


def _conversation_kind(session: Any, key: str, session_kind: str) -> str:
    if session_kind == "chat":
        if ":webchat:" in key:
            return "direct"
        derived = _lower(derive_chat_type(key))
        if derived == "direct":
            return "direct"
        return "main"
    if session_kind != "channel":
        return "unknown"
    chat_type = _lower(getattr(session, "chat_type", None))
    if chat_type in {"direct", "group", "channel"}:
        return chat_type
    derived = _lower(derive_chat_type(key))
    if derived in {"direct", "group", "channel"}:
        return derived
    return "unknown"


def _thread(session: Any, key: str) -> dict[str, str] | None:
    raw = _display(getattr(session, "last_thread_id", None))
    kind = "topic" if ":topic:" in key else "thread"
    if not raw:
        marker = ":topic:" if ":topic:" in key else ":thread:"
        if marker in key:
            raw = key.rsplit(marker, 1)[1]
    if not raw:
        return None
    return {"id": raw, "kind": kind}


def _title(
    session: Any,
    key: str,
    effective_agent_id: str,
    session_kind: str,
    surface: str,
    transcript_title: str | None = None,
) -> str:
    for attr in ("display_name", "derived_title", "subject"):
        value = _display(getattr(session, attr, None))
        if value:
            if (
                session_kind == "chat"
                and surface == "webchat"
                and transcript_title
                and _is_generic_webchat_title(value, effective_agent_id)
            ):
                continue
            return value
    if transcript_title:
        return transcript_title
    if session_kind == "chat" and surface == "webchat":
        if effective_agent_id != "main":
            return _humanize(effective_agent_id)
        return "Web chat"
    if session_kind == "chat" and surface == "cli":
        return "CLI session"
    if session_kind == "chat" and surface == "unknown":
        if _is_direct_agent_chat_key(key):
            return "Direct chat"
        if _is_main_agent_chat_key(key):
            return f"{_humanize(effective_agent_id)} main"
    if session_kind == "task" and surface == "subagent":
        return "Subagent task"
    if session_kind == "cron":
        parts = key.split(":")
        if len(parts) >= 2:
            return _humanize(parts[1])
        return "Cron run"
    if session_kind == "channel":
        target = _display(getattr(session, "last_to", None))
        if target:
            suffix = " thread" if _thread(session, key) else ""
            return f"{target}{suffix}"
        return f"{_humanize(surface)} conversation"
    return key or "Unknown session"


def _subtitle(
    session: Any,
    key: str,
    effective_agent_id: str,
    session_kind: str,
    surface: str,
    conversation_kind: str,
) -> str:
    if session_kind == "channel":
        kind = _humanize(surface)
        thread = _thread(session, key)
        if thread:
            return f"{kind} {thread['kind']}"
        if conversation_kind != "unknown":
            return f"{kind} {conversation_kind}"
        return kind
    if session_kind == "task":
        parent_key = _display(getattr(session, "parent_session_key", None))
        return f"Spawned from {_parent_label(parent_key)}" if parent_key else "Background task"
    if session_kind == "cron":
        return "Cron isolated run"
    if session_kind == "chat" and surface == "webchat" and effective_agent_id != "main":
        return "Web chat"
    if effective_agent_id:
        return effective_agent_id
    return _humanize(surface)


def _group_label(session_kind: str, surface: str) -> str:
    if session_kind == "chat" and surface == "webchat":
        return "Web chat"
    if session_kind == "chat" and surface == "cli":
        return "CLI"
    if session_kind == "chat":
        return "Chats"
    if session_kind == "channel":
        return _humanize(surface)
    if session_kind == "task" and surface == "subagent":
        return "Subagents"
    if session_kind == "cron":
        return "Cron"
    if session_kind == "system":
        return "System"
    return "Other"


def _run_status(task_rows: list[Any]) -> str:
    values = [_lower(getattr(row, "status", None)) for row in task_rows]
    if "running" in values:
        return "running"
    if "queued" in values:
        return "queued"
    latest = _latest_task_status(task_rows)
    if latest == "abandoned":
        return "interrupted"
    if latest in {"failed", "error"}:
        return "failed"
    if latest in {"timeout", "timed_out"}:
        return "timeout"
    if latest in {"cancelled", "canceled", "killed"}:
        return "cancelled"
    return "idle"


def _epoch_ms(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, epoch)


def _task_activity_at(row: Any) -> int:
    return max(
        _epoch_ms(getattr(row, "updated_at", None)),
        _epoch_ms(getattr(row, "started_at", None)),
        _epoch_ms(getattr(row, "created_at", None)),
    )


def _last_activity_at(session: Any, task_rows: list[Any], now_ms: int) -> int:
    latest = _epoch_ms(getattr(session, "updated_at", None)) or now_ms
    for row in task_rows:
        if _lower(getattr(row, "status", None)) in {"queued", "running"}:
            latest = max(latest, _task_activity_at(row))
    return latest


def _interactive(session_kind: str, surface: str) -> bool:
    return session_kind == "chat" and surface == "webchat"


def _channel_context(session: Any, surface: str) -> dict[str, str] | None:
    if surface not in _CHANNEL_SURFACES:
        return None
    result: dict[str, str] = {"name": surface}
    last_to = _display(getattr(session, "last_to", None))
    account_id = _display(getattr(session, "last_account_id", None))
    thread_id = _display(getattr(session, "last_thread_id", None))
    if last_to:
        result["id"] = last_to
    if account_id:
        result["accountId"] = account_id
    if thread_id:
        result["threadId"] = thread_id
    return result


def _parent(session: Any, origin: dict[str, Any]) -> dict[str, Any] | None:
    parent_key = _display(getattr(session, "parent_session_key", None))
    if not parent_key:
        return None
    parent: dict[str, Any] = {"key": parent_key}
    spawned_by = _display(getattr(session, "spawned_by", None))
    if spawned_by:
        parent["taskId"] = spawned_by
    spawn_depth = origin.get("spawnDepth", origin.get("spawn_depth"))
    if isinstance(spawn_depth, int):
        parent["spawnDepth"] = spawn_depth
    return parent


def _cron(session: Any, key: str, origin: dict[str, Any]) -> dict[str, Any] | None:
    raw = origin.get("cron")
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if v not in (None, "")}
    if origin.get("kind") == "cron" or key.startswith("cron:"):
        result: dict[str, Any] = {}
        job_id = origin.get("jobId") or origin.get("job_id")
        if not job_id and key.startswith("cron:"):
            parts = key.split(":")
            if len(parts) >= 2:
                job_id = parts[1]
        if job_id:
            result["jobId"] = str(job_id)
        session_target = origin.get("sessionTarget") or origin.get("session_target")
        if session_target:
            result["sessionTarget"] = str(session_target)
        return result or None
    return None


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _display(value: Any) -> str:
    return str(value or "").strip()


def _latest_task_status(task_rows: list[Any]) -> str:
    if not task_rows:
        return ""
    latest = max(task_rows, key=lambda row: getattr(row, "created_at", 0) or 0)
    return _lower(getattr(latest, "status", None))


def _surface_from_key(key: str) -> str:
    parts = key.split(":")
    if key.startswith("agent:") and len(parts) >= 3:
        return parts[2].strip().lower()
    return ""


def _is_main_agent_chat_key(key: str) -> bool:
    parts = key.split(":")
    return key.startswith("agent:") and len(parts) == 3 and parts[2].lower() == "main"


def _is_direct_agent_chat_key(key: str) -> bool:
    parts = key.split(":")
    return key.startswith("agent:") and len(parts) >= 4 and parts[2].lower() in {
        "direct",
        "dm",
    }


def _parent_label(key: str) -> str:
    if ":webchat:" in key:
        return "Web chat"
    if ":cli:" in key or ":standalone:" in key:
        return "CLI"
    if ":subagent:" in key or key.startswith("subagent:"):
        return "Subagent"
    if key.startswith("cron:"):
        return "Cron"
    surface = _surface_from_key(key)
    if surface in _CHANNEL_SURFACES:
        return _humanize(surface)
    return key


def _humanize(value: str) -> str:
    cleaned = value.replace("_", " ").replace("-", " ").strip()
    return cleaned[:1].upper() + cleaned[1:] if cleaned else ""


def derive_transcript_title(content: Any, *, max_chars: int = 34) -> str:
    text = _content_text(content)
    if not text:
        return ""
    text = _TIME_PREFIX_RE.sub("", text, count=1)
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = cleaned.strip("\"'` ")
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        raw = content.strip()
        if not raw:
            return ""
        if raw[0] in "[{":
            try:
                return _content_text(json.loads(raw))
            except Exception:
                return raw
        return raw
    if isinstance(content, dict):
        for key in (
            "text",
            "message",
            "semantic_message",
            "prompt",
            "query",
            "content",
        ):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in content.values():
            nested = _content_text(value)
            if nested:
                return nested
        return ""
    if isinstance(content, list):
        for value in content:
            nested = _content_text(value)
            if nested:
                return nested
    return ""


def _is_generic_webchat_title(value: str, effective_agent_id: str) -> bool:
    normalized = value.strip().lower().replace("_", " ").replace("-", " ")
    generic = {
        "",
        "chat",
        "current session",
        "direct chat",
        "new chat",
        "web chat",
        "webchat",
    }
    if normalized in generic:
        return True
    return normalized == effective_agent_id.strip().lower()
