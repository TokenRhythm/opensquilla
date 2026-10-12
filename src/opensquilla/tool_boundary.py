"""Side-effect-free tool-call boundary objects shared across runtime layers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from opensquilla.execution_status import ExecutionStatus


@dataclass(frozen=True)
class ToolContinuation:
    """Runtime-only authority used to resume one suspended tool request."""

    approval_id: str
    tool_use_id: str
    session_key: str
    sandbox_override: str = "danger_full_access"

    def matches(self, *, tool_use_id: str, session_key: str | None) -> bool:
        return self.tool_use_id == tool_use_id and self.session_key == str(
            session_key or ""
        )


@dataclass
class ToolCall:
    tool_use_id: str
    tool_name: str
    arguments: dict[str, Any]
    synthetic_from_text: bool = False
    # Optional raw assistant-message origin trace for the tool_use block.
    # Populated by the agent when available; consulted by tools.dispatch to
    # refuse calls whose origin lies inside an <untrusted> envelope.
    origin_trace: str | None = None
    # Never serialized into provider-visible tool arguments. The Agent sets
    # this only after the exact suspended request has been approved.
    continuation: ToolContinuation | None = None


@dataclass
class ToolResult:
    tool_use_id: str
    tool_name: str
    content: str
    is_error: bool = False
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    execution_status: ExecutionStatus | None = None
    terminates_turn: bool = False


AgentToolHandler = Callable[[ToolCall], Awaitable[ToolResult]]

# Preserve pickle/type-display identity for callers that imported these
# dataclasses from the previous engine.types path.
ToolCall.__module__ = "opensquilla.engine.types"
ToolResult.__module__ = "opensquilla.engine.types"
