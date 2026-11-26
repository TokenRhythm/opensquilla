"""Session-event → pet-notification mapping, identity dedup, and payloads.

Ported from the established external bridge (``opensquilla-pet-bridge.mjs``)
so the in-process service is behavior-for-behavior equivalent (PRD G4/§8.2):

* ``session.event.done`` / ``chat.done`` / ``task.succeeded`` → ``stop``
* ``session.event.approval_required``                       → ``permission``
* ``session.event.plan_presentation``                       → ``question``
* ``session.event.turn_committed``                          → ``turn-start``

Dedup is identity-aware: events carrying an id (approval/turn/task/event) are
deduped per identity so consecutive turns/approvals each notify once, while a
session-level watermark still suppresses identity-less echoes (``chat.done``
arriving right after ``done``). A new turn (``turn_committed``) clears the
session's ledger so the next turn's ``done`` is not swallowed by the window.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

# Notification kinds understood by the native bridge ``/notify`` endpoint.
KIND_STOP = "stop"
KIND_PERMISSION = "permission"
KIND_QUESTION = "question"
KIND_TURN_START = "turn-start"

_EVENT_TO_KIND: dict[str, str] = {
    "session.event.done": KIND_STOP,
    "chat.done": KIND_STOP,
    "task.succeeded": KIND_STOP,
    "session.event.approval_required": KIND_PERMISSION,
    "session.event.plan_presentation": KIND_QUESTION,
    "session.event.turn_committed": KIND_TURN_START,
}

# Kinds that raise the pet window to the foreground (permission/question).
_RAISING_KINDS = frozenset({KIND_PERMISSION, KIND_QUESTION})

# Clarify/choice cards ride this event (payload inspection, not a direct map).
TOOL_RESULT_EVENT = "session.event.tool_result"


def map_event(event_name: str) -> str | None:
    """Return the notification kind for an event name, or None to ignore it."""
    return _EVENT_TO_KIND.get(event_name)


def raises_foreground(kind: str) -> bool:
    return kind in _RAISING_KINDS


def session_label(title: str, project: str) -> str:
    """Friendly session name for ``{session}``: title, else project dir name.

    Mirrors the external bridge: a generic "WebChat" title is not spoken, so it
    falls back to the project directory name (e.g. "Opensquilla"), then a
    generic placeholder.
    """
    trimmed = (title or "").strip()
    if trimmed and trimmed.lower() != "webchat":
        return trimmed
    parts = [part for part in re.split(r"[\\/]", project or "") if part]
    return parts[-1] if parts else "这个会话"


def event_identity(payload: dict[str, Any]) -> str:
    """Derive a stable identity for dedup, mirroring the external bridge.

    Prefers explicit ids (event/approval/request/turn/task); falls back to the
    stream sequence number. Empty string means the event has no identity and
    must be deduped at the session level only.
    """
    for key in (
        "event_id", "eventId",
        "approval_id", "approvalId",
        "request_id", "requestId",
        "turn_id", "turnId",
        "task_id", "taskId",
    ):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    seq = payload.get("stream_seq", payload.get("streamSeq"))
    if seq is None:
        return ""
    return f"seq:{seq}"


def _result_record(value: Any) -> dict[str, Any] | None:
    """Coerce a tool-result value (object or JSON string) to a dict."""
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("{"):
            return None
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        return value
    return None


def pending_clarify_request(payload: dict[str, Any]) -> str | None:
    """Return the request id of a pending clarify/choice form, else None.

    Interactive tools surface a paused user-input form as a ``tool_result``
    whose ``result`` carries ``kind="user_input"``, ``paused=True`` and a
    ``clarify_schema`` (the "需要输入" choice card). This is not a dedicated
    session event, so the pet must inspect the tool-result payload to speak a
    question prompt when such a card appears. Only *pending* forms notify;
    answered/cancelled/expired outcomes (``paused=False``) are ignored.
    """
    result = _result_record(payload.get("result"))
    if result is None:
        # Some gateways lift the contract to a top-level field.
        result = _result_record(payload.get("user_input_request"))
    if result is None or result.get("kind") != "user_input":
        return None
    if result.get("paused") is not True:
        return None
    status = result.get("status")
    if status is not None and status != "input_required":
        return None
    request_id = result.get("request_id", result.get("requestId"))
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    schema = _result_record(result.get("clarify_schema"))
    if schema is None or not isinstance(schema.get("fields"), list) or not schema["fields"]:
        return None
    return request_id.strip()


class DedupeLedger:
    """Identity-aware dedup with a sliding window and per-turn reset.

    ``allow`` returns True the first time a (session, kind, identity) is seen
    within the window and False for repeats. Identity-bearing events also
    refresh the session-level watermark so later identity-less echoes of the
    same kind are suppressed. ``reset_session`` clears a session's entries on
    turn start.
    """

    def __init__(self, window_ms: int, *, clock: Any = None) -> None:
        self._window_ms = max(0, int(window_ms))
        self._clock = clock or (lambda: time.monotonic() * 1000.0)
        self._recent: dict[str, float] = {}

    def _now(self) -> float:
        return float(self._clock())

    def allow(self, session_key: str, kind: str, payload: dict[str, Any]) -> bool:
        now = self._now()
        identity = event_identity(payload)
        key = (
            f"{session_key}|{kind}|{identity}"
            if identity
            else f"{session_key}|{kind}"
        )
        last = self._recent.get(key)
        if last is not None and now - last < self._window_ms:
            return False
        self._recent[key] = now
        if identity:
            self._recent[f"{session_key}|{kind}"] = now
        if len(self._recent) > 200:
            for stale_key, ts in list(self._recent.items()):
                if now - ts > self._window_ms:
                    del self._recent[stale_key]
        return True

    def reset_session(self, session_key: str) -> None:
        prefix = f"{session_key}|"
        for key in list(self._recent.keys()):
            if key.startswith(prefix):
                del self._recent[key]


def build_notify_payload(
    *,
    kind: str,
    message: str,
    title: str,
    session_key: str = "",
    event_id: str = "",
    raise_window: str = "",
    project: str = "",
) -> dict[str, Any]:
    """Build the native ``/notify`` body (same field names as the old bridge)."""
    body: dict[str, Any] = {"event": kind, "title": title, "message": message}
    if raise_window:
        body["raise"] = raise_window
    if session_key:
        body["session_id"] = session_key
    if event_id:
        body["event_id"] = event_id
    if project:
        body["project"] = project
    return body


__all__ = [
    "KIND_STOP",
    "KIND_PERMISSION",
    "KIND_QUESTION",
    "KIND_TURN_START",
    "TOOL_RESULT_EVENT",
    "DedupeLedger",
    "build_notify_payload",
    "event_identity",
    "map_event",
    "pending_clarify_request",
    "raises_foreground",
    "session_label",
]
