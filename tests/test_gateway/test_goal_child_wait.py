"""Goal/child-completion integration with event-controlled ordinary runtime turns."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.gateway import background_completion, subagent_announce
from opensquilla.gateway.background_completion import BackgroundCompletionManager
from opensquilla.gateway.rpc_goals import (
    _handle_goals_clear,
    _handle_goals_pause,
    _handle_goals_set,
)
from opensquilla.gateway.task_runtime import TaskRun
from opensquilla.gateway.websocket import get_registry
from opensquilla.session.goals import GoalTurnContext
from opensquilla.session.usage_ledger import UsageEventCompletion, UsageEventStart
from tests.test_gateway.test_goal_rpc import (
    SOURCE_KEY,
    _goal_connection,
    _mutation_params,
    _open_goal_rpc_stack,
    _set_params,
    _table_count,
)


@asynccontextmanager
async def _child_group_stack(
    tmp_path, monkeypatch, *, record_usage=False, execution_policy="foreground",
):
    parent_started = asyncio.Event()
    release_parent = asyncio.Event()
    synthesis_started = asyncio.Event()
    release_synthesis = asyncio.Event()
    continuation_started = asyncio.Event()
    release_continuation = asyncio.Event()
    group_released = asyncio.Event()
    runs: list[TaskRun] = []
    group_events: list[str] = []
    manager = None

    async def handler(run):
        runs.append(run)
        if len(runs) == 1:
            assert manager is not None
            await manager.emit_waiting(
                parent_session_key=SOURCE_KEY,
                parent_task_id=run.task_id,
                pending_count=1,
                parent_envelope=run.envelope,
            )
            parent_started.set()
            await release_parent.wait()
        elif run.run_kind == "runtime_send":
            synthesis_started.set()
            await release_synthesis.wait()
            if record_usage:
                context = GoalTurnContext.from_task_detail(run.goal_context)
                assert context is not None
                call = UsageEventStart(
                    event_id=f"call-{run.task_id}",
                    execution_id=run.task_id,
                    call_index=1,
                    turn_id=run.task_id,
                    session_id=run.envelope.session_id,
                    session_epoch=run.envelope.session_epoch,
                    started_at_ms=200,
                    root_turn_id=run.envelope.metadata["usage_root_turn_id"],
                    goal_id=context.goal_id,
                )
                await stack.storage.start_usage_event(call)
                await stack.storage.finalize_usage_event(
                    call.event_id,
                    UsageEventCompletion(
                        completed_at_ms=250, input_tokens=7, output_tokens=3, total_tokens=10
                    ),
                )
        else:
            continuation_started.set()
            await release_continuation.wait()

    async def group_event(_session_key, name, _payload):
        group_events.append(name)

    async with _open_goal_rpc_stack(
        tmp_path / "goal-child-wait.sqlite",
        handler=handler,
        wire_lifecycle=True,
        max_turns=3,
    ) as stack:
        manager = BackgroundCompletionManager(
            session_manager=stack.manager, event_emitter=group_event
        )

        def released(session_key):
            # Re-entrant consumers must never be invoked under the manager lock.
            assert not manager._state_lock.locked()
            stack.service.schedule_idle_evaluation(session_key)
            group_released.set()

        manager.set_idle_listener(released)
        manager.set_cancel_listener(stack.service.on_completion_group_cancelled)
        monkeypatch.setattr(subagent_announce, "_background_completion_manager", manager)

        async def wake():
            await manager.send_parent_wake(
                parent_session_key=SOURCE_KEY,
                parent_task_id=runs[0].task_id,
                payloads=[{"task_id": "synthetic-child", "status": "succeeded"}],
                task_runtime=stack.runtime,
                message="Summarize the completed synthetic child result.",
                provenance={
                    "kind": "internal_system",
                    "source_tool": "subagent_completion",
                    "parent_task_id": runs[0].task_id,
                },
                parent_envelope=runs[0].envelope,
            )

        try:
            created = await _handle_goals_set(
                {**_set_params(), "executionPolicy": execution_policy}, stack.context,
            )
            await asyncio.wait_for(parent_started.wait(), timeout=3)
            yield SimpleNamespace(
                stack=stack,
                manager=manager,
                created=created,
                runs=runs,
                group_events=group_events,
                group_released=group_released,
                release_parent=release_parent,
                synthesis_started=synthesis_started,
                release_synthesis=release_synthesis,
                continuation_started=continuation_started,
                wake=wake,
            )
        finally:
            release_parent.set()
            release_synthesis.set()
            release_continuation.set()
            await manager.close(timeout=3)


async def test_child_synthesis_group_releases_before_goal_continuation(tmp_path, monkeypatch):
    async with _child_group_stack(tmp_path, monkeypatch) as state:
        await state.wake()
        state.release_parent.set()
        await asyncio.wait_for(state.synthesis_started.wait(), timeout=3)
        assert [run.run_kind for run in state.runs] == ["session_turn", "runtime_send"]
        assert await state.manager.active_group_ids(SOURCE_KEY)
        await state.stack.service._kick_if_idle(SOURCE_KEY)
        assert len(state.runs) == 2
        assert not state.continuation_started.is_set()

        state.release_synthesis.set()
        await asyncio.wait_for(state.group_released.wait(), timeout=3)
        await asyncio.wait_for(state.continuation_started.wait(), timeout=3)
        await state.manager.drain(timeout=3)
        assert not await state.manager.active_group_ids(SOURCE_KEY)
        assert state.group_events == [
            "session.event.task_group.waiting",
            "session.event.task_group.synthesizing",
            "session.event.task_group.done",
        ]
        assert len(state.runs) == 3
        context = GoalTurnContext.from_task_detail(state.runs[-1].goal_context)
        assert context is not None and context.automatic
        assert context.goal_id == state.created["goal"]["goalId"]


async def test_child_synthesis_claims_current_goal_through_normal_runtime(tmp_path, monkeypatch):
    async with _child_group_stack(tmp_path, monkeypatch) as state:
        await state.wake()
        state.release_parent.set()
        await asyncio.wait_for(state.synthesis_started.wait(), timeout=3)
        synthesis = state.runs[-1]
        assert synthesis.run_kind == "runtime_send"
        context = GoalTurnContext.from_task_detail(synthesis.goal_context)
        assert context is not None
        assert context.goal_id == state.created["goal"]["goalId"]
        assert context.task_id == synthesis.task_id
        goal = await state.stack.storage.get_goal(SOURCE_KEY)
        assert goal is not None and goal.active_task_id == synthesis.task_id
        assert goal.turns_started == 2 and goal.turns_settled == 1
        assert synthesis.envelope.metadata["usage_root_turn_id"] == synthesis.task_id
        assert synthesis.task_id != state.runs[0].task_id


async def test_child_synthesis_usage_settles_under_its_own_goal_root(tmp_path, monkeypatch):
    async with _child_group_stack(tmp_path, monkeypatch, record_usage=True) as state:
        await state.wake()
        state.release_parent.set()
        await asyncio.wait_for(state.synthesis_started.wait(), timeout=3)
        state.release_synthesis.set()
        await asyncio.wait_for(state.continuation_started.wait(), timeout=3)
        goal = await state.stack.storage.get_goal(SOURCE_KEY)
        assert goal is not None
        assert goal.total_tokens == goal.budget_tokens_used == 10
        assert goal.turns_started == 3 and goal.turns_settled == 2
        await state.manager.drain(timeout=3)
        assert not await state.manager.active_group_ids(SOURCE_KEY)


async def test_duplicate_wake_admission_cannot_leave_finished_group_blocking_goal(
    tmp_path, monkeypatch
):
    duplicate_lookup_started = asyncio.Event()
    release_duplicate_lookup = asyncio.Event()
    original_capture = background_completion._capture_delivery_target
    lookups = 0

    async def capture(**kwargs):
        nonlocal lookups
        lookups += 1
        if lookups == 2:
            duplicate_lookup_started.set()
            await release_duplicate_lookup.wait()
        return await original_capture(**kwargs)

    monkeypatch.setattr(background_completion, "_capture_delivery_target", capture)
    async with _child_group_stack(tmp_path, monkeypatch) as state:
        await state.wake()
        state.release_parent.set()
        await asyncio.wait_for(state.synthesis_started.wait(), timeout=3)
        duplicate = asyncio.create_task(state.wake())
        try:
            await asyncio.wait_for(duplicate_lookup_started.wait(), timeout=3)
            state.release_synthesis.set()
            await state.manager.drain(timeout=3)
            assert "session.event.task_group.done" in state.group_events
            assert not state.continuation_started.is_set()
            release_duplicate_lookup.set()
            await asyncio.wait_for(duplicate, timeout=3)

            assert not await state.manager.active_group_ids(SOURCE_KEY)
            await asyncio.wait_for(state.continuation_started.wait(), timeout=3)
            assert len(state.runs) == 3
        finally:
            release_duplicate_lookup.set()
            await asyncio.wait_for(duplicate, timeout=3)


@pytest.mark.parametrize("case", ["ordinary", "paused", "replaced", "wrong_parent"])
async def test_completion_candidate_does_not_grant_unrelated_goal_authority(tmp_path, case):
    runs = []

    async def handler(run):
        runs.append(run)

    async with _open_goal_rpc_stack(
        tmp_path / "goal-completion-authority.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=3)
        parent = runs[0]
        current = await stack.storage.get_goal(SOURCE_KEY)
        assert current is not None
        mutation = {
            "sessionKey": SOURCE_KEY,
            "expectedGoalId": current.goal_id,
            "expectedStateRevision": current.state_revision,
            "clientRequestId": "00000000-0000-4000-8000-000000000002",
        }
        if case == "paused":
            await _handle_goals_pause(mutation, stack.context)
        elif case == "replaced":
            await _handle_goals_clear(mutation, stack.context)
            second = await _handle_goals_set(
                _set_params(
                    objective="A distinct synthetic Goal.", request_index=3, message_index=103
                ),
                stack.context,
            )
            await stack.runtime.wait(second["taskId"], timeout=3)
            assert second["goal"]["goalId"] != created["goal"]["goalId"]
        before = await stack.storage.get_goal(SOURCE_KEY)
        provenance = {
            "kind": "internal_system",
            "source_tool": "other" if case == "ordinary" else "subagent_completion",
            "parent_task_id": "missing-parent" if case == "wrong_parent" else parent.task_id,
        }
        handle = await stack.runtime.send_with_envelope(
            parent.envelope,
            "Summarize the prior result without new Goal authority.",
            provenance=provenance,
        )
        await stack.runtime.wait(handle.task_id, timeout=3)
        assert runs[-1].run_kind == "runtime_send"
        assert runs[-1].goal_context is None
        assert await stack.storage.get_goal(SOURCE_KEY) == before


async def _disconnect_owner_and_open_controller(stack):
    conn_id = stack.context.conn_id
    get_registry().unregister(conn_id)
    stack.subscriptions.remove_connection(conn_id)
    await stack.service.on_subscription_lost(conn_id, SOURCE_KEY)
    assert get_registry().get(conn_id) is None
    assert SOURCE_KEY not in stack.service._leases
    assert SOURCE_KEY in stack.service._continuity_grants
    controller = replace(stack.context, conn_id=f"{conn_id}-controller")
    get_registry().register(_goal_connection(controller.conn_id))
    return controller


async def test_disconnected_background_clear_cannot_reclaim_goal_from_late_child(
    tmp_path, monkeypatch,
):
    async with _child_group_stack(
        tmp_path, monkeypatch, execution_policy="background",
    ) as state:
        stack = state.stack
        controller = await _disconnect_owner_and_open_controller(stack)
        try:
            before = await stack.service.snapshot(await stack.storage.get_goal(SOURCE_KEY))
            assert before["executionPolicy"] == "background"
            response = await _handle_goals_clear(
                _mutation_params(before, request_index=2), controller,
            )
            assert response["goal"] is None
            assert SOURCE_KEY not in stack.service._continuity_grants
            assert SOURCE_KEY not in stack.service._leases
            # Clear does not cancel ordinary accepted work. Its late child
            # result can be summarized, but cannot restore Goal ownership.
            await state.wake()
            state.release_parent.set()
            await asyncio.wait_for(state.synthesis_started.wait(), timeout=3)
            assert state.runs[-1].run_kind == "runtime_send"
            assert state.runs[-1].goal_context is None
            state.release_synthesis.set()
            await stack.runtime.wait(state.runs[-1].task_id, timeout=3)
            await state.manager.drain(timeout=3)
            await stack.service._kick_if_idle(SOURCE_KEY)
            await asyncio.gather(*list(stack.service._kick_tasks.values()))
            assert await stack.storage.get_goal(SOURCE_KEY) is None
            assert not state.continuation_started.is_set()
            assert [run.run_kind for run in state.runs] == ["session_turn", "runtime_send"]
            assert await _table_count(stack.storage, "agent_tasks") == 2
            assert SOURCE_KEY not in stack.service._continuity_grants
        finally:
            get_registry().unregister(controller.conn_id)


@pytest.mark.parametrize(
    ("parent_goal", "background_disconnected"),
    [("none", False), ("current", False), ("replaced", False), ("current", True)],
)
async def test_public_stop_releases_old_group_without_reviving_its_goal(
    parent_goal, background_disconnected, tmp_path, monkeypatch,
):
    from opensquilla.gateway.routing import RouteEnvelope, SourceKind
    from opensquilla.gateway.rpc_sessions import _handle_sessions_abort_contract

    calls = []
    continuation_started, finish = asyncio.Event(), asyncio.Event()
    cancellation_started, finish_cancellation = asyncio.Event(), asyncio.Event()
    released = []
    manager = None

    async def handler(run):
        calls.append(run)
        if len(calls) == 1:
            await manager.emit_waiting(
                parent_session_key=SOURCE_KEY, parent_task_id=run.task_id,
                parent_envelope=run.envelope, pending_count=1,
            )
        elif run.run_kind == "goal":
            continuation_started.set()
            await finish.wait()

    async with _open_goal_rpc_stack(
        tmp_path / "cancel-group.sqlite", handler=handler, wire_lifecycle=True,
    ) as stack:
        manager = BackgroundCompletionManager(session_manager=stack.manager)
        monkeypatch.setattr(subagent_announce, "_background_completion_manager", manager)

        def idle(key):
            assert not manager._state_lock.locked()
            released.append(key)
            stack.service.schedule_idle_evaluation(key)

        async def cancel_authority(key, task_id):
            assert not manager._state_lock.locked()
            assert await manager.active_group_ids(key)
            cancellation_started.set()
            await finish_cancellation.wait()
            await stack.service.on_completion_group_cancelled(key, task_id)

        manager.set_idle_listener(idle)
        manager.set_cancel_listener(cancel_authority)
        controller = stack.context
        try:
            if parent_goal == "none":
                session = await stack.storage.get_session(SOURCE_KEY)
                parent = await stack.runtime.enqueue(RouteEnvelope(
                    source_kind=SourceKind.WEB, source_name="synthetic-parent", agent_id="main",
                    session_key=SOURCE_KEY, session_id=session.session_id,
                    session_epoch=session.epoch,
                ), "Start ordinary child investigation")
                await stack.runtime.wait(parent.task_id, timeout=2)
            created = await _handle_goals_set(
                {**_set_params(), "executionPolicy": (
                    "background" if background_disconnected else "foreground"
                )}, stack.context,
            )
            await stack.runtime.wait(created["taskId"], timeout=2)
            if parent_goal == "replaced":
                goal = await stack.service.snapshot(await stack.storage.get_goal(SOURCE_KEY))
                await _handle_goals_clear(_mutation_params(goal, request_index=2), stack.context)
                created = await _handle_goals_set(
                    _set_params(request_index=3, message_index=103), stack.context,
                )
                await stack.runtime.wait(created["taskId"], timeout=2)
            await asyncio.gather(*list(stack.service._kick_tasks.values()))
            assert not continuation_started.is_set()
            if background_disconnected:
                controller = await _disconnect_owner_and_open_controller(stack)
            parent_task_id = calls[0].task_id
            params = {"key": SOURCE_KEY, "taskId": parent_task_id, "scope": "task"}
            cancellation = asyncio.create_task(
                _handle_sessions_abort_contract(params, controller)
            )
            await asyncio.wait_for(cancellation_started.wait(), 2)
            await stack.service._kick_if_idle(SOURCE_KEY)
            assert not continuation_started.is_set()
            finish_cancellation.set()
            response = await asyncio.wait_for(cancellation, 3)
            assert response["aborted"]
            assert not await manager.active_group_ids(SOURCE_KEY)
            assert released == [SOURCE_KEY]
            if parent_goal == "current":
                await asyncio.gather(*list(stack.service._kick_tasks.values()))
                goal = await stack.storage.get_goal(SOURCE_KEY)
                assert goal.status == "paused"
                assert goal.pause_reason == "user_cancelled"
                assert not continuation_started.is_set()
                assert SOURCE_KEY not in stack.service._leases
                assert SOURCE_KEY not in stack.service._continuity_grants
                before_late_child = await _table_count(stack.storage, "agent_tasks")
                await manager.send_parent_wake(
                    parent_session_key=SOURCE_KEY, parent_task_id=parent_task_id,
                    payloads=[{"task_id": "late-synthetic-child", "status": "succeeded"}],
                    task_runtime=stack.runtime, message="Late synthetic child result.",
                    provenance={
                        "kind": "internal_system", "source_tool": "subagent_completion",
                        "parent_task_id": parent_task_id,
                    }, parent_envelope=calls[0].envelope,
                )
                await manager.drain(timeout=3)
                await stack.service._kick_if_idle(SOURCE_KEY)
                assert await _table_count(stack.storage, "agent_tasks") == before_late_child
                assert not continuation_started.is_set()
            else:
                await asyncio.wait_for(continuation_started.wait(), 2)
                goal = await stack.storage.get_goal(SOURCE_KEY)
                assert goal.status == "active"
                assert goal.goal_id == created["goal"]["goalId"]
            before_replay = len(calls)
            await _handle_sessions_abort_contract(params, controller)
            await stack.service._kick_if_idle(SOURCE_KEY)
            assert len(calls) == before_replay
            assert released == [SOURCE_KEY]
        finally:
            if controller is not stack.context:
                get_registry().unregister(controller.conn_id)
            finish_cancellation.set()
            finish.set()
            await manager.close(timeout=2)
