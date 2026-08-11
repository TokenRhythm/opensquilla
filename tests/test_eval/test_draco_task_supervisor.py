from __future__ import annotations

import asyncio
import gc
import weakref

import pytest

from opensquilla.eval.draco_task_supervisor import (
    DracoRollingTaskWindow,
    DracoTaskSupervisor,
)


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
    assert (
        supervisor.failure_payload(
            asyncio.CancelledError(), rows_written=3, stage="result_serialization"
        )["cleanup"]["cancel_requested_count"]
        == 1
    )


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


@pytest.mark.asyncio
async def test_rolling_window_releases_consumed_task_results() -> None:
    class Payload:
        __slots__ = ("content", "__weakref__")

        def __init__(self, marker: int) -> None:
            self.content = bytes([marker]) * 1_000_000

    async def produce(marker: int) -> Payload:
        await asyncio.sleep(0)
        return Payload(marker)

    window = DracoRollingTaskWindow(
        [lambda marker=marker: produce(marker) for marker in range(6)],
        max_live_tasks=2,
    )
    payload_refs: list[weakref.ReferenceType[Payload]] = []
    task_refs: list[weakref.ReferenceType[asyncio.Task[Payload]]] = []
    consumed = 0
    while (task := await window.next_completed_task()) is not None:
        payload = task.result()
        payload_refs.append(weakref.ref(payload))
        task_refs.append(weakref.ref(task))
        assert window.live_task_count <= 2
        window.release_completed(task)
        consumed += 1
        del payload, task
        await asyncio.sleep(0)
        gc.collect()

    assert consumed == 6
    assert window.scheduled_count == 6
    assert window.live_task_count == 0
    assert window.not_started_count == 0
    assert all(reference() is None for reference in payload_refs)
    assert all(reference() is None for reference in task_refs)


@pytest.mark.asyncio
async def test_rolling_window_backpressures_scheduling_behind_slow_writer() -> None:
    started: list[int] = []

    async def produce(marker: int) -> int:
        started.append(marker)
        await asyncio.sleep(0)
        return marker

    window = DracoRollingTaskWindow(
        [lambda marker=marker: produce(marker) for marker in range(12)],
        max_live_tasks=3,
    )
    consumed = 0
    while (task := await window.next_completed_task()) is not None:
        assert task.result() in range(12)
        assert window.live_task_count <= 3
        await asyncio.sleep(0.01)
        # No fourth worker is admitted while the completed result is awaiting
        # durable writer/fact projection consumption.
        assert len(started) <= consumed + 3
        window.release_completed(task)
        consumed += 1

    assert consumed == 12
    assert len(started) == 12


@pytest.mark.asyncio
async def test_rolling_window_failure_keeps_planned_count_and_cancels_live_only() -> None:
    waiting_cancelled = asyncio.Event()
    started = 0

    async def fail() -> None:
        nonlocal started
        started += 1
        await asyncio.sleep(0)
        raise RuntimeError("private provider detail")

    async def wait_until_cancelled() -> None:
        nonlocal started
        started += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            waiting_cancelled.set()
            raise

    factories = [
        fail,
        wait_until_cancelled,
        *[lambda: asyncio.sleep(0) for _ in range(8)],
    ]
    window = DracoRollingTaskWindow(factories, max_live_tasks=2)
    completed = await window.next_completed_task()
    assert completed is not None
    with pytest.raises(RuntimeError, match="private provider detail"):
        completed.result()

    await window.cancel_and_wait()
    payload = window.failure_payload(RuntimeError("secret"), rows_written=0)

    assert started == 2
    assert waiting_cancelled.is_set()
    assert payload["scheduled_task_count"] == 10
    assert payload["cleanup"]["cancel_requested_count"] == 1
    assert payload["cleanup"]["remaining_task_count"] == 0
    assert payload["cleanup"]["exception_types"] == ["RuntimeError"]
    assert "private provider detail" not in repr(payload)


@pytest.mark.asyncio
async def test_rolling_window_preserves_same_tick_completion_fifo() -> None:
    gate = asyncio.Event()

    async def produce(marker: int) -> int:
        await gate.wait()
        return marker

    window = DracoRollingTaskWindow(
        [lambda marker=marker: produce(marker) for marker in range(8)],
        max_live_tasks=8,
    )
    first_result = asyncio.create_task(window.next_completed_task())
    await asyncio.sleep(0)
    gate.set()

    completed: list[int] = []
    first = await first_result
    assert first is not None
    completed.append(first.result())
    window.release_completed(first)
    while (task := await window.next_completed_task()) is not None:
        completed.append(task.result())
        window.release_completed(task)

    assert completed == list(range(8))


@pytest.mark.asyncio
async def test_rolling_window_surfaces_first_same_tick_failure_before_successes() -> None:
    gate = asyncio.Event()

    async def produce(marker: int) -> int:
        await gate.wait()
        if marker == 0:
            raise RuntimeError("first worker failed")
        return marker

    window = DracoRollingTaskWindow(
        [lambda marker=marker: produce(marker) for marker in range(4)],
        max_live_tasks=4,
    )
    first_result = asyncio.create_task(window.next_completed_task())
    await asyncio.sleep(0)
    gate.set()

    first = await first_result
    assert first is not None
    rows_written = 0
    with pytest.raises(RuntimeError, match="first worker failed"):
        first.result()
    await window.cancel_and_wait()

    payload = window.failure_payload(RuntimeError(), rows_written=rows_written)
    assert payload["rows_written"] == 0
    assert payload["cleanup"]["exception_types"] == ["RuntimeError"]
