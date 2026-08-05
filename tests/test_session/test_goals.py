"""Domain validation and state transitions for durable goal runs."""

from __future__ import annotations

import pytest

from opensquilla.session.goals import (
    GOAL_RETRYABLE_TURN_STATUSES,
    GOAL_RUN_ACTIVE_STATUSES,
    GOAL_RUN_TERMINAL_STATUSES,
    IDLE_PROGRESS_PROMPT,
    GoalAdvance,
    GoalFailureAdvance,
    GoalValidationError,
    advance_goal_after_failure,
    advance_goal_after_turn,
    goal_retry_delay_ms,
    goal_run_snapshot,
    new_goal_run,
    parse_goal_status_marker,
)
from opensquilla.session.models import GoalRunRecord

GOAL_ID = "goal-1"
SESSION_KEY = "agent:main:webchat:goals"
AGENT_ID = "main"
GOAL_TEXT = "Ship the goal mode data layer."


def _goal(**overrides: object) -> GoalRunRecord:
    values: dict[str, object] = {
        "goal_id": GOAL_ID,
        "session_key": SESSION_KEY,
        "agent_id": AGENT_ID,
        "goal_text": GOAL_TEXT,
        "status": "running",
        "turns": 0,
        "idle_turns": 0,
        "blocked_retries": 0,
        "started_at": 1000,
        "created_at": 1000,
        "updated_at": 1000,
    }
    values.update(overrides)
    return GoalRunRecord(**values)


def _advance(
    goal: GoalRunRecord,
    marker: tuple[str, str | None] | None,
    *,
    max_turns: int = 50,
    idle_turns: int = 2,
    blocked_retries: int = 3,
    runtime_budget_seconds: int | None = None,
    now_ms: int = 2000,
) -> GoalAdvance:
    return advance_goal_after_turn(
        goal,
        marker,
        max_turns=max_turns,
        idle_turns=idle_turns,
        blocked_retries=blocked_retries,
        runtime_budget_seconds=runtime_budget_seconds,
        now_ms=now_ms,
    )


def test_goal_status_sets_are_disjoint_and_exhaustive() -> None:
    assert GOAL_RUN_ACTIVE_STATUSES == {"running", "paused"}
    assert GOAL_RUN_TERMINAL_STATUSES == {"complete", "blocked", "cancelled"}
    assert GOAL_RUN_ACTIVE_STATUSES.isdisjoint(GOAL_RUN_TERMINAL_STATUSES)


def test_new_goal_run_starts_running_with_zero_counters() -> None:
    run = new_goal_run(
        goal_id=GOAL_ID,
        session_key=SESSION_KEY,
        agent_id=AGENT_ID,
        goal_text="  Ship the goal mode data layer.  ",
    )
    assert isinstance(run, GoalRunRecord)
    assert run.goal_id == GOAL_ID
    assert run.session_key == SESSION_KEY
    assert run.agent_id == AGENT_ID
    assert run.goal_text == "Ship the goal mode data layer."
    assert run.status == "running"
    assert run.turns == 0
    assert run.idle_turns == 0
    assert run.blocked_retries == 0
    assert run.blocked_reason is None
    assert run.terminal_reason is None
    assert run.started_at is not None
    assert run.created_at == run.updated_at


def test_new_goal_run_rejects_empty_goal_text() -> None:
    with pytest.raises(GoalValidationError, match="must not be empty"):
        new_goal_run(
            goal_id=GOAL_ID,
            session_key=SESSION_KEY,
            agent_id=AGENT_ID,
            goal_text="   ",
        )


def test_new_goal_run_rejects_oversized_goal_text() -> None:
    with pytest.raises(GoalValidationError, match="8000"):
        new_goal_run(
            goal_id=GOAL_ID,
            session_key=SESSION_KEY,
            agent_id=AGENT_ID,
            goal_text="x" * 8001,
        )


def test_new_goal_run_accepts_exactly_8000_characters() -> None:
    run = new_goal_run(
        goal_id=GOAL_ID,
        session_key=SESSION_KEY,
        agent_id=AGENT_ID,
        goal_text="x" * 8000,
    )
    assert len(run.goal_text) == 8000


def test_new_goal_run_requires_identity_fields() -> None:
    with pytest.raises(GoalValidationError, match="required"):
        new_goal_run(
            goal_id="",
            session_key=SESSION_KEY,
            agent_id=AGENT_ID,
            goal_text=GOAL_TEXT,
        )


def test_goal_run_snapshot_is_camel_case() -> None:
    run = _goal(
        progress=[{"note": "inspected the spec"}],
        turns=3,
        idle_turns=1,
        blocked_reason="no_api_key",
        blocked_retries=2,
        failure_retries=1,
        next_retry_at_ms=2000,
        pause_reason="goal_unwatched",
        last_error="turn_timeout",
        plan_run_id="run-9",
        last_turn_at=1500,
    )
    payload = goal_run_snapshot(run)
    assert payload == {
        "goalId": GOAL_ID,
        "sessionKey": SESSION_KEY,
        "agentId": AGENT_ID,
        "goalText": GOAL_TEXT,
        "status": "running",
        "progress": [{"note": "inspected the spec"}],
        "turns": 3,
        "idleTurns": 1,
        "blockedReason": "no_api_key",
        "blockedRetries": 2,
        "failureRetries": 1,
        "nextRetryAtMs": 2000,
        "pauseReason": "goal_unwatched",
        "lastError": "turn_timeout",
        "planRunId": "run-9",
        "startedAt": 1000,
        "lastTurnAt": 1500,
        "finishedAt": None,
        "terminalReason": None,
    }


def test_parse_goal_status_marker_continue() -> None:
    assert parse_goal_status_marker("Work done.\n[goal:continue]") == (
        "continue",
        None,
    )


def test_parse_goal_status_marker_complete() -> None:
    assert parse_goal_status_marker("Done.\n[goal:complete]") == ("complete", None)


def test_parse_goal_status_marker_blocked_with_reason() -> None:
    assert parse_goal_status_marker(
        "Cannot reach the API.\n[goal:blocked:no_api_key]"
    ) == ("blocked", "no_api_key")


def test_parse_goal_status_marker_blocked_without_reason() -> None:
    assert parse_goal_status_marker("[goal:blocked]") == ("blocked", None)
    # The spec regex requires a non-empty reason after the colon.
    assert parse_goal_status_marker("[goal:blocked:]") is None


def test_parse_goal_status_marker_ignores_non_last_lines() -> None:
    assert parse_goal_status_marker("[goal:continue]\nmore work follows") is None


def test_parse_goal_status_marker_requires_an_entire_last_line() -> None:
    assert parse_goal_status_marker("I mention [goal:complete] in prose") is None
    assert parse_goal_status_marker("[goal:complete] and more text") is None


def test_parse_goal_status_marker_missing() -> None:
    assert parse_goal_status_marker("Just a normal reply.") is None
    assert parse_goal_status_marker("") is None
    assert parse_goal_status_marker("goal:continue") is None
    assert parse_goal_status_marker("[goal:sidequest]") is None
    assert parse_goal_status_marker(None) is None


def test_advance_continue() -> None:
    advance = _advance(_goal(), ("continue", None))
    assert advance.continue_ is True
    assert advance.inject_prompt is None
    assert advance.terminal is False
    assert advance.terminal_reason is None


def test_advance_complete() -> None:
    advance = _advance(_goal(), ("complete", None))
    assert advance.continue_ is False
    assert advance.inject_prompt is None
    assert advance.terminal is True
    assert advance.terminal_reason is None


def test_advance_blocked_retries_three_times_then_terminal() -> None:
    goal = _goal()
    for _ in range(2):
        advance = _advance(goal, ("blocked", "no_api_key"))
        assert advance.continue_ is True
        assert advance.terminal is False
        goal = goal.model_copy(
            update={
                "blocked_reason": "no_api_key",
                "blocked_retries": goal.blocked_retries + 1,
            }
        )
    advance = _advance(goal, ("blocked", "no_api_key"))
    assert advance.continue_ is False
    assert advance.terminal is True
    assert advance.terminal_reason == "blocked_after_retries:no_api_key"


def test_advance_blocked_reason_change_resets_retry_count() -> None:
    goal = _goal().model_copy(
        update={"blocked_reason": "no_api_key", "blocked_retries": 2}
    )
    advance = _advance(goal, ("blocked", "different_reason"))
    assert advance.continue_ is True
    assert advance.terminal is False
    # A fresh cause starts at one retry, far below the threshold of three.
    goal = goal.model_copy(
        update={"blocked_reason": "different_reason", "blocked_retries": 0}
    )
    advance = _advance(goal, ("blocked", "different_reason"))
    assert advance.continue_ is True
    advance = _advance(
        goal.model_copy(update={"blocked_retries": 2}),
        ("blocked", "different_reason"),
    )
    assert advance.terminal is True
    assert advance.terminal_reason == "blocked_after_retries:different_reason"


def test_advance_blocked_without_reason_accumulates_retries() -> None:
    goal = _goal()
    for _ in range(2):
        advance = _advance(goal, ("blocked", None))
        assert advance.continue_ is True
        assert advance.terminal is False
        # The driver persists an empty reason string for reason-less markers.
        goal = goal.model_copy(
            update={"blocked_reason": "", "blocked_retries": goal.blocked_retries + 1}
        )
    advance = _advance(goal, ("blocked", None))
    assert advance.continue_ is False
    assert advance.terminal is True
    assert advance.terminal_reason == "blocked_after_retries:"


def test_goal_retry_delay_exponential_backoff() -> None:
    assert goal_retry_delay_ms(0, base_ms=30_000, max_ms=600_000) == 30_000
    assert goal_retry_delay_ms(1, base_ms=30_000, max_ms=600_000) == 60_000
    assert goal_retry_delay_ms(2, base_ms=30_000, max_ms=600_000) == 120_000
    # Capped at the configured maximum.
    assert goal_retry_delay_ms(10, base_ms=30_000, max_ms=600_000) == 600_000
    with pytest.raises(GoalValidationError):
        goal_retry_delay_ms(-1)


def test_advance_after_failure_retryable_schedules_backoff() -> None:
    goal = _goal()
    advance = advance_goal_after_failure(
        goal,
        task_status="timeout",
        failure_retries=0,
        max_failure_retries=3,
        base_backoff_ms=30_000,
        max_backoff_ms=600_000,
        now_ms=2000,
    )
    assert isinstance(advance, GoalFailureAdvance)
    assert advance.retry_at_ms == 32_000
    assert advance.terminal is False
    assert advance.terminal_reason is None


def test_advance_after_failure_exhausts_retries_then_terminal() -> None:
    goal = _goal()
    advance = advance_goal_after_failure(
        goal,
        task_status="failed",
        failure_retries=3,
        max_failure_retries=3,
        base_backoff_ms=30_000,
        max_backoff_ms=600_000,
        now_ms=2000,
    )
    assert advance.retry_at_ms is None
    assert advance.terminal is True
    assert advance.terminal_reason == "goal_turn_failed_after_retries:failed"


def test_advance_after_failure_non_retryable_never_auto_retries() -> None:
    goal = _goal()
    for task_status in ("cancelled", "abandoned"):
        advance = advance_goal_after_failure(
            goal,
            task_status=task_status,
            failure_retries=0,
            max_failure_retries=3,
            base_backoff_ms=30_000,
            max_backoff_ms=600_000,
            now_ms=2000,
        )
        assert advance.retry_at_ms is None
        assert advance.terminal is False
    assert GOAL_RETRYABLE_TURN_STATUSES == {"failed", "timeout"}


def test_advance_after_failure_rejects_unknown_status() -> None:
    with pytest.raises(GoalValidationError):
        advance_goal_after_failure(
            _goal(),
            task_status="succeeded",
            failure_retries=0,
            max_failure_retries=3,
            base_backoff_ms=30_000,
            max_backoff_ms=600_000,
            now_ms=2000,
        )


def test_advance_idle_prompt_after_two_markerless_turns() -> None:
    goal = _goal()
    first = _advance(goal, None)
    assert first.continue_ is True
    assert first.inject_prompt is None
    goal = goal.model_copy(update={"idle_turns": 1})
    second = _advance(goal, None)
    assert second.continue_ is True
    assert second.terminal is False
    assert second.inject_prompt == IDLE_PROGRESS_PROMPT


def test_advance_max_turns_blocks_goal() -> None:
    goal = _goal(turns=49)
    advance = _advance(goal, ("continue", None), max_turns=50)
    assert advance.continue_ is False
    assert advance.terminal is True
    assert advance.terminal_reason == "goal_continuation_limit_reached"


def test_advance_max_turns_allows_turns_below_limit() -> None:
    goal = _goal(turns=48)
    advance = _advance(goal, ("continue", None), max_turns=50)
    assert advance.continue_ is True
    assert advance.terminal is False


def test_advance_runtime_budget_blocks_goal() -> None:
    goal = _goal(started_at=1000)
    advance = _advance(
        goal,
        ("continue", None),
        runtime_budget_seconds=2,
        now_ms=4001,
    )
    assert advance.continue_ is False
    assert advance.terminal is True
    assert advance.terminal_reason == "goal_runtime_budget_exceeded"


def test_advance_runtime_budget_within_limit_continues() -> None:
    goal = _goal(started_at=1000)
    advance = _advance(
        goal,
        ("continue", None),
        runtime_budget_seconds=2,
        now_ms=3000,
    )
    assert advance.continue_ is True
    assert advance.terminal is False


def test_advance_rejects_invalid_guard_config() -> None:
    with pytest.raises(GoalValidationError, match="max_turns"):
        _advance(_goal(), ("continue", None), max_turns=0)
    with pytest.raises(GoalValidationError, match="blocked_retries"):
        _advance(_goal(), ("blocked", "x"), blocked_retries=0)
    with pytest.raises(GoalValidationError, match="unknown"):
        _advance(_goal(), ("sidequest", None))
