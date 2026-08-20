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


_EFFECT_STATES = frozenset({"none", "started", "committed", "unknown"})
_RETRY_POLICIES = frozenset(
    {"same_turn", "new_turn", "refresh", "reconcile", "never"}
)
_LOOP_ACTIONS = frozenset({"continue", "finalize_without_tools", "stop"})


@dataclass(frozen=True)
class ToolEffectOutcome:
    """Runtime-owned side-effect facts and loop control for one tool result.

    ``is_error`` remains the provider-facing success/error bit.  This object
    carries the orthogonal facts needed by an agent loop: whether a side
    effect began, whether retry is safe, and whether the next provider call
    may use tools.  Payload keys are camelCase at RPC/transcript boundaries;
    snake_case aliases are accepted when reading persisted development rows.
    """

    effect_state: str
    retry_policy: str
    loop_action: str
    outcome_code: str
    safe_details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.effect_state not in _EFFECT_STATES:
            raise ValueError(f"invalid tool effect state: {self.effect_state}")
        if self.retry_policy not in _RETRY_POLICIES:
            raise ValueError(f"invalid tool retry policy: {self.retry_policy}")
        if self.loop_action not in _LOOP_ACTIONS:
            raise ValueError(f"invalid tool loop action: {self.loop_action}")
        if not isinstance(self.outcome_code, str) or not self.outcome_code.strip():
            raise ValueError("tool outcome code must be non-empty")
        if not isinstance(self.safe_details, dict):
            raise TypeError("tool outcome safe details must be an object")

    def to_payload(self) -> dict[str, Any]:
        return {
            "effectState": self.effect_state,
            "retryPolicy": self.retry_policy,
            "loopAction": self.loop_action,
            "outcomeCode": self.outcome_code,
            "safeDetails": dict(self.safe_details),
        }

    @classmethod
    def from_payload(cls, value: object) -> ToolEffectOutcome | None:
        if not isinstance(value, dict):
            return None
        effect_state = value.get("effectState", value.get("effect_state"))
        retry_policy = value.get("retryPolicy", value.get("retry_policy"))
        loop_action = value.get("loopAction", value.get("loop_action"))
        outcome_code = value.get("outcomeCode", value.get("outcome_code"))
        safe_details = value.get("safeDetails", value.get("safe_details", {}))
        if (
            not isinstance(effect_state, str)
            or not isinstance(retry_policy, str)
            or not isinstance(loop_action, str)
            or not isinstance(outcome_code, str)
            or not isinstance(safe_details, dict)
        ):
            return None
        try:
            return cls(
                effect_state=effect_state,
                retry_policy=retry_policy,
                loop_action=loop_action,
                outcome_code=outcome_code,
                safe_details=dict(safe_details),
            )
        except (TypeError, ValueError):
            return None


@dataclass
class ToolResult:
    tool_use_id: str
    tool_name: str
    content: str
    is_error: bool = False
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    execution_status: ExecutionStatus | None = None
    terminates_turn: bool = False
    effect_outcome: ToolEffectOutcome | None = None
    # Optional authoritative user-visible text for a successful terminal tool.
    # The dispatcher mints this only for explicitly opted-in registered tools;
    # the Agent may use it instead of provisional text emitted before the tool ran.
    terminal_response_text: str | None = None


AgentToolHandler = Callable[[ToolCall], Awaitable[ToolResult]]

# Preserve pickle/type-display identity for callers that imported these
# dataclasses from the previous engine.types path.
ToolCall.__module__ = "opensquilla.engine.types"
ToolResult.__module__ = "opensquilla.engine.types"
ToolEffectOutcome.__module__ = "opensquilla.engine.types"
