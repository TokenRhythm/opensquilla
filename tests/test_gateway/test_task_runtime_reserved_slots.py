"""TaskRuntime reserves capacity for non-subagent tasks under fan-out load."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
import structlog.testing

from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus


@dataclass
class _StubStorage:
    records: dict

    @classmethod
    def fresh(cls) -> _StubStorage:
        return cls(records={})

    async def create_agent_task(self, record: AgentTaskRecord) -> None:
        self.records[record.task_id] = record

    async def get_agent_task(self, task_id):
        return self.records.get(task_id)

    async def list_agent_tasks(self, **_):
        return list(self.records.values())

    async def update_agent_task(self, task_id, **fields):
        rec = self.records.get(task_id)
        if rec is None:
            return
        for k, v in fields.items():
            setattr(rec, k, v)


def _envelope(session_key: str = "agent:s:main") -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.SYSTEM,
        source_name="test",
        agent_id="s",
        session_key=session_key,
    )


@pytest.mark.asyncio
async def test_subagent_reserved_slots_clamp_when_reserved_exceeds_max() -> None:
    """When config asks reserved >= max, clamp to max-1 (avoiding deadlock)."""
    storage = _StubStorage.fresh()

    async def handler(_):
        return None

    with structlog.testing.capture_logs() as logs:
        rt = TaskRuntime(
            storage=storage,
            turn_handler=handler,
            max_concurrency=1,
            subagent_reserved_slots=2,
        )

    assert rt._subagent_reserved_slots == 0
    assert {
        "event": "task_runtime.subagent_reserved_slots_clamped",
        "requested": 2,
        "max_concurrency": 1,
        "clamped_to": 0,
        "log_level": "warning",
    } in logs


@pytest.mark.asyncio
async def test_subagent_reserved_slots_clamp_to_max_minus_one() -> None:
    """When max=4 and reserved=4, clamp to 3 so subagents can still acquire."""
    storage = _StubStorage.fresh()

    async def handler(_):
        return None

    rt = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_concurrency=4,
        subagent_reserved_slots=4,
    )

    assert rt._subagent_reserved_slots == 3


@pytest.mark.asyncio
async def test_default_run_kind_acquires_normally() -> None:
    """A non-subagent task acquires a slot and increments counters."""
    storage = _StubStorage.fresh()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def handler(run):
        started.set()
        await finish.wait()

    rt = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_concurrency=2,
        subagent_reserved_slots=1,
    )

    handle = await rt.enqueue(_envelope("agent:s:main"), "hi", run_kind="default")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    # While in-flight, counters reflect the running default-kind task.
    assert rt._global_in_flight == 1
    assert rt._subagent_in_flight == 0

    finish.set()
    await rt.wait(handle.task_id, timeout=1.0)
    assert rt._global_in_flight == 0


@pytest.mark.asyncio
async def test_subagent_blocks_when_reserved_floor_would_be_violated() -> None:
    """With max=2 and reserved=1, only one subagent can be in flight at once."""
    storage = _StubStorage.fresh()
    sub_started = []
    finish_first = asyncio.Event()
    finish_second = asyncio.Event()

    async def handler(run):
        sub_started.append(run.task_id)
        if len(sub_started) == 1:
            await finish_first.wait()
        else:
            await finish_second.wait()

    rt = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_concurrency=2,
        subagent_reserved_slots=1,
    )

    h1 = await rt.enqueue(_envelope("agent:s:one"), "a", run_kind="subagent")
    h2 = await rt.enqueue(_envelope("agent:s:two"), "b", run_kind="subagent")

    # Give the loop a few cycles for h1 to start; h2 must remain queued
    # because only 1 subagent slot is available (max=2 - reserved=1).
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(sub_started) == 1
    assert rt._subagent_in_flight == 1

    finish_first.set()
    # After h1 finishes, h2 should be admitted.
    for _ in range(50):
        await asyncio.sleep(0)
        if len(sub_started) == 2:
            break
    assert len(sub_started) == 2

    finish_second.set()
    await rt.wait(h1.task_id, timeout=1.0)
    await rt.wait(h2.task_id, timeout=1.0)
    assert rt._global_in_flight == 0
    assert rt._subagent_in_flight == 0


@pytest.mark.asyncio
async def test_no_reservation_leaks_counters_after_handler_error() -> None:
    """An exception in the turn handler must still drop the in-flight count."""
    storage = _StubStorage.fresh()

    async def handler(_):
        raise RuntimeError("boom")

    rt = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_concurrency=2,
        subagent_reserved_slots=1,
    )

    handle = await rt.enqueue(_envelope(), "x", run_kind="subagent")
    record = await rt.wait(handle.task_id, timeout=1.0)
    assert record.status == AgentTaskStatus.FAILED
    assert rt._global_in_flight == 0
    assert rt._subagent_in_flight == 0


@pytest.mark.asyncio
async def test_send_uses_runtime_send_kind_for_internal_followups() -> None:
    """Every send path stays non-interactive and outside subagent capacity."""
    storage = _StubStorage.fresh()
    seen: list[str] = []

    async def handler(run):
        seen.append(run.run_kind)

    rt = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_concurrency=2,
        subagent_reserved_slots=1,
    )

    uncached = await rt.send("agent:s:uncached", "wake")
    await rt.wait(uncached.task_id, timeout=1.0)

    # Seed another session's envelope cache, then exercise both cached paths.
    h0 = await rt.enqueue(_envelope("agent:s:main"), "init", run_kind="default")
    await rt.wait(h0.task_id, timeout=1.0)

    h1 = await rt.send("agent:s:main", "wake")
    await rt.wait(h1.task_id, timeout=1.0)
    h2 = await rt.send(
        "agent:s:main",
        "wake with provenance",
        provenance={"kind": "internal_system"},
    )
    await rt.wait(h2.task_id, timeout=1.0)

    assert "subagent" not in seen
    assert seen == ["runtime_send", "default", "runtime_send", "runtime_send"]


@pytest.mark.asyncio
async def test_send_passes_stream_event_sink_to_parent_wake_task() -> None:
    storage = _StubStorage.fresh()
    seen_sinks = []

    async def handler(run):
        seen_sinks.append(run.stream_event_sink)

    rt = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_concurrency=2,
        subagent_reserved_slots=1,
    )

    h0 = await rt.enqueue(_envelope("agent:s:main"), "init", run_kind="default")
    await rt.wait(h0.task_id, timeout=1.0)

    async def sink(_event) -> None:
        return None

    h1 = await rt.send("agent:s:main", "wake", stream_event_sink=sink)
    await rt.wait(h1.task_id, timeout=1.0)

    assert seen_sinks == [None, sink]
