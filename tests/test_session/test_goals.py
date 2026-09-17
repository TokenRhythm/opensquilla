"""Pure Goal domain contract tests."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from opensquilla.session.goals import (
    GOAL_STATUSES,
    GOAL_TERMINAL_STATUSES,
    GOAL_UNFINISHED_STATUSES,
    GoalClaimCandidate,
    GoalCommandRequest,
    GoalStatus,
    GoalTurnContext,
    GoalValidationError,
    goal_snapshot,
    new_goal,
    normalize_goal_progress,
)

SESSION_KEY = "agent:main:webchat:goals"
SESSION_ID = "session-goals"


def test_goal_status_partition_matches_resumable_fsm() -> None:
    assert GOAL_UNFINISHED_STATUSES == {
        "active",
        "paused",
        "blocked",
        "usage_limited",
    }
    assert GOAL_TERMINAL_STATUSES == {"complete"}
    assert GOAL_STATUSES == {status.value for status in GoalStatus}
    assert GOAL_UNFINISHED_STATUSES.isdisjoint(GOAL_TERMINAL_STATUSES)


def test_new_goal_normalizes_objective_and_initializes_revisions() -> None:
    goal = new_goal(
        goal_id="goal-1",
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        session_epoch=3,
        objective="  Ship the safe Goal runtime.  ",
        task_id="task-1",
        source_user_message_id="message-goal-1",
        created_at_ms=100,
    )

    assert goal.objective == "Ship the safe Goal runtime."
    assert goal.status == "active"
    assert (goal.state_revision, goal.objective_revision, goal.progress_revision) == (
        1,
        1,
        0,
    )
    assert goal.active_task_id == "task-1"
    assert goal.source_user_message_id == "message-goal-1"
    assert goal.terminal_task_id is None
    assert (goal.turns_started, goal.turns_settled, goal.window_turns_started) == (
        1,
        0,
        1,
    )


@pytest.mark.parametrize("objective", ["", "   ", "x" * 4_001, None])
def test_new_goal_rejects_invalid_objective(objective: object) -> None:
    with pytest.raises(GoalValidationError) as exc_info:
        new_goal(
            goal_id="goal-1",
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch=0,
            objective=objective,  # type: ignore[arg-type]
        )
    assert exc_info.value.code == "INVALID_GOAL_OBJECTIVE"


def test_new_goal_accepts_exactly_4000_unicode_characters() -> None:
    goal = new_goal(
        goal_id="goal-1",
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        session_epoch=0,
        objective="目" * 4_000,
    )
    assert len(goal.objective) == 4_000


def test_new_goal_rejects_non_integer_epoch() -> None:
    with pytest.raises(GoalValidationError, match="session_epoch"):
        new_goal(
            goal_id="goal-1",
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch="3",  # type: ignore[arg-type]
            objective="Ship the Goal runtime",
        )


def test_new_goal_rejects_blank_source_message_identity() -> None:
    with pytest.raises(GoalValidationError, match="source_user_message_id"):
        new_goal(
            goal_id="goal-1",
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch=0,
            objective="Ship the Goal runtime",
            source_user_message_id="  ",
        )


def test_progress_normalizes_step_shape_and_single_in_progress() -> None:
    progress = normalize_goal_progress(
        explanation="  Working through the contract.  ",
        steps=[
            {"step": "  Inspect schema  ", "status": "completed"},
            {"step": "Implement CAS", "status": "in_progress"},
        ],
    )
    assert progress == {
        "explanation": "Working through the contract.",
        "steps": [
            {"step": "Inspect schema", "status": "completed"},
            {"step": "Implement CAS", "status": "in_progress"},
        ],
    }

    with pytest.raises(GoalValidationError, match="at most one") as exc_info:
        normalize_goal_progress(
            explanation=None,
            steps=[
                {"step": "one", "status": "in_progress"},
                {"step": "two", "status": "in_progress"},
            ],
        )
    assert exc_info.value.code == "INVALID_GOAL_PROGRESS"


def test_progress_enforces_count_and_text_bounds() -> None:
    with pytest.raises(GoalValidationError, match="at most 20"):
        normalize_goal_progress(
            explanation=None,
            steps=[{"step": str(index), "status": "pending"} for index in range(21)],
        )
    with pytest.raises(GoalValidationError, match="200"):
        normalize_goal_progress(
            explanation=None,
            steps=[{"step": "x" * 201, "status": "pending"}],
        )
    with pytest.raises(GoalValidationError, match="1000"):
        normalize_goal_progress(explanation="x" * 1_001, steps=[])
    with pytest.raises(GoalValidationError, match="only step and status"):
        normalize_goal_progress(
            explanation=None,
            steps=[
                {
                    "step": "Inspect",
                    "status": "pending",
                    "assistant_text": "must not persist",
                }
            ],
        )


def test_goal_task_context_and_candidate_are_strictly_versioned() -> None:
    context = GoalTurnContext(
        session_id=SESSION_ID,
        epoch=2,
        goal_id="goal-1",
        objective_revision=4,
        objective_snapshot="Finish the work",
        task_id="task-1",
        continuation_seq=3,
        automatic=True,
    )
    assert GoalTurnContext.from_task_detail(context.as_task_detail()) == context
    assert GoalTurnContext.from_task_detail({"schemaVersion": 2}) is None
    invalid_revision = context.as_task_detail()
    invalid_revision["objectiveRevision"] = 0
    assert GoalTurnContext.from_task_detail(invalid_revision) is None
    padded_objective = context.as_task_detail()
    padded_objective["objectiveSnapshot"] = "  Finish the work  "
    assert GoalTurnContext.from_task_detail(padded_objective) is None

    candidate = GoalClaimCandidate(session_id=SESSION_ID, epoch=2, goal_id="goal-1")
    assert GoalClaimCandidate.from_task_detail(candidate.as_task_detail()) == candidate
    assert GoalClaimCandidate.from_task_detail({"schemaVersion": 1, "epoch": True}) is None


def test_command_request_requires_canonical_uuid_v4() -> None:
    valid = str(uuid.uuid4())
    GoalCommandRequest(
        source_scope="gateway:goals",
        request_session_key=SESSION_KEY,
        client_request_id=valid,
        action="set",
        request_fingerprint=hashlib.sha256(b"synthetic").hexdigest(),
    ).validate()

    for invalid in ("not-a-uuid", valid.upper(), str(uuid.uuid1())):
        with pytest.raises(GoalValidationError, match="UUID v4"):
            GoalCommandRequest(
                source_scope="gateway:goals",
                request_session_key=SESSION_KEY,
                client_request_id=invalid,
                action="set",
                request_fingerprint=hashlib.sha256(b"synthetic").hexdigest(),
            ).validate()

    with pytest.raises(GoalValidationError, match="SHA-256"):
        GoalCommandRequest(
            source_scope="gateway:goals",
            request_session_key=SESSION_KEY,
            client_request_id=valid,
            action="set",
            request_fingerprint="not-a-digest",
        ).validate()


def test_goal_snapshot_exposes_final_accounting_contract() -> None:
    goal = new_goal(
        goal_id="goal-1",
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        session_epoch=3,
        objective="Ship Goal mode",
        task_id="task-1",
        source_user_message_id="message-goal-1",
        created_at_ms=100,
    ).model_copy(
        update={
            "progress_json": {
                "explanation": None,
                "steps": [{"step": "Implement", "status": "in_progress"}],
            },
            "progress_revision": 2,
            "turns_settled": 1,
            "active_time_ms": 250,
            "input_tokens": 7,
            "output_tokens": 5,
            "reasoning_tokens": 2,
            "cache_read_tokens": 3,
            "cache_write_tokens": 1,
            "total_tokens": 12,
            "status": GoalStatus.COMPLETE.value,
            "terminal_task_id": "task-1",
        }
    )

    snapshot = goal_snapshot(goal)
    assert snapshot["objective"] == "Ship Goal mode"
    assert snapshot["executionState"] == "working"
    assert snapshot["progressRevision"] == 2
    assert snapshot["sourceMessageId"] == "message-goal-1"
    assert snapshot["terminalTurnId"] == "task-1"
    assert snapshot["usage"] == {
        "inputTokens": 7,
        "outputTokens": 5,
        "reasoningTokens": 2,
        "cacheReadTokens": 3,
        "cacheWriteTokens": 1,
        "totalTokens": 12,
    }
    assert "goalText" not in snapshot
    assert "planRunId" not in snapshot
    assert "terminalTaskId" not in snapshot


def test_goal_snapshot_exposes_blocker_only_while_currently_blocked() -> None:
    historical = new_goal(
        goal_id="goal-resumed",
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        session_epoch=3,
        objective="Resume safely",
        created_at_ms=100,
    ).model_copy(
        update={
            "status": GoalStatus.ACTIVE.value,
            "blocked_reason": "Previous blocker, retained only for the resumed prompt.",
        }
    )
    assert goal_snapshot(historical)["blockedReason"] is None

    current = historical.model_copy(
        update={
            "status": GoalStatus.BLOCKED.value,
            "blocked_reason": "Current blocker.",
        }
    )
    assert goal_snapshot(current)["blockedReason"] == "Current blocker."
