"""Accepted Goal tasks keep cleanup ownership across Goal lifecycle changes."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from opensquilla.gateway import goal_service as goal_service_module
from opensquilla.gateway.goal_lifecycle import make_goal_lifecycle_listener
from opensquilla.gateway.rpc_goals import _handle_goals_clear, _handle_goals_set
from opensquilla.gateway.rpc_sessions import _handle_sessions_reset
from opensquilla.gateway.session_lifecycle import TaskLifecycleEvent
from opensquilla.session.goals import effective_goal_turn_context
from opensquilla.session.models import AgentTaskStatus
from tests.test_gateway.test_goal_recovery import (
    _allow_goal_owner_release,
    _deny_goal_owner_release,
)
from tests.test_gateway.test_goal_rpc import (
    SOURCE_KEY,
    _mutation_params,
    _open_goal_rpc_stack,
    _set_params,
    _table_count,
    _wait_for_goal,
    _wait_until,
)


def _assert_admission_released(task) -> None:
    assert task.status not in {AgentTaskStatus.QUEUED, AgentTaskStatus.RUNNING}
    owner = task.details["admission_owner"]
    assert owner["resident_lease"] == "released"
    assert owner["settlement"] == "settled"


async def _wait_for_recovery_drain(service) -> None:
    async def drained() -> bool:
        return not service._settlement_recoveries and not service._settlement_tasks

    await _wait_until(drained, timeout=5)


@pytest.mark.asyncio
async def test_initial_set_retains_failed_activation_compensation_until_sqlite_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[str] = []

    async def handler(run) -> None:
        runs.append(run.task_id)

    async with _open_goal_rpc_stack(
        tmp_path / "initial-activation.sqlite", handler=handler,
        wire_lifecycle=True, wire_idle=True,
    ) as stack:
        async def fail_before_runtime_boundary(reservation, **_kwargs):
            assert not reservation.activated
            await _deny_goal_owner_release(stack.storage, compensation_too=True)
            raise RuntimeError("controlled preactivation failure")

        monkeypatch.setattr(stack.runtime, "activate", fail_before_runtime_boundary)
        with capture_logs():
            created = await _handle_goals_set(_set_params(), stack.context)
        before = await stack.storage.get_goal(SOURCE_KEY)
        assert created["accepted"] is True
        assert before.active_task_id == created["taskId"]
        assert stack.service._settlement_recoveries
        assert not stack.runtime._reservations_by_session
        assert not stack.runtime._tasks
        await _allow_goal_owner_release(stack.storage)
        # No new user signal is necessary within the existing recovery window.
        await _wait_for_recovery_drain(stack.service)
        healed = await stack.storage.get_goal(SOURCE_KEY)
        task = await stack.storage.get_agent_task(created["taskId"])
        assert healed.active_task_id is None
        assert healed.status == "paused" and healed.pause_reason == "activation_failed"
        assert healed.turns_started == healed.turns_settled == 1
        assert task.status == AgentTaskStatus.ABANDONED and task.started_at is None
        _assert_admission_released(task)
        replay = await _handle_goals_set(_set_params(), stack.context)
        assert replay["taskId"] == created["taskId"]
        assert await stack.storage.get_goal(SOURCE_KEY) == healed
        assert await _table_count(stack.storage, "agent_tasks") == 1
        assert runs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["clear", "reset"])
async def test_unstarted_cleanup_survives_goal_removal_and_cannot_touch_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    runs: list[str] = []
    successor_started = asyncio.Event()
    release_successor = asyncio.Event()

    async def handler(run) -> None:
        runs.append(run.task_id)
        if len(runs) > 1:
            successor_started.set()
            await release_successor.wait()

    async with _open_goal_rpc_stack(
        tmp_path / f"removed-{mutation}.sqlite", handler=handler,
        wire_lifecycle=True, wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda value: value.active_task_id is None)
        original_activate = stack.runtime.activate

        async def fail_before_runtime_boundary(reservation, **_kwargs):
            assert not reservation.activated
            await _deny_goal_owner_release(stack.storage, compensation_too=True)
            raise RuntimeError("controlled preactivation failure")

        monkeypatch.setattr(stack.runtime, "activate", fail_before_runtime_boundary)
        with capture_logs():
            stack.service.schedule_idle_evaluation(SOURCE_KEY)
            await asyncio.wait_for(stack.service._kick_tasks[SOURCE_KEY], timeout=5)
        pending = next(iter(stack.service._settlement_recoveries.values()))
        old_task_id = pending.event.task_id
        before = await stack.storage.get_goal(SOURCE_KEY)
        assert before.active_task_id == old_task_id
        if mutation == "clear":
            status = await stack.service.status(SOURCE_KEY)
            await _handle_goals_clear(
                _mutation_params(status["goal"], request_index=2), stack.context,
            )
        else:
            async def skip_archive(*_args) -> None:
                pass

            monkeypatch.setattr(stack.manager, "write_session_archive", skip_archive)
            await _handle_sessions_reset({"key": SOURCE_KEY}, stack.context)
        await _allow_goal_owner_release(stack.storage)
        await _wait_for_recovery_drain(stack.service)
        assert await stack.storage.get_goal(SOURCE_KEY) is None
        old_task = await stack.storage.get_agent_task(old_task_id)
        assert old_task.started_at is None
        _assert_admission_released(old_task)
        assert not stack.runtime._reservations_by_session
        assert runs == [created["taskId"]]

        monkeypatch.setattr(stack.runtime, "activate", original_activate)
        successor = await _handle_goals_set(
            _set_params(request_index=3, message_index=103), stack.context,
        )
        await asyncio.wait_for(successor_started.wait(), timeout=5)
        successor_before = await stack.storage.get_goal(SOURCE_KEY)
        # A delayed duplicate of the exact old recovery cannot abandon the
        # successor or account the previously released task a second time.
        await stack.service._reconcile_terminal_goal(pending)
        assert await stack.storage.get_goal(SOURCE_KEY) == successor_before
        assert await stack.storage.get_agent_task(old_task_id) == old_task
        running = await stack.storage.get_agent_task(successor["taskId"])
        assert running.status == AgentTaskStatus.RUNNING
        assert len(runs) == 2
        release_successor.set()
        await stack.runtime.wait(successor["taskId"], timeout=5)


@pytest.mark.asyncio
async def test_delayed_activation_compensation_does_not_abandon_started_task(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[str] = []

    async def handler(run) -> None:
        runs.append(run.task_id)
        started.set()
        await release.wait()

    async with _open_goal_rpc_stack(
        tmp_path / "started-fence.sqlite", handler=handler,
        wire_lifecycle=True, wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await asyncio.wait_for(started.wait(), timeout=5)
        task_before = await stack.storage.get_agent_task(created["taskId"])
        goal_before = await stack.storage.get_goal(SOURCE_KEY)
        context = effective_goal_turn_context(task_before.details)
        assert context is not None
        # Model a delayed activation observation arriving after ownership has
        # crossed to the driver; storage must perform the fence atomically.
        stack.service._retain_goal_activation_compensation(
            context, SOURCE_KEY, sqlite3.OperationalError("delayed activation observation"),
        )
        await _wait_for_recovery_drain(stack.service)
        assert await stack.storage.get_agent_task(created["taskId"]) == task_before
        assert await stack.storage.get_goal(SOURCE_KEY) == goal_before
        assert runs == [created["taskId"]]
        release.set()
        await stack.runtime.wait(created["taskId"], timeout=5)
        healed = await stack.storage.get_goal(SOURCE_KEY)
        assert healed.active_task_id is None and healed.turns_settled == 1


@pytest.mark.asyncio
async def test_failed_terminal_persistence_cleanup_survives_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_INITIAL_SECONDS", 0.5)
    started = asyncio.Event()
    release = asyncio.Event()
    terminal_seen = asyncio.Event()
    terminals: list[TaskLifecycleEvent] = []
    runs: list[str] = []

    async def handler(run) -> None:
        runs.append(run.task_id)
        started.set()
        await release.wait()

    async def project(event: TaskLifecycleEvent) -> None:
        if event.phase == "terminal":
            terminals.append(event)

    async with _open_goal_rpc_stack(
        tmp_path / "terminal-clear.sqlite", handler=handler,
        wire_lifecycle=True, wire_idle=False,
    ) as stack:
        production = make_goal_lifecycle_listener(project, stack.service)

        async def observe(event: TaskLifecycleEvent) -> None:
            await production(event)
            if event.phase == "terminal":
                terminal_seen.set()

        stack.runtime.set_lifecycle_listener(observe)
        created = await _handle_goals_set(_set_params(), stack.context)
        await asyncio.wait_for(started.wait(), timeout=5)
        await stack.storage.conn.execute("""CREATE TRIGGER test_deny_task_success
            BEFORE UPDATE OF status ON agent_tasks WHEN NEW.status = 'succeeded'
            BEGIN SELECT RAISE(FAIL, 'controlled terminal persistence outage'); END""")
        await stack.storage.conn.commit()
        await _deny_goal_owner_release(stack.storage, compensation_too=True)
        with capture_logs():
            release.set()
            await asyncio.wait_for(terminal_seen.wait(), timeout=5)
        assert len(terminals) == 1 and terminals[0].terminal_persisted is False
        assert stack.service._settlement_recoveries
        task = await stack.storage.get_agent_task(created["taskId"])
        assert task.status == AgentTaskStatus.RUNNING
        status = await stack.service.status(SOURCE_KEY)
        await _handle_goals_clear(
            _mutation_params(status["goal"], request_index=2), stack.context,
        )
        await _allow_goal_owner_release(stack.storage)
        # Keep normal terminal persistence unavailable until Goal compensation
        # has released the old task, so runtime's own retry cannot mask a gap.
        await _wait_for_recovery_drain(stack.service)
        assert await stack.storage.get_goal(SOURCE_KEY) is None
        task = await stack.storage.get_agent_task(created["taskId"])
        assert task.status == AgentTaskStatus.ABANDONED
        _assert_admission_released(task)
        assert len(runs) == 1 and len(terminals) == 1
        await stack.storage.conn.execute("DROP TRIGGER test_deny_task_success")
        await stack.storage.conn.commit()
        successor = await _handle_goals_set(
            _set_params(request_index=3, message_index=103), stack.context,
        )
        await stack.runtime.wait(successor["taskId"], timeout=5)
        assert len(runs) == 2
