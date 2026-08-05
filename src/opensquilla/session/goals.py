"""Domain validation and state transitions for durable goal runs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opensquilla.session.models import GoalRunRecord

MAX_GOAL_TEXT_CHARS = 8000

GOAL_RUN_ACTIVE_STATUSES = frozenset({"running", "paused"})
GOAL_RUN_TERMINAL_STATUSES = frozenset({"complete", "blocked", "cancelled"})
GOAL_RUN_STATUSES = GOAL_RUN_ACTIVE_STATUSES | GOAL_RUN_TERMINAL_STATUSES

_GOAL_STATUS_MARKER_PATTERN = re.compile(
    r"^\s*\[goal:(continue|complete|blocked)(?::([^\]]+))?\]\s*$"
)

IDLE_PROGRESS_PROMPT = (
    "You have not made progress; either take a concrete action or mark "
    "[goal:complete]/[goal:blocked:<reason>]"
)

# Task outcomes that may recover on retry (provider overload / rate limit /
# transport errors surface as ``failed`` or ``timeout`` turns).
GOAL_RETRYABLE_TURN_STATUSES = frozenset({"failed", "timeout"})
# Task outcomes that must never auto-retry: the user cancelled, or the process
# abandoned the turn during shutdown.
GOAL_NON_RETRYABLE_TURN_STATUSES = frozenset({"cancelled", "abandoned"})


class GoalValidationError(ValueError):
    """Raised when a goal violates its durable wire contract."""


class GoalConflictError(RuntimeError):
    """Raised when a mutable goal run changed before a compare-and-set write."""


@dataclass
class GoalAdvance:
    """Decision outcome of one goal turn against the configured guards."""

    continue_: bool
    inject_prompt: str | None
    terminal: bool
    terminal_reason: str | None


@dataclass
class GoalFailureAdvance:
    """Decision outcome after a failed goal turn (transient vs terminal)."""

    retry_at_ms: int | None
    terminal: bool
    terminal_reason: str | None


def _now_ms() -> int:
    """Return the current UTC time as epoch milliseconds."""

    return int(datetime.now(UTC).timestamp() * 1000)


def goal_retry_delay_ms(
    retry_index: int,
    *,
    base_ms: int = 30_000,
    max_ms: int = 600_000,
) -> int:
    """Exponential backoff delay (ms) for goal retry ``retry_index`` (0-based).

    Mirrors the turn-level provider backoff shape (base * 2**index, capped at
    ``max_ms``) so transient provider pressure backs off instead of hammering.
    """

    if retry_index < 0:
        raise GoalValidationError("retry_index must be >= 0")
    if base_ms < 1 or max_ms < 1:
        raise GoalValidationError("backoff bounds must be positive")
    delay = int(min(base_ms * (2**retry_index), max_ms))
    return max(1, delay)


def advance_goal_after_failure(
    goal: GoalRunRecord,
    *,
    task_status: str,
    failure_retries: int,
    max_failure_retries: int,
    base_backoff_ms: int,
    max_backoff_ms: int,
    now_ms: int,
) -> GoalFailureAdvance:
    """Decide whether a failed goal turn should retry automatically.

    ``task_status`` is the terminal turn outcome (``failed`` / ``timeout`` are
    retryable; ``cancelled`` / ``abandoned`` are not). A retryable failure
    schedules the next attempt at ``now + backoff(failure_retries)`` until
    ``max_failure_retries`` consecutive failures block the goal. Non-retryable
    outcomes return ``retry_at_ms=None`` with no terminal transition so the
    caller can park the goal in ``paused`` instead.
    """

    if max_failure_retries < 1:
        raise GoalValidationError("max_failure_retries must be positive")
    if failure_retries < 0:
        raise GoalValidationError("failure_retries must be >= 0")

    if task_status in GOAL_NON_RETRYABLE_TURN_STATUSES:
        return GoalFailureAdvance(
            retry_at_ms=None,
            terminal=False,
            terminal_reason=None,
        )
    if task_status not in GOAL_RETRYABLE_TURN_STATUSES:
        raise GoalValidationError(f"unknown goal turn status: {task_status}")

    if failure_retries >= max_failure_retries:
        return GoalFailureAdvance(
            retry_at_ms=None,
            terminal=True,
            terminal_reason=f"goal_turn_failed_after_retries:{task_status}",
        )
    retry_at_ms = now_ms + goal_retry_delay_ms(
        failure_retries,
        base_ms=base_backoff_ms,
        max_ms=max_backoff_ms,
    )
    return GoalFailureAdvance(
        retry_at_ms=retry_at_ms,
        terminal=False,
        terminal_reason=None,
    )


def _bounded_goal_text(value: Any) -> str:
    """Normalize and length-check raw goal text for the durable contract."""

    if not isinstance(value, str):
        raise GoalValidationError("goal_text must be a string")
    normalized = value.strip()
    if not normalized:
        raise GoalValidationError("goal_text must not be empty")
    if len(normalized) > MAX_GOAL_TEXT_CHARS:
        raise GoalValidationError(f"goal_text exceeds {MAX_GOAL_TEXT_CHARS} characters")
    return normalized


def new_goal_run(
    *,
    goal_id: str,
    session_key: str,
    agent_id: str,
    goal_text: str,
    plan_run_id: str | None = None,
    started_at: int | None = None,
    created_at: int | None = None,
) -> GoalRunRecord:
    """Build a fresh running goal run with validated input.

    The run starts in ``running`` status with zero turns so it is eligible
    for automatic continuation and the per-session active unique index.
    """

    text = _bounded_goal_text(goal_text)
    if not goal_id or not session_key or not agent_id:
        raise GoalValidationError("goal_id, session_key and agent_id are required")
    timestamp = _now_ms() if created_at is None else created_at
    return GoalRunRecord(
        goal_id=goal_id,
        session_key=session_key,
        agent_id=agent_id,
        goal_text=text,
        status="running",
        turns=0,
        idle_turns=0,
        blocked_retries=0,
        plan_run_id=plan_run_id,
        started_at=started_at if started_at is not None else timestamp,
        created_at=timestamp,
        updated_at=timestamp,
    )


def goal_run_snapshot(run: GoalRunRecord) -> dict[str, Any]:
    """Return the stable camelCase server-authoritative goal payload."""

    return {
        "goalId": run.goal_id,
        "sessionKey": run.session_key,
        "agentId": run.agent_id,
        "goalText": run.goal_text,
        "status": run.status,
        "progress": run.progress,
        "turns": run.turns,
        "idleTurns": run.idle_turns,
        "blockedReason": run.blocked_reason,
        "blockedRetries": run.blocked_retries,
        "failureRetries": run.failure_retries,
        "nextRetryAtMs": run.next_retry_at_ms,
        "pauseReason": run.pause_reason,
        "lastError": run.last_error,
        "planRunId": run.plan_run_id,
        "startedAt": run.started_at,
        "lastTurnAt": run.last_turn_at,
        "finishedAt": run.finished_at,
        "terminalReason": run.terminal_reason,
    }


def parse_goal_status_marker(text: str) -> tuple[str, str | None] | None:
    """Parse the trailing goal marker line of an assistant reply.

    Returns ``("continue", None)`` / ``("complete", None)`` /
    ``("blocked", reason)`` when the last line carries a goal marker and
    ``None`` otherwise.
    """

    if not isinstance(text, str):
        return None
    lines = text.rstrip("\n").split("\n")
    if not lines:
        return None
    match = _GOAL_STATUS_MARKER_PATTERN.fullmatch(lines[-1])
    if match is None:
        return None
    kind = match.group(1)
    if kind == "blocked":
        reason = match.group(2) or None
        return ("blocked", reason)
    return (kind, None)


def _validate_advance_guards(
    marker: tuple[str, str | None] | None,
    *,
    max_turns: int,
    idle_turns: int,
    blocked_retries: int,
) -> None:
    """Reject malformed markers and non-positive guard limits.

    Centralizes the wire-contract validation so ``advance_goal_after_turn``
    stays focused on the decision tree itself.
    """

    if marker is not None and marker[0] not in {"continue", "complete", "blocked"}:
        raise GoalValidationError(f"unknown goal status marker: {marker[0]}")
    if max_turns < 1:
        raise GoalValidationError("max_turns must be positive")
    if idle_turns < 1:
        raise GoalValidationError("idle_turns must be positive")
    if blocked_retries < 1:
        raise GoalValidationError("blocked_retries must be positive")


def _runtime_budget_advance(
    goal: GoalRunRecord,
    *,
    runtime_budget_seconds: int | None,
    now_ms: int,
) -> GoalAdvance | None:
    """Return a terminal advance when the run exceeds its wall-clock budget."""

    if (
        runtime_budget_seconds is not None
        and now_ms - goal.started_at > runtime_budget_seconds * 1000
    ):
        return GoalAdvance(
            continue_=False,
            inject_prompt=None,
            terminal=True,
            terminal_reason="goal_runtime_budget_exceeded",
        )
    return None


def _turn_limit_advance(goal: GoalRunRecord, *, max_turns: int) -> GoalAdvance | None:
    """Return a terminal advance when the run reaches ``max_turns``."""

    if goal.turns + 1 >= max_turns:
        return GoalAdvance(
            continue_=False,
            inject_prompt=None,
            terminal=True,
            terminal_reason="goal_continuation_limit_reached",
        )
    return None


def _missing_marker_advance(goal: GoalRunRecord, *, idle_turns: int) -> GoalAdvance:
    """Advance a markerless turn, nudging the agent after ``idle_turns``."""

    if goal.idle_turns + 1 >= idle_turns:
        return GoalAdvance(
            continue_=True,
            inject_prompt=IDLE_PROGRESS_PROMPT,
            terminal=False,
            terminal_reason=None,
        )
    return GoalAdvance(
        continue_=True,
        inject_prompt=None,
        terminal=False,
        terminal_reason=None,
    )


def _blocked_marker_advance(
    goal: GoalRunRecord,
    *,
    reason: str | None,
    blocked_retries: int,
) -> GoalAdvance:
    """Advance a blocked turn, retrying the same cause up to ``blocked_retries``."""

    reason_text = reason or ""
    same_cause = goal.blocked_reason is not None and goal.blocked_reason == reason_text
    retries_after = goal.blocked_retries + 1 if same_cause else 1
    if retries_after >= blocked_retries:
        return GoalAdvance(
            continue_=False,
            inject_prompt=None,
            terminal=True,
            terminal_reason=f"blocked_after_retries:{reason_text}",
        )
    return GoalAdvance(
        continue_=True,
        inject_prompt=None,
        terminal=False,
        terminal_reason=None,
    )


def advance_goal_after_turn(
    goal: GoalRunRecord,
    marker: tuple[str, str | None] | None,
    *,
    max_turns: int,
    idle_turns: int,
    blocked_retries: int,
    runtime_budget_seconds: int | None,
    now_ms: int,
) -> GoalAdvance:
    """Decide whether a goal run continues after one finished turn.

    The caller is responsible for applying the per-turn fixed actions
    (``turns += 1``, ``last_turn_at = now``, idle/blocked counters) and
    persisting any terminal transition. Guards are evaluated first:

    - a non-null ``runtime_budget_seconds`` with ``now - started_at > budget``
      blocks the run;
    - reaching ``max_turns`` blocks the run with
      ``goal_continuation_limit_reached``;
    - otherwise the marker decides: ``complete`` finishes the run,
      ``blocked`` retries up to ``blocked_retries`` consecutive same-cause
      blocks, and a missing marker counts toward the idle prompt.

    Each guard and marker branch delegates to a dedicated helper
    (``_runtime_budget_advance``, ``_turn_limit_advance``,
    ``_missing_marker_advance``, ``_blocked_marker_advance``) so the decision
    tree stays readable and each policy is independently unit-testable.
    """

    _validate_advance_guards(
        marker,
        max_turns=max_turns,
        idle_turns=idle_turns,
        blocked_retries=blocked_retries,
    )

    budget_advance = _runtime_budget_advance(
        goal,
        runtime_budget_seconds=runtime_budget_seconds,
        now_ms=now_ms,
    )
    if budget_advance is not None:
        return budget_advance

    limit_advance = _turn_limit_advance(goal, max_turns=max_turns)
    if limit_advance is not None:
        return limit_advance

    if marker is None:
        return _missing_marker_advance(goal, idle_turns=idle_turns)

    kind, reason = marker
    if kind == "continue":
        return GoalAdvance(
            continue_=True,
            inject_prompt=None,
            terminal=False,
            terminal_reason=None,
        )
    if kind == "complete":
        return GoalAdvance(
            continue_=False,
            inject_prompt=None,
            terminal=True,
            terminal_reason=None,
        )
    return _blocked_marker_advance(
        goal,
        reason=reason,
        blocked_retries=blocked_retries,
    )
