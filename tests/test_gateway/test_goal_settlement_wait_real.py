"""Settlement admission with actual SQLite failures and a controlled retry hint.

The SQLite trigger, rollback, successful transaction, runtime, and lifecycle are
real. Only translating the observed SQLite error to a long Retry-After is a test
adapter; SQLite itself does not return an HTTP-style retry duration.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from opensquilla.gateway import goal_service as goal_service_module
from opensquilla.gateway.goal_lifecycle import make_goal_lifecycle_listener
from opensquilla.gateway.rpc_goals import _handle_goals_clear, _handle_goals_set
from opensquilla.gateway.session_lifecycle import TaskLifecycleEvent
from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.storage import StorageBusyError
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


def _observe_real_writes_with_hint(
    monkeypatch: pytest.MonkeyPatch,
    storage: Any,
    names: tuple[str, ...],
    *,
    retry_after_ms: int,
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for name in names:
        original = getattr(storage, name)

        async def observed(context, *args, _original=original, _name=name, **kwargs):
            entry = {
                "method": _name,
                "at": time.monotonic(),
                "task_id": context.task_id,
                "goal_id": context.goal_id,
            }
            attempts.append(entry)
            try:
                result = await _original(context, *args, **kwargs)
            except sqlite3.DatabaseError as error:
                # A transaction was actually attempted and rolled back. The
                # controlled hint does not replace the SQLite fault itself.
                assert "controlled Goal owner-release outage" in str(error)
                entry["sqlite_error"] = str(error)
                raise StorageBusyError(
                    "Retry-After adapter for actual SQLite trigger failure",
                    waited_ms=1,
                    retry_after_ms=retry_after_ms,
                ) from error
            entry["committed"] = result is not None
            return result

        monkeypatch.setattr(storage, name, observed)
    return attempts


async def _external_wake(service: Any, *, repeat: int = 1) -> None:
    for _ in range(repeat):
        service.schedule_idle_evaluation(SOURCE_KEY)
    wake = service._kick_tasks.get(SOURCE_KEY)
    if wake is not None:
        await asyncio.wait_for(asyncio.shield(wake), timeout=3)


async def _wait_for_exhaustion(service: Any, recovery: Any) -> None:
    async def exhausted() -> bool:
        return recovery.window.exhausted and not service._settlement_tasks

    await _wait_until(exhausted, timeout=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["terminal", "activation"])
async def test_long_retry_after_survives_repeated_external_wakes_and_exhausted_episodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property,
    kind: str,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_BUDGET_SECONDS", 0.18)
    release = asyncio.Event()
    terminal = asyncio.Event()
    runs: list[str] = []
    terminal_events: list[TaskLifecycleEvent] = []

    async def handler(run) -> None:
        runs.append(run.task_id)
        await release.wait()

    async with _open_goal_rpc_stack(
        tmp_path / f"settlement-hint-{kind}.sqlite",
        handler=handler,
        max_turns=1,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        attempts = _observe_real_writes_with_hint(
            monkeypatch,
            stack.storage,
            (
                "settle_goal_task", "compensate_terminal_persistence_failure",
            ) if kind == "terminal" else ("compensate_goal_activation_failure",),
            retry_after_ms=1800,
        )

        async def project(event: TaskLifecycleEvent) -> None:
            if event.phase == "terminal":
                terminal_events.append(event)

        production = make_goal_lifecycle_listener(project, stack.service)

        async def observe(event: TaskLifecycleEvent) -> None:
            await production(event)
            if event.phase == "terminal":
                terminal.set()

        stack.runtime.set_lifecycle_listener(observe)
        if kind == "activation":
            async def fail_activation(reservation, **_kwargs):
                assert not reservation.activated
                await _deny_goal_owner_release(stack.storage, compensation_too=True)
                raise RuntimeError("controlled preactivation failure")

            monkeypatch.setattr(stack.runtime, "activate", fail_activation)

        with capture_logs():
            created = await _handle_goals_set(_set_params(), stack.context)
            if kind == "terminal":
                await _deny_goal_owner_release(stack.storage, compensation_too=True)
                release.set()
                await asyncio.wait_for(terminal.wait(), timeout=5)
            recovery = next(iter(stack.service._settlement_recoveries.values()))
            initial_attempts = len(attempts)
            assert initial_attempts == (2 if kind == "terminal" else 1)
            assert all("sqlite_error" in item for item in attempts)
            floor = recovery.window.not_before
            first_deadline = recovery.window.expires_at
            assert floor > first_deadline
            await _wait_for_exhaustion(stack.service, recovery)
            before = await stack.storage.get_goal(SOURCE_KEY)
            assert before.active_task_id == created["taskId"]
            assert before.turns_settled == 0
            # Healing the database is not permission to bypass its retained
            # server wait or to revive an exhausted automatic episode.
            await _allow_goal_owner_release(stack.storage)

            for _ in range(2):
                previous = recovery.window
                await _external_wake(stack.service, repeat=12)
                current = recovery.window
                assert current is not previous
                assert current.not_before == floor
                assert current.expires_at < floor
                assert len(attempts) == initial_attempts, attempts
                deadline = current.expires_at
                for _ in range(4):
                    await _external_wake(stack.service, repeat=12)
                    if terminal_events:
                        # Replay the service callback, not the projection.
                        await stack.service.on_task_lifecycle(terminal_events[0])
                    assert recovery.window is current
                    assert current.expires_at == deadline
                    assert current.not_before == floor
                    assert len(attempts) == initial_attempts, attempts
                # Waiting must not retain the per-session transition lock.
                async with asyncio.timeout(0.2):
                    async with stack.service._lock(SOURCE_KEY):
                        pass
                await _wait_for_exhaustion(stack.service, recovery)
                assert len(attempts) == initial_attempts
                assert await stack.storage.get_goal(SOURCE_KEY) == before

            # An expired episode remains stopped even after the floor passes.
            await asyncio.sleep(max(0, floor - time.monotonic()) + 0.02)
            assert len(attempts) == initial_attempts
            await _external_wake(stack.service, repeat=20)
            healed = await _wait_for_goal(
                stack.storage,
                lambda value: value.active_task_id is None,
                timeout=3,
            )

        assert len(attempts) == initial_attempts + 1, attempts
        assert attempts[-1]["at"] >= floor
        assert attempts[-1]["committed"] is True
        assert {item["task_id"] for item in attempts} == {created["taskId"]}
        assert healed.turns_started == healed.turns_settled == 1
        assert healed.pause_reason == (
            "persistence_error" if kind == "terminal" else "activation_failed"
        )
        task = await stack.storage.get_agent_task(created["taskId"])
        assert task.details["admission_owner"]["resident_lease"] == "released"
        assert task.details["admission_owner"]["settlement"] == "settled"
        if kind == "terminal":
            assert task.status == AgentTaskStatus.SUCCEEDED
            assert runs == [created["taskId"]]
            assert len(terminal_events) == 1
        else:
            assert task.status == AgentTaskStatus.ABANDONED
            assert task.started_at is None
            assert runs == terminal_events == []
        assert await _table_count(stack.storage, "agent_tasks") == 1
        assert not stack.service._settlement_recoveries
        assert not stack.service._settlement_tasks
        record_property("actual_sqlite_failures", initial_attempts)
        record_property("exhausted_episodes_before_floor", 3)
        record_property("successful_owner_commits", 1)
        record_property("retry_after_ms", 1800)
        record_property("earliest_success_margin_ms", (attempts[-1]["at"] - floor) * 1000)


@pytest.mark.asyncio
async def test_waiting_old_settlement_cannot_release_successor_owner_in_real_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_BUDGET_SECONDS", 0.18)
    release_first = asyncio.Event()
    release_successor = asyncio.Event()
    first_terminal = asyncio.Event()
    successor_started = asyncio.Event()
    runs: list[str] = []

    async def handler(run) -> None:
        runs.append(run.task_id)
        if len(runs) == 1:
            await release_first.wait()
        else:
            successor_started.set()
            await release_successor.wait()

    async with _open_goal_rpc_stack(
        tmp_path / "settlement-successor.sqlite",
        handler=handler,
        max_turns=1,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        attempts = _observe_real_writes_with_hint(
            monkeypatch,
            stack.storage,
            ("settle_goal_task", "compensate_terminal_persistence_failure"),
            retry_after_ms=1200,
        )

        async def observe(event: TaskLifecycleEvent) -> None:
            await stack.service.on_task_lifecycle(event)
            if event.phase == "terminal":
                first_terminal.set()

        stack.runtime.set_lifecycle_listener(observe)
        created = await _handle_goals_set(_set_params(), stack.context)
        await _deny_goal_owner_release(stack.storage, compensation_too=True)
        with capture_logs():
            release_first.set()
            await asyncio.wait_for(first_terminal.wait(), timeout=5)
            recovery = next(iter(stack.service._settlement_recoveries.values()))
            floor = recovery.window.not_before
            await _wait_for_exhaustion(stack.service, recovery)
            await _allow_goal_owner_release(stack.storage)
            await _external_wake(stack.service, repeat=10)
            assert len(attempts) == 2
            status = await stack.service.status(SOURCE_KEY)
            await _handle_goals_clear(
                _mutation_params(status["goal"], request_index=2), stack.context,
            )
            successor = await _handle_goals_set(
                _set_params(request_index=3, message_index=103), stack.context,
            )
            await asyncio.wait_for(successor_started.wait(), timeout=5)
            successor_before = await stack.storage.get_goal(SOURCE_KEY)
            assert successor_before.active_task_id == successor["taskId"]
            await _wait_for_exhaustion(stack.service, recovery)
            await asyncio.sleep(max(0, floor - time.monotonic()) + 0.02)
            await _external_wake(stack.service, repeat=10)

        old_attempts = [item for item in attempts if item["task_id"] == created["taskId"]]
        assert len(old_attempts) == 3
        assert old_attempts[-1]["at"] >= floor
        assert old_attempts[-1]["committed"] is False
        assert await stack.storage.get_goal(SOURCE_KEY) == successor_before
        successor_task = await stack.storage.get_agent_task(successor["taskId"])
        assert successor_task.status == AgentTaskStatus.RUNNING
        assert runs == [created["taskId"], successor["taskId"]]
        assert await _table_count(stack.storage, "agent_tasks") == 2
        assert not stack.service._settlement_recoveries
        release_successor.set()
        await stack.runtime.wait(successor["taskId"], timeout=5)
        record_property("actual_sqlite_failures", 2)
        record_property("old_owner_successor_commits", 0)
        record_property("retry_after_ms", 1200)
