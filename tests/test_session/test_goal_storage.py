"""Durable goal-run storage contracts."""

from __future__ import annotations

import pytest
import pytest_asyncio

from opensquilla.session.goals import GoalConflictError, GoalValidationError
from opensquilla.session.models import GoalRunRecord, SessionNode
from opensquilla.session.storage import SessionStorage

SESSION_KEY = "agent:main:webchat:goals"
SESSION_ID = "session-goals"


@pytest_asyncio.fixture
async def storage() -> SessionStorage:
    value = SessionStorage(":memory:")
    await value.connect()
    await value.upsert_session(
        SessionNode(
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            agent_id="main",
            created_at=100,
            updated_at=100,
            epoch=0,
        )
    )
    yield value
    await value.close()


def _goal(
    goal_id: str = "goal-1",
    *,
    session_key: str = SESSION_KEY,
    status: str = "running",
    turns: int = 0,
    created_at: int = 300,
    plan_run_id: str | None = None,
) -> GoalRunRecord:
    return GoalRunRecord(
        goal_id=goal_id,
        session_key=session_key,
        agent_id="main",
        goal_text="Ship the goal mode data layer.",
        status=status,
        turns=turns,
        started_at=created_at,
        created_at=created_at,
        updated_at=created_at,
        plan_run_id=plan_run_id,
    )


async def test_create_and_get_goal_run_roundtrip(storage: SessionStorage) -> None:
    created = await storage.create_goal_run(_goal())
    assert created.status == "running"
    assert created.turns == 0
    assert created.failure_retries == 0
    assert created.next_retry_at_ms is None
    assert created.pause_reason is None
    assert created.last_error is None

    loaded = await storage.get_goal_run("goal-1")
    assert loaded is not None
    assert loaded.goal_id == "goal-1"
    assert loaded.session_key == SESSION_KEY
    assert loaded.agent_id == "main"
    assert loaded.goal_text == "Ship the goal mode data layer."
    assert loaded.progress is None
    assert loaded.last_turn_at is None
    assert loaded.finished_at is None
    assert loaded.terminal_reason is None

    assert await storage.get_goal_run("missing") is None


async def test_goal_run_retry_fields_roundtrip(storage: SessionStorage) -> None:
    created = await storage.create_goal_run(_goal())
    updated = await storage.update_goal_run(
        created.goal_id,
        expected_updated_at=int(created.updated_at),
        failure_retries=2,
        next_retry_at_ms=123_000,
        pause_reason="goal_unwatched",
        last_error="turn_timeout",
    )
    assert updated.failure_retries == 2
    assert updated.next_retry_at_ms == 123_000
    assert updated.pause_reason == "goal_unwatched"
    assert updated.last_error == "turn_timeout"

    loaded = await storage.get_goal_run(created.goal_id)
    assert loaded is not None
    assert loaded.failure_retries == 2
    assert loaded.next_retry_at_ms == 123_000
    assert loaded.pause_reason == "goal_unwatched"
    assert loaded.last_error == "turn_timeout"


async def test_list_goal_runs_due_for_retry_filters_by_time_and_status(
    storage: SessionStorage,
) -> None:
    due = await storage.create_goal_run(
        _goal(goal_id="goal-due", session_key="agent:main:webchat:a", created_at=100)
    )
    await storage.update_goal_run(
        due.goal_id,
        expected_updated_at=int(due.updated_at),
        next_retry_at_ms=500,
    )
    future = await storage.create_goal_run(
        _goal(goal_id="goal-future", session_key="agent:main:webchat:b", created_at=200)
    )
    await storage.update_goal_run(
        future.goal_id,
        expected_updated_at=int(future.updated_at),
        next_retry_at_ms=9_999,
    )
    parked = await storage.create_goal_run(
        _goal(
            goal_id="goal-paused",
            session_key="agent:main:webchat:c",
            status="paused",
            created_at=300,
        )
    )
    await storage.update_goal_run(
        parked.goal_id,
        expected_updated_at=int(parked.updated_at),
        next_retry_at_ms=500,
    )

    results = await storage.list_goal_runs_due_for_retry(now_ms=500)
    assert {run.goal_id for run in results} == {"goal-due"}


async def test_list_active_goal_runs_includes_running_and_paused(
    storage: SessionStorage,
) -> None:
    await storage.create_goal_run(
        _goal(goal_id="goal-running", session_key="agent:main:webchat:a", created_at=100)
    )
    await storage.create_goal_run(
        _goal(
            goal_id="goal-paused",
            session_key="agent:main:webchat:b",
            status="paused",
            created_at=200,
        )
    )
    await storage.create_goal_run(
        _goal(
            goal_id="goal-blocked",
            session_key="agent:main:webchat:c",
            status="blocked",
            created_at=300,
        )
    )
    results = await storage.list_active_goal_runs()
    assert {run.goal_id for run in results} == {"goal-running", "goal-paused"}


async def test_create_goal_run_rejects_second_active_run(storage: SessionStorage) -> None:
    await storage.create_goal_run(_goal("goal-1"))
    with pytest.raises(GoalConflictError, match="already exists"):
        await storage.create_goal_run(_goal("goal-2"))


async def test_get_active_goal_run_uses_active_statuses(storage: SessionStorage) -> None:
    assert await storage.get_active_goal_run(SESSION_KEY) is None
    await storage.create_goal_run(_goal("goal-1"))
    active = await storage.get_active_goal_run(SESSION_KEY)
    assert active is not None
    assert active.goal_id == "goal-1"

    paused = await storage.pause_goal_run(
        "goal-1",
        expected_updated_at=active.updated_at,
    )
    assert paused.status == "paused"
    assert (await storage.get_active_goal_run(SESSION_KEY)).goal_id == "goal-1"


async def test_update_goal_run_cas_guards_against_stale_writes(
    storage: SessionStorage,
) -> None:
    created = await storage.create_goal_run(_goal())
    updated = await storage.update_goal_run(
        created.goal_id,
        expected_updated_at=created.updated_at,
        turns=1,
        last_turn_at=400,
    )
    assert updated.turns == 1
    assert updated.last_turn_at == 400
    assert updated.updated_at != created.updated_at

    with pytest.raises(GoalConflictError, match="changed before the update"):
        await storage.update_goal_run(
            created.goal_id,
            expected_updated_at=created.updated_at,
            turns=2,
        )


async def test_update_goal_run_rejects_unknown_or_invalid_fields(
    storage: SessionStorage,
) -> None:
    created = await storage.create_goal_run(_goal())
    with pytest.raises(GoalValidationError, match="goal_text"):
        await storage.update_goal_run(
            created.goal_id,
            expected_updated_at=created.updated_at,
            goal_text="mutated",
        )
    with pytest.raises(GoalValidationError, match="invalid goal run status"):
        await storage.update_goal_run(
            created.goal_id,
            expected_updated_at=created.updated_at,
            status="sidequest",
        )


async def test_update_goal_run_missing_run_raises_key_error(
    storage: SessionStorage,
) -> None:
    with pytest.raises(KeyError, match="not found"):
        await storage.update_goal_run(
            "missing",
            expected_updated_at=300,
            turns=1,
        )


async def test_update_goal_run_persists_progress_json(storage: SessionStorage) -> None:
    created = await storage.create_goal_run(_goal())
    updated = await storage.update_goal_run(
        created.goal_id,
        expected_updated_at=created.updated_at,
        progress=[{"note": "inspected the spec"}],
    )
    assert updated.progress == [{"note": "inspected the spec"}]
    reloaded = await storage.get_goal_run(created.goal_id)
    assert reloaded is not None
    assert reloaded.progress == [{"note": "inspected the spec"}]


async def test_supersede_active_goal_runs_cancels_active_but_keeps_terminal(
    storage: SessionStorage,
) -> None:
    first = await storage.create_goal_run(_goal("goal-1"))
    await storage.complete_goal_run("goal-1", expected_updated_at=first.updated_at)
    await storage.create_goal_run(_goal("goal-2", created_at=400))

    changed = await storage.supersede_active_goal_runs(SESSION_KEY)
    assert changed == 1
    assert (await storage.get_goal_run("goal-1")).status == "complete"
    persisted = await storage.get_goal_run("goal-2")
    assert persisted is not None
    assert persisted.status == "cancelled"
    assert persisted.terminal_reason == "superseded_by_new_goal"
    assert persisted.finished_at is not None
    assert await storage.get_active_goal_run(SESSION_KEY) is None


async def test_supersede_active_goal_runs_can_except_new_goal(
    storage: SessionStorage,
) -> None:
    old = await storage.create_goal_run(_goal("goal-old"))
    await storage.pause_goal_run("goal-old", expected_updated_at=old.updated_at)
    # Excluding the only active run changes nothing.
    changed = await storage.supersede_active_goal_runs(
        SESSION_KEY,
        except_goal_id="goal-old",
    )
    assert changed == 0
    assert (await storage.get_goal_run("goal-old")).status == "paused"

    # Without the exception the same call cancels it.
    changed = await storage.supersede_active_goal_runs(SESSION_KEY)
    assert changed == 1

    # A new running goal survives a supersede that excludes it.
    await storage.create_goal_run(_goal("goal-fresh", created_at=500))
    changed = await storage.supersede_active_goal_runs(
        SESSION_KEY,
        except_goal_id="goal-fresh",
    )
    assert changed == 0
    assert (await storage.get_goal_run("goal-fresh")).status == "running"
    assert await storage.get_active_goal_run(SESSION_KEY) is not None


async def test_complete_goal_run_sets_terminal_state(storage: SessionStorage) -> None:
    created = await storage.create_goal_run(_goal())
    completed = await storage.complete_goal_run(
        created.goal_id,
        expected_updated_at=created.updated_at,
    )
    assert completed.status == "complete"
    assert completed.finished_at is not None
    assert completed.terminal_reason is None
    assert await storage.get_active_goal_run(SESSION_KEY) is None

    with pytest.raises(GoalConflictError, match="changed before the update"):
        await storage.complete_goal_run(
            created.goal_id,
            expected_updated_at=created.updated_at,
            terminal_reason="goal_completed",
        )


async def test_block_goal_run_records_cause_and_terminal_reason(
    storage: SessionStorage,
) -> None:
    created = await storage.create_goal_run(_goal())
    blocked = await storage.block_goal_run(
        created.goal_id,
        expected_updated_at=created.updated_at,
        blocked_reason="no_api_key",
        terminal_reason="blocked_after_retries:no_api_key",
    )
    assert blocked.status == "blocked"
    assert blocked.blocked_reason == "no_api_key"
    assert blocked.terminal_reason == "blocked_after_retries:no_api_key"
    assert blocked.finished_at is not None
    assert await storage.get_active_goal_run(SESSION_KEY) is None


async def test_pause_resume_goal_run_roundtrip(storage: SessionStorage) -> None:
    created = await storage.create_goal_run(_goal())
    paused = await storage.pause_goal_run(
        created.goal_id,
        expected_updated_at=created.updated_at,
    )
    assert paused.status == "paused"
    assert paused.finished_at is None

    resumed = await storage.resume_goal_run(
        created.goal_id,
        expected_updated_at=paused.updated_at,
    )
    assert resumed.status == "running"
    assert await storage.get_active_goal_run(SESSION_KEY) is not None


async def test_cancel_goal_run_sets_terminal_state(storage: SessionStorage) -> None:
    created = await storage.create_goal_run(_goal())
    cancelled = await storage.cancel_goal_run(
        created.goal_id,
        expected_updated_at=created.updated_at,
        terminal_reason="user_cleared",
    )
    assert cancelled.status == "cancelled"
    assert cancelled.terminal_reason == "user_cleared"
    assert cancelled.finished_at is not None
    assert await storage.get_active_goal_run(SESSION_KEY) is None
