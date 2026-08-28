"""Structured events emitted by the TUI backend runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from opensquilla.cli.tui.backend.domain_events import (
    KIND_DONE,
    KIND_ERROR,
    KIND_REASONING_DELTA,
    KIND_REASONING_FLUSH,
    KIND_ROUTER_DECISION,
    KIND_STATUS,
    KIND_TEXT_DELTA,
    KIND_TEXT_FLUSH,
    KIND_TOOL_FINISHED,
    KIND_TOOL_STARTED,
    KIND_WARNING,
    TUI_DOMAIN_EVENT_KINDS,
    TuiDomainEvent,
    now_ms,
)

__all__ = [
    "KIND_DONE",
    "KIND_ERROR",
    "KIND_REASONING_DELTA",
    "KIND_REASONING_FLUSH",
    "KIND_ROUTER_DECISION",
    "KIND_STATUS",
    "KIND_TEXT_DELTA",
    "KIND_TEXT_FLUSH",
    "KIND_TOOL_FINISHED",
    "KIND_TOOL_STARTED",
    "KIND_WARNING",
    "TUI_DOMAIN_EVENT_KINDS",
    "TuiDomainEvent",
    "TuiEvent",
    "TuiEventKind",
    "TuiEventSink",
    "now_ms",
]


class TuiEventKind(Enum):
    """Lifecycle events observable through ``TuiRuntimeConfig.event_sink``.

    The backend runtime currently emits USER_INPUT_ACCEPTED,
    QUEUED_INPUT_PROMOTED, TURN_STARTED, TURN_CANCELLED, and TURN_FINISHED.
    The remaining kinds are reserved for adapter/renderer emitters and have
    no emitter yet — consumers must not assume every kind is produced.
    """

    USER_INPUT_ACCEPTED = "user_input_accepted"
    QUEUED_INPUT_PROMOTED = "queued_input_promoted"
    TURN_STARTED = "turn_started"
    TURN_CANCELLED = "turn_cancelled"
    TURN_FINISHED = "turn_finished"
    STATUS_CHANGED = "status_changed"
    APPROVAL_STARTED = "approval_started"
    APPROVAL_FINISHED = "approval_finished"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    TEXT_DELTA_EMITTED = "text_delta_emitted"


@dataclass(frozen=True)
class TuiEvent:
    kind: TuiEventKind
    input_text: str | None = None
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


type TuiEventSink = Callable[[TuiEvent], None]
