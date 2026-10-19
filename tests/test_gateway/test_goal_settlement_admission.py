"""Settlement admission contracts without storage or wall-clock timing races."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.gateway import goal_service as module
from opensquilla.gateway.goal_service import (
    GoalService,
    _GoalRecoveryWindow,
    _GoalSettlementRecovery,
)
from opensquilla.gateway.session_lifecycle import TaskLifecycleEvent
from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.storage import StorageBusyError


@dataclass
class _Clock:
    now: float = 0
    waits: list[tuple[float, asyncio.Future[None]]] = field(default_factory=list)

    async def sleep(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.waits.append((delay, future))
        await future

    async def waiting(self) -> tuple[float, asyncio.Future[None]]:
        for _ in range(100):
            if self.waits:
                return self.waits.pop(0)
            await asyncio.sleep(0)
        raise AssertionError("settlement worker did not reach its wait")


def _stack(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(module, "_GOAL_RECOVERY_BUDGET_SECONDS", 10.0)
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(
        sleep=clock.sleep, create_task=asyncio.create_task, current_task=asyncio.current_task,
    ))
    service = object.__new__(GoalService)
    service._closed = False
    service._settlement_recoveries = {}
    service._settlement_tasks = {}
    service._reconcile_terminal_goal = AsyncMock()
    service.schedule_idle_evaluation = Mock()
    event = TaskLifecycleEvent(
        phase="terminal", session_key="agent:main:webchat:settlement-admission",
        task_id="task-1", task_status=AgentTaskStatus.SUCCEEDED, run_kind="goal",
        terminal_persisted=True, session_id="session-1", session_epoch=0,
    )
    return service, event, clock


def _retain(service, event, *, expires=10, not_before=0, exhausted=False, kind="terminal"):
    recovery = _GoalSettlementRecovery(
        event=event, goal_id="goal-1", compensation_kind=kind,
        window=_GoalRecoveryWindow(expires, not_before=not_before, exhausted=exhausted),
    )
    identity = service._settlement_key(event)
    service._settlement_recoveries[identity] = recovery
    return identity, recovery


@pytest.mark.asyncio
async def test_first_observation_settles_immediately_without_creating_retry_worker(monkeypatch):
    service, event, _clock = _stack(monkeypatch)
    assert await service._observe_terminal_goal(event)
    service._reconcile_terminal_goal.assert_awaited_once()
    assert not service._settlement_tasks and not service._settlement_recoveries


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["terminal", "activation", "defer"])
async def test_external_renewal_waits_for_absolute_floor_and_coalesces_wakes(monkeypatch, kind):
    service, event, clock = _stack(monkeypatch)
    identity, recovery = _retain(
        service, event, expires=1, not_before=8, exhausted=True, kind=kind,
    )
    clock.now = 2
    assert not await service._observe_terminal_goal(event, external_wake=True)
    renewed = recovery.window
    assert renewed.expires_at == 12 and renewed.not_before == 8
    worker = service._settlement_tasks[identity]
    delay, wake = await clock.waiting()
    assert delay == 6
    for _ in range(5):
        assert not await service._observe_terminal_goal(event, external_wake=True)
        assert not await service._observe_terminal_goal(event)
    assert service._settlement_tasks[identity] is worker
    assert recovery.window is renewed and renewed.failures == 0
    service._reconcile_terminal_goal.assert_not_awaited()
    clock.now = 8
    wake.set_result(None)
    await worker
    service._reconcile_terminal_goal.assert_awaited_once_with(recovery)
    assert not service._settlement_tasks and not service._settlement_recoveries
    service.schedule_idle_evaluation.assert_called_once_with(identity[0])


@pytest.mark.asyncio
async def test_exhaustion_before_floor_never_issues_attempt_or_records_another_failure(monkeypatch):
    service, event, clock = _stack(monkeypatch)
    identity, recovery = _retain(service, event, expires=1, not_before=40, exhausted=True)
    clock.now = 2
    for expected_deadline in (12, 22, 32):
        assert not await service._observe_terminal_goal(event, external_wake=True)
        assert recovery.window.expires_at == expected_deadline
        assert recovery.window.not_before == 40 and recovery.window.failures == 0
        worker = service._settlement_tasks[identity]
        delay, wake = await clock.waiting()
        assert delay == 10
        clock.now = expected_deadline
        wake.set_result(None)
        await worker
        assert recovery.window.exhausted
        assert not service._settlement_tasks
        assert not await service._observe_terminal_goal(event)
    service._reconcile_terminal_goal.assert_not_awaited()
    assert service._settlement_recoveries[identity] is recovery


@pytest.mark.asyncio
async def test_raw_expired_deadline_blocks_direct_observer_without_exhausted_flag(monkeypatch):
    service, event, clock = _stack(monkeypatch)
    _identity, recovery = _retain(service, event, expires=10)
    clock.now = 10
    assert not await service._observe_terminal_goal(event)
    assert recovery.window.exhausted
    assert not service._settlement_tasks
    service._reconcile_terminal_goal.assert_not_awaited()
    assert await service._observe_terminal_goal(event, external_wake=True)
    service._reconcile_terminal_goal.assert_awaited_once_with(recovery)


@pytest.mark.asyncio
async def test_external_wake_renews_expired_sleeping_worker_without_replacing_it(monkeypatch):
    service, event, clock = _stack(monkeypatch)
    identity, recovery = _retain(service, event, expires=10, not_before=17)
    clock.now = 9
    assert not await service._observe_terminal_goal(event)
    worker = service._settlement_tasks[identity]
    old_window = recovery.window
    delay, wake = await clock.waiting()
    assert delay == 1
    assert not await service._observe_terminal_goal(event, external_wake=True)
    assert recovery.window is old_window
    clock.now = 11
    assert not await service._observe_terminal_goal(event, external_wake=True)
    assert recovery.window.expires_at == 21 and recovery.window.not_before == 17
    assert service._settlement_tasks[identity] is worker
    wake.set_result(None)
    delay, wake = await clock.waiting()
    assert delay == 6
    service._reconcile_terminal_goal.assert_not_awaited()
    clock.now = 17
    wake.set_result(None)
    await worker
    service._reconcile_terminal_goal.assert_awaited_once_with(recovery)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_attempt", ["direct", "worker"])
async def test_later_external_wake_survives_inflight_attempt_crossing_expiry(
    monkeypatch, initial_attempt,
):
    service, event, clock = _stack(monkeypatch)
    identity, recovery = _retain(service, event, expires=10)
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts = []

    async def reconcile(_recovery):
        attempts.append(clock.now)
        if len(attempts) == 1:
            entered.set()
            await release.wait()
            raise StorageBusyError("controlled wait", waited_ms=1, retry_after_ms=2000)

    service._reconcile_terminal_goal.side_effect = reconcile
    if initial_attempt == "direct":
        initial = asyncio.create_task(service._observe_terminal_goal(event))
    else:
        service._start_goal_settlement_worker(identity, recovery)
        initial = service._settlement_tasks[identity]
    await entered.wait()
    old_window = recovery.window
    clock.now = 9
    assert not await service._observe_terminal_goal(event, external_wake=True)
    assert recovery.window is old_window
    clock.now = 11
    assert not await service._observe_terminal_goal(event, external_wake=True)
    renewed = recovery.window
    assert renewed is not old_window and renewed.expires_at == 21
    assert not await service._observe_terminal_goal(event, external_wake=True)
    assert recovery.window is renewed
    release.set()
    if initial_attempt == "direct":
        assert not await initial
    delay, wake = await clock.waiting()
    assert delay == 2 and renewed.not_before == 13 and renewed.failures == 1
    assert attempts == [0]
    worker = service._settlement_tasks[identity]
    clock.now = 13
    wake.set_result(None)
    await worker
    assert attempts == [0, 13]
    assert not service._settlement_tasks and not service._settlement_recoveries


@pytest.mark.asyncio
async def test_fatal_failure_stays_stopped_until_an_external_wake(monkeypatch):
    service, event, clock = _stack(monkeypatch)
    service._reconcile_terminal_goal.side_effect = ValueError("invalid settlement fixture")
    assert not await service._observe_terminal_goal(event)
    recovery = next(iter(service._settlement_recoveries.values()))
    assert recovery.window.exhausted and recovery.window.failures == 1
    assert not await service._observe_terminal_goal(event)
    service._reconcile_terminal_goal.assert_awaited_once()
    assert not service._settlement_tasks
    # Existing backend semantics permit a later outside wake to retry stopped
    # cleanup; preserving this behavior must still preserve its waiting floor.
    not_before = recovery.window.not_before
    service._reconcile_terminal_goal.side_effect = None
    assert not await service._observe_terminal_goal(event, external_wake=True)
    assert recovery.window.not_before == not_before and recovery.window.failures == 0
    worker = service._settlement_tasks[service._settlement_key(event)]
    delay, wake = await clock.waiting()
    assert delay == not_before
    service._reconcile_terminal_goal.assert_awaited_once()
    clock.now = not_before
    wake.set_result(None)
    await worker
    assert service._reconcile_terminal_goal.await_count == 2
