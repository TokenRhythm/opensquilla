"""Renderer-independent TUI domain events."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

TuiDomainEventSource = Literal["runtime", "gateway", "turn_runner", "renderer"]

KIND_TEXT_DELTA = "text_delta"
KIND_TEXT_FLUSH = "text_flush"
KIND_REASONING_DELTA = "reasoning_delta"
KIND_REASONING_FLUSH = "reasoning_flush"
KIND_TOOL_STARTED = "tool_started"
KIND_TOOL_FINISHED = "tool_finished"
KIND_ROUTER_DECISION = "router_decision"
KIND_WARNING = "warning"
KIND_ERROR = "error"
KIND_DONE = "done"
KIND_STATUS = "status"

TUI_DOMAIN_EVENT_KINDS = frozenset(
    {
        KIND_TEXT_DELTA,
        KIND_TEXT_FLUSH,
        KIND_REASONING_DELTA,
        KIND_REASONING_FLUSH,
        KIND_TOOL_STARTED,
        KIND_TOOL_FINISHED,
        KIND_ROUTER_DECISION,
        KIND_WARNING,
        KIND_ERROR,
        KIND_DONE,
        KIND_STATUS,
    }
)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class TuiDomainEvent:
    kind: str
    source: TuiDomainEventSource
    payload: Mapping[str, Any]
    turn_id: str | None
    timestamp_ms: int
