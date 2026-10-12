"""Pure recovery-first turn-control decisions.

This module intentionally has no dependency on :class:`Agent`.  The live agent
loop remains the only owner of event emission; these helpers only classify what
should be tried before a terminal presentation is allowed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

RunMode = Literal["interactive", "gateway", "cli", "unattended", "subagent"]
BlockedReason = Literal[
    "human_decision_required",
    "permission_unavailable",
    "external_dependency",
    "no_progress",
    "provider_unavailable",
    "tool_policy_denied",
    "context_unsalvageable",
]
TurnControlAction = Literal[
    "continue",
    "retry",
    "compact_then_continue",
    "respond_to_model",
    "finalize_partial",
    "budget_limited",
    "blocked",
    "failed",
    "interrupted",
]
TurnControlPresentation = Literal[
    "silent",
    "telemetry",
    "model_visible",
    "user_visible",
    "terminal",
]

_BUDGET_CODES = frozenset(
    {
        "provider_request_budget_exhausted",
        "provider_request_too_large",
        "current_turn_context_exhausted",
        "turn_llm_call_budget_exceeded",
        "turn_input_token_budget_exceeded",
        "turn_output_token_budget_exceeded",
        "turn_billed_cost_budget_exceeded",
    }
)
_PARTIAL_CODES = frozenset(
    {
        "max_iterations",
        "turn_tool_error_budget_exceeded",
        "tool_failure_loop_exhausted",
        "output_truncated",
        "provider_output_truncated",
    }
)
_BLOCKED_CODES = frozenset(
    {
        "sandbox_threshold_exceeded",
        "compaction_refused_flush_timeout",
        "compaction_refused_memory_flush",
        "compaction_refused_empty_summary",
    }
)
_INTERRUPTED_CODES = frozenset({"cancelled", "interrupted", "timeout", "iteration_timeout"})
_RECOVERY_FIRST_CODES = _BUDGET_CODES | _PARTIAL_CODES | _BLOCKED_CODES | _INTERRUPTED_CODES


@dataclass(frozen=True)
class TurnStateSnapshot:
    iteration: int
    max_iterations: int = 0
    provider_call_count: int = 0
    last_provider_error_code: str | None = None
    last_provider_error_message: str | None = None
    last_tool_error_signature: str | None = None
    successful_tool_result: bool = False
    user_visible_output: bool = False
    artifact_completed: bool = False
    run_mode: RunMode | str = "interactive"
    recovery_attempted: bool = False
    finalization_attempted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TurnControlDecision:
    action: TurnControlAction
    presentation: TurnControlPresentation
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_turn_control(
    snapshot: TurnStateSnapshot,
    *,
    stop_code: str | None = None,
    fatal: bool = False,
) -> TurnControlDecision:
    """Classify a stop surface without emitting user-facing terminal events."""

    code = _normalize(stop_code)
    if not code:
        return TurnControlDecision("continue", "silent", "no_stop")

    details = {"stop_code": code, "snapshot": snapshot.to_dict()}
    if fatal:
        return TurnControlDecision("failed", "terminal", code, details)

    if code in _RECOVERY_FIRST_CODES and not _recovery_or_finalization_attempted(snapshot):
        if code in _BUDGET_CODES:
            action: TurnControlAction = "compact_then_continue"
        elif code in _INTERRUPTED_CODES:
            action = "interrupted"
        else:
            action = "finalize_partial"
        return TurnControlDecision(action, "telemetry", code, details)

    if code in _BUDGET_CODES:
        return TurnControlDecision("budget_limited", "terminal", code, details)
    if code in _PARTIAL_CODES:
        return TurnControlDecision("finalize_partial", "user_visible", code, details)
    if code in _BLOCKED_CODES:
        return TurnControlDecision("blocked", "terminal", code, details)
    if code in _INTERRUPTED_CODES:
        return TurnControlDecision("interrupted", "user_visible", code, details)
    return TurnControlDecision("failed", "terminal", code, details)


def _recovery_or_finalization_attempted(snapshot: TurnStateSnapshot) -> bool:
    return snapshot.recovery_attempted or snapshot.finalization_attempted


def _normalize(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_")
