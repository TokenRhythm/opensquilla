from __future__ import annotations

import asyncio
import time

import pytest

from opensquilla.gateway.shutdown import ShutdownBudget, ShutdownRequest


@pytest.mark.parametrize(
    ("fallback", "accepted", "expected_deadline", "expected_work"),
    [(75, None, 75, 62.95), (75, 10, 10, 8), (75, 600, 75, 62.95), (75, 0, 0, 0)],
)
def test_shutdown_budget_reserves_cleanup_inside_accepted_limit(
    fallback: float, accepted: float | None, expected_deadline: float, expected_work: float,
) -> None:
    request = None if accepted is None else ShutdownRequest("drain", accepted)
    budget = ShutdownBudget(0, fallback, request, now=lambda: 0)
    assert budget.deadline == expected_deadline
    assert budget.work_deadline == pytest.approx(expected_work)


@pytest.mark.asyncio
async def test_wait_observes_inflight_escalation_without_cancelling_phase() -> None:
    started = time.monotonic()
    request = ShutdownRequest("drain", started + 60)
    budget = ShutdownBudget(started, started + 75, request)
    release = asyncio.Event()
    phase = asyncio.create_task(release.wait())
    waiting = asyncio.create_task(budget.wait(phase, work=True))
    try:
        await asyncio.sleep(0)
        request.update("quit", 20)
        assert await asyncio.wait_for(waiting, 0.5) is False
        assert not phase.done()
        assert phase.cancelling() == 0
    finally:
        release.set()
        await phase


@pytest.mark.asyncio
async def test_request_changes_do_not_renew_phase_cap() -> None:
    started = time.monotonic()
    clock = [started]
    request = ShutdownRequest("drain", started + 60)
    budget = ShutdownBudget(started, started + 75, request, now=lambda: clock[0])
    phase = asyncio.create_task(asyncio.Event().wait())
    waiting = asyncio.create_task(budget.wait(phase, limit=1))
    try:
        await asyncio.sleep(0)
        clock[0] += 2
        request.update("drain", 60_000)
        assert await asyncio.wait_for(waiting, 0.5) is False
        assert not phase.done()
    finally:
        phase.cancel()
        await asyncio.gather(phase, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_waiter_keeps_owned_phase_alive() -> None:
    started = time.monotonic()
    budget = ShutdownBudget(started, started + 60)
    phase = asyncio.create_task(asyncio.Event().wait())
    waiting = asyncio.create_task(budget.wait(phase))
    try:
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert phase.cancelling() == 0
    finally:
        phase.cancel()
        await asyncio.gather(phase, return_exceptions=True)


@pytest.mark.asyncio
async def test_completed_phase_is_observed_at_expired_deadline() -> None:
    completed = asyncio.get_running_loop().create_future()
    completed.set_result("settled")
    budget = ShutdownBudget(0, 0)
    assert await budget.wait(completed) is True
    assert completed.result() == "settled"
