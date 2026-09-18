"""Historical Plan projections never replace ordinary task admission locks."""

from __future__ import annotations

import pytest

from opensquilla.session.goals import (
    ClaimCurrentGoalMutation,
    GoalConflictError,
    automatic_goal_task_id,
)
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, TranscriptEntry
from tests.test_session.test_goal_storage import (
    SESSION_ID,
    SESSION_KEY,
    _expected,
    _set_goal,
    storage,
)

__all__ = ["storage"]


async def _legacy_plan_projection(storage, *, session_key, session_id, status):
    # Synthetic orphan left by an older runtime: its association exists, but
    # there is no queued/running AgentTask to own execution.
    await storage.conn.execute(
        """INSERT INTO plan_runs (
            run_id, session_key, session_id, session_epoch, plan_revision_id,
            driver_kind, status, step_states, active_task_id, created_at, updated_at
        ) VALUES ('legacy-run', ?, ?, 0, 'legacy-revision', 'manual', ?, '[]',
                  'retired-task', 100, 100)""",
        (session_key, session_id, status),
    )
    await storage.conn.commit()


@pytest.mark.parametrize("plan_status", ["queued", "running", "paused", "blocked"])
async def test_historical_plan_projection_does_not_lock_goal_start(storage, plan_status):
    await _legacy_plan_projection(
        storage, session_key=SESSION_KEY, session_id=SESSION_ID, status=plan_status,
    )
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None
    assert accepted.goal_context.task_id == "task-1"
    await storage.update_agent_task(
        "task-1", status=AgentTaskStatus.SUCCEEDED, started_at=200, finished_at=250,
    )
    idle = await storage.settle_goal_task(
        accepted.goal_context, max_turns=50, runtime_budget_seconds=3600,
    )
    assert idle is not None
    task_id = automatic_goal_task_id(idle.goal_id, idle.objective_revision, 1)
    continued = await storage.accept_goal_continuation(
        expected=_expected(idle), expected_continuation_seq=idle.continuation_seq,
        task_record=AgentTaskRecord(
            task_id=task_id, session_key=SESSION_KEY, status=AgentTaskStatus.QUEUED,
        ),
    )
    assert continued.context.task_id == task_id
    projection = await storage.get_plan_run("legacy-run")
    assert projection is not None and projection.status == plan_status


@pytest.mark.parametrize("busy_status", [AgentTaskStatus.QUEUED, AgentTaskStatus.RUNNING])
async def test_real_task_still_locks_goal_start_with_historical_plan(storage, busy_status):
    await _legacy_plan_projection(
        storage, session_key=SESSION_KEY, session_id=SESSION_ID, status="paused",
    )
    await storage.create_agent_task(AgentTaskRecord(
        task_id="real-task", session_key=SESSION_KEY, status=busy_status,
        details={"metadata": {"plan_run_id": "legacy-run"}},
    ))
    await storage.conn.execute("UPDATE plan_runs SET active_task_id = 'real-task'")
    await storage.conn.commit()
    with pytest.raises(GoalConflictError) as exc:
        await _set_goal(storage)
    assert exc.value.code == "GOAL_BUSY"
    assert await storage.get_goal(SESSION_KEY) is None
    assert await storage.get_agent_task("task-1") is None


@pytest.mark.parametrize("plan_status", ["queued", "running", "paused", "blocked"])
async def test_user_turn_claims_goal_despite_historical_plan_projection(storage, plan_status):
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None
    await storage.update_agent_task(
        "task-1", status=AgentTaskStatus.SUCCEEDED, started_at=200, finished_at=250,
    )
    await storage.settle_goal_task(
        accepted.goal_context, max_turns=50, runtime_budget_seconds=3600,
    )
    await _legacy_plan_projection(
        storage, session_key=SESSION_KEY, session_id=SESSION_ID, status=plan_status,
    )
    claimed = await storage.accept_turn(
        TranscriptEntry(
            session_id=SESSION_ID, session_key=SESSION_KEY, message_id="followup-message",
            role="user", content="Continue the synthetic work.", created_at=300,
        ),
        expected_epoch=0, updated_at=300,
        source_scope="gateway:sessions.send", request_session_key=SESSION_KEY,
        client_request_id="plan-history-followup", request_fingerprint="synthetic-followup",
        task_record=AgentTaskRecord(
            task_id="followup-task", session_key=SESSION_KEY, status=AgentTaskStatus.QUEUED,
        ),
        goal_mutation=ClaimCurrentGoalMutation(),
    )
    assert claimed.goal_context is not None
    assert claimed.goal_context.task_id == "followup-task"
    assert claimed.goal_candidate is None
