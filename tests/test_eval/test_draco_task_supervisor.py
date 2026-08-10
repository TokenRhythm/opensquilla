from __future__ import annotations

import asyncio

import pytest

from opensquilla.eval.draco_task_supervisor import DracoTaskSupervisor


@pytest.mark.asyncio
async def test_supervisor_cancels_and_retrieves_every_worker_after_failure() -> None:
    cancellation_observed = asyncio.Event()

    async def fail() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("sensitive provider detail must not be persisted")

    async def wait_until_cancelled() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_observed.set()
            await asyncio.sleep(0)
            raise

    failing = asyncio.create_task(fail())
    waiting = asyncio.create_task(wait_until_cancelled())
    supervisor = DracoTaskSupervisor([failing, waiting])

    with pytest.raises(RuntimeError):
        await failing
    await supervisor.cancel_and_wait()

    assert cancellation_observed.is_set()
    assert all(task.done() for task in (failing, waiting))
    payload = supervisor.failure_payload(RuntimeError("secret"), rows_written=0)
    assert payload == {
        "schema": "opensquilla.draco-task-supervisor-failure/v1",
        "stage": "task_execution",
        "exception_type": "RuntimeError",
        "scheduled_task_count": 2,
        "rows_written": 0,
        "model_or_judge_started": True,
        "cleanup": {
            "complete": True,
            "cancel_requested_count": 1,
            "cancelled_count": 1,
            "remaining_task_count": 0,
            "exception_types": ["RuntimeError"],
        },
    }
    assert "sensitive" not in repr(payload)
    assert "secret" not in repr(payload)


@pytest.mark.asyncio
async def test_supervisor_cleanup_is_idempotent() -> None:
    task = asyncio.create_task(asyncio.sleep(60))
    supervisor = DracoTaskSupervisor([task])

    await supervisor.cancel_and_wait()
    await supervisor.cancel_and_wait()

    assert task.done()
    assert supervisor.pending_count == 0
    assert supervisor.failure_payload(
        asyncio.CancelledError(), rows_written=3, stage="result_serialization"
    )["cleanup"]["cancel_requested_count"] == 1


@pytest.mark.asyncio
async def test_supervisor_waits_for_worker_that_finishes_after_cancellation() -> None:
    worker_started = asyncio.Event()
    cancellation_observed = asyncio.Event()

    async def finish_cleanup_after_cancel() -> None:
        worker_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_observed.set()
            await asyncio.sleep(0.01)

    task = asyncio.create_task(finish_cleanup_after_cancel())
    supervisor = DracoTaskSupervisor([task])

    await worker_started.wait()
    await supervisor.cancel_and_wait()

    assert cancellation_observed.is_set()
    assert task.done()
    assert supervisor.pending_count == 0
    assert supervisor.failure_payload(RuntimeError(), rows_written=0)["cleanup"] == {
        "complete": True,
        "cancel_requested_count": 1,
        "cancelled_count": 0,
        "remaining_task_count": 0,
        "exception_types": [],
    }
