"""Recovery cancellation keeps ownership until physical cleanup finishes."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opensquilla.gateway import recovery_scheduler
from opensquilla.gateway.recovery_scheduler import RecoveryOperation, RecoveryScheduler


@pytest.fixture
def recovery_clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    # Windows' monotonic clock can tick past a 10 ms deadline before submit.
    # Replace only the scheduler's clock; asyncio keeps its real timeout clock.
    monkeypatch.setattr(
        recovery_scheduler, "time", SimpleNamespace(monotonic=lambda: clock.now),
    )
    return clock


def operation(runtime, request_id, key, *, connection_id="connection", predecessors=()):
    return RecoveryOperation(
        request_id=request_id,
        connection_id=connection_id,
        method="sessions.messages.snapshot.read",
        key=key,
        runtime=runtime,
        deadline=time.monotonic() + 7,
        predecessors=predecessors,
    )


async def test_retiring_key_cannot_occupy_another_slot_and_other_key_runs():
    scheduler = RecoveryScheduler()
    runtime = object()
    entered = asyncio.Event()
    retiring = asyncio.Event()
    physical_release = asyncio.Event()
    healthy = asyncio.Event()
    successor = asyncio.Event()
    terminal: list[str] = []

    async def stuck():
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            retiring.set()
            await physical_release.wait()

    first = operation(runtime, "first", "A")
    assert scheduler.submit(first, stuck, stale=lambda: terminal.append("stale"))
    await entered.wait()
    scheduler.cancel(first)
    assert terminal == ["stale"]
    scheduler.cancel(first)
    assert terminal == ["stale"]
    assert scheduler.running == 1
    await retiring.wait()
    second = operation(runtime, "second", "A", connection_id="reconnected")
    assert scheduler.submit(second, lambda: _set(successor))
    assert scheduler.submit(operation(runtime, "healthy", "B"), lambda: _set(healthy))
    await asyncio.wait_for(healthy.wait(), 1)
    assert not successor.is_set()
    assert scheduler.running >= 1
    physical_release.set()
    await asyncio.wait_for(successor.wait(), 1)
    await asyncio.sleep(0)


async def _set(event):
    event.set()


async def test_pending_subscription_does_not_hold_active_slot():
    scheduler = RecoveryScheduler()
    runtime = object()
    subscribed = asyncio.get_running_loop().create_future()
    completed = asyncio.Event()
    snapshot = operation(runtime, "snapshot", "A", predecessors=(subscribed,))
    assert scheduler.submit(snapshot, lambda: _set(completed))
    assert scheduler.running == 0

    async def subscribe():
        subscribed.set_result(True)

    assert scheduler.submit(operation(runtime, "subscribe", "A"), subscribe)
    await asyncio.wait_for(completed.wait(), 1)
    await asyncio.sleep(0)
    assert scheduler.waiting == 0


async def test_cancelling_queued_job_finishes_without_starting():
    scheduler = RecoveryScheduler()
    gate = asyncio.get_running_loop().create_future()
    started = asyncio.Event()
    finished = []
    item = operation(object(), "cancelled", "A", predecessors=(gate,))
    assert scheduler.submit(item, lambda: _set(started), finish=lambda: finished.append(True))
    scheduler.cancel(item)
    scheduler.cancel(item)
    gate.set_result(True)
    await asyncio.sleep(0)
    assert not started.is_set()
    assert finished == [True]
    assert scheduler.waiting == scheduler.running == 0


async def test_deadline_includes_admission_wait_and_does_not_run_after_gate(recovery_clock):
    scheduler = RecoveryScheduler()
    gate = asyncio.get_running_loop().create_future()
    expired = asyncio.Event()
    started = asyncio.Event()
    item = operation(object(), "timed-out", "A", predecessors=(gate,))
    item.deadline = recovery_clock.now + 0.01
    # Capture only submission's timer, leaving wait_for's real timer untouched.
    with patch.object(asyncio.get_running_loop(), "call_later") as timer:
        assert scheduler.submit(item, lambda: _set(started), expire=expired.set)
    assert scheduler.waiting == 1
    assert not expired.is_set()
    timer.assert_called_once()
    delay, callback, scheduled_item = timer.call_args.args
    assert delay == pytest.approx(0.01)
    recovery_clock.now = item.deadline
    callback(scheduled_item)
    await asyncio.wait_for(expired.wait(), 1)
    gate.set_result(True)
    await asyncio.sleep(0)
    assert item.closed
    assert not started.is_set()
    assert scheduler.waiting == scheduler.running == 0


@pytest.mark.parametrize("elapsed", [0.01, 0.015625])
def test_deadline_expired_before_admission_is_rejected(recovery_clock, elapsed):
    scheduler = RecoveryScheduler()
    item = operation(object(), "expired-before-submit", "A")
    item.deadline = recovery_clock.now + 0.01
    recovery_clock.now += elapsed
    assert not scheduler.submit(item, lambda: _set(asyncio.Event()))
    assert not item.started
    assert scheduler.waiting == scheduler.running == 0


async def test_stale_queued_job_reports_request_local_terminal_result_once():
    scheduler = RecoveryScheduler()
    gate = asyncio.get_running_loop().create_future()
    stale: list[bool] = []
    current = True
    item = operation(object(), "stale", "A", predecessors=(gate,))
    item.is_current = lambda: current
    assert scheduler.submit(
        item, lambda: _set(asyncio.Event()), stale=lambda: stale.append(True),
    )
    current = False
    gate.set_result(True)
    await asyncio.sleep(0)
    assert item.closed
    assert stale == [True]
    assert scheduler.waiting == scheduler.running == 0


async def test_running_job_that_loses_current_reports_stale_before_finish():
    scheduler = RecoveryScheduler()
    runtime = object()
    current = True
    entered = asyncio.Event()
    release = asyncio.Event()
    terminal: list[str] = []
    item = operation(runtime, "running-stale", "A")
    item.is_current = lambda: current

    async def run():
        entered.set()
        await release.wait()

    assert scheduler.submit(item, run, stale=lambda: terminal.append("stale"))
    await entered.wait()
    current = False
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert terminal == ["stale"]
    assert scheduler.running == scheduler.waiting == 0


async def test_expire_then_cancel_claims_only_one_terminal_result(recovery_clock):
    scheduler = RecoveryScheduler()
    item = operation(object(), "expire-race", "A")
    item.deadline = recovery_clock.now + 0.01
    entered = asyncio.Event()
    expired = asyncio.Event()
    finished = asyncio.Event()
    terminal: list[str] = []
    finish_calls: list[bool] = []

    async def run():
        entered.set()
        await asyncio.Future()

    def expire():
        terminal.append("expire")
        expired.set()

    def finish():
        finish_calls.append(True)
        finished.set()

    with patch.object(asyncio.get_running_loop(), "call_later") as timer:
        assert scheduler.submit(
            item,
            run,
            finish=finish,
            expire=expire,
            stale=lambda: terminal.append("stale"),
        )
    await entered.wait()
    timer.assert_called_once()
    delay, callback, scheduled_item = timer.call_args.args
    assert delay == pytest.approx(0.01)
    recovery_clock.now = item.deadline
    callback(scheduled_item)
    await expired.wait()
    assert terminal == ["expire"]
    scheduler.cancel(item)
    await finished.wait()
    assert terminal == ["expire"]
    assert finish_calls == [True]
    assert item.closed
    assert scheduler.waiting == scheduler.running == 0

async def test_dispatcher_response_claim_suppresses_late_stale():
    scheduler = RecoveryScheduler()
    item = operation(object(), "claimed", "A")
    terminal: list[str] = []
    assert scheduler.submit(
        item,
        lambda: _set(asyncio.Event()),
        stale=lambda: terminal.append("stale"),
    )
    assert item.claim_response()
    assert not item.claim_response()
    item.is_current = lambda: False
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert terminal == []
    assert scheduler.running == scheduler.waiting == 0


async def test_owner_preserving_mutation_keeps_dispatch_order_without_blocking_reads():
    scheduler = RecoveryScheduler()
    runtime = object()
    predecessor, reset = scheduler.admit_mutation(runtime, "A")
    assert predecessor is None
    predecessor, compact = scheduler.admit_mutation(runtime, "A", blocks_reads=False)
    assert predecessor is reset
    assert scheduler.mutation_tail(runtime, "A") is compact
    assert scheduler.read_barrier_tail(runtime, "A") is reset
    reset.set_result(None)
    await asyncio.sleep(0)
    assert scheduler.read_barrier_tail(runtime, "A") is None
    assert scheduler.mutation_tail(runtime, "A") is compact
    compact.set_result(None)
    await asyncio.sleep(0)
    assert scheduler.mutation_tail(runtime, "A") is None


async def test_old_barrier_completion_cannot_clear_a_later_mutation_barrier():
    scheduler = RecoveryScheduler()
    runtime = object()
    _, old = scheduler.admit_mutation(runtime, "A")
    _, compact = scheduler.admit_mutation(runtime, "A", blocks_reads=False)
    predecessor, replacement = scheduler.admit_mutation(runtime, "A")
    assert predecessor is compact
    old.set_result(None)
    compact.set_result(None)
    await asyncio.sleep(0)
    assert scheduler.mutation_tail(runtime, "A") is replacement
    assert scheduler.read_barrier_tail(runtime, "A") is replacement
    assert scheduler.read_barrier_tail(object(), "A") is None
    replacement.set_result(None)
    await asyncio.sleep(0)
    assert scheduler.read_barrier_tail(runtime, "A") is None
