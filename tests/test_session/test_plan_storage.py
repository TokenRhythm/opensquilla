"""Durable collaboration-plan storage contracts."""

from __future__ import annotations

import sqlite3

import pytest
import pytest_asyncio

from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    CollaborationMode,
    PlanRevisionRecord,
    PlanRunRecord,
    PlanRunStatus,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.plans import (
    PlanConflictError,
    PlanRunConflictError,
    plan_revision_snapshot,
    plan_run_snapshot,
)
from opensquilla.session.storage import SessionStorage

SESSION_KEY = "agent:main:webchat:plans"
SESSION_ID = "session-plans"


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


def _revision(
    revision_id: str = "revision-1",
    *,
    plan_id: str = "plan-1",
    parent_revision_id: str | None = None,
    generation: int = 1,
    message_id: str = "assistant-plan-1",
) -> PlanRevisionRecord:
    return PlanRevisionRecord(
        revision_id=revision_id,
        plan_id=plan_id,
        parent_revision_id=parent_revision_id,
        generation=generation,
        source_session_key=SESSION_KEY,
        source_session_id=SESSION_ID,
        source_epoch=0,
        source_turn_id=f"turn-{generation}",
        source_message_id=message_id,
        title=f"Plan revision {generation}",
        markdown=f"## Revision {generation}",
        steps=[
            {"title": "Inspect the current behavior"},
            {
                "stepId": "implement",
                "title": "Implement the change",
                "details": "Keep the durable contracts explicit.",
            },
        ],
        content_hash="",
        created_at=100 + generation,
    )


def _assistant_entry(message_id: str) -> TranscriptEntry:
    return TranscriptEntry(
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        message_id=message_id,
        role="assistant",
        content="A structured implementation plan.",
        created_at=200,
    )


def _run(
    run_id: str,
    revision_id: str = "revision-1",
    *,
    created_at: int = 300,
    active_task_id: str | None = None,
    driver_kind: str = "manual",
    driver_id: str | None = None,
) -> PlanRunRecord:
    return PlanRunRecord(
        run_id=run_id,
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        session_epoch=0,
        plan_revision_id=revision_id,
        driver_kind=driver_kind,
        driver_id=driver_id,
        status=PlanRunStatus.QUEUED,
        active_task_id=active_task_id,
        created_at=created_at,
        updated_at=created_at,
    )


def _user_entry(message_id: str) -> TranscriptEntry:
    return TranscriptEntry(
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        message_id=message_id,
        role="user",
        content="Implement this plan.",
        created_at=400,
    )


def _task(task_id: str) -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id=task_id,
        session_key=SESSION_KEY,
        agent_id="main",
        source_kind="webui",
        queue_mode="followup",
        run_kind="web_turn",
        status=AgentTaskStatus.QUEUED,
        created_at=400,
        updated_at=400,
    )


async def test_collaboration_mode_is_user_controlled_with_cas(
    storage: SessionStorage,
) -> None:
    initial = await storage.get_session(SESSION_KEY)
    assert initial is not None
    assert initial.collaboration_mode == CollaborationMode.DEFAULT
    assert initial.collaboration_revision == 0

    planned = await storage.set_collaboration_mode(
        SESSION_KEY,
        CollaborationMode.PLAN,
        expected_revision=0,
    )
    assert planned.collaboration_mode == CollaborationMode.PLAN
    assert planned.collaboration_revision == 1

    unchanged = await storage.set_collaboration_mode(
        SESSION_KEY,
        CollaborationMode.PLAN,
        expected_revision=1,
    )
    assert unchanged.collaboration_revision == 1

    with pytest.raises(PlanConflictError, match="changed"):
        await storage.set_collaboration_mode(
            SESSION_KEY,
            CollaborationMode.DEFAULT,
            expected_revision=0,
        )


async def test_append_plan_revision_is_atomic_immutable_and_idempotent(
    storage: SessionStorage,
) -> None:
    revision = _revision()
    entry = _assistant_entry("assistant-plan-1")
    entry.token_count = 7
    created = await storage.append_plan_revision(
        entry,
        revision,
        expected_epoch=0,
        expected_parent_revision_id=None,
    )

    assert created.content_hash
    assert [step["step_id"] for step in created.steps] == ["step-1", "implement"]
    current = await storage.get_current_plan_revision(SESSION_KEY)
    assert current == created
    session = await storage.get_session(SESSION_KEY)
    assert session is not None
    assert session.active_plan_revision_id == created.revision_id
    assert session.collaboration_revision == 1
    assert session.total_tokens == 7
    assert session.total_tokens_fresh is False
    assert await storage.count_transcript_entries(SESSION_ID) == 1

    replay = await storage.append_plan_revision(
        _assistant_entry("assistant-plan-1"),
        revision,
        expected_epoch=0,
        expected_parent_revision_id=None,
    )
    assert replay == created
    assert await storage.count_transcript_entries(SESSION_ID) == 1

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        await storage.conn.execute(
            "UPDATE plan_revisions SET title = 'mutated' WHERE revision_id = ?",
            (created.revision_id,),
        )


async def test_replan_requires_the_current_parent_and_preserves_plan_identity(
    storage: SessionStorage,
) -> None:
    first = await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    second = await storage.create_plan_revision(
        _revision(
            "revision-2",
            parent_revision_id=first.revision_id,
            generation=2,
            message_id="assistant-plan-2",
        ),
        expected_parent_revision_id=first.revision_id,
    )

    assert second.plan_id == first.plan_id
    assert second.generation == 2
    assert await storage.get_current_plan_revision(SESSION_KEY) == second
    assert [
        revision.revision_id
        for revision in await storage.list_plan_revisions(plan_id=first.plan_id)
    ] == ["revision-2", "revision-1"]

    stale = _revision(
        "revision-stale",
        parent_revision_id=first.revision_id,
        generation=2,
        message_id="assistant-plan-stale",
    )
    with pytest.raises(PlanConflictError, match="active plan revision changed"):
        await storage.create_plan_revision(
            stale,
            expected_parent_revision_id=first.revision_id,
        )
    assert await storage.get_plan_revision("revision-stale") is None


async def test_plan_run_progress_is_server_authoritative_and_cas_guarded(
    storage: SessionStorage,
) -> None:
    revision = await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    queued = await storage.start_plan_run(_run("run-1", revision.revision_id))
    assert queued.status == PlanRunStatus.QUEUED
    assert [step["status"] for step in queued.step_states] == ["pending", "pending"]

    running = await storage.mark_plan_run_running(
        queued.run_id,
        expected_state_revision=0,
        active_task_id="task-1",
    )
    assert running.status == PlanRunStatus.RUNNING
    assert running.current_step_id == "step-1"
    assert running.step_states[0]["status"] == "in_progress"
    assert running.state_revision == 1

    with pytest.raises(PlanRunConflictError, match="state changed"):
        await storage.checkpoint_plan_run(
            running.run_id,
            expected_state_revision=0,
            step_id="step-1",
            step_status="completed",
        )

    advanced = await storage.checkpoint_plan_run(
        running.run_id,
        expected_state_revision=1,
        expected_active_task_id="task-1",
        step_id="step-1",
        step_status="completed",
    )
    assert advanced.status == PlanRunStatus.RUNNING
    assert advanced.current_step_id == "implement"
    assert [step["status"] for step in advanced.step_states] == [
        "completed",
        "in_progress",
    ]

    final_checkpoint = await storage.checkpoint_plan_run(
        running.run_id,
        expected_state_revision=2,
        expected_active_task_id="task-1",
        step_id="implement",
        step_status="completed",
        reason="all_steps_completed",
    )
    assert final_checkpoint.status == PlanRunStatus.RUNNING
    assert final_checkpoint.current_step_id is None
    assert final_checkpoint.active_task_id == "task-1"
    assert final_checkpoint.finished_at is None
    assert [step["status"] for step in final_checkpoint.step_states] == [
        "completed",
        "completed",
    ]
    assert await storage.get_active_plan_run(SESSION_KEY) == final_checkpoint

    completed = await storage.complete_plan_run(
        running.run_id,
        expected_state_revision=final_checkpoint.state_revision,
        expected_active_task_id="task-1",
    )
    assert completed.status == PlanRunStatus.COMPLETED
    assert completed.current_step_id is None
    assert completed.active_task_id is None
    assert completed.finished_at is not None
    assert await storage.get_active_plan_run(SESSION_KEY) is None


async def test_complete_plan_run_requires_final_checkpoint_owner_and_fresh_revision(
    storage: SessionStorage,
) -> None:
    revision = await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    queued = await storage.start_plan_run(_run("run-completion-cas", revision.revision_id))
    running = await storage.mark_plan_run_running(
        queued.run_id,
        expected_state_revision=queued.state_revision,
        active_task_id="task-1",
    )

    with pytest.raises(PlanRunConflictError, match="final checkpoint"):
        await storage.complete_plan_run(
            running.run_id,
            expected_state_revision=running.state_revision,
            expected_active_task_id="task-1",
        )
    with pytest.raises(PlanRunConflictError, match="another task"):
        await storage.complete_plan_run(
            running.run_id,
            expected_state_revision=running.state_revision,
            expected_active_task_id="task-other",
        )

    await storage.conn.execute(
        "UPDATE plan_runs SET current_step_id = NULL WHERE run_id = ?",
        (running.run_id,),
    )
    with pytest.raises(PlanRunConflictError, match="unfinished steps"):
        await storage.complete_plan_run(
            running.run_id,
            expected_state_revision=running.state_revision,
            expected_active_task_id="task-1",
        )
    await storage.conn.execute(
        "UPDATE plan_runs SET current_step_id = 'step-1' WHERE run_id = ?",
        (running.run_id,),
    )

    advanced = await storage.checkpoint_plan_run(
        running.run_id,
        expected_state_revision=running.state_revision,
        expected_active_task_id="task-1",
        step_id="step-1",
        step_status="completed",
    )
    final_checkpoint = await storage.checkpoint_plan_run(
        running.run_id,
        expected_state_revision=advanced.state_revision,
        expected_active_task_id="task-1",
        step_id="implement",
        step_status="skipped",
        reason="No implementation was required.",
    )

    with pytest.raises(PlanRunConflictError, match="state changed"):
        await storage.complete_plan_run(
            running.run_id,
            expected_state_revision=advanced.state_revision,
            expected_active_task_id="task-1",
        )
    completed = await storage.complete_plan_run(
        running.run_id,
        expected_state_revision=final_checkpoint.state_revision,
        expected_active_task_id="task-1",
    )
    assert completed.status == PlanRunStatus.COMPLETED


async def test_resuming_paused_or_blocked_run_clears_stale_terminal_reason(
    storage: SessionStorage,
) -> None:
    await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    queued = await storage.start_plan_run(
        _run("run-clear-reason", active_task_id="task-1")
    )
    running = await storage.mark_plan_run_running(
        queued.run_id,
        expected_state_revision=queued.state_revision,
        active_task_id="task-1",
    )
    blocked = await storage.checkpoint_plan_run(
        running.run_id,
        expected_state_revision=running.state_revision,
        expected_active_task_id="task-1",
        step_id="step-1",
        step_status="blocked",
        reason="waiting_for_dependency",
    )
    await storage.conn.execute(
        "UPDATE plan_runs SET terminal_reason = 'legacy_reason' WHERE run_id = ?",
        (blocked.run_id,),
    )

    queued_again = await storage.start_plan_run(
        blocked.model_copy(
            update={
                "active_task_id": "task-2",
                "state_revision": blocked.state_revision,
            }
        )
    )
    assert queued_again.status == PlanRunStatus.QUEUED
    assert queued_again.terminal_reason is None

    await storage.conn.execute(
        "UPDATE plan_runs SET terminal_reason = 'legacy_reason' WHERE run_id = ?",
        (queued_again.run_id,),
    )
    running_again = await storage.mark_plan_run_running(
        queued_again.run_id,
        expected_state_revision=queued_again.state_revision,
        active_task_id="task-2",
    )
    assert running_again.status == PlanRunStatus.RUNNING
    assert running_again.terminal_reason is None


async def test_plan_run_preserves_skipped_reason_in_storage_and_snapshot(
    storage: SessionStorage,
) -> None:
    revision = await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    queued = await storage.start_plan_run(_run("run-skipped", revision.revision_id))
    running = await storage.mark_plan_run_running(
        queued.run_id,
        expected_state_revision=0,
        active_task_id="task-1",
    )

    advanced = await storage.checkpoint_plan_run(
        running.run_id,
        expected_state_revision=1,
        expected_active_task_id="task-1",
        step_id="step-1",
        step_status="skipped",
        reason="The repository already satisfies this prerequisite.",
    )
    reloaded = await storage.get_plan_run(running.run_id)

    assert advanced.step_states[0]["reason"] == (
        "The repository already satisfies this prerequisite."
    )
    assert reloaded is not None
    assert reloaded.step_states[0]["reason"] == advanced.step_states[0]["reason"]
    assert plan_run_snapshot(reloaded)["steps"][0]["reason"] == (
        advanced.step_states[0]["reason"]
    )


async def test_plan_run_pause_resume_cancel_and_supersede(
    storage: SessionStorage,
) -> None:
    await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    first = await storage.start_plan_run(_run("run-1"))
    running = await storage.mark_plan_run_running(
        first.run_id,
        expected_state_revision=0,
        active_task_id="task-1",
    )
    paused = await storage.pause_plan_run(
        first.run_id,
        expected_state_revision=running.state_revision,
        reason="manual_turn_finished",
    )
    assert paused.status == PlanRunStatus.PAUSED
    assert paused.active_task_id is None

    resumed = await storage.mark_plan_run_running(
        first.run_id,
        expected_state_revision=paused.state_revision,
        active_task_id="task-2",
    )
    cancelled = await storage.cancel_plan_run(
        first.run_id,
        expected_state_revision=resumed.state_revision,
        reason="cancelled_by_user",
    )
    assert cancelled.status == PlanRunStatus.CANCELLED
    assert cancelled.finished_at is not None

    second = await storage.start_plan_run(_run("run-2", created_at=500))
    second = await storage.pause_plan_run(
        second.run_id,
        expected_state_revision=second.state_revision,
        reason="manual_turn_finished",
    )
    third = await storage.start_plan_run(_run("run-3", created_at=600))
    persisted_second = await storage.get_plan_run(second.run_id)
    assert persisted_second is not None
    assert persisted_second.status == PlanRunStatus.SUPERSEDED
    assert third.supersedes_run_id == second.run_id
    assert await storage.get_active_plan_run(SESSION_KEY) == third


async def test_distinct_plan_runs_have_monotonic_creation_order_within_one_millisecond(
    storage: SessionStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("opensquilla.session.storage._now_ms", lambda: 500)
    await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    first = await storage.start_plan_run(_run("run-same-ms-1", created_at=500))
    cancelled = await storage.cancel_plan_run(
        first.run_id,
        expected_state_revision=first.state_revision,
        reason="cancelled_by_user",
    )
    second = await storage.start_plan_run(_run("run-same-ms-2", created_at=500))

    assert cancelled.created_at == 500
    assert second.created_at == 501
    assert second.created_at > cancelled.created_at


async def test_same_revision_resume_reuses_progress_and_rebinds_task(
    storage: SessionStorage,
) -> None:
    await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    queued = await storage.start_plan_run(
        _run("run-resume", active_task_id="task-1")
    )
    running = await storage.mark_plan_run_running(
        queued.run_id,
        expected_state_revision=queued.state_revision,
        active_task_id="task-1",
    )
    advanced = await storage.checkpoint_plan_run(
        running.run_id,
        expected_state_revision=running.state_revision,
        expected_active_task_id="task-1",
        step_id="step-1",
        step_status="completed",
    )
    paused = await storage.pause_plan_run(
        advanced.run_id,
        expected_state_revision=advanced.state_revision,
        reason="manual_turn_finished",
    )

    rebound = await storage.start_plan_run(
        paused.model_copy(update={"active_task_id": "task-2"})
    )

    assert rebound.run_id == paused.run_id
    assert rebound.status == PlanRunStatus.QUEUED
    assert rebound.active_task_id == "task-2"
    assert rebound.current_step_id == "implement"
    assert [step["status"] for step in rebound.step_states] == [
        "completed",
        "in_progress",
    ]


async def test_delivery_ready_run_can_resume_without_reopening_completed_steps(
    storage: SessionStorage,
) -> None:
    await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    queued = await storage.start_plan_run(
        _run(
            "run-delivery-resume",
            active_task_id="goal-task-1",
            driver_kind="goal",
            driver_id="goal-a",
        )
    )
    running = await storage.mark_plan_run_running(
        queued.run_id,
        expected_state_revision=queued.state_revision,
        active_task_id="goal-task-1",
    )
    advanced = await storage.checkpoint_plan_run(
        running.run_id,
        expected_state_revision=running.state_revision,
        expected_active_task_id="goal-task-1",
        step_id="step-1",
        step_status="completed",
    )
    delivery_ready = await storage.checkpoint_plan_run(
        advanced.run_id,
        expected_state_revision=advanced.state_revision,
        expected_active_task_id="goal-task-1",
        step_id="implement",
        step_status="completed",
    )
    paused = await storage.pause_plan_run(
        delivery_ready.run_id,
        expected_state_revision=delivery_ready.state_revision,
        expected_active_task_id="goal-task-1",
        expected_driver_kind="goal",
        expected_driver_id="goal-a",
        reason="goal_turn_failed",
    )
    rebound = await storage.start_plan_run(
        paused.model_copy(update={"active_task_id": "goal-task-2"})
    )

    resumed = await storage.mark_plan_run_running(
        rebound.run_id,
        expected_state_revision=rebound.state_revision,
        active_task_id="goal-task-2",
    )

    assert resumed.status == PlanRunStatus.RUNNING
    assert resumed.current_step_id is None
    assert resumed.active_task_id == "goal-task-2"
    assert resumed.driver_kind == "goal"
    assert resumed.driver_id == "goal-a"
    assert [step["status"] for step in resumed.step_states] == [
        "completed",
        "completed",
    ]


async def test_restart_abandonment_releases_plan_run_owner_for_resume(
    storage: SessionStorage,
) -> None:
    await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    await storage.create_agent_task(_task("goal-task-restart"))
    queued = await storage.start_plan_run(
        _run(
            "run-restart-resume",
            active_task_id="goal-task-restart",
            driver_kind="goal",
            driver_id="goal-a",
        )
    )
    running = await storage.mark_plan_run_running(
        queued.run_id,
        expected_state_revision=queued.state_revision,
        active_task_id="goal-task-restart",
    )
    await storage.update_agent_task(
        "goal-task-restart",
        status=AgentTaskStatus.RUNNING,
        started_at=450,
    )

    abandoned = await storage.mark_abandoned_agent_tasks(now_ms=500)
    recovered = await storage.get_plan_run(running.run_id)

    assert abandoned == 1
    assert recovered is not None
    assert recovered.status == PlanRunStatus.PAUSED
    assert recovered.active_task_id is None
    assert recovered.pause_reason == "process_restart"
    assert recovered.terminal_reason == "process_restart"
    assert recovered.state_revision == running.state_revision + 1
    assert recovered.driver_kind == "goal"
    assert recovered.driver_id == "goal-a"
    assert recovered.current_step_id == "step-1"
    assert recovered.step_states[0]["status"] == "in_progress"

    rebound = await storage.start_plan_run(
        recovered.model_copy(update={"active_task_id": "goal-task-next"})
    )
    resumed = await storage.mark_plan_run_running(
        rebound.run_id,
        expected_state_revision=rebound.state_revision,
        active_task_id="goal-task-next",
    )
    assert resumed.status == PlanRunStatus.RUNNING
    assert resumed.active_task_id == "goal-task-next"
    assert resumed.driver_kind == "goal"
    assert resumed.driver_id == "goal-a"


async def test_goal_run_owner_must_resume_the_same_run_and_driver(
    storage: SessionStorage,
) -> None:
    await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    goal = await storage.start_plan_run(
        _run(
            "goal-run",
            active_task_id="goal-task-1",
            driver_kind="goal",
            driver_id="goal-a",
        )
    )
    running = await storage.mark_plan_run_running(
        goal.run_id,
        expected_state_revision=goal.state_revision,
        active_task_id="goal-task-1",
    )
    paused = await storage.pause_plan_run(
        goal.run_id,
        expected_state_revision=running.state_revision,
        expected_active_task_id="goal-task-1",
        expected_driver_kind="goal",
        expected_driver_id="goal-a",
        reason="goal_turn_finished",
    )

    with pytest.raises(PlanRunConflictError, match="different execution driver"):
        await storage.start_plan_run(
            _run(
                "goal-b-takeover",
                active_task_id="goal-task-b",
                driver_kind="goal",
                driver_id="goal-b",
                created_at=500,
            )
        )
    with pytest.raises(PlanRunConflictError, match="existing run_id"):
        await storage.start_plan_run(
            _run(
                "goal-a-replacement",
                active_task_id="goal-task-2",
                driver_kind="goal",
                driver_id="goal-a",
                created_at=600,
            )
        )
    with pytest.raises(PlanRunConflictError, match="different execution driver"):
        await storage.start_plan_run(
            paused.model_copy(
                update={
                    "active_task_id": "goal-task-b",
                    "driver_id": "goal-b",
                }
            )
        )

    resumed = await storage.start_plan_run(
        paused.model_copy(update={"active_task_id": "goal-task-2"})
    )
    assert resumed.run_id == goal.run_id
    assert resumed.driver_kind == "goal"
    assert resumed.driver_id == "goal-a"
    assert resumed.active_task_id == "goal-task-2"


async def test_old_task_cleanup_cannot_pause_a_rebound_run(
    storage: SessionStorage,
) -> None:
    await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    queued = await storage.start_plan_run(
        _run("run-rebound-owner", active_task_id="task-old")
    )
    running = await storage.mark_plan_run_running(
        queued.run_id,
        expected_state_revision=queued.state_revision,
        active_task_id="task-old",
    )
    blocked = await storage.checkpoint_plan_run(
        running.run_id,
        expected_state_revision=running.state_revision,
        expected_active_task_id="task-old",
        step_id="step-1",
        step_status="blocked",
        reason="waiting_for_dependency",
    )
    rebound = await storage.start_plan_run(
        blocked.model_copy(update={"active_task_id": "task-new"})
    )
    rebound = await storage.mark_plan_run_running(
        rebound.run_id,
        expected_state_revision=rebound.state_revision,
        active_task_id="task-new",
    )

    with pytest.raises(PlanRunConflictError, match="another task"):
        await storage.pause_plan_run(
            rebound.run_id,
            expected_state_revision=rebound.state_revision,
            expected_active_task_id="task-old",
            reason="manual_turn_finished",
        )
    assert await storage.get_plan_run(rebound.run_id) == rebound


async def test_new_revision_supersedes_only_an_idle_prior_run(
    storage: SessionStorage,
) -> None:
    first = await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    queued = await storage.start_plan_run(_run("run-prior"))
    with pytest.raises(PlanRunConflictError, match="implementation task is active"):
        await storage.create_plan_revision(
            _revision(
                "revision-2",
                parent_revision_id=first.revision_id,
                generation=2,
                message_id="assistant-plan-2",
            ),
            expected_parent_revision_id=first.revision_id,
        )
    assert (await storage.get_session(SESSION_KEY)).active_plan_revision_id == first.revision_id

    paused = await storage.pause_plan_run(
        queued.run_id,
        expected_state_revision=queued.state_revision,
        reason="manual_turn_finished",
    )
    second = await storage.create_plan_revision(
        _revision(
            "revision-2",
            parent_revision_id=first.revision_id,
            generation=2,
            message_id="assistant-plan-2",
        ),
        expected_parent_revision_id=first.revision_id,
    )

    persisted = await storage.get_plan_run(paused.run_id)
    assert second.parent_revision_id == first.revision_id
    assert persisted is not None
    assert persisted.status == PlanRunStatus.SUPERSEDED
    assert persisted.terminal_reason == "superseded_by_new_revision"
    assert await storage.get_active_plan_run(SESSION_KEY) is None


async def test_mark_running_supersedes_a_run_if_revision_changed_at_boundary(
    storage: SessionStorage,
) -> None:
    revision = await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    queued = await storage.start_plan_run(
        _run("run-stale", active_task_id="task-stale")
    )
    await storage.conn.execute(
        """
        UPDATE sessions
        SET active_plan_revision_id = NULL
        WHERE session_key = ?
        """,
        (SESSION_KEY,),
    )

    stale = await storage.mark_plan_run_running(
        queued.run_id,
        expected_state_revision=queued.state_revision,
        active_task_id="task-stale",
    )

    assert revision.revision_id == queued.plan_revision_id
    assert stale.status == PlanRunStatus.SUPERSEDED
    assert stale.terminal_reason == "stale_plan_revision"


async def test_accept_turn_can_copy_a_revision_and_start_its_run_atomically(
    storage: SessionStorage,
) -> None:
    copied = _revision(
        revision_id="copied-revision",
        plan_id="copied-plan",
        message_id="copied-source-message",
    )
    run = _run(
        "copied-run",
        revision_id=copied.revision_id,
        active_task_id="copied-task",
    )

    accepted = await storage.accept_turn(
        _user_entry("copy-implementation"),
        expected_epoch=0,
        updated_at=500,
        task_record=_task("copied-task"),
        source_scope="webui",
        request_session_key=SESSION_KEY,
        client_request_id="copy-request",
        request_fingerprint="sha256:copy-request",
        session_updates={"collaboration_mode": CollaborationMode.DEFAULT},
        plan_revision=copied,
        plan_run=run,
    )

    assert accepted.replayed is False
    node = await storage.get_session(SESSION_KEY)
    assert node is not None
    assert node.active_plan_revision_id == copied.revision_id
    persisted = await storage.get_plan_run(run.run_id)
    assert persisted is not None
    assert persisted.active_task_id == "copied-task"


async def test_accept_turn_atomically_starts_run_and_updates_collaboration_state(
    storage: SessionStorage,
) -> None:
    planned = await storage.set_collaboration_mode(
        SESSION_KEY,
        CollaborationMode.PLAN,
        expected_revision=0,
    )
    revision = await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    assert planned.collaboration_revision == 1
    old_run = await storage.start_plan_run(_run("run-old"))
    old_run = await storage.pause_plan_run(
        old_run.run_id,
        expected_state_revision=old_run.state_revision,
        reason="manual_turn_finished",
    )

    accepted = await storage.accept_turn(
        _user_entry("user-implement"),
        expected_epoch=0,
        updated_at=400,
        task_record=_task("task-implement"),
        source_scope="webui",
        request_session_key=SESSION_KEY,
        client_request_id="request-implement",
        request_fingerprint="sha256:request-implement",
        session_updates={
            "collaboration_mode": CollaborationMode.DEFAULT,
            "active_plan_revision_id": revision.revision_id,
        },
        plan_run=_run(
            "run-new",
            created_at=400,
            active_task_id="task-implement",
        ),
    )

    assert accepted.replayed is False
    current_session = await storage.get_session(SESSION_KEY)
    assert current_session is not None
    assert current_session.collaboration_mode == CollaborationMode.DEFAULT
    assert current_session.active_plan_revision_id == revision.revision_id
    assert current_session.collaboration_revision == 3
    persisted_old = await storage.get_plan_run(old_run.run_id)
    assert persisted_old is not None
    assert persisted_old.status == PlanRunStatus.SUPERSEDED
    active = await storage.get_active_plan_run(SESSION_KEY)
    assert active is not None
    assert active.run_id == "run-new"
    assert await storage.get_agent_task("task-implement") is not None
    assert [
        entry.message_id for entry in await storage.get_transcript(SESSION_ID)
    ] == ["user-implement"]


async def test_accept_turn_rolls_back_plan_run_supersession_on_failure(
    storage: SessionStorage,
) -> None:
    await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    original = await storage.start_plan_run(_run("run-original"))
    original = await storage.pause_plan_run(
        original.run_id,
        expected_state_revision=original.state_revision,
        reason="manual_turn_finished",
    )
    await storage.conn.execute(
        """
        CREATE TRIGGER fail_plan_acceptance_task
        BEFORE INSERT ON agent_tasks
        BEGIN
            SELECT RAISE(ABORT, 'injected task failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected task failure"):
        await storage.accept_turn(
            _user_entry("user-failed"),
            expected_epoch=0,
            updated_at=400,
            task_record=_task("task-failed"),
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="request-failed",
            request_fingerprint="sha256:request-failed",
            session_updates={"collaboration_mode": CollaborationMode.DEFAULT},
            plan_run=_run("run-prospective", active_task_id="task-failed"),
        )

    active = await storage.get_active_plan_run(SESSION_KEY)
    assert active is not None
    assert active.run_id == original.run_id
    assert active.status == PlanRunStatus.PAUSED
    assert await storage.get_plan_run("run-prospective") is None
    assert await storage.count_transcript_entries(SESSION_ID) == 0
    assert await storage.get_agent_task("task-failed") is None


async def test_plan_snapshots_are_camel_case_and_keep_progress_out_of_revision(
    storage: SessionStorage,
) -> None:
    revision = await storage.create_plan_revision(
        _revision(),
        expected_parent_revision_id=None,
    )
    run = await storage.start_plan_run(_run("run-1"))

    revision_payload = plan_revision_snapshot(revision, current=True)
    run_payload = plan_run_snapshot(run)

    assert revision_payload["revisionId"] == revision.revision_id
    assert revision_payload["steps"][0] == {
        "stepId": "step-1",
        "title": "Inspect the current behavior",
    }
    assert "status" not in revision_payload["steps"][0]
    assert run_payload["planRevisionId"] == revision.revision_id
    assert run_payload["steps"][0]["status"] == "pending"
