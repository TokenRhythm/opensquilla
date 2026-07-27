"""Normalized turn outcome taxonomy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

TurnOutcomeKind = Literal[
    "completed",
    "partial",
    "budgetLimited",
    "blocked",
    "failed",
    "interrupted",
]

_BUDGET_CODES = frozenset(
    {
        "current_turn_context_exhausted",
        "provider_request_too_large",
        "provider_request_budget_exhausted",
        "provider_output_limit",
        "tool_run_budget_exhausted",
        "llm_budget_exhausted",
        "turn_llm_call_budget_exceeded",
        "turn_input_token_budget_exceeded",
        "turn_output_token_budget_exceeded",
        "turn_billed_cost_budget_exceeded",
    }
)
_PARTIAL_CODES = frozenset(
    {
        "max_iterations",
        "output_truncated",
        "provider_output_truncated",
        "turn_tool_error_budget_exceeded",
        "tool_failure_loop_exhausted",
    }
)
_INTERRUPTED_CODES = frozenset(
    {
        "cancelled",
        "cancelled_before_start",
        "dropped_by_overflow",
        "interrupted",
        "timeout",
        "iteration_timeout",
    }
)
_BLOCKED_CODES = frozenset(
    {
        "human_decision_required",
        "approval_required",
        "external_dependency",
        "provider_unavailable",
        "usage_accounting_unavailable",
        "sandbox_threshold_exceeded",
        "tool_policy_denied",
        "compaction_refused_flush_timeout",
        "compaction_refused_memory_flush",
        "compaction_refused_empty_summary",
        "context_unsalvageable",
    }
)


@dataclass(frozen=True)
class TurnOutcome:
    kind: TurnOutcomeKind
    reason: str
    error_class: str | None = None
    error_message: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def completed_outcome(reason: str = "done") -> TurnOutcome:
    return TurnOutcome(kind="completed", reason=reason)


def outcome_from_error(
    *,
    code: str | None,
    message: str | None = None,
    error_class: str | None = None,
) -> TurnOutcome:
    normalized = _normalize_code(code)
    text = message or None
    if normalized in _BUDGET_CODES:
        return TurnOutcome(
            kind="budgetLimited",
            reason=normalized,
            error_class=error_class or normalized,
            error_message=text,
            retryable=True,
        )
    if normalized in _PARTIAL_CODES:
        return TurnOutcome(
            kind="partial",
            reason=normalized,
            error_class=error_class or normalized,
            error_message=text,
            retryable=normalized == "provider_output_truncated",
        )
    if normalized in _INTERRUPTED_CODES:
        return TurnOutcome(
            kind="interrupted",
            reason=normalized,
            error_class=error_class or normalized,
            error_message=text,
            retryable=True,
        )
    if normalized in _BLOCKED_CODES:
        return TurnOutcome(
            kind="blocked",
            reason=normalized,
            error_class=error_class or normalized,
            error_message=text,
            retryable=True,
        )
    return TurnOutcome(
        kind="failed",
        reason=normalized or "error",
        error_class=error_class or normalized or "error",
        error_message=text,
    )


def turn_outcome_details(outcome: TurnOutcome) -> dict[str, Any]:
    return {"turn_outcome": outcome.to_dict()}


def _normalize_code(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_")
