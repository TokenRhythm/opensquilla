"""Recovery cancellation keeps ownership until physical cleanup finishes."""

import asyncio
import time

from opensquilla.gateway.recovery_scheduler import RecoveryOperation, RecoveryScheduler


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


async def test_deadline_includes_admission_wait_and_does_not_run_after_gate():
    scheduler = RecoveryScheduler()
    gate = asyncio.get_running_loop().create_future()
    expired = asyncio.Event()
    started = asyncio.Event()
    item = operation(object(), "timed-out", "A", predecessors=(gate,))
    item.deadline = time.monotonic() + 0.01
    assert scheduler.submit(item, lambda: _set(started), expire=expired.set)
    await asyncio.wait_for(expired.wait(), 1)
    gate.set_result(True)
    await asyncio.sleep(0)
    assert item.closed
    assert not started.is_set()
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


async def test_expire_then_cancel_claims_only_one_terminal_result():
    scheduler = RecoveryScheduler()
    item = operation(object(), "expire-race", "A")
    item.deadline = time.monotonic() + 0.01
    expired = asyncio.Event()
    finished = asyncio.Event()
    terminal: list[str] = []
    finish_calls: list[bool] = []

    async def run():
        await asyncio.Future()

    def expire():
        terminal.append("expire")
        expired.set()

    def finish():
        finish_calls.append(True)
        finished.set()

    assert scheduler.submit(
        item,
        run,
        finish=finish,
        expire=expire,
        stale=lambda: terminal.append("stale"),
    )
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
