"""Tests for the TaskRuntime terminal-state dict leak fix.

Verifies that, at terminal state, the four short-lived tracking dicts
(``_tasks``, ``_running_by_session``, ``_pending_by_session``,
``_last_envelope_by_session``) drop the task / session_key, while
``_session_locks`` is intentionally retained to prevent split-brain on
rapid re-enqueue. Also covers exception-path cleanup and a 10 000-task
tracemalloc-bounded soak.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import tracemalloc
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway import task_runtime
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.desktop_artifact_bridge import TurnAuthorityCleanup
from opensquilla.gateway.routing import (
    RouteEnvelope,
    SourceKind,
    tool_context_from_envelope,
)
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_sessions import _handle_plans_cancel_run
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.sandbox.guest_profile import GuestProfileFactory
from opensquilla.session.goals import (
    GOAL_OBJECTIVE_UPDATE_DETAIL_KEY,
    GoalObjectiveUpdate,
    GoalTurnContext,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    PlanRevisionRecord,
    PlanRunRecord,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage
from opensquilla.session.turn_context import current_turn_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope(session_key: str = "agent-1::sess-1") -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="agent-1",
        session_key=session_key,
        input_provenance={"kind": "test"},
    )


def _make_storage() -> Any:
    """Minimal storage mock."""
    storage = MagicMock()
    task_db: dict[str, AgentTaskRecord] = {}

    async def create(record: AgentTaskRecord) -> None:
        task_db[record.task_id] = record

    async def update(task_id: str, **kwargs: Any) -> None:
        rec = task_db.get(task_id)
        if rec is None:
            return
        for k, v in kwargs.items():
            if hasattr(rec, k):
                object.__setattr__(rec, k, v)

    async def get(task_id: str) -> AgentTaskRecord | None:
        return task_db.get(task_id)

    async def list_tasks(**_: Any) -> list[AgentTaskRecord]:
        return list(task_db.values())

    turn_context_updates: list[tuple[str, str, dict[str, Any]]] = []

    async def update_turn_context(
        session_key: str,
        message_id: str,
        context: dict[str, Any],
    ) -> bool:
        turn_context_updates.append((session_key, message_id, dict(context)))
        return True

    storage.create_agent_task = create
    storage.update_agent_task = update
    storage.get_agent_task = get
    storage.list_agent_tasks = list_tasks
    storage.update_transcript_turn_context = update_turn_context
    storage.turn_context_updates = turn_context_updates
    return storage


def _authority_envelope(
    authority: TurnAuthorityCleanup,
    session_key: str,
) -> RouteEnvelope:
    return replace(
        _make_envelope(session_key),
        runtime_services={
            "desktop_artifact_bridge": object(),
            "turn_authority_cleanup": authority,
            "turn_cleanup_callbacks": [authority.aclose],
            "durable_service": "kept",
        },
    )


@pytest.mark.asyncio
async def test_turn_authority_cleanup_runs_once_on_success_and_reservation_abort() -> None:
    success_calls = 0
    abort_calls = 0

    async def release_success() -> None:
        nonlocal success_calls
        success_calls += 1

    async def release_abort() -> None:
        nonlocal abort_calls
        abort_calls += 1

    success_authority = TurnAuthorityCleanup(release_success)
    runtime = _make_runtime()
    success = await runtime.enqueue(
        _authority_envelope(success_authority, "agent:main:webchat:authority-success"),
        "success",
    )
    await runtime.wait(success.task_id, timeout=2.0)

    abort_authority = TurnAuthorityCleanup(release_abort)
    reservation = await runtime.reserve(
        _authority_envelope(abort_authority, "agent:main:webchat:authority-abort"),
        "abort",
    )
    await runtime.abort_reservation(reservation)
    await runtime.abort_reservation(reservation)
    await runtime.shutdown()

    assert success_calls == 1
    assert abort_calls == 1


@pytest.mark.asyncio
async def test_turn_authority_cleanup_runs_once_when_activation_fails_before_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_calls = 0

    async def release() -> None:
        nonlocal release_calls
        release_calls += 1

    runtime = _make_runtime()
    authority = TurnAuthorityCleanup(release)

    async def fail_activation(_reservation: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("synthetic pre-boundary activation failure")

    monkeypatch.setattr(runtime, "activate", fail_activation)

    with pytest.raises(RuntimeError, match="pre-boundary activation failure"):
        await runtime.enqueue(
            _authority_envelope(
                authority,
                "agent:main:webchat:authority-activation-failure",
            ),
            "activation failure",
        )

    await authority.aclose()
    await runtime.shutdown()

    assert release_calls == 1
    assert runtime._reservations_by_session == {}


def test_reusable_route_envelope_strips_all_turn_authority() -> None:
    async def release() -> None:
        return None

    authority = TurnAuthorityCleanup(release)
    reusable = task_runtime._reusable_route_envelope(
        _authority_envelope(authority, "agent:main:webchat:authority-cache")
    )

    assert reusable.runtime_services == {"durable_service": "kept"}


def _make_runtime(
    turn_handler: Callable[..., Awaitable[Any]] | None = None,
    max_concurrency: int = 4,
    max_pending_per_session: int | None = 64,
) -> TaskRuntime:
    async def _default_handler(_run: Any) -> None:
        pass

    return TaskRuntime(
        storage=_make_storage(),
        turn_handler=turn_handler or _default_handler,
        max_concurrency=max_concurrency,
        max_pending_per_session=max_pending_per_session,
    )


# ---------------------------------------------------------------------------
# terminal_clears_all_dicts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_terminal_clears_all_dicts() -> None:
    """After a task succeeds, tracking dicts (except _session_locks) must not contain its key.

    ``_session_locks`` is intentionally NOT cleaned at terminal to prevent
    split-brain under concurrent enqueue. All other dicts are cleaned.
    """
    rt = _make_runtime()
    env = _make_envelope("agent-1::sess-a")
    handle = await rt.enqueue(env, "hello")
    await rt.wait(handle.task_id, timeout=2.0)

    sk = env.session_key
    assert handle.task_id not in rt._tasks
    assert sk not in rt._running_by_session
    assert sk not in rt._pending_by_session
    # _session_locks is intentionally retained: never pop while _execute may
    # still hold the lock; prevents split-brain on rapid re-enqueue.
    assert sk not in rt._last_envelope_by_session


@pytest.mark.asyncio
async def test_terminal_lifecycle_settles_before_session_lane_cleanup() -> None:
    """The current task remains admission-visible until lifecycle settlement ends."""

    terminal_entered = asyncio.Event()
    release_terminal = asyncio.Event()

    async def lifecycle(event: Any) -> None:
        if event.phase != "terminal":
            return
        terminal_entered.set()
        await release_terminal.wait()

    rt = _make_runtime()
    rt.set_lifecycle_listener(lifecycle)
    env = _make_envelope("agent-1::sess-terminal-settlement")
    handle = await rt.enqueue(env, "hello")

    await asyncio.wait_for(terminal_entered.wait(), timeout=2.0)
    persisted = await rt.status(handle.task_id)
    assert persisted.status is AgentTaskStatus.SUCCEEDED
    assert await rt.has_session_work(env.session_key) is True
    assert rt._running_by_session[env.session_key].task_id == handle.task_id
    assert handle.task_id in rt._tasks

    waiter = asyncio.create_task(rt.wait(handle.task_id, timeout=2.0))
    await asyncio.sleep(0)
    assert not waiter.done()

    release_terminal.set()
    settled = await waiter
    assert settled.status is AgentTaskStatus.SUCCEEDED
    assert await rt.has_session_work(env.session_key) is False
    assert env.session_key not in rt._running_by_session
    assert handle.task_id not in rt._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("followup_api", ["send", "send_with_envelope"])
async def test_guest_runtime_send_materializes_fresh_profile_per_task(
    tmp_path,
    followup_api: str,
) -> None:
    """A reusable route must never point a follow-up at the prior deleted root."""

    state_dir = tmp_path / "state"
    first_profile = GuestProfileFactory.create("ingress", state_dir=state_dir)
    started = asyncio.Event()
    release = asyncio.Event()
    observed_roots: list[Path] = []
    roots_were_live: list[bool] = []

    async def handler(run: Any) -> None:
        root = Path(run.envelope.metadata["guest_profile_root"])
        observed_roots.append(root)
        roots_were_live.append(root.is_dir())
        if len(observed_roots) == 1:
            started.set()
            await release.wait()

    rt = _make_runtime(handler, max_concurrency=1)
    envelope = replace(
        _make_envelope("agent:main:webchat:guest:runtime-send"),
        metadata={
            "guest_safe": True,
            "guest_profile_root": str(first_profile.root),
            "guest_managed_root": str(first_profile.managed_root),
            "guest_environment": dict(first_profile.environment),
            "run_mode": "safe",
            "sandbox_mounts": first_profile.run_context().to_origin_payload()["mounts"],
            "sandbox_run_context": first_profile.run_context().to_origin_payload(),
        },
        sandbox_run_context_fresh=True,
        runtime_services={
            "guest_profile_factory": lambda task_id: GuestProfileFactory.create(
                task_id,
                state_dir=state_dir,
            )
        },
    )

    first = await rt.enqueue(envelope, "first")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    if followup_api == "send_with_envelope":
        second = await rt.send_with_envelope(envelope, "second")
    else:
        second = await rt.send(envelope.session_key, "second")
    cached = rt._last_envelope_by_session[envelope.session_key]
    try:
        release.set()
        first_record = await rt.wait(first.task_id, timeout=2.0)
        second_record = await rt.wait(second.task_id, timeout=2.0)
    finally:
        release.set()
        await rt.shutdown()
        first_profile.cleanup()

    assert first_record.status.value == "succeeded", first_record.error_message
    assert second_record.status.value == "succeeded", second_record.error_message
    assert observed_roots[0] != observed_roots[1]
    assert roots_were_live == [True, True]
    assert not observed_roots[0].exists()
    assert not observed_roots[1].exists()
    for stale_key in (
        "guest_profile_root",
        "guest_environment",
        "sandbox_mounts",
        "sandbox_run_context",
    ):
        assert stale_key not in cached.metadata


@pytest.mark.asyncio
async def test_guest_profile_is_cleaned_when_driver_is_cancelled_before_start(
    tmp_path,
) -> None:
    """Activation followed by same-tick cancellation cannot leak a guest root."""

    state_dir = tmp_path / "state"
    ingress_profile = GuestProfileFactory.create("ingress", state_dir=state_dir)
    envelope = replace(
        _make_envelope("agent:main:webchat:guest:prestart-cancel"),
        metadata={"guest_safe": True},
        runtime_services={
            "guest_profile_factory": lambda task_id: GuestProfileFactory.create(
                task_id,
                state_dir=state_dir,
            )
        },
    )
    storage = _make_storage()
    rt = TaskRuntime(storage=storage, turn_handler=lambda _run: None)

    async def no_yield_queued_event(*_args: Any, **_kwargs: Any) -> None:
        return None

    rt._emit_queued_activation = no_yield_queued_event  # type: ignore[method-assign]
    reservation = await rt.reserve(envelope, "cancel immediately")
    profile_root = Path(
        reservation.runtime_task.envelope.metadata["guest_profile_root"]
    )
    await storage.create_agent_task(reservation.task_record)

    try:
        handle = await rt.activate(reservation)
        assert profile_root.is_dir()
        assert await rt.cancel(task_id=handle.task_id) == 1
        record = await rt.wait(handle.task_id, timeout=2.0)
    finally:
        await rt.shutdown()
        ingress_profile.cleanup()

    assert record.status.value == "cancelled"
    assert not profile_root.exists()


async def _make_durable_plan_run(
    *,
    session_key: str,
    run_id: str,
    task_id: str,
    driver_kind: str = "manual",
    driver_id: str | None = None,
) -> tuple[SessionStorage, PlanRunRecord]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    node = SessionNode(
        session_key=session_key,
        session_id="session-plan-runtime",
        agent_id="agent-1",
        created_at=100,
        updated_at=100,
    )
    await storage.upsert_session(node)
    revision = await storage.create_plan_revision(
        PlanRevisionRecord(
            revision_id="revision-plan-runtime",
            plan_id="plan-runtime",
            generation=1,
            source_session_key=session_key,
            source_session_id=node.session_id,
            source_epoch=0,
            source_message_id="assistant-plan-runtime",
            title="Runtime plan",
            markdown="## Runtime plan",
            steps=[
                {"step_id": "inspect", "title": "Inspect"},
                {"step_id": "implement", "title": "Implement"},
            ],
            content_hash="",
            created_at=101,
        ),
        expected_parent_revision_id=None,
    )
    run = await storage.start_plan_run(
        PlanRunRecord(
            run_id=run_id,
            session_key=session_key,
            session_id=node.session_id,
            session_epoch=0,
            plan_revision_id=revision.revision_id,
            driver_kind=driver_kind,
            driver_id=driver_id,
            status="queued",
            active_task_id=task_id,
            created_at=102,
            updated_at=102,
        )
    )
    return storage, run


@pytest.mark.asyncio
async def test_plan_run_is_running_only_during_its_execution_turn() -> None:
    session_key = "agent-1::plan-runtime"
    task_id = "task-plan-runtime"
    storage, run = await _make_durable_plan_run(
        session_key=session_key,
        run_id="run-plan-runtime",
        task_id=task_id,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    observed_statuses: list[str] = []
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _handler(_run: Any) -> None:
        current = await storage.get_plan_run(run.run_id)
        assert current is not None
        observed_statuses.append(current.status)
        entered.set()
        await release.wait()

    async def _emit(session: str, name: str, payload: dict[str, Any]) -> None:
        events.append((session, name, payload))

    rt = TaskRuntime(storage=storage, turn_handler=_handler, event_emitter=_emit)
    envelope = replace(
        _make_envelope(session_key),
        metadata={"plan_run_id": run.run_id},
    )
    handle = await rt.enqueue(envelope, "Implement the plan", task_id=task_id)
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    running = await storage.get_plan_run(run.run_id)
    assert running is not None
    assert running.status == "running"
    assert running.current_step_id == "inspect"
    assert running.step_states[0]["status"] == "in_progress"
    assert observed_statuses == ["running"]

    release.set()
    await rt.wait(handle.task_id, timeout=2.0)
    paused = await storage.get_plan_run(run.run_id)
    assert paused is not None
    assert paused.status == "paused"
    assert paused.active_task_id is None
    assert [
        payload["plan_run"]["status"]
        for _session, name, payload in events
        if name == "session.event.plan_run"
    ] == ["running", "paused"]
    await storage.close()


@pytest.mark.asyncio
async def test_plan_run_completes_only_after_owning_task_succeeds() -> None:
    session_key = "agent-1::plan-runtime-complete"
    task_id = "task-plan-runtime-complete"
    storage, run = await _make_durable_plan_run(
        session_key=session_key,
        run_id="run-plan-runtime-complete",
        task_id=task_id,
    )
    observed_after_final_checkpoint: list[tuple[str, str | None, str | None]] = []

    async def _handler(_run: Any) -> None:
        current = await storage.get_plan_run(run.run_id)
        assert current is not None
        advanced = await storage.checkpoint_plan_run(
            run.run_id,
            expected_state_revision=current.state_revision,
            expected_active_task_id=task_id,
            step_id="inspect",
            step_status="completed",
        )
        final_checkpoint = await storage.checkpoint_plan_run(
            run.run_id,
            expected_state_revision=advanced.state_revision,
            expected_active_task_id=task_id,
            step_id="implement",
            step_status="completed",
        )
        observed_after_final_checkpoint.append(
            (
                final_checkpoint.status,
                final_checkpoint.current_step_id,
                final_checkpoint.active_task_id,
            )
        )

    runtime = TaskRuntime(storage=storage, turn_handler=_handler)
    handle = await runtime.enqueue(
        replace(
            _make_envelope(session_key),
            metadata={"plan_run_id": run.run_id},
        ),
        "Implement the plan",
        task_id=task_id,
    )

    task = await runtime.wait(handle.task_id, timeout=2.0)
    completed = await storage.get_plan_run(run.run_id)

    assert str(task.status) == "succeeded"
    assert observed_after_final_checkpoint == [("running", None, task_id)]
    assert completed is not None
    assert completed.status == "completed"
    assert completed.active_task_id is None
    assert completed.finished_at is not None
    await storage.close()


@pytest.mark.asyncio
async def test_failed_delivery_after_final_checkpoint_remains_resumable() -> None:
    session_key = "agent-1::plan-runtime-delivery-failure"
    task_id = "task-plan-runtime-delivery-failure"
    storage, run = await _make_durable_plan_run(
        session_key=session_key,
        run_id="run-plan-runtime-delivery-failure",
        task_id=task_id,
    )

    async def _handler(_run: Any) -> None:
        current = await storage.get_plan_run(run.run_id)
        assert current is not None
        advanced = await storage.checkpoint_plan_run(
            run.run_id,
            expected_state_revision=current.state_revision,
            expected_active_task_id=task_id,
            step_id="inspect",
            step_status="completed",
        )
        final_checkpoint = await storage.checkpoint_plan_run(
            run.run_id,
            expected_state_revision=advanced.state_revision,
            expected_active_task_id=task_id,
            step_id="implement",
            step_status="completed",
        )
        assert final_checkpoint.status == "running"
        assert final_checkpoint.current_step_id is None
        raise RuntimeError("artifact delivery failed")

    runtime = TaskRuntime(storage=storage, turn_handler=_handler)
    handle = await runtime.enqueue(
        replace(
            _make_envelope(session_key),
            metadata={"plan_run_id": run.run_id},
        ),
        "Implement the plan",
        task_id=task_id,
    )

    task = await runtime.wait(handle.task_id, timeout=2.0)
    paused = await storage.get_plan_run(run.run_id)

    assert str(task.status) == "failed"
    assert paused is not None
    assert paused.status == "paused"
    assert paused.current_step_id is None
    assert paused.pause_reason == "manual_turn_failed"
    assert all(
        step["status"] in {"completed", "skipped"}
        for step in paused.step_states
    )
    await storage.close()


@pytest.mark.asyncio
async def test_goal_owned_plan_run_yields_for_later_driver_attempt() -> None:
    session_key = "agent-1::goal-plan-runtime"
    task_id = "task-goal-plan-runtime"
    storage, run = await _make_durable_plan_run(
        session_key=session_key,
        run_id="run-goal-plan-runtime",
        task_id=task_id,
        driver_kind="goal",
        driver_id="goal-1",
    )

    async def _handler(task: Any) -> None:
        tool_context = tool_context_from_envelope(task.envelope, is_owner=True)
        assert tool_context.plan_revision is not None
        assert tool_context.plan_run is not None
        assert tool_context.plan_run.driver_kind == "goal"

    runtime = TaskRuntime(storage=storage, turn_handler=_handler)
    handle = await runtime.enqueue(
        replace(
            _make_envelope(session_key),
            # Future Goal attempts only need the durable run binding; runtime
            # derives the immutable revision authoritatively.
            metadata={"plan_run_id": run.run_id},
        ),
        "Continue the goal-owned plan",
        task_id=task_id,
    )

    task = await runtime.wait(handle.task_id, timeout=2.0)
    paused = await storage.get_plan_run(run.run_id)

    assert str(task.status) == "succeeded"
    assert paused is not None
    assert paused.status == "paused"
    assert paused.driver_kind == "goal"
    assert paused.driver_id == "goal-1"
    assert paused.pause_reason == "goal_turn_finished"
    assert paused.active_task_id is None
    await storage.close()


@pytest.mark.asyncio
async def test_resumed_plan_run_progress_is_injected_into_provider_prompt() -> None:
    session_key = "agent-1::plan-resume-runtime"
    first_task_id = "task-plan-resume-1"
    second_task_id = "task-plan-resume-2"
    storage, run = await _make_durable_plan_run(
        session_key=session_key,
        run_id="run-plan-resume",
        task_id=first_task_id,
    )
    running = await storage.mark_plan_run_running(
        run.run_id,
        expected_state_revision=run.state_revision,
        active_task_id=first_task_id,
    )
    advanced = await storage.checkpoint_plan_run(
        run.run_id,
        expected_state_revision=running.state_revision,
        expected_active_task_id=first_task_id,
        step_id="inspect",
        step_status="completed",
    )
    paused = await storage.pause_plan_run(
        run.run_id,
        expected_state_revision=advanced.state_revision,
        expected_active_task_id=first_task_id,
        reason="manual_turn_finished",
    )
    await storage.start_plan_run(
        paused.model_copy(update={"active_task_id": second_task_id})
    )
    captured_context: dict[str, Any] = {}

    async def _handler(task: Any) -> None:
        tool_context = tool_context_from_envelope(task.envelope, is_owner=True)
        captured_context.update(
            TurnRunner._extra_context_for_tool_context(tool_context)
        )
        assert tool_context.plan_run is not None
        assert tool_context.plan_run.status == "running"

    runtime = TaskRuntime(storage=storage, turn_handler=_handler)
    envelope = replace(
        _make_envelope(session_key),
        metadata={
            "plan_run_id": run.run_id,
        },
    )
    handle = await runtime.enqueue(
        envelope,
        "Resume the approved plan",
        task_id=second_task_id,
    )
    await runtime.wait(handle.task_id, timeout=2.0)

    progress = captured_context["PlanRun Progress"]
    payload = json.loads(progress[progress.index("{") :])
    assert payload["runId"] == run.run_id
    assert payload["currentStepId"] == "implement"
    assert payload["steps"] == [
        {"stepId": "inspect", "status": "completed"},
        {"stepId": "implement", "status": "in_progress"},
    ]
    await storage.close()


@pytest.mark.asyncio
async def test_cancel_plan_run_stops_the_implementation_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent-1::plan-cancel"
    task_id = "task-plan-cancel"
    storage, run = await _make_durable_plan_run(
        session_key=session_key,
        run_id="run-plan-cancel",
        task_id=task_id,
    )
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def _handler(_run: Any) -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _ignore_emit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_emit,
    )
    rt = TaskRuntime(storage=storage, turn_handler=_handler)
    manager = SessionManager(storage, inject_time_prefix=False, task_runtime=rt)
    ctx = RpcContext(
        conn_id="test-plan-cancel",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
        config=GatewayConfig(memory={"flush_enabled": False}),
        task_runtime=rt,
    )
    ctx.session_manager = manager
    envelope = replace(
        _make_envelope(session_key),
        metadata={"plan_run_id": run.run_id},
    )
    handle = await rt.enqueue(envelope, "Implement the plan", task_id=task_id)
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    running = await storage.get_plan_run(run.run_id)
    assert running is not None

    response = await _handle_plans_cancel_run(
        {
            "sessionKey": session_key,
            "runId": run.run_id,
            "expectedStateRevision": running.state_revision,
        },
        ctx,
    )

    await asyncio.wait_for(cancelled.wait(), timeout=2.0)
    task = await rt.wait(handle.task_id, timeout=2.0)
    persisted = await storage.get_plan_run(run.run_id)
    assert str(task.status) == "cancelled"
    assert persisted is not None
    assert persisted.status == "cancelled"
    assert response["planRun"]["status"] == "cancelled"
    await storage.close()


@pytest.mark.asyncio
async def test_terminal_expires_pending_approvals_for_owning_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = MagicMock()
    monkeypatch.setattr(
        "opensquilla.application.approval_queue.get_approval_queue",
        lambda: queue,
    )
    rt = _make_runtime()
    env = _make_envelope("agent-1::approval-owner")

    handle = await rt.enqueue(env, "hello")
    await rt.wait(handle.task_id, timeout=2.0)

    queue.expire_pending_for_session.assert_called_once_with(env.session_key)


@pytest.mark.asyncio
async def test_prestart_queued_cancel_preserves_running_owner_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = MagicMock()
    monkeypatch.setattr(
        "opensquilla.application.approval_queue.get_approval_queue",
        lambda: queue,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def _handler(run: Any) -> None:
        if run.message == "running":
            started.set()
            await release.wait()

    rt = _make_runtime(turn_handler=_handler, max_concurrency=1)
    env = _make_envelope("agent-1::approval-running-owner")
    running = await rt.enqueue(env, "running")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    queued = await rt.enqueue(env, "queued")

    assert await rt.cancel(task_id=queued.task_id, source="webui_stop") == 1
    queued_record = await rt.wait(queued.task_id, timeout=2.0)
    assert queued_record.status is AgentTaskStatus.CANCELLED
    queue.expire_pending_for_session.assert_not_called()

    release.set()
    await rt.wait(running.task_id, timeout=2.0)
    queue.expire_pending_for_session.assert_called_once_with(env.session_key)


@pytest.mark.asyncio
async def test_preallocated_turn_identity_is_propagated_to_handler() -> None:
    observed: list[dict[str, Any] | None] = []

    async def _handler(_run: Any) -> None:
        observed.append(current_turn_context())

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::identity")
    env = replace(
        env,
        metadata={
            "client_request_id": "request-1",
            "client_message_id": "client-1",
            "surface_id": "tui:test",
        },
    )
    handle = await rt.enqueue(env, "hello", task_id="turn-preallocated")
    await rt.wait(handle.task_id, timeout=2.0)

    assert handle.task_id == "turn-preallocated"
    assert observed == [
        {
            "turn_id": "turn-preallocated",
            "client_request_id": "request-1",
            "client_message_id": "client-1",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "applied",
            "revision": 1,
        }
    ]


@pytest.mark.asyncio
async def test_identity_aware_turn_emits_applied_input_disposition() -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, name, payload))

    async def _handler(_run: Any) -> None:
        return None

    rt = TaskRuntime(
        storage=_make_storage(),
        turn_handler=_handler,
        event_emitter=_emit,
    )
    env = replace(
        _make_envelope("agent-1::identity-event"),
        metadata={
            "client_message_id": "client-1",
            "surface_id": "tui:test",
        },
    )
    handle = await rt.enqueue(
        env,
        "hello",
        task_id="turn-preallocated",
        persisted_user_message_id="message-1",
    )
    await rt.wait(handle.task_id, timeout=2.0)

    disposition_events = [
        event for event in events if event[1] == "session.event.input_disposition"
    ]
    assert disposition_events == [
        (
            env.session_key,
            "session.event.input_disposition",
            {
                "session_key": env.session_key,
                "user_message_id": "message-1",
                "turn_id": "turn-preallocated",
                "client_message_id": "client-1",
                "surface_id": "tui:test",
                "intent": "send",
                "disposition": "applied",
                "revision": 1,
            },
        )
    ]


@pytest.mark.asyncio
async def test_identity_aware_collect_rebinds_each_prompt_to_the_running_turn() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[tuple[str, str, list[dict[str, Any]], str | None]] = []
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _handler(run: Any) -> None:
        runs.append((run.task_id, run.message, run.attachments, run.semantic_message))
        if run.message == "blocker":
            started.set()
            await release.wait()

    async def _emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, name, payload))

    storage = _make_storage()
    rt = TaskRuntime(
        storage=storage,
        turn_handler=_handler,
        event_emitter=_emit,
        max_concurrency=1,
    )
    env = _make_envelope("agent-1::identity-collect")
    blocker = await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    first_env = replace(
        env,
        metadata={
            "client_request_id": "request-1",
            "client_message_id": "client-1",
            "surface_id": "tui:test",
        },
    )
    second_env = replace(
        env,
        metadata={
            "client_request_id": "request-2",
            "client_message_id": "client-2",
            "surface_id": "tui:test",
        },
    )
    first = await rt.enqueue(
        first_env,
        "first collected input",
        attachments=[{"name": "first.txt"}],
        mode="collect",
        semantic_message="first semantic",
        task_id="turn-collect-1",
        persisted_user_message_id="message-1",
    )
    second = await rt.enqueue(
        second_env,
        "second collected input",
        attachments=[{"name": "second.txt"}],
        mode="collect",
        semantic_message="second semantic",
        task_id="turn-collect-2",
        persisted_user_message_id="message-2",
    )

    assert first.task_id == "turn-collect-1"
    assert second.task_id == first.task_id
    assert storage.turn_context_updates[-1] == (
        env.session_key,
        "message-2",
        {
            "turn_id": first.task_id,
            "client_request_id": "request-2",
            "client_message_id": "client-2",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "queued",
            "target_turn_id": first.task_id,
            "revision": 2,
        },
    )

    release.set()
    await rt.wait(blocker.task_id, timeout=2.0)
    await rt.wait(first.task_id, timeout=2.0)
    assert runs == [
        (blocker.task_id, "blocker", [], None),
        (
            first.task_id,
            "first collected input\nsecond collected input",
            [{"name": "first.txt"}, {"name": "second.txt"}],
            "first semantic\n\nsecond semantic",
        ),
    ]
    applied = [
        context
        for _session, message_id, context in storage.turn_context_updates
        if message_id == "message-2" and context.get("disposition") == "applied"
    ]
    assert applied == [
        {
            "turn_id": first.task_id,
            "client_request_id": "request-2",
            "client_message_id": "client-2",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "applied",
            "target_turn_id": first.task_id,
            "revision": 2,
        }
    ]
    assert any(
        name == "session.event.input_disposition"
        and payload.get("user_message_id") == "message-2"
        and payload.get("disposition") == "applied"
        and payload.get("client_request_id") == "request-2"
        for _session, name, payload in events
    )


@pytest.mark.asyncio
async def test_prestart_cancel_closes_every_collected_prompt_identity() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _handler(run: Any) -> None:
        if run.message == "blocker":
            started.set()
            await release.wait()

    async def _emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, name, payload))

    storage = _make_storage()
    rt = TaskRuntime(
        storage=storage,
        turn_handler=_handler,
        event_emitter=_emit,
        max_concurrency=1,
    )
    env = _make_envelope("agent-1::identity-collect-cancel")
    blocker = await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    first = await rt.enqueue(
        replace(
            env,
            metadata={
                "client_request_id": "request-1",
                "client_message_id": "client-1",
                "surface_id": "tui:test",
            },
        ),
        "first",
        mode="collect",
        task_id="turn-collect-cancel-1",
        persisted_user_message_id="message-1",
    )
    second = await rt.enqueue(
        replace(
            env,
            metadata={
                "client_request_id": "request-2",
                "client_message_id": "client-2",
                "surface_id": "tui:test",
            },
        ),
        "second",
        mode="collect",
        task_id="turn-collect-cancel-2",
        persisted_user_message_id="message-2",
    )
    assert second.task_id == first.task_id

    assert await rt.cancel(task_id=first.task_id) == 1
    record = await rt.wait(first.task_id, timeout=2.0)
    assert record.status.value == "cancelled"
    terminal = {
        message_id: context
        for _session, message_id, context in storage.turn_context_updates
        if context.get("disposition") == "cancelled"
    }
    assert set(terminal) == {"message-1", "message-2"}
    assert terminal["message-1"]["turn_id"] == first.task_id
    assert terminal["message-1"]["client_request_id"] == "request-1"
    assert terminal["message-2"] == {
        "turn_id": first.task_id,
        "client_request_id": "request-2",
        "client_message_id": "client-2",
        "surface_id": "tui:test",
        "intent": "send",
        "disposition": "cancelled",
        "target_turn_id": first.task_id,
        "revision": 2,
    }
    terminal_events = {
        payload["user_message_id"]: payload
        for _session, name, payload in events
        if name == "session.event.input_disposition"
        and payload.get("disposition") == "cancelled"
    }
    assert terminal_events["message-1"]["client_request_id"] == "request-1"
    assert terminal_events["message-2"]["client_request_id"] == "request-2"

    release.set()
    await rt.wait(blocker.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_collect_details_failure_does_not_reject_an_accepted_input() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)
        if run.message == "blocker":
            started.set()
            await release.wait()

    storage = _make_storage()
    update_agent_task = storage.update_agent_task

    async def _fail_collected_details(task_id: str, **kwargs: Any) -> None:
        if (kwargs.get("details") or {}).get("collected"):
            raise RuntimeError("diagnostic write unavailable")
        await update_agent_task(task_id, **kwargs)

    storage.update_agent_task = _fail_collected_details
    rt = TaskRuntime(storage=storage, turn_handler=_handler, max_concurrency=1)
    env = _make_envelope("agent-1::collect-details-failure")
    blocker = await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    first = await rt.enqueue(env, "one", mode="collect")

    second = await rt.enqueue(env, "two", mode="collect")
    assert second.task_id == first.task_id

    release.set()
    await rt.wait(blocker.task_id, timeout=2.0)
    await rt.wait(first.task_id, timeout=2.0)
    assert runs == ["blocker", "one\ntwo"]


@pytest.mark.asyncio
async def test_identity_free_collect_preserves_legacy_coalescing() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)
        if run.message == "blocker":
            started.set()
            await release.wait()

    rt = _make_runtime(turn_handler=_handler, max_concurrency=1)
    env = _make_envelope("agent-1::legacy-collect")
    blocker = await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    first = await rt.enqueue(env, "one", mode="collect")
    second = await rt.enqueue(env, "two", mode="collect")
    assert second.task_id == first.task_id

    release.set()
    await rt.wait(blocker.task_id, timeout=2.0)
    await rt.wait(first.task_id, timeout=2.0)
    assert runs == ["blocker", "one\ntwo"]


@pytest.mark.asyncio
async def test_prestart_cancel_closes_primary_input_disposition() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _handler(run: Any) -> None:
        if run.message == "blocker":
            started.set()
            await release.wait()

    async def _emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, name, payload))

    storage = _make_storage()
    rt = TaskRuntime(
        storage=storage,
        turn_handler=_handler,
        event_emitter=_emit,
        max_concurrency=1,
    )
    env = _make_envelope("agent-1::prestart-cancel")
    blocker = await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    queued_env = replace(
        env,
        metadata={
            "client_request_id": "request-cancel",
            "client_message_id": "client-cancel",
            "surface_id": "tui:test",
        },
    )
    queued = await rt.enqueue(
        queued_env,
        "queued",
        task_id="turn-cancelled-before-start",
        persisted_user_message_id="message-cancelled-before-start",
    )

    assert await rt.cancel(task_id=queued.task_id) == 1
    record = await rt.wait(queued.task_id, timeout=2.0)
    assert record.status.value == "cancelled"
    assert storage.turn_context_updates[-1] == (
        env.session_key,
        "message-cancelled-before-start",
        {
            "turn_id": queued.task_id,
            "client_request_id": "request-cancel",
            "client_message_id": "client-cancel",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "cancelled",
            "revision": 2,
        },
    )
    disposition = next(
        payload
        for _session, name, payload in events
        if name == "session.event.input_disposition" and payload.get("turn_id") == queued.task_id
    )
    assert disposition["disposition"] == "cancelled"
    assert disposition["client_request_id"] == "request-cancel"
    assert disposition["terminal_reason"] == "cancelled_before_start"

    release.set()
    await rt.wait(blocker.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_shutdown_closes_queued_primary_input_disposition() -> None:
    started = asyncio.Event()

    async def _handler(run: Any) -> None:
        if run.message == "blocker":
            started.set()
            await asyncio.Event().wait()

    storage = _make_storage()
    rt = TaskRuntime(storage=storage, turn_handler=_handler, max_concurrency=1)
    env = _make_envelope("agent-1::shutdown-queued")
    await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    queued_env = replace(
        env,
        metadata={
            "client_request_id": "request-shutdown",
            "client_message_id": "client-shutdown",
            "surface_id": "tui:test",
        },
    )
    queued = await rt.enqueue(
        queued_env,
        "queued",
        task_id="turn-shutdown-before-start",
        persisted_user_message_id="message-shutdown-before-start",
    )

    await rt.shutdown(cancel=True, timeout=2.0)
    record = await rt.status(queued.task_id)
    assert record.status.value == "cancelled"
    assert storage.turn_context_updates[-1][1:] == (
        "message-shutdown-before-start",
        {
            "turn_id": queued.task_id,
            "client_request_id": "request-shutdown",
            "client_message_id": "client-shutdown",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "cancelled",
            "revision": 2,
        },
    )


@pytest.mark.asyncio
async def test_gateway_shutdown_promotes_unapplied_steer_without_starting_followup() -> None:
    started = asyncio.Event()
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)
        started.set()
        await asyncio.Event().wait()

    storage = _make_storage()
    rt = TaskRuntime(storage=storage, turn_handler=_handler)
    env = _make_envelope("agent-1::shutdown-steer")
    handle = await rt.enqueue(env, "first")
    await started.wait()
    assert await rt.steer(
        env.session_key,
        "preserve after restart",
        persisted_user_message_id="message-shutdown-steer",
        client_request_id="request-shutdown-steer",
        client_message_id="client-shutdown-steer",
        surface_id="webui",
    ) == handle.task_id

    await rt.shutdown(cancel=True, timeout=2.0)

    dispositions = [
        context
        for _session, message_id, context in storage.turn_context_updates
        if message_id == "message-shutdown-steer"
    ]
    assert [context["disposition"] for context in dispositions] == ["promoted"]
    assert dispositions[0]["promoted_from_turn_id"] == handle.task_id
    assert runs == ["first"]
    promoted_tasks = [
        task
        for task in await storage.list_agent_tasks()
        if task.task_id != handle.task_id
    ]
    assert len(promoted_tasks) == 1
    assert promoted_tasks[0].status.value == "queued"
    assert promoted_tasks[0].details["metadata"]["steer_restart_recovery"] is True


@pytest.mark.asyncio
async def test_shutdown_timeout_rejects_unstarted_primary_input() -> None:
    started = asyncio.Event()

    async def _handler(run: Any) -> None:
        if run.message == "blocker":
            started.set()
            await asyncio.Event().wait()

    storage = _make_storage()
    rt = TaskRuntime(storage=storage, turn_handler=_handler, max_concurrency=1)
    env = _make_envelope("agent-1::shutdown-abandoned")
    await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    queued_env = replace(
        env,
        metadata={
            "client_request_id": "request-abandoned",
            "client_message_id": "client-abandoned",
            "surface_id": "tui:test",
        },
    )
    queued = await rt.enqueue(
        queued_env,
        "queued",
        task_id="turn-abandoned-before-start",
        persisted_user_message_id="message-abandoned-before-start",
    )

    await rt.shutdown(cancel=False, timeout=0.01)
    record = await rt.status(queued.task_id)
    assert record.status.value == "abandoned"
    assert storage.turn_context_updates[-1][1:] == (
        "message-abandoned-before-start",
        {
            "turn_id": queued.task_id,
            "client_request_id": "request-abandoned",
            "client_message_id": "client-abandoned",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "rejected",
            "revision": 2,
        },
    )


@pytest.mark.asyncio
async def test_steer_is_drained_by_running_turn_provider() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    application_recorded = asyncio.Event()
    finish = asyncio.Event()
    drained: list[str] = []

    async def _handler(run: Any) -> None:
        started.set()
        await release.wait()
        drained.extend(run.pending_input_provider.drain_pending())
        application = run.pending_input_provider.mark_applied(
            iteration=2,
            model_call_id="call-steer",
        )
        if inspect.isawaitable(application):
            await application
        application_recorded.set()
        await finish.wait()

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::steer-drain")
    handle = await rt.enqueue(env, "first")
    await started.wait()

    assert await rt.active_task_id(env.session_key) == handle.task_id
    accepted = await rt.steer(
        env.session_key,
        "change direction",
        persisted_user_message_id="msg-steer",
    )
    assert accepted == handle.task_id

    release.set()
    await asyncio.wait_for(application_recorded.wait(), timeout=2.0)
    assert drained == ["change direction"]
    applied = [
        context
        for _session, message_id, context in rt._storage.turn_context_updates
        if message_id == "msg-steer" and context.get("disposition") == "applied"
    ]
    assert applied == [
        {
            "turn_id": handle.task_id,
            "client_message_id": None,
            "surface_id": None,
            "intent": "steer",
            "disposition": "applied",
            "target_turn_id": handle.task_id,
            "revision": 2,
            "applied_iteration": 2,
            "model_call_id": "call-steer",
        }
    ]

    finish.set()
    await rt.wait(handle.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_applied_steer_retries_failed_durable_ack_before_terminal() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[str] = []
    provider: Any | None = None

    async def _handler(run: Any) -> None:
        nonlocal provider
        runs.append(run.message)
        provider = run.pending_input_provider
        started.set()
        await release.wait()
        assert provider.drain_pending() == ["change direction"]
        application = provider.mark_applied(
            iteration=2,
            model_call_id="call-retry-applied",
        )
        if inspect.isawaitable(application):
            await application

    storage = _make_storage()
    durable_update = storage.update_transcript_turn_context
    applied_attempts = 0

    async def _flaky_update(
        session_key: str,
        message_id: str,
        context: dict[str, Any],
    ) -> bool:
        nonlocal applied_attempts
        if context.get("disposition") == "applied":
            applied_attempts += 1
            if applied_attempts == 1:
                raise RuntimeError("transient disposition write failure")
        return await durable_update(session_key, message_id, context)

    storage.update_transcript_turn_context = _flaky_update
    rt = TaskRuntime(storage=storage, turn_handler=_handler)
    env = _make_envelope("agent-1::steer-applied-retry")
    handle = await rt.enqueue(env, "first")
    await started.wait()
    assert await rt.steer(
        env.session_key,
        "change direction",
        persisted_user_message_id="msg-applied-retry",
    ) == handle.task_id

    release.set()
    record = await rt.wait(handle.task_id, timeout=2.0)

    assert record.status.value == "succeeded"
    assert applied_attempts == 2
    assert runs == ["first"]
    assert provider is not None
    assert provider.reclaim_all() == []
    applied = [
        context
        for _session, message_id, context in storage.turn_context_updates
        if message_id == "msg-applied-retry"
    ]
    assert [context["disposition"] for context in applied] == ["applied"]


@pytest.mark.asyncio
async def test_unacknowledged_applied_steer_is_kept_with_terminal_evidence() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    provider: Any | None = None

    async def _handler(run: Any) -> None:
        nonlocal provider
        provider = run.pending_input_provider
        started.set()
        await release.wait()
        assert provider.drain_pending() == ["persist this application"]
        application = provider.mark_applied(
            iteration=4,
            model_call_id="call-terminal-evidence",
        )
        if inspect.isawaitable(application):
            await application

    storage = _make_storage()

    async def _missing_update(
        _session_key: str,
        _message_id: str,
        _context: dict[str, Any],
    ) -> bool:
        return False

    storage.update_transcript_turn_context = _missing_update
    rt = TaskRuntime(storage=storage, turn_handler=_handler)
    env = _make_envelope("agent-1::steer-terminal-evidence")
    handle = await rt.enqueue(env, "first")
    await started.wait()
    assert await rt.steer(
        env.session_key,
        "persist this application",
        persisted_user_message_id="msg-terminal-evidence",
    ) == handle.task_id

    release.set()
    record = await rt.wait(handle.task_id, timeout=2.0)

    assert record.status.value == "succeeded"
    assert record.details["applied_steer_evidence"] == [
        {
            "message_id": "msg-terminal-evidence",
            "applied_iteration": 4,
            "model_call_id": "call-terminal-evidence",
        }
    ]
    assert provider is not None
    retained = provider.reclaim_all()
    assert len(retained) == 1
    assert retained[0].persisted_user_message_id == "msg-terminal-evidence"


@pytest.mark.asyncio
async def test_claimed_steer_without_provider_application_is_promoted() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    followup_seen = asyncio.Event()
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)
        if run.message == "first":
            started.set()
            await release.wait()
            assert run.pending_input_provider.drain_pending() == ["not yet applied"]
            return
        followup_seen.set()

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::steer-claimed-promote")
    first = await rt.enqueue(env, "first")
    await started.wait()
    assert await rt.steer(
        env.session_key,
        "not yet applied",
        persisted_user_message_id="msg-claimed",
    ) == first.task_id

    release.set()
    await rt.wait(first.task_id, timeout=2.0)
    await asyncio.wait_for(followup_seen.wait(), timeout=2.0)

    promoted = [
        context
        for _session, message_id, context in rt._storage.turn_context_updates
        if message_id == "msg-claimed" and context.get("disposition") == "promoted"
    ]
    assert len(promoted) == 1
    assert promoted[0]["promoted_from_turn_id"] == first.task_id
    assert promoted[0]["promoted_turn_id"] == promoted[0]["turn_id"]
    assert runs == ["first", "not yet applied"]


@pytest.mark.asyncio
async def test_admit_steer_rejects_expected_turn_mismatch_before_persistence() -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()
    persist_calls = 0

    async def _handler(_run: Any) -> None:
        started.set()
        await blocker.wait()

    async def _persist(_turn_id: str) -> Any:
        nonlocal persist_calls
        persist_calls += 1
        raise AssertionError("mismatched turn must not persist")

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::steer-mismatch")
    handle = await rt.enqueue(env, "first")
    await started.wait()

    result = await rt.admit_steer(
        env.session_key,
        "different-turn",
        "late",
        persist=_persist,
    )

    assert result.accepted is False
    assert result.failure_code == "EXPECTED_TURN_MISMATCH"
    assert result.task_id == handle.task_id
    assert persist_calls == 0
    await rt.cancel(task_id=handle.task_id)
    await rt.wait(handle.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_ensemble_active_turn_exposes_queue_only_steer_capability() -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()
    persist_calls = 0
    accepted_config = SimpleNamespace(
        squilla_router=SimpleNamespace(enabled=True, rollout_phase="enforce"),
        llm_ensemble=SimpleNamespace(
            enabled=True,
            selection_mode="",
            candidates=[],
        ),
    )

    async def _handler(_run: Any) -> None:
        started.set()
        await blocker.wait()

    async def _persist(_turn_id: str) -> Any:
        nonlocal persist_calls
        persist_calls += 1
        raise AssertionError("ensemble steer must queue instead of persisting")

    rt = TaskRuntime(
        storage=_make_storage(),
        turn_handler=_handler,
        accepted_config_provider=lambda: accepted_config,
    )
    env = _make_envelope("agent-1::ensemble-steer")
    handle = await rt.enqueue(env, "first")
    await started.wait()

    capability = await rt.steer_capability(env.session_key)
    admission = await rt.admit_steer(
        env.session_key,
        handle.task_id,
        "change direction",
        persist=_persist,
    )

    assert capability == {
        "mode": "queue_only",
        "expected_turn_id": handle.task_id,
        "input_kinds": ["text"],
        "reason": "ensemble_requires_followup_turn",
    }
    assert admission.accepted is False
    assert admission.failure_code == "ACTIVE_TURN_NOT_STEERABLE"
    assert admission.capability == capability
    assert persist_calls == 0
    await rt.cancel(task_id=handle.task_id)
    await rt.wait(handle.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_goal_edit_admission_queues_internal_update_on_running_task() -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()
    persisted_task_ids: list[str | None] = []

    async def _handler(_run: Any) -> None:
        started.set()
        await blocker.wait()

    storage = _make_storage()
    rt = TaskRuntime(storage=storage, turn_handler=_handler)
    env = _make_envelope("agent-1::goal-edit-same-turn")
    handle = await rt.enqueue(env, "first")
    await started.wait()

    async def _persist(task_id: str | None) -> dict[str, Any]:
        persisted_task_ids.append(task_id)
        assert task_id == handle.task_id
        record = await storage.get_agent_task(task_id)
        assert record is not None
        details = dict(record.details or {})
        update = GoalObjectiveUpdate(
            context=GoalTurnContext(
                session_id=env.session_key,
                epoch=1,
                goal_id="goal-edit-same-turn",
                objective_revision=2,
                objective_snapshot="Use the revised objective.",
                task_id=task_id,
            ),
            state_revision=2,
            accepted_at_ms=123,
        )
        details[GOAL_OBJECTIVE_UPDATE_DETAIL_KEY] = update.as_task_detail()
        await storage.update_agent_task(task_id, details=details)
        return {"status": "active", "revision": 2}

    response = await rt.apply_goal_objective_edit(
        env.session_key,
        persist=_persist,
    )

    assert response == {"status": "active", "revision": 2}
    assert persisted_task_ids == [handle.task_id]
    assert await rt.active_task_id(env.session_key) == handle.task_id
    assert [record.task_id for record in await storage.list_agent_tasks()] == [
        handle.task_id
    ]
    runtime_task = rt._running_by_session[env.session_key]
    provider = runtime_task.pending_input_provider
    assert provider._pending == []
    assert provider._goal_pending is not None
    assert provider._goal_pending.context.task_id == handle.task_id
    assert provider._goal_pending.context.objective_revision == 2
    assert storage.turn_context_updates == []

    # Durable Clear happens before this in-memory projection is revoked. An
    # edit that has not crossed the Agent safe boundary must not be claimed or
    # injected afterward, and revocation must not stop the owning task.
    await rt.revoke_goal_objective_updates(env.session_key)
    assert (await provider.claim_pending()).texts == ()
    assert await rt.active_task_id(env.session_key) == handle.task_id

    await rt.cancel(task_id=handle.task_id)
    await rt.wait(handle.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_goal_clear_after_claim_keeps_assembled_input_but_blocks_late_apply() -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()
    applied_updates: list[GoalObjectiveUpdate] = []

    async def _handler(_run: Any) -> None:
        started.set()
        await blocker.wait()

    storage = _make_storage()

    async def _claim(update: GoalObjectiveUpdate) -> GoalObjectiveUpdate:
        return replace(update, status="claimed")

    async def _apply(
        update: GoalObjectiveUpdate,
        *,
        iteration: int,
        model_call_id: str,
    ) -> GoalObjectiveUpdate:
        del iteration, model_call_id
        applied_updates.append(update)
        return replace(update, status="applied")

    storage.claim_goal_objective_update = _claim
    storage.apply_goal_objective_update = _apply
    rt = TaskRuntime(storage=storage, turn_handler=_handler)
    env = _make_envelope("agent-1::goal-edit-claimed-clear")
    handle = await rt.enqueue(env, "first")
    await started.wait()
    queued_updates: list[GoalObjectiveUpdate] = []

    async def _persist(task_id: str | None) -> dict[str, Any]:
        assert task_id == handle.task_id
        record = await storage.get_agent_task(task_id)
        assert record is not None
        details = dict(record.details or {})
        update = GoalObjectiveUpdate(
            context=GoalTurnContext(
                session_id=env.session_key,
                epoch=1,
                goal_id="goal-edit-claimed-clear",
                objective_revision=2,
                objective_snapshot="Use the claimed objective update.",
                task_id=task_id,
            ),
            state_revision=2,
            accepted_at_ms=123,
        )
        queued_updates.append(update)
        details[GOAL_OBJECTIVE_UPDATE_DETAIL_KEY] = update.as_task_detail()
        await storage.update_agent_task(task_id, details=details)
        return {"status": "active", "revision": 2}

    await rt.apply_goal_objective_edit(env.session_key, persist=_persist)
    runtime_task = rt._running_by_session[env.session_key]
    provider = runtime_task.pending_input_provider
    claim = await provider.claim_pending()
    assert queued_updates
    assert claim.goal_context == queued_updates[0].context.as_task_detail()
    assert len(claim.texts) == 1
    assert "Use the claimed objective update." in claim.texts[0]

    # The returned claim models prompt context already assembled by Agent.
    # Clear cannot recall that value, but it removes the provider's authority
    # acknowledgement so a later provider start cannot apply the Goal edit.
    await rt.revoke_goal_objective_updates(env.session_key)
    assert claim.texts[0]
    assert provider._goal_claimed is None
    assert provider.mark_applied(iteration=2, model_call_id="after-clear") is None
    assert provider.take_applied_goal_context() is None
    assert applied_updates == []
    assert runtime_task.goal_context is None
    assert await rt.active_task_id(env.session_key) == handle.task_id

    await rt.cancel(task_id=handle.task_id)
    await rt.wait(handle.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_goal_edit_post_commit_projection_failure_still_returns_acceptance() -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()
    persisted_task_ids: list[str | None] = []

    async def _handler(_run: Any) -> None:
        started.set()
        await blocker.wait()

    storage = _make_storage()
    rt = TaskRuntime(storage=storage, turn_handler=_handler)
    env = _make_envelope("agent-1::goal-edit-projection-failure")
    handle = await rt.enqueue(env, "first")
    await started.wait()

    async def _persist(task_id: str | None) -> dict[str, Any]:
        persisted_task_ids.append(task_id)
        return {"accepted": True, "status": "active", "revision": 2}

    async def _failed_read(_task_id: str) -> AgentTaskRecord | None:
        raise OSError("synthetic post-commit read failure")

    original_get_agent_task = storage.get_agent_task
    storage.get_agent_task = _failed_read
    response = await rt.apply_goal_objective_edit(
        env.session_key,
        persist=_persist,
    )

    assert response == {"accepted": True, "status": "active", "revision": 2}
    assert persisted_task_ids == [handle.task_id]
    runtime_task = rt._running_by_session[env.session_key]
    assert runtime_task.pending_input_provider._goal_pending is None
    assert runtime_task.pending_input_provider._pending == []
    assert storage.turn_context_updates == []

    storage.get_agent_task = original_get_agent_task
    await rt.cancel(task_id=handle.task_id)
    await rt.wait(handle.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_goal_edit_admission_defers_ensemble_to_next_goal_turn() -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()
    persisted_task_ids: list[str | None] = []
    accepted_config = SimpleNamespace(
        squilla_router=SimpleNamespace(enabled=True, rollout_phase="enforce"),
        llm_ensemble=SimpleNamespace(
            enabled=True,
            selection_mode="",
            candidates=[],
        ),
    )

    async def _handler(_run: Any) -> None:
        started.set()
        await blocker.wait()

    async def _persist(task_id: str | None) -> dict[str, Any]:
        persisted_task_ids.append(task_id)
        return {"status": "active", "revision": 2}

    storage = _make_storage()
    rt = TaskRuntime(
        storage=storage,
        turn_handler=_handler,
        accepted_config_provider=lambda: accepted_config,
    )
    env = _make_envelope("agent-1::goal-edit-ensemble")
    handle = await rt.enqueue(env, "first")
    await started.wait()
    runtime_task = rt._running_by_session[env.session_key]

    response = await rt.apply_goal_objective_edit(
        env.session_key,
        persist=_persist,
    )

    assert response == {"status": "active", "revision": 2}
    assert persisted_task_ids == [None]
    assert await rt.active_task_id(env.session_key) == handle.task_id
    assert rt._running_by_session[env.session_key] is runtime_task
    assert [record.task_id for record in await storage.list_agent_tasks()] == [
        handle.task_id
    ]
    assert runtime_task.pending_input_provider._pending == []
    assert runtime_task.pending_input_provider._goal_pending is None
    assert storage.turn_context_updates == []

    await rt.cancel(task_id=handle.task_id)
    await rt.wait(handle.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_noninteractive_task_does_not_expose_same_turn_steer() -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def _handler(_run: Any) -> None:
        started.set()
        await blocker.wait()

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::subagent-no-steer")
    handle = await rt.enqueue(env, "background work", run_kind="subagent")
    await started.wait()

    assert await rt.steer_capability(env.session_key) == {
        "mode": "disabled",
        "expected_turn_id": handle.task_id,
        "input_kinds": [],
        "reason": "task_kind_not_steerable",
    }

    await rt.cancel(task_id=handle.task_id)
    await rt.wait(handle.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_channel_turn_queues_steer_when_restart_recovery_is_unavailable() -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def _handler(_run: Any) -> None:
        started.set()
        await blocker.wait()

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::channel-steer-capability")
    handle = await rt.enqueue(env, "channel input", run_kind="channel_turn")
    await started.wait()

    assert await rt.steer_capability(env.session_key) == {
        "mode": "queue_only",
        "expected_turn_id": handle.task_id,
        "input_kinds": ["text"],
        "reason": "restart_recovery_unavailable",
    }

    await rt.cancel(task_id=handle.task_id)
    await rt.wait(handle.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_admit_steer_persistence_is_fenced_against_cancel() -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()
    persist_started = asyncio.Event()
    release_persist = asyncio.Event()

    async def _handler(_run: Any) -> None:
        started.set()
        await blocker.wait()

    async def _persist(_turn_id: str) -> Any:
        persist_started.set()
        await release_persist.wait()
        return SimpleNamespace(
            replayed=False,
            receipt=SimpleNamespace(message_id="msg-atomic-steer"),
        )

    storage = _make_storage()
    rt = TaskRuntime(storage=storage, turn_handler=_handler)
    env = _make_envelope("agent-1::steer-cancel-fence")
    handle = await rt.enqueue(env, "first")
    await started.wait()

    admission_task = asyncio.create_task(
        rt.admit_steer(
            env.session_key,
            handle.task_id,
            "accepted before stop",
            persist=_persist,
            client_request_id="request-atomic-steer",
            client_message_id="client-atomic-steer",
            surface_id="webui",
        )
    )
    await persist_started.wait()
    cancel_task = asyncio.create_task(
        rt.cancel(task_id=handle.task_id, source="webui_stop")
    )
    await asyncio.sleep(0)
    assert cancel_task.done() is False

    release_persist.set()
    admission = await admission_task
    assert admission.accepted is True
    assert await cancel_task == 1
    await rt.wait(handle.task_id, timeout=2.0)

    cancelled = [
        context
        for _session, message_id, context in storage.turn_context_updates
        if message_id == "msg-atomic-steer"
    ]
    assert cancelled == [
        {
            "turn_id": handle.task_id,
            "client_message_id": "client-atomic-steer",
            "surface_id": "webui",
            "intent": "steer",
            "disposition": "cancelled",
                "target_turn_id": handle.task_id,
                "revision": 2,
                "client_request_id": "request-atomic-steer",
                "failure_code": "TURN_CANCELLED",
                "retryable": True,
                "recovery": "restore_to_composer",
                "fallback_safe": True,
            }
        ]


@pytest.mark.asyncio
async def test_undrained_late_steer_is_promoted_to_followup() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    followup_seen = asyncio.Event()
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)
        if run.message == "first":
            first_started.set()
            await release_first.wait()
            return
        followup_seen.set()

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::steer-fallback")
    handle = await rt.enqueue(env, "first")
    await first_started.wait()
    assert await rt.steer(
        env.session_key,
        "too late for a tool boundary",
        persisted_user_message_id="msg-late",
    ) == handle.task_id

    release_first.set()
    await rt.wait(handle.task_id, timeout=2.0)
    await asyncio.wait_for(followup_seen.wait(), timeout=2.0)
    assert runs == ["first", "too late for a tool boundary"]
    promoted = [
        context
        for _session, message_id, context in rt._storage.turn_context_updates
        if message_id == "msg-late" and context.get("disposition") == "promoted"
    ]
    assert len(promoted) == 1
    assert promoted[0]["turn_id"] != handle.task_id
    assert promoted[0]["promoted_from_turn_id"] == handle.task_id


@pytest.mark.asyncio
async def test_late_steer_commit_precedes_session_routing_set() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    followup_seen = asyncio.Event()
    promotion_committed = asyncio.Event()
    release_promotion_commit = asyncio.Event()
    setting_entered = asyncio.Event()
    current_mode = "direct"
    seen: list[tuple[str, str | None]] = []
    committed_audits: list[dict[str, Any]] = []
    promoted_task_ids: list[str] = []

    def accepted_config(*, session_key: str, run_kind: str) -> Any:
        assert session_key == "agent-1::steer-routing-linearization"
        assert run_kind == "session_turn"
        return SimpleNamespace(
            squilla_router=SimpleNamespace(
                enabled=current_mode == "router",
                rollout_phase="enforce" if current_mode == "router" else "observe",
            ),
            llm_ensemble=SimpleNamespace(enabled=False, selection_mode=""),
            session_mode=current_mode,
            session_routing_revision=1 if current_mode == "direct" else 2,
            session_routing_source="session_override",
        )

    async def handler(run: Any) -> None:
        seen.append((run.message, getattr(run.accepted_config, "session_mode", None)))
        if run.message == "first":
            first_started.set()
            await release_first.wait()
            return
        followup_seen.set()

    storage = _make_storage()
    create_agent_task = storage.create_agent_task

    async def create_with_blocked_promotion_return(record: AgentTaskRecord) -> None:
        details = record.details if isinstance(record.details, dict) else {}
        metadata = details.get("metadata")
        if isinstance(metadata, dict) and metadata.get("steer_restart_recovery") is True:
            # The durable boundary has crossed, but the runtime has not yet
            # activated the promoted task or returned its admission gate.
            await create_agent_task(record)
            committed_audits.append(dict(details["accepted_model_routing"]))
            promoted_task_ids.append(record.task_id)
            promotion_committed.set()
            await release_promotion_commit.wait()
            return
        await create_agent_task(record)

    storage.create_agent_task = create_with_blocked_promotion_return
    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        accepted_config_provider=accepted_config,
    )
    envelope = _make_envelope("agent-1::steer-routing-linearization")
    first = await runtime.enqueue(
        envelope,
        "first",
        run_kind="session_turn",
    )
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    assert await runtime.steer(
        envelope.session_key,
        "late correction",
        persisted_user_message_id="message-routing-linearization",
    ) == first.task_id

    release_first.set()
    await asyncio.wait_for(promotion_committed.wait(), timeout=2.0)

    async def set_session_mode() -> None:
        nonlocal current_mode
        async with runtime.collect_admission(envelope.session_key):
            setting_entered.set()
            current_mode = "router"

    setting = asyncio.create_task(set_session_mode())
    await asyncio.sleep(0)
    assert setting_entered.is_set() is False
    assert committed_audits[0]["effective_mode"] == "direct"
    assert committed_audits[0]["session_revision"] == 1

    release_promotion_commit.set()
    await asyncio.wait_for(setting, timeout=2.0)
    await runtime.wait(first.task_id, timeout=2.0)
    await asyncio.wait_for(followup_seen.wait(), timeout=2.0)
    await runtime.wait(promoted_task_ids[0], timeout=2.0)

    assert seen == [("first", "direct"), ("late correction", "direct")]


@pytest.mark.asyncio
async def test_terminal_waits_for_late_steer_handoff_and_publishes_in_order() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    promotion_started = asyncio.Event()
    release_promotion = asyncio.Event()
    followup_seen = asyncio.Event()
    events: list[tuple[str, dict[str, Any]]] = []

    async def _handler(run: Any) -> None:
        if run.message == "first":
            first_started.set()
            await release_first.wait()
            return
        followup_seen.set()

    async def _emit(_session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    storage = _make_storage()
    create_agent_task = storage.create_agent_task

    async def _create_with_blocked_handoff(record: AgentTaskRecord) -> None:
        details = record.details if isinstance(record.details, dict) else {}
        metadata = details.get("metadata")
        if isinstance(metadata, dict) and metadata.get("steer_restart_recovery") is True:
            promotion_started.set()
            await release_promotion.wait()
        await create_agent_task(record)

    storage.create_agent_task = _create_with_blocked_handoff
    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_handler,
        event_emitter=_emit,
    )
    envelope = _make_envelope("agent-1::steer-terminal-order")
    first = await runtime.enqueue(envelope, "first")
    await first_started.wait()
    assert await runtime.steer(
        envelope.session_key,
        "late correction",
        persisted_user_message_id="message-terminal-order",
        client_request_id="request-terminal-order",
        client_message_id="client-terminal-order",
        surface_id="webui",
    ) == first.task_id

    release_first.set()
    waiter = asyncio.create_task(runtime.wait(first.task_id, timeout=2.0))
    await asyncio.wait_for(promotion_started.wait(), timeout=2.0)

    runtime_task = runtime._tasks[first.task_id]
    persisted = await runtime.status(first.task_id)
    assert persisted.status is AgentTaskStatus.SUCCEEDED
    assert runtime_task.terminal_settling is True
    assert runtime_task.terminal_emitted is False
    assert runtime_task.terminal_settled is False
    assert not waiter.done()
    assert not any(
        name == "task.succeeded" and payload.get("task_id") == first.task_id
        for name, payload in events
    )

    driver = runtime_task.asyncio_task
    assert driver is not None
    driver.cancel()
    await asyncio.sleep(0)
    assert not waiter.done()
    assert not any(
        name == "task.succeeded" and payload.get("task_id") == first.task_id
        for name, payload in events
    )

    release_promotion.set()
    settled = await waiter
    assert settled.status is AgentTaskStatus.SUCCEEDED
    assert runtime_task.terminal_settling is False
    assert runtime_task.terminal_emitted is True
    assert runtime_task.terminal_settled is True
    await asyncio.wait_for(followup_seen.wait(), timeout=2.0)

    promoted_index = next(
        index
        for index, (name, payload) in enumerate(events)
        if name == "session.event.input_disposition"
        and payload.get("disposition") == "promoted"
    )
    terminal_index = next(
        index
        for index, (name, payload) in enumerate(events)
        if name == "task.succeeded" and payload.get("task_id") == first.task_id
    )
    queued_index = next(
        index
        for index, (name, payload) in enumerate(events)
        if name == "task.queued" and payload.get("task_id") != first.task_id
    )
    assert promoted_index < terminal_index < queued_index
    assert sum(
        name == "task.succeeded" and payload.get("task_id") == first.task_id
        for name, payload in events
    ) == 1

    promoted_payload = events[promoted_index][1]
    promoted_task_id = promoted_payload["promoted_turn_id"]
    await runtime.wait(promoted_task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_live_v2_promotion_atomically_rebinds_batch_and_preserves_ids(
    tmp_path,
) -> None:
    session_key = "agent-1::live-v2-promotion"
    session_id = "session-live-v2-promotion"
    storage = await SessionStorage.open(str(tmp_path / "live-v2-promotion.db"))
    await storage.upsert_session(
        SessionNode(
            session_key=session_key,
            session_id=session_id,
            agent_id="agent-1",
            created_at=100,
            updated_at=100,
        )
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    followup_seen = asyncio.Event()
    runs: list[tuple[str, str]] = []

    async def _handler(run: Any) -> None:
        runs.append((run.task_id, run.message))
        if run.message == "first":
            first_started.set()
            await release_first.wait()
            return
        followup_seen.set()

    rt = TaskRuntime(storage=storage, turn_handler=_handler)
    env = replace(
        _make_envelope(session_key),
        session_id=session_id,
    )
    first = await rt.enqueue(env, "first")
    await first_started.wait()

    async def _admit(index: int, text: str) -> None:
        message_id = f"message-live-promote-{index}"
        request_id = f"request-live-promote-{index}"
        entry = TranscriptEntry(
            session_id=session_id,
            session_key=session_key,
            message_id=message_id,
            role="user",
            content=text,
            created_at=200,
            turn_context={
                "turn_id": first.task_id,
                "target_turn_id": first.task_id,
                "client_request_id": request_id,
                "client_message_id": f"client-live-promote-{index}",
                "surface_id": "webui",
                "intent": "steer",
                "disposition": "steering",
                "revision": 1,
            },
        )

        async def _persist(active_turn_id: str) -> Any:
            return await storage.accept_turn(
                entry,
                expected_epoch=0,
                updated_at=200 + index,
                task_record=None,
                receipt_task_id=active_turn_id,
                source_scope="rpc:web:steer.v2",
                request_session_key=session_key,
                client_request_id=request_id,
                request_fingerprint=f"fingerprint-{index}",
            )

        admission = await rt.admit_steer(
            session_key,
            first.task_id,
            text,
            persist=_persist,
            client_request_id=request_id,
            client_message_id=f"client-live-promote-{index}",
            surface_id="webui",
        )
        assert admission.accepted is True

    await _admit(1, "first correction")
    await _admit(2, "second correction")
    release_first.set()
    await asyncio.wait_for(followup_seen.wait(), timeout=2.0)

    promoted_task_id, promoted_message = runs[-1]
    assert promoted_message == "first correction\n\nsecond correction"
    assert promoted_task_id != first.task_id
    promoted_task = await storage.get_agent_task(promoted_task_id)
    assert promoted_task is not None
    assert promoted_task.details["persisted_user_message_ids"] == [
        "message-live-promote-1",
        "message-live-promote-2",
    ]
    assert promoted_task.details["metadata"]["steer_restart_recovery"] is True
    for index in (1, 2):
        receipt = await storage.get_turn_ingress_receipt(
            source_scope="rpc:web:steer.v2",
            request_session_key=session_key,
            client_request_id=f"request-live-promote-{index}",
        )
        assert receipt is not None
        assert receipt.receipt.task_id == promoted_task_id
        entry = await storage.get_canonical_transcript_entry(
            session_id,
            f"message-live-promote-{index}",
        )
        assert entry is not None
        assert entry.turn_context is not None
        assert entry.turn_context["disposition"] == "promoted"
        assert entry.turn_context["promoted_turn_id"] == promoted_task_id
    await rt.wait(promoted_task_id, timeout=2.0)
    await storage.close()


@pytest.mark.asyncio
async def test_undrained_steer_survives_failed_active_turn_as_followup() -> None:
    first_started = asyncio.Event()
    fail_first = asyncio.Event()
    followup_seen = asyncio.Event()
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)
        if run.message == "first":
            first_started.set()
            await fail_first.wait()
            raise RuntimeError("provider failed after accepting steer")
        followup_seen.set()

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::steer-error-fallback")
    handle = await rt.enqueue(env, "first")
    await first_started.wait()
    assert await rt.steer(
        env.session_key,
        "continue despite provider failure",
        persisted_user_message_id="msg-after-error",
    ) == handle.task_id

    fail_first.set()
    await rt.wait(handle.task_id, timeout=2.0)
    await asyncio.wait_for(followup_seen.wait(), timeout=2.0)
    assert runs == ["first", "continue despite provider failure"]


@pytest.mark.asyncio
async def test_failed_late_steer_promotion_is_durable_and_emits_recovery_state() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    release_queued = asyncio.Event()
    rejected_seen = asyncio.Event()
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _handler(run: Any) -> None:
        if run.message == "first":
            first_started.set()
            await release_first.wait()
            return
        await release_queued.wait()

    async def _emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, name, payload))
        if (
            name == "session.event.input_disposition"
            and payload.get("failure_code") == "STEER_PROMOTION_QUEUE_FULL"
        ):
            rejected_seen.set()

    storage = _make_storage()
    rt = TaskRuntime(
        storage=storage,
        turn_handler=_handler,
        event_emitter=_emit,
        max_concurrency=1,
        max_pending_per_session=1,
    )
    env = _make_envelope("agent-1::steer-promotion-full")
    first = await rt.enqueue(env, "first")
    await first_started.wait()
    queued = await rt.enqueue(env, "already queued")
    assert await rt.steer(
        env.session_key,
        "accepted but cannot promote",
        persisted_user_message_id="msg-rejected",
        client_message_id="client-rejected",
        surface_id="tui:test",
    ) == first.task_id

    release_first.set()
    await asyncio.wait_for(rejected_seen.wait(), timeout=2.0)

    rejected = [
        context
        for _session, message_id, context in storage.turn_context_updates
        if message_id == "msg-rejected" and context.get("disposition") == "rejected"
    ]
    assert rejected == [
        {
            "turn_id": first.task_id,
            "client_message_id": "client-rejected",
            "surface_id": "tui:test",
            "intent": "steer",
            "disposition": "rejected",
            "target_turn_id": first.task_id,
            "revision": 2,
            "promoted_from_turn_id": first.task_id,
            "failure_code": "STEER_PROMOTION_QUEUE_FULL",
            "retryable": True,
            "recovery": "resend_after_queue_drains",
        }
    ]
    failure_event = next(
        payload
        for _session, name, payload in events
        if name == "session.event.input_disposition"
        and payload.get("failure_code") == "STEER_PROMOTION_QUEUE_FULL"
    )
    assert failure_event["retryable"] is True
    assert failure_event["recovery"] == "resend_after_queue_drains"
    await rt.wait(first.task_id, timeout=2.0)
    rejected_index = next(
        index
        for index, (_session, name, payload) in enumerate(events)
        if name == "session.event.input_disposition"
        and payload.get("failure_code") == "STEER_PROMOTION_QUEUE_FULL"
    )
    terminal_index = next(
        index
        for index, (_session, name, payload) in enumerate(events)
        if name == "task.succeeded" and payload.get("task_id") == first.task_id
    )
    assert rejected_index < terminal_index

    release_queued.set()
    await rt.wait(queued.task_id, timeout=2.0)


# ---------------------------------------------------------------------------
# cancel_clears_dicts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_clears_dicts() -> None:
    """After a task is cancelled, all five tracking dicts must not contain its key."""
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def _blocking_handler(_run: Any) -> None:
        started.set()
        await blocker.wait()  # blocks until test cancels

    rt = _make_runtime(turn_handler=_blocking_handler)
    env = _make_envelope("agent-1::sess-b")
    handle = await rt.enqueue(env, "hello")

    # Wait for the handler to actually start, then cancel.
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await rt.cancel(task_id=handle.task_id)
    await rt.wait(handle.task_id, timeout=2.0)

    sk = env.session_key
    assert handle.task_id not in rt._tasks
    assert sk not in rt._running_by_session
    assert sk not in rt._pending_by_session
    # _session_locks is intentionally retained.
    assert sk not in rt._last_envelope_by_session


@pytest.mark.asyncio
async def test_cancel_closes_steer_window_before_disposition_persistence() -> None:
    """A steer cannot enter after cancellation reclaimed the accepted inputs."""

    started = asyncio.Event()
    blocker = asyncio.Event()
    cleanup_persisting = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def _blocking_handler(_run: Any) -> None:
        started.set()
        await blocker.wait()

    storage = _make_storage()
    update_turn_context = storage.update_transcript_turn_context

    async def _blocking_turn_context_update(
        session_key: str,
        message_id: str,
        context: dict[str, Any],
    ) -> bool:
        if message_id == "msg-before-cancel":
            cleanup_persisting.set()
            await release_cleanup.wait()
        return await update_turn_context(session_key, message_id, context)

    storage.update_transcript_turn_context = _blocking_turn_context_update
    rt = TaskRuntime(storage=storage, turn_handler=_blocking_handler)
    env = _make_envelope("agent-1::cancel-steer-race")
    handle = await rt.enqueue(env, "first")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    assert await rt.steer(
        env.session_key,
        "accepted before cancellation",
        persisted_user_message_id="msg-before-cancel",
    ) == handle.task_id
    runtime_task = rt._tasks[handle.task_id]

    assert await rt.cancel(task_id=handle.task_id) == 1
    # cancel() is the acknowledgement boundary. Even before the cancelled
    # task gets another event-loop slice, steer must already reject input.
    assert await rt.steer(
        env.session_key,
        "racing immediately after cancel acknowledgement",
        persisted_user_message_id="msg-after-cancel-ack",
    ) is None
    await asyncio.wait_for(cleanup_persisting.wait(), timeout=2.0)

    # Cancellation has reclaimed the earlier input and is waiting on storage.
    # The acceptance window must already be closed, so this cannot become an
    # orphaned pending item after the task is marked terminal.
    assert await rt.steer(
        env.session_key,
        "racing during cancellation cleanup",
        persisted_user_message_id="msg-during-cancel",
    ) is None

    release_cleanup.set()
    await rt.wait(handle.task_id, timeout=2.0)

    cancelled = [
        context
        for _session, message_id, context in storage.turn_context_updates
        if message_id == "msg-before-cancel"
    ]
    assert cancelled == [
        {
            "turn_id": handle.task_id,
            "client_message_id": None,
            "surface_id": None,
            "intent": "steer",
            "disposition": "cancelled",
                "target_turn_id": handle.task_id,
                "revision": 2,
                "failure_code": "TURN_CANCELLED",
                "retryable": True,
                "recovery": "restore_to_composer",
                "fallback_safe": True,
            }
        ]
    assert runtime_task.pending_input_provider.reclaim_all() == []


# ---------------------------------------------------------------------------
# session_lock_kept_during_pending
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_lock_kept_during_pending() -> None:
    """_session_locks must NOT be removed while another task is still pending."""
    first_started = asyncio.Event()
    first_release = asyncio.Event()

    async def _slow_handler(_run: Any) -> None:
        first_started.set()
        await first_release.wait()

    # Only 1 concurrency slot so the second task stays pending.
    rt = _make_runtime(turn_handler=_slow_handler, max_concurrency=1)
    env = _make_envelope("agent-1::sess-c")

    handle1 = await rt.enqueue(env, "first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)

    # Enqueue second task — it will be QUEUED (pending) while first is running.
    handle2 = await rt.enqueue(env, "second")

    sk = env.session_key
    # Session lock must exist because there is still a pending task.
    assert sk in rt._session_locks

    # Now let the first task finish.
    first_release.set()
    await rt.wait(handle1.task_id, timeout=2.0)

    # The lock should still exist because the second task is still alive.
    assert sk in rt._session_locks

    # Wait for second task to finish.
    await rt.wait(handle2.task_id, timeout=2.0)

    # _session_locks is intentionally retained after all tasks complete;
    # do not assert its absence here.


@pytest.mark.asyncio
async def test_older_terminal_task_keeps_newer_route_envelope_cached() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "first":
            first_started.set()
            await release_first.wait()
        elif run.message == "second":
            second_started.set()
            await release_second.wait()

    rt = _make_runtime(turn_handler=handler, max_concurrency=1)
    session_key = "agent-1::route-cache-race"
    first_envelope = _make_envelope(session_key)
    second_envelope = RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="newer-route",
        agent_id="agent-1",
        session_key=session_key,
        input_provenance={"kind": "newer-test-route"},
    )

    first = await rt.enqueue(first_envelope, "first")
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    second = await rt.enqueue(second_envelope, "second")
    second_runtime_task = rt._tasks[second.task_id]
    cached = rt._last_envelope_by_session[session_key]
    assert cached == second_runtime_task.envelope
    assert cached is not second_runtime_task.envelope
    assert cached.metadata is not second_runtime_task.envelope.metadata
    assert cached.sandbox_run_context_fresh is False
    assert rt._last_envelope_task_id_by_session[session_key] == second.task_id

    release_first.set()
    await rt.wait(first.task_id, timeout=1.0)
    await asyncio.wait_for(second_started.wait(), timeout=1.0)

    # The older task may clean up only the envelope it installed. The newer
    # task's route remains available to TaskRuntime.send until that task ends.
    assert rt._last_envelope_by_session[session_key] is cached
    assert rt._last_envelope_task_id_by_session[session_key] == second.task_id

    release_second.set()
    await rt.wait(second.task_id, timeout=1.0)
    assert session_key not in rt._last_envelope_by_session
    assert session_key not in rt._last_envelope_task_id_by_session


# ---------------------------------------------------------------------------
# exception path cleans up
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exception_path_clears_dicts() -> None:
    """Even when the turn handler raises, cleanup must run for 4 tracking dicts.

    ``_session_locks`` is intentionally NOT cleared on terminal: retaining
    the lock prevents split-brain when a new enqueue races with _execute's
    post-terminal cleanup. All other 4 dicts (``_tasks``,
    ``_running_by_session``, ``_pending_by_session``,
    ``_last_envelope_by_session``) must be cleaned up.
    """

    async def _failing_handler(_run: Any) -> None:
        raise RuntimeError("deliberate failure")

    rt = _make_runtime(turn_handler=_failing_handler)
    env = _make_envelope("agent-1::sess-d")
    handle = await rt.enqueue(env, "hello")
    await rt.wait(handle.task_id, timeout=2.0)

    sk = env.session_key
    assert handle.task_id not in rt._tasks
    assert sk not in rt._running_by_session
    # _session_locks is intentionally retained after terminal: prevents
    # split-brain on rapid re-enqueue; lock is cheap and bounded per session_key.
    assert sk not in rt._pending_by_session
    assert sk not in rt._last_envelope_by_session


# ---------------------------------------------------------------------------
# no_leak_under_load (tracemalloc quantitative)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.ci_serial
async def test_no_leak_under_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """10 000 tasks, each <=50 ms; dict sizes after GC must be within ±2 of baseline."""
    num_tasks = 10_000
    session_count = 50  # rotate sessions to mimic real load
    monkeypatch.setattr(task_runtime, "_emit_metric", lambda *_args, **_kwargs: None)

    async def _instant_handler(_run: Any) -> None:
        pass  # returns immediately — well under 50 ms

    rt = _make_runtime(
        turn_handler=_instant_handler,
        max_concurrency=32,
        max_pending_per_session=None,
    )

    # --- baseline snapshot (before any tasks) ---
    gc.collect()
    tracemalloc.start()
    snap_before = tracemalloc.take_snapshot()

    baseline_tasks = len(rt._tasks)
    baseline_pending = len(rt._pending_by_session)
    baseline_running = len(rt._running_by_session)
    baseline_envelope = len(rt._last_envelope_by_session)

    # --- run 10 000 tasks ---
    # Keep the workload at 10 000 tasks, but drain it in bounded batches. A
    # single gather of 10 000 ``runtime.wait`` calls creates another 10 000
    # waiter tasks and timer bookkeeping on top of the runtime workload; that
    # amplification is what made this quantitative check consume a minute on
    # loaded Windows runners. The shared deadline still catches a stuck task.
    batch_size = 500
    deadline = asyncio.get_running_loop().time() + 60.0
    for batch_start in range(0, num_tasks, batch_size):
        handles = []
        for i in range(batch_start, min(batch_start + batch_size, num_tasks)):
            sk = f"agent-1::sess-load-{i % session_count}"
            env = _make_envelope(sk)
            handles.append(await rt.enqueue(env, f"msg-{i}"))
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("task runtime leak workload exceeded its shared deadline")
        async with asyncio.timeout(remaining):
            await asyncio.gather(*(rt.wait(h.task_id) for h in handles))

    # --- post-GC snapshot ---
    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    tracemalloc.stop()

    after_tasks = len(rt._tasks)
    after_locks = len(rt._session_locks)
    after_pending = len(rt._pending_by_session)
    after_running = len(rt._running_by_session)
    after_envelope = len(rt._last_envelope_by_session)

    tolerance = 2
    assert abs(after_tasks - baseline_tasks) <= tolerance, (
        f"_tasks leaked: baseline={baseline_tasks}, after={after_tasks}"
    )
    # _session_locks is intentionally NOT cleaned at terminal to prevent
    # split-brain on rapid re-enqueue.  The dict grows by # unique session_keys
    # (capped at session_count=50 here), not by # tasks.  We verify it is bounded
    # by session_count rather than by num_tasks.
    assert after_locks <= session_count + tolerance, (
        f"_session_locks grew beyond unique session count: {after_locks} > {session_count}"
    )
    assert abs(after_pending - baseline_pending) <= tolerance, (
        f"_pending_by_session leaked: baseline={baseline_pending}, after={after_pending}"
    )
    assert abs(after_running - baseline_running) <= tolerance, (
        f"_running_by_session leaked: baseline={baseline_running}, after={after_running}"
    )
    assert abs(after_envelope - baseline_envelope) <= tolerance, (
        f"_last_envelope_by_session leaked: baseline={baseline_envelope}, after={after_envelope}"
    )

    # Confirm memory allocation delta is reasonable (no catastrophic growth).
    # Informational only — the dict-size assertions above are authoritative.
    # 10 000 asyncio tasks create significant transient allocation for
    # Task/Future/Event objects; allow up to 200 MB of incidental growth.
    top_stats = snap_after.compare_to(snap_before, "lineno")
    total_added = sum(s.size_diff for s in top_stats if s.size_diff > 0)
    assert total_added < 200 * 1024 * 1024, (
        f"Unexpected memory growth: {total_added / 1024:.1f} KB"
    )
