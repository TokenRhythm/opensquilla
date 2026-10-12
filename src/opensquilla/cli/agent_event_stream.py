"""Stable stderr progress events for one-shot CLI automation."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

from opensquilla.engine.types import (
    AgentEvent,
    ArtifactEvent,
    DoneEvent,
    ErrorEvent,
    RouterDecisionEvent,
    RunHeartbeatEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseStartEvent,
    WarningEvent,
)
from opensquilla.redaction import redact_error_text
from opensquilla.session.terminal_reply import sanitize_agent_error

AGENT_EVENT_STREAM_SCHEMA_VERSION = 1
_EVENT_MESSAGE_MAX_CHARS = 4096
_STREAM_DISABLED_DIAGNOSTIC = (
    "Warning: agent progress event stream disabled after an encoding or stderr write failure.\n"
)

AgentEventSink = Callable[[AgentEvent], None]


def _base_event(kind: str) -> dict[str, Any]:
    return {
        "_event": True,
        "schema_version": AGENT_EVENT_STREAM_SCHEMA_VERSION,
        "kind": kind,
    }


def _safe_text(value: object, *, max_len: int = _EVENT_MESSAGE_MAX_CHARS) -> str:
    return redact_error_text(str(value or ""), max_len=max_len)


def project_agent_event_v1(event: AgentEvent | object) -> dict[str, Any] | None:
    """Project an internal engine event onto the stable, low-sensitivity v1 schema."""

    if isinstance(event, RouterDecisionEvent):
        return {
            **_base_event(event.kind),
            "tier": event.tier,
            "model": event.model,
            "source": event.source,
        }
    if isinstance(event, ThinkingEvent):
        return _base_event(event.kind)
    if isinstance(event, TextDeltaEvent):
        return {**_base_event(event.kind), "presentation": event.presentation}
    if isinstance(event, RunHeartbeatEvent):
        return {
            **_base_event(event.kind),
            "phase": event.phase,
            "elapsed_ms": event.elapsed_ms,
            "idle_ms": event.idle_ms,
        }
    if isinstance(event, ToolUseStartEvent):
        return {
            **_base_event(event.kind),
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
            "started_at": event.started_at,
        }
    if isinstance(event, ToolResultEvent):
        return {
            **_base_event(event.kind),
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
            "is_error": event.is_error,
        }
    if isinstance(event, WarningEvent):
        return {
            **_base_event(event.kind),
            "code": _safe_text(event.code, max_len=200),
            "message": _safe_text(event.message),
        }
    if isinstance(event, ErrorEvent):
        safe_code, safe_message = sanitize_agent_error(
            event,
            fallback_error_class=event.code or None,
            fallback_error_message="Agent error",
        )
        return {
            **_base_event(event.kind),
            "code": _safe_text(safe_code or event.code, max_len=200),
            "message": _safe_text(safe_message),
        }
    if isinstance(event, ArtifactEvent):
        return {
            **_base_event(event.kind),
            "id": event.id,
            "name": event.name,
            "mime": event.mime,
            "size": event.size,
        }
    if isinstance(event, DoneEvent):
        return _base_event(event.kind)
    return None


def agent_event_to_jsonl(event: AgentEvent | object) -> str | None:
    """Serialize one supported event as strict, compact JSONL payload text."""

    payload = project_agent_event_v1(event)
    if payload is None:
        return None
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


@dataclass
class StderrAgentEventSink:
    """Best-effort stderr writer that disables itself after its first failure."""

    stream: TextIO = field(default_factory=lambda: sys.stderr)
    active: bool = field(default=True, init=False)

    def __call__(self, event: AgentEvent) -> None:
        if not self.active:
            return
        try:
            line = agent_event_to_jsonl(event)
            if line is None:
                return
            self.stream.write(f"{line}\n")
            self.stream.flush()
        except Exception:
            self.active = False
            try:
                self.stream.write(_STREAM_DISABLED_DIAGNOSTIC)
                self.stream.flush()
            except Exception:
                pass


__all__ = [
    "AGENT_EVENT_STREAM_SCHEMA_VERSION",
    "AgentEventSink",
    "StderrAgentEventSink",
    "agent_event_to_jsonl",
    "project_agent_event_v1",
]
