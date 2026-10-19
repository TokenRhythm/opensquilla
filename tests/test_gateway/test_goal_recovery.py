"""Goal recovery through production lifecycle wiring and real SQLite writes."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from opensquilla.gateway import goal_service as goal_service_module
from opensquilla.gateway.goal_lifecycle import make_goal_lifecycle_listener
from opensquilla.gateway.rpc_goals import (
    _handle_goals_clear,
    _handle_goals_reattach,
    _handle_goals_set,
)
from opensquilla.gateway.rpc_sessions import _handle_sessions_reset
from opensquilla.gateway.session_lifecycle import TaskLifecycleEvent
from opensquilla.gateway.websocket import get_registry
from opensquilla.session.goals import GoalConflictError
from opensquilla.session.models import AgentTaskStatus
from tests.test_gateway.test_goal_rpc import (
    SOURCE_KEY,
    _goal_connection,
    _mutation_params,
    _open_goal_rpc_stack,
    _reattach_params,
    _set_params,
    _table_count,
    _wait_for_goal,
    _wait_until,
)


async def _deny_goal_owner_release(storage, *, compensation_too: bool) -> None:
    # Actual SQLite trigger failure: neither the storage method nor transaction
    # layer is replaced. A one-turn Goal settles with turn_limit, while the
    # compensating path settles with persistence_error.
    condition = "" if compensation_too else "AND NEW.pause_reason != 'persistence_error'"
    await storage.conn.execute(
        f"""CREATE TRIGGER test_deny_goal_owner_release
        BEFORE UPDATE OF active_task_id ON session_goals
        WHEN OLD.active_task_id IS NOT NULL AND NEW.active_task_id IS NULL {condition}
        BEGIN SELECT RAISE(FAIL, 'controlled Goal owner-release outage'); END"""
    )
    await storage.conn.commit()


async def _allow_goal_owner_release(storage) -> None:
    await storage.conn.execute("DROP TRIGGER IF EXISTS test_deny_goal_owner_release")
    await storage.conn.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("compensation_too", [False, True])
async def test_goal_settlement_recovers_real_sqlite_failure_without_lifecycle_replay(
    tmp_path: Path,
    compensation_too: bool,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    terminal_observed = asyncio.Event()
    projections: list[TaskLifecycleEvent] = []

    async def handler(_run) -> None:
        started.set()
        await release.wait()

    async def project(event: TaskLifecycleEvent) -> None:
        projections.append(event)

    async with _open_goal_rpc_stack(
        tmp_path / "goal-settlement.sqlite",
        handler=handler,
        max_turns=1,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        production = make_goal_lifecycle_listener(project, stack.service)

        async def observe(event: TaskLifecycleEvent) -> None:
            await production(event)
            if event.phase == "terminal":
                terminal_observed.set()

        stack.runtime.set_lifecycle_listener(observe)
        created = await _handle_goals_set(_set_params(), stack.context)
        await asyncio.wait_for(started.wait(), timeout=5)
        await _deny_goal_owner_release(stack.storage, compensation_too=compensation_too)
        try:
            with capture_logs():
                release.set()
                await asyncio.wait_for(terminal_observed.wait(), timeout=5)
            task = await stack.storage.get_agent_task(created["taskId"])
            before = await stack.storage.get_goal(SOURCE_KEY)
            assert task is not None and task.status == AgentTaskStatus.SUCCEEDED
            assert before is not None
            if compensation_too:
                # The same outage defeated both real transactions. The task
                # terminal is durable but its Goal owner remains until healing.
                assert before.active_task_id == created["taskId"]
                assert before.turns_settled == 0
            else:
                assert before.active_task_id is None
                assert before.pause_reason == "persistence_error"
        finally:
            await _allow_goal_owner_release(stack.storage)
            release.set()

        healed = await _wait_for_goal(
            stack.storage,
            lambda goal: goal.active_task_id is None and goal.turns_settled == 1,
            timeout=5,
        )
        assert healed.status == "paused"
        assert healed.turns_started == 1
        assert await _table_count(stack.storage, "agent_tasks") == 1
        terminals = [event for event in projections if event.phase == "terminal"]
        assert len(terminals) == 1


@pytest.mark.asyncio
async def test_idle_wake_reconciles_a_dropped_terminal_observer_from_sqlite(tmp_path: Path) -> None:
    terminal = asyncio.Event()
    async with _open_goal_rpc_stack(
        tmp_path / "goal-dropped-observer.sqlite",
        max_turns=1,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:

        async def dropped_goal_listener(event: TaskLifecycleEvent) -> None:
            if event.phase == "terminal":
                terminal.set()
            else:
                await stack.service.on_task_lifecycle(event)

        stack.runtime.set_lifecycle_listener(dropped_goal_listener)
        created = await _handle_goals_set(_set_params(), stack.context)
        await asyncio.wait_for(terminal.wait(), timeout=5)
        task = await stack.storage.get_agent_task(created["taskId"])
        goal = await stack.storage.get_goal(SOURCE_KEY)
        assert task is not None and task.status == AgentTaskStatus.SUCCEEDED
        assert goal is not None and goal.active_task_id == task.task_id
        assert not stack.service._settlement_recoveries
        stack.service.schedule_idle_evaluation(SOURCE_KEY)
        repaired = await _wait_for_goal(
            stack.storage,
            lambda value: value.active_task_id is None,
            timeout=5,
        )
        assert repaired.turns_settled == 1
        assert repaired.pause_reason == "turn_limit"
        assert await _table_count(stack.storage, "agent_tasks") == 1


@pytest.mark.asyncio
async def test_exhausted_settlement_retains_owner_for_later_external_wake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_BUDGET_SECONDS", 0.15)
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_MAX_SECONDS", 0.02)
    release = asyncio.Event()
    terminal = asyncio.Event()
    terminal_events: list[TaskLifecycleEvent] = []

    async def handler(_run) -> None:
        await release.wait()

    async with _open_goal_rpc_stack(
        tmp_path / "goal-exhausted-settlement.sqlite",
        handler=handler,
        max_turns=1,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:

        async def project(event: TaskLifecycleEvent) -> None:
            if event.phase == "terminal":
                terminal_events.append(event)

        production = make_goal_lifecycle_listener(project, stack.service)

        async def observe(event: TaskLifecycleEvent) -> None:
            await production(event)
            if event.phase == "terminal":
                terminal.set()

        stack.runtime.set_lifecycle_listener(observe)
        created = await _handle_goals_set(_set_params(), stack.context)
        await _deny_goal_owner_release(stack.storage, compensation_too=True)
        try:
            with capture_logs():
                release.set()
                await asyncio.wait_for(terminal.wait(), timeout=5)
                recovery = next(iter(stack.service._settlement_recoveries.values()))
                deadline = recovery.window.expires_at
                while time.monotonic() < deadline - 0.02:
                    await stack.service.on_task_lifecycle(terminal_events[0])
                    stack.service.schedule_idle_evaluation(SOURCE_KEY)
                    assert recovery.window.expires_at == deadline
                    await asyncio.sleep(0.005)

                async def exhausted() -> bool:
                    return recovery.window.exhausted and not stack.service._settlement_tasks

                await _wait_until(exhausted, timeout=3)
                assert recovery.window.expires_at == deadline
                goal = await stack.storage.get_goal(SOURCE_KEY)
                assert goal is not None and goal.active_task_id == created["taskId"]
                assert goal.turns_settled == 0
        finally:
            await _allow_goal_owner_release(stack.storage)
            release.set()

        stack.service.schedule_idle_evaluation(SOURCE_KEY)
        healed = await _wait_for_goal(
            stack.storage,
            lambda value: value.active_task_id is None,
            timeout=5,
        )
        assert healed.turns_settled == 1
        assert healed.pause_reason == "persistence_error"
        assert len(terminal_events) == 1
        wake = stack.service._kick_tasks.get(SOURCE_KEY)
        if wake is not None:
            await asyncio.wait_for(asyncio.shield(wake), timeout=5)

        async def recovery_completed() -> bool:
            # A renewed episode may wait in its settlement worker after the
            # idle wake returns. Owner release is committed before its guards
            # and notification finish; observe the complete recovery boundary.
            return (
                not stack.service._settlement_recoveries
                and not stack.service._settlement_tasks
                and SOURCE_KEY not in stack.service._kick_tasks
            )

        await _wait_until(recovery_completed, timeout=5)
        assert not stack.service._settlement_recoveries
        assert not stack.service._settlement_tasks
        final_goal = await stack.storage.get_goal(SOURCE_KEY)
        final_task = await stack.storage.get_agent_task(created["taskId"])
        assert final_goal is not None and final_goal.active_task_id is None
        assert final_goal.status == "paused" and final_goal.pause_reason == "persistence_error"
        assert final_goal.turns_started == final_goal.turns_settled == 1
        assert final_task is not None and final_task.status == AgentTaskStatus.SUCCEEDED
        assert await _table_count(stack.storage, "agent_tasks") == 1
        assert len(terminal_events) == 1


@pytest.mark.asyncio
async def test_idle_retry_recovers_real_sqlite_writer_lock_without_another_wake(
    tmp_path: Path,
) -> None:
    database = tmp_path / "goal-idle-writer.sqlite"
    automatic_runs: list[str] = []
    service = None

    async def handler(run) -> None:
        if run.input_mode == "goal_continuation":
            automatic_runs.append(run.task_id)
            assert service is not None
            await service.commit_model_status(run.goal_context, status="complete", reason=None)

    async with _open_goal_rpc_stack(
        database,
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        service = stack.service
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)
        stack.storage._busy_budget_seconds = 0.03
        blocker = sqlite3.connect(database, timeout=0)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with capture_logs():
                stack.service.schedule_idle_evaluation(SOURCE_KEY)

                async def scheduled_retry() -> bool:
                    return SOURCE_KEY in stack.service._idle_recovery_windows

                await _wait_until(scheduled_retry, timeout=5)
                assert automatic_runs == []
        finally:
            blocker.rollback()
            blocker.close()
        finished = await _wait_for_goal(
            stack.storage,
            lambda goal: goal.status == "complete" and goal.active_task_id is None,
            timeout=5,
        )
        assert len(automatic_runs) == 1
        assert finished.continuation_seq == 1
        assert finished.turns_started == 2
        assert finished.turns_settled == 2
        assert await _table_count(stack.storage, "agent_tasks") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["empty_continuations", "no_progress"])
async def test_committed_settlement_retries_failed_guard_before_next_continuation(
    tmp_path: Path,
    reason: str,
) -> None:
    runs: list[str] = []
    terminals: list[TaskLifecycleEvent] = []

    async def handler(run) -> None:
        runs.append(run.task_id)
        task = await stack.storage.get_agent_task(run.task_id)
        details = dict(task.details)
        details["activity_snapshot"] = {
            "version": 2,
            "task_id": run.task_id,
            "turn_id": run.task_id,
            "complete": True,
            "entries": [],
        }
        if reason == "no_progress":
            details["terminal_assistant_message_content"] = json.dumps({"text": "Still working."})
        await stack.storage.update_agent_task(run.task_id, details=details)

    async def project(event: TaskLifecycleEvent) -> None:
        if event.phase == "terminal":
            terminals.append(event)

    async with _open_goal_rpc_stack(
        tmp_path / f"goal-guard-{reason}.sqlite",
        handler=handler,
        wire_lifecycle=True,
    ) as stack:
        stack.runtime.set_lifecycle_listener(make_goal_lifecycle_listener(project, stack.service))
        await stack.storage.conn.execute(
            f"""CREATE TRIGGER test_deny_guard BEFORE UPDATE OF pause_reason ON session_goals
            WHEN NEW.pause_reason = '{reason}'
            BEGIN SELECT RAISE(FAIL, 'controlled guard pause outage'); END"""
        )
        await stack.storage.conn.commit()
        try:
            with capture_logs():
                await _handle_goals_set(_set_params(), stack.context)

                async def guard_pending() -> bool:
                    return any(
                        record.settled_revision is not None and record.window.failures > 0
                        for record in stack.service._settlement_recoveries.values()
                    )

                await _wait_until(guard_pending, timeout=10)
                recovery = next(iter(stack.service._settlement_recoveries.values()))
                deadline = recovery.window.expires_at
                for _ in range(10):
                    stack.service.schedule_idle_evaluation(SOURCE_KEY)
                    await stack.service.on_task_lifecycle(terminals[-1])
                    await asyncio.sleep(0.01)
                goal = await stack.storage.get_goal(SOURCE_KEY)
                assert goal.active_task_id is None and goal.status == "active"
                assert goal.turns_started == goal.turns_settled == len(runs) == 4
                assert recovery.window.expires_at == deadline
        finally:
            await stack.storage.conn.execute("DROP TRIGGER test_deny_guard")
            await stack.storage.conn.commit()
        goal = await _wait_for_goal(stack.storage, lambda item: item.status == "paused", timeout=5)
        assert goal.pause_reason == reason
        assert goal.turns_started == goal.turns_settled == 4
        assert len(runs) == len(terminals) == 4
        assert await _table_count(stack.storage, "agent_tasks") == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["clear", "reset", "successor"])
async def test_delayed_settlement_cannot_mutate_cleared_reset_or_successor_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_INITIAL_SECONDS", 1.0)
    release = asyncio.Event()
    terminal = asyncio.Event()

    async def handler(_run) -> None:
        await release.wait()

    async with _open_goal_rpc_stack(
        tmp_path / f"goal-fence-{mutation}.sqlite",
        handler=handler,
        max_turns=1,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:

        async def project(event: TaskLifecycleEvent) -> None:
            pass

        production = make_goal_lifecycle_listener(project, stack.service)

        async def observe(event: TaskLifecycleEvent) -> None:
            await production(event)
            if event.phase == "terminal":
                terminal.set()

        stack.runtime.set_lifecycle_listener(observe)
        await _handle_goals_set(_set_params(), stack.context)
        await _deny_goal_owner_release(stack.storage, compensation_too=True)
        with capture_logs():
            release.set()
            await asyncio.wait_for(terminal.wait(), timeout=5)
        assert stack.service._settlement_recoveries
        await _allow_goal_owner_release(stack.storage)
        if mutation == "reset":

            async def skip_archive(*_args) -> None:
                pass

            monkeypatch.setattr(stack.manager, "write_session_archive", skip_archive)
            await _handle_sessions_reset({"key": SOURCE_KEY}, stack.context)
        else:
            status = await stack.service.status(SOURCE_KEY)
            await _handle_goals_clear(
                _mutation_params(status["goal"], request_index=2), stack.context
            )
        if mutation == "successor":
            await _handle_goals_set(_set_params(request_index=3, message_index=103), stack.context)
            before = await _wait_for_goal(stack.storage, lambda item: item.active_task_id is None)
            assert before.pause_reason == "turn_limit"
        else:
            before = None

        async def recovered() -> bool:
            return not stack.service._settlement_recoveries and not stack.service._settlement_tasks

        await _wait_until(recovered, timeout=5)
        assert await stack.storage.get_goal(SOURCE_KEY) == before


@pytest.mark.asyncio
async def test_terminal_observation_needs_persisted_terminal_or_explicit_compensation(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    terminals: list[TaskLifecycleEvent] = []

    async def handler(_run) -> None:
        started.set()
        await release.wait()

    async def project(event: TaskLifecycleEvent) -> None:
        if event.phase == "terminal":
            terminals.append(event)

    async with _open_goal_rpc_stack(
        tmp_path / "goal-terminal-persistence.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        stack.runtime.set_lifecycle_listener(make_goal_lifecycle_listener(project, stack.service))
        created = await _handle_goals_set(_set_params(), stack.context)
        await asyncio.wait_for(started.wait(), timeout=5)
        before = await stack.storage.get_goal(SOURCE_KEY)
        await stack.service.on_task_lifecycle(
            TaskLifecycleEvent(
                phase="terminal",
                session_key=SOURCE_KEY,
                task_id=created["taskId"],
                task_status=AgentTaskStatus.SUCCEEDED,
                run_kind="foreground",
                terminal_persisted=True,
                session_id=before.session_id,
                session_epoch=before.session_epoch,
            )
        )
        assert await stack.storage.get_goal(SOURCE_KEY) == before
        assert (
            await stack.storage.get_agent_task(created["taskId"])
        ).status == AgentTaskStatus.RUNNING
        # Runtime cannot persist success; its real terminal_persisted=False
        # notification permits the explicit fail-closed compensation path.
        await stack.storage.conn.execute("""CREATE TRIGGER test_deny_task_success
            BEFORE UPDATE OF status ON agent_tasks WHEN NEW.status = 'succeeded'
            BEGIN SELECT RAISE(FAIL, 'controlled terminal persistence outage'); END""")
        await stack.storage.conn.commit()
        with capture_logs():
            release.set()
            repaired = await _wait_for_goal(stack.storage, lambda item: item.active_task_id is None)
        assert repaired.pause_reason == "persistence_error"
        assert repaired.turns_settled == 1
        assert (
            await stack.storage.get_agent_task(created["taskId"])
        ).status == AgentTaskStatus.ABANDONED
        assert len(terminals) == 1 and terminals[0].terminal_persisted is False


@pytest.mark.asyncio
async def test_explicit_nonretryable_authority_failure_does_not_schedule_idle_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_goal_rpc_stack(
        tmp_path / "goal-revoked-authority.sqlite",
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)
        attempts = 0

        async def revoked(_goal):
            nonlocal attempts
            attempts += 1
            raise GoalConflictError("GOAL_AUTHORITY_UNAVAILABLE", "revoked", retryable=False)

        monkeypatch.setattr(stack.service, "_lease_for_async", revoked)
        stack.service.schedule_idle_evaluation(SOURCE_KEY)
        await asyncio.wait_for(stack.service._kick_tasks[SOURCE_KEY], timeout=5)
        assert attempts == 1
        assert not stack.service._idle_recovery_windows
        assert await _table_count(stack.storage, "agent_tasks") == 1


@pytest.mark.asyncio
async def test_idle_retry_deadline_is_not_renewed_by_repeated_signals_and_later_wake_heals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_BUDGET_SECONDS", 0.4)
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_INITIAL_SECONDS", 0.02)
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_MAX_SECONDS", 0.03)
    database = tmp_path / "goal-idle-exhausted.sqlite"
    runs: list[str] = []

    async def handler(run) -> None:
        runs.append(run.task_id)
        if run.input_mode == "goal_continuation":
            await stack.service.commit_model_status(
                run.goal_context, status="complete", reason=None
            )

    async with _open_goal_rpc_stack(
        database,
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)
        stack.storage._busy_budget_seconds = 0.03
        blocker = sqlite3.connect(database, timeout=0)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with capture_logs():
                stack.service.schedule_idle_evaluation(SOURCE_KEY)

                async def pending() -> bool:
                    return SOURCE_KEY in stack.service._idle_recovery_windows

                await _wait_until(pending, timeout=5)
                window = stack.service._idle_recovery_windows[SOURCE_KEY][1]
                deadline = window.expires_at
                while time.monotonic() < deadline - 0.03:
                    stack.service.schedule_idle_evaluation(SOURCE_KEY)
                    assert window.expires_at == deadline
                    await asyncio.sleep(0.01)

                async def exhausted() -> bool:
                    return window.exhausted and SOURCE_KEY not in stack.service._kick_tasks

                await _wait_until(exhausted, timeout=5)
                assert window.expires_at == deadline
                assert len(runs) == 1
        finally:
            blocker.rollback()
            blocker.close()
        # Exhaustion retains the state, but does not continuously poll. One
        # genuinely later runtime/connection wake starts another bounded try.
        assert SOURCE_KEY not in stack.service._kick_tasks
        stack.service.schedule_idle_evaluation(SOURCE_KEY)
        goal = await _wait_for_goal(
            stack.storage,
            lambda value: value.status == "complete" and value.active_task_id is None,
        )
        assert goal.turns_started == goal.turns_settled == len(runs) == 2


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_settlement_and_idle_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_INITIAL_SECONDS", 5.0)
    release = asyncio.Event()
    terminal = asyncio.Event()

    async def handler(_run) -> None:
        await release.wait()

    async with _open_goal_rpc_stack(
        tmp_path / "goal-shutdown-recovery.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:

        async def listener(event: TaskLifecycleEvent) -> None:
            await stack.service.on_task_lifecycle(event)
            if event.phase == "terminal":
                terminal.set()

        stack.runtime.set_lifecycle_listener(listener)
        await _handle_goals_set(_set_params(), stack.context)
        await _deny_goal_owner_release(stack.storage, compensation_too=True)
        with capture_logs():
            release.set()
            await asyncio.wait_for(terminal.wait(), timeout=5)
        workers = list(stack.service._settlement_tasks.values())
        assert workers
        stack.service.schedule_idle_evaluation(SOURCE_KEY)
        workers.extend(stack.service._kick_tasks.values())
        await _allow_goal_owner_release(stack.storage)
        await stack.service.prepare_shutdown()
        assert all(task.done() for task in workers)
        assert not stack.service._settlement_tasks and not stack.service._settlement_recoveries
        assert not stack.service._kick_tasks and not stack.service._idle_recovery_windows
        stack.service.schedule_idle_evaluation(SOURCE_KEY)
        assert not stack.service._kick_tasks
        assert await _table_count(stack.storage, "agent_tasks") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_call", [2, 3])
async def test_idle_authority_failure_releases_reservation_on_both_sides_of_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    runs: list[str] = []

    async def handler(run) -> None:
        runs.append(run.task_id)
        if run.input_mode == "goal_continuation":
            await stack.service.commit_model_status(
                run.goal_context,
                status="complete",
                reason=None,
            )

    async with _open_goal_rpc_stack(
        tmp_path / f"goal-authority-boundary-{failure_call}.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)
        original = stack.service._lease_for_async
        reads = 0

        async def authority_outage(goal):
            nonlocal reads
            reads += 1
            if reads == failure_call:
                raise GoalConflictError(
                    "GOAL_AUTHORITY_UNAVAILABLE",
                    "temporary authority storage outage",
                    retryable=True,
                )
            return await original(goal)

        monkeypatch.setattr(stack.service, "_lease_for_async", authority_outage)
        with capture_logs():
            stack.service.schedule_idle_evaluation(SOURCE_KEY)
            await asyncio.wait_for(stack.service._kick_tasks[SOURCE_KEY], timeout=5)
        expected = "complete" if failure_call == 2 else "paused"
        goal = await _wait_for_goal(
            stack.storage,
            lambda item: item.status == expected and item.active_task_id is None,
        )
        assert not stack.runtime._reservations_by_session
        assert goal.turns_started == goal.turns_settled == 2
        assert await _table_count(stack.storage, "agent_tasks") == 2
        if failure_call == 2:
            assert len(runs) == 2
        else:
            assert goal.pause_reason == "activation_failed"
            assert len(runs) == 1


@pytest.mark.asyncio
async def test_idle_kick_that_passed_early_guard_scan_is_fenced_again_at_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_INITIAL_SECONDS", 1.0)
    runs: list[str] = []
    third_automatic = asyncio.Event()
    finish_third = asyncio.Event()
    read_waiting = asyncio.Event()
    release_read = asyncio.Event()

    async def handler(run) -> None:
        runs.append(run.task_id)
        task = await stack.storage.get_agent_task(run.task_id)
        details = dict(task.details)
        details["activity_snapshot"] = {
            "version": 2,
            "task_id": run.task_id,
            "turn_id": run.task_id,
            "complete": True,
            "entries": [],
        }
        await stack.storage.update_agent_task(run.task_id, details=details)
        if len(runs) == 4:
            third_automatic.set()
            await finish_third.wait()

    async with _open_goal_rpc_stack(
        tmp_path / "goal-overlapping-guard.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda value: value.active_task_id is None)

        async def ready_for_manual_kick() -> bool:
            return (
                not stack.service._settlement_recoveries
                and not await stack.runtime.has_session_work(
                    SOURCE_KEY,
                )
            )

        for settled_count in (2, 3):
            await _wait_until(ready_for_manual_kick, timeout=5)
            await stack.service._kick_if_idle(SOURCE_KEY)
            await _wait_for_goal(
                stack.storage,
                lambda value: value.turns_settled == settled_count and value.active_task_id is None,
            )
        await stack.storage.conn.execute("""CREATE TRIGGER test_deny_overlapping_guard
            BEFORE UPDATE OF pause_reason ON session_goals
            WHEN NEW.pause_reason = 'empty_continuations'
            BEGIN SELECT RAISE(FAIL, 'controlled overlapping guard outage'); END""")
        await stack.storage.conn.commit()
        await _wait_until(ready_for_manual_kick, timeout=5)
        await stack.service._kick_if_idle(SOURCE_KEY)
        await asyncio.wait_for(third_automatic.wait(), timeout=5)
        original_get_goal = stack.storage.get_goal

        async def gated_read(key):
            if (
                asyncio.current_task().get_name() == "goal-overlapping-idle"
                and not read_waiting.is_set()
            ):
                read_waiting.set()
                await release_read.wait()
            return await original_get_goal(key)

        monkeypatch.setattr(stack.storage, "get_goal", gated_read)
        racing = asyncio.create_task(
            stack.service._kick_if_idle(SOURCE_KEY),
            name="goal-overlapping-idle",
        )
        try:
            await asyncio.wait_for(read_waiting.wait(), timeout=5)
            with capture_logs():
                finish_third.set()

                async def failed_guard() -> bool:
                    return any(
                        record.settled_revision is not None and record.window.failures > 0
                        for record in stack.service._settlement_recoveries.values()
                    )

                await _wait_until(failed_guard, timeout=5)
                release_read.set()
                await asyncio.wait_for(racing, timeout=5)
            goal = await stack.storage.get_goal(SOURCE_KEY)
            assert goal.active_task_id is None and goal.status == "active"
            assert goal.turns_started == goal.turns_settled == len(runs) == 4
            assert not stack.runtime._reservations_by_session
        finally:
            finish_third.set()
            release_read.set()
            await stack.storage.conn.execute("DROP TRIGGER test_deny_overlapping_guard")
            await stack.storage.conn.commit()
        goal = await _wait_for_goal(
            stack.storage, lambda value: value.status == "paused", timeout=5
        )
        assert goal.pause_reason == "empty_continuations"
        assert goal.turns_started == goal.turns_settled == len(runs) == 4


@pytest.mark.asyncio
async def test_failed_postaccept_compensation_retains_unstarted_owner_until_sqlite_heals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_BUDGET_SECONDS", 0.12)
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_MAX_SECONDS", 0.02)
    runs: list[str] = []

    async def handler(run) -> None:
        runs.append(run.task_id)

    async with _open_goal_rpc_stack(
        tmp_path / "goal-postaccept-compensation.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda value: value.active_task_id is None)
        original = stack.service._lease_for_async
        reads = 0

        async def authority_outage(goal):
            nonlocal reads
            reads += 1
            if reads == 3:
                await _deny_goal_owner_release(stack.storage, compensation_too=True)
                raise GoalConflictError(
                    "GOAL_AUTHORITY_UNAVAILABLE",
                    "post-commit authority outage",
                    retryable=True,
                )
            return await original(goal)

        monkeypatch.setattr(stack.service, "_lease_for_async", authority_outage)
        with capture_logs():
            stack.service.schedule_idle_evaluation(SOURCE_KEY)
            await asyncio.wait_for(stack.service._kick_tasks[SOURCE_KEY], timeout=5)

            async def exhausted() -> bool:
                return (
                    bool(stack.service._settlement_recoveries)
                    and all(
                        record.window.exhausted
                        for record in stack.service._settlement_recoveries.values()
                    )
                    and not stack.service._settlement_tasks
                )

            await _wait_until(exhausted, timeout=5)
        before = await stack.storage.get_goal(SOURCE_KEY)
        assert before.active_task_id is not None
        task = await stack.storage.get_agent_task(before.active_task_id)
        assert task.status == AgentTaskStatus.QUEUED
        assert not stack.runtime._reservations_by_session
        assert SOURCE_KEY not in stack.service._continuity_grants
        assert len(runs) == 1
        await _allow_goal_owner_release(stack.storage)
        stack.service.schedule_idle_evaluation(SOURCE_KEY)
        repaired = await _wait_for_goal(stack.storage, lambda value: value.active_task_id is None)
        assert repaired.status == "paused" and repaired.pause_reason == "activation_failed"
        assert repaired.turns_started == repaired.turns_settled == 2
        task = await stack.storage.get_agent_task(task.task_id)
        assert task.status == AgentTaskStatus.ABANDONED
        assert len(runs) == 1
        assert await _table_count(stack.storage, "agent_tasks") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["connection", "subscription", "feature_disabled", "revoked"])
async def test_failed_detach_or_stop_compensation_recovers_its_original_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    runs: list[str] = []

    async def handler(run) -> None:
        runs.append(run.task_id)
        if run.input_mode == "goal_continuation":
            await stack.service.commit_model_status(
                run.goal_context,
                status="complete",
                reason=None,
            )

    async with _open_goal_rpc_stack(
        tmp_path / f"goal-compensation-{boundary}.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda value: value.active_task_id is None)
        original = stack.service._lease_for_async
        reads = 0

        async def lose_boundary_after_commit(goal):
            nonlocal reads
            reads += 1
            if reads == 3:
                await _deny_goal_owner_release(stack.storage, compensation_too=True)
                if boundary == "connection":
                    get_registry().unregister(stack.context.conn_id)
                elif boundary == "subscription":
                    stack.subscriptions.unsubscribe_messages(stack.context.conn_id, SOURCE_KEY)
                elif boundary == "feature_disabled":
                    stack.service.config.execution_enabled = False
                else:
                    get_registry().get(stack.context.conn_id).principal = None
            return await original(goal)

        monkeypatch.setattr(stack.service, "_lease_for_async", lose_boundary_after_commit)
        with capture_logs():
            stack.service.schedule_idle_evaluation(SOURCE_KEY)
            await asyncio.wait_for(stack.service._kick_tasks[SOURCE_KEY], timeout=5)
        before = await stack.storage.get_goal(SOURCE_KEY)
        task = await stack.storage.get_agent_task(before.active_task_id)
        assert task.status == AgentTaskStatus.QUEUED and task.started_at is None
        assert not stack.runtime._reservations_by_session
        assert stack.service._settlement_recoveries
        assert SOURCE_KEY not in stack.service._leases
        detached = boundary in {"connection", "subscription"}
        if detached:
            assert (
                stack.service._continuity_grants[SOURCE_KEY].continuity_token
                == (created["continuityToken"])
            )
        else:
            assert SOURCE_KEY not in stack.service._continuity_grants
        assert len(runs) == 1
        await _allow_goal_owner_release(stack.storage)
        repaired = await _wait_for_goal(stack.storage, lambda value: value.active_task_id is None)
        assert repaired.turns_started == repaired.turns_settled == 2
        assert repaired.continuation_seq == 1
        task = await stack.storage.get_agent_task(task.task_id)
        assert task.status == AgentTaskStatus.ABANDONED and task.started_at is None
        assert len(runs) == 1
        assert await _table_count(stack.storage, "agent_tasks") == 2
        if not detached:
            assert repaired.status == "paused"
            assert repaired.pause_reason == (
                "feature_disabled" if boundary == "feature_disabled" else "activation_failed"
            )
            return
        assert repaired.status == "active" and repaired.pause_reason is None
        assert task.terminal_reason == "owner_disconnected"
        assert (
            stack.service._continuity_grants[SOURCE_KEY].continuity_token
            == (created["continuityToken"])
        )
        # Healing alone cannot restore a transport lease or run the provider.
        # The original continuity credential can explicitly reattach without
        # a takeover, after which normal admission uses the next sequence.
        assert SOURCE_KEY not in stack.service._leases
        if get_registry().get(stack.context.conn_id) is None:
            get_registry().register(_goal_connection(stack.context.conn_id))
        stack.subscriptions.subscribe_messages(stack.context.conn_id, SOURCE_KEY)
        reattached = await _handle_goals_reattach(
            _reattach_params(created, continuity_token=created["continuityToken"]),
            stack.context,
        )
        assert reattached["goal"]["status"] == "active"
        completed = await _wait_for_goal(
            stack.storage,
            lambda value: value.status == "complete" and value.active_task_id is None,
        )
        assert completed.turns_started == completed.turns_settled == 3
        assert completed.continuation_seq == 2
        assert len(runs) == 2
        assert await _table_count(stack.storage, "agent_tasks") == 3
