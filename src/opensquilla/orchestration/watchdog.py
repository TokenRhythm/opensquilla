"""Orchestration health signals that require generation-fenced subtree stops."""

from __future__ import annotations

from typing import Any

from opensquilla.orchestration.models import TaskOutcome

_HARD_TERMINAL_REASONS = frozenset(
    {
        "hard_deadline_exceeded",
        "invalid_state",
        "max_iterations",
        "model_repetition_loop_detected",
        "repeated_failure",
        "token_budget_exceeded",
        "tool_call_budget_exceeded",
        "turn_budget_exceeded",
        "wall_time_budget_exceeded",
    }
)

_ENGINE_ERROR_REASONS = {
    "subagent_max_iterations": "max_iterations",
    "subagent_retrieval_stalled": "repeated_failure",
    "subagent_tool_failure_fallback": "repeated_failure",
}


def terminal_subtree_interrupt_reason(
    outcome: TaskOutcome,
    result: dict[str, Any],
) -> str | None:
    """Return the hard terminal reason that must stop live descendants."""

    reason = str(result.get("terminal_reason") or "").strip()
    if outcome is TaskOutcome.TIMED_OUT:
        return reason or "timed_out"
    if reason in _HARD_TERMINAL_REASONS:
        return reason
    error_type = str(result.get("error_type") or "").strip()
    normalized = _ENGINE_ERROR_REASONS.get(error_type, error_type)
    if normalized in _HARD_TERMINAL_REASONS:
        return normalized
    return None


__all__ = ["terminal_subtree_interrupt_reason"]
