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
    r"\[goal:(continue|complete|blocked)(?::([^\]]+))?\]"
)

IDLE_PROGRESS_PROMPT = (
    "You have not made progress; either take a concrete action or mark "
    "[goal:complete]/[goal:blocked:<reason>]"
)


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


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _bounded_goal_text(value: Any) -> str:
    if not isinstance(value, str):
        raise GoalValidationError("goal_text must be a string")
    normalized = value.strip()
    if not normalized:
        raise GoalValidationError("goal_text must not be empty")
    if len(normalized) > MAX_GOAL_TEXT_CHARS:
        raise GoalValidationError(
            f"goal_text exceeds {MAX_GOAL_TEXT_CHARS} characters"
        )
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
    match = _GOAL_STATUS_MARKER_PATTERN.search(lines[-1])
    if match is None:
        return None
    kind = match.group(1)
    if kind == "blocked":
        reason = match.group(2) or None
        return ("blocked", reason)
    return (kind, None)


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
    """

    if marker is not None and marker[0] not in {"continue", "complete", "blocked"}:
        raise GoalValidationError(f"unknown goal status marker: {marker[0]}")
    if max_turns < 1:
        raise GoalValidationError("max_turns must be positive")
    if idle_turns < 1:
        raise GoalValidationError("idle_turns must be positive")
    if blocked_retries < 1:
        raise GoalValidationError("blocked_retries must be positive")

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

    turns_after = goal.turns + 1
    if turns_after >= max_turns:
        return GoalAdvance(
            continue_=False,
            inject_prompt=None,
            terminal=True,
            terminal_reason="goal_continuation_limit_reached",
        )

    if marker is None:
        idle_after = goal.idle_turns + 1
        if idle_after >= idle_turns:
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

    reason_text = reason or ""
    same_cause = (
        goal.blocked_reason is not None and goal.blocked_reason == reason_text
    )
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
