from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import (
    TaskRuntime,
    TaskRuntimeShuttingDownError,
)
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus


@dataclass
class _Storage:
    records: dict[str, AgentTaskRecord] = field(default_factory=dict)

    async def create_agent_task(self, record: AgentTaskRecord) -> None:
        self.records[record.task_id] = record

    async def update_agent_task(self, task_id: str, **fields: Any) -> None:
        record = self.records.get(task_id)
        if record is None:
            return
        for name, value in fields.items():
            setattr(record, name, value)

    async def get_agent_task(self, task_id: str) -> AgentTaskRecord | None:
        return self.records.get(task_id)

    async def list_agent_tasks(self, **_: Any) -> list[AgentTaskRecord]:
        return list(self.records.values())


def _envelope(session_key: str = "agent-1::shutdown-fence") -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="shutdown-test",
        agent_id="agent-1",
        session_key=session_key,
        input_provenance={"kind": "synthetic-test"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shutdown_kwargs",
    [
        {"timeout": 0.02},
        {"graceful": True, "graceful_timeout": 0.01, "timeout": 0.02},
    ],
    ids=("immediate-cancel", "graceful-fallback"),
)
async def test_shutdown_timeout_reports_and_tracks_residual_driver(
    shutdown_kwargs: dict[str, Any],
) -> None:
    storage = _Storage()
    started = asyncio.Event()
    release = asyncio.Event()
    cancellation_count = 0

    async def handler(_run: Any) -> None:
        nonlocal cancellation_count
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_count += 1

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    handle = await runtime.enqueue(_envelope(), "stubborn")
    await asyncio.wait_for(started.wait(), timeout=1.0)
    driver = runtime._tasks[handle.task_id].asyncio_task
    assert driver is not None

    result = await asyncio.wait_for(
        runtime.shutdown(**shutdown_kwargs),
        timeout=1.0,
    )

    assert result.clean is False
    assert result.abandoned_task_count == 1
    assert result.remaining_driver_count == 1
    assert result.remaining_reservation_count == 0
    assert result.remaining_auxiliary_count == 0
    assert driver.done() is False
    assert runtime._tasks == {}
    assert runtime._running_by_session == {}
    assert driver in runtime._driver_tasks_by_session[_envelope().session_key]
    record = await runtime.status(handle.task_id)
    assert record.status == AgentTaskStatus.ABANDONED
    assert record.terminal_reason == "shutdown_timeout"
    assert cancellation_count >= 2

    release.set()
    await asyncio.wait_for(driver, timeout=1.0)
    assert driver.done() is True
    assert driver.cancelled() is False
    assert runtime._driver_tasks_by_session == {}
    settled = await runtime.status(handle.task_id)
    assert settled.status == AgentTaskStatus.ABANDONED
    assert settled.terminal_reason == "shutdown_timeout"


@pytest.mark.asyncio
async def test_concurrent_shutdown_is_single_flight_and_closes_admission() -> None:
    storage = _Storage()
    started = asyncio.Event()

    async def handler(_run: Any) -> None:
        started.set()
        await asyncio.sleep(60)

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    await runtime.enqueue(_envelope(), "cooperative")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    first = asyncio.create_task(runtime.shutdown(timeout=1.0))
    second = asyncio.create_task(runtime.shutdown(timeout=0.0, cancel=False))
    first_result, second_result = await asyncio.wait_for(
        asyncio.gather(first, second),
        timeout=2.0,
    )

    assert first_result is second_result
    assert first_result.clean is True
    with pytest.raises(TaskRuntimeShuttingDownError):
        await runtime.reserve(_envelope("agent-1::late-root"), "late")


@pytest.mark.asyncio
async def test_shutdown_observes_reservation_activated_after_close_started() -> None:
    storage = _Storage()
    started = asyncio.Event()

    async def handler(_run: Any) -> None:
        started.set()
        await asyncio.sleep(60)

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    reservation = await runtime.reserve(_envelope(), "commit-gap")
    await storage.create_agent_task(reservation.task_record)

    closing = asyncio.create_task(runtime.shutdown(timeout=1.0))
    await asyncio.sleep(0)
    handle = await runtime.activate(reservation)
    driver = runtime._tasks[handle.task_id].asyncio_task
    assert driver is not None

    result = await asyncio.wait_for(closing, timeout=2.0)

    assert result.clean is True
    assert driver.done() is True
    assert runtime._driver_tasks_by_session == {}
    assert runtime._reservations_by_session == {}


@pytest.mark.asyncio
async def test_graceful_timeout_sets_system_source_before_cancelling() -> None:
    storage = _Storage()
    started = asyncio.Event()

    async def handler(_run: Any) -> None:
        started.set()
        await asyncio.sleep(60)

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    handle = await runtime.enqueue(_envelope(), "graceful-timeout")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    result = await asyncio.wait_for(
        runtime.shutdown(graceful=True, graceful_timeout=0.01, timeout=1.0),
        timeout=2.0,
    )

    assert result.clean is True
    record = await runtime.status(handle.task_id)
    assert record.status == AgentTaskStatus.CANCELLED
    assert record.terminal_reason == "cancelled"
    assert record.details["cancellation"] == {
        "source": "gateway_shutdown",
        "reason": "graceful_timeout",
    }
