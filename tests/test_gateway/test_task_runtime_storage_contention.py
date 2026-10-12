"""Terminal notifications must not wait indefinitely behind the storage gate."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc_sessions import _overlay_runtime_task_snapshot, _task_state_summary
from opensquilla.gateway.task_runtime import TaskResidentBusyError, TaskRuntime
from opensquilla.observability.log_privacy import private_log_event
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus
from opensquilla.session.storage import SessionStorage


@pytest.fixture(autouse=True)
def operational_logs():
    """Use Gateway's exception projection without the default Rich rendering."""
    with capture_logs(processors=[private_log_event]) as events:
        yield events


@pytest.mark.asyncio
@pytest.mark.parametrize("release_on_terminal", [False, True])
async def test_terminal_settlement_survives_shared_storage_gate_contention(
    tmp_path, release_on_terminal: bool,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "terminal-contention.db"))
    storage._busy_budget_seconds = 0.02
    started = asyncio.Event()
    fail_turn = asyncio.Event()
    terminal_events: list[dict[str, Any]] = []
    gate_held = False

    async def handler(_run: Any) -> None:
        started.set()
        await fail_turn.wait()
        raise RuntimeError("synthetic turn failure")

    async def emit(_session: str, event: str, payload: dict[str, Any]) -> None:
        nonlocal gate_held
        if event == "task.failed":
            # The observer can release storage only once terminal feedback is
            # delivered. An unbounded fallback read deadlocks this dependency.
            terminal_events.append(payload)
            if release_on_terminal and gate_held:
                storage._operation_lock.release()
                gate_held = False

    runtime = TaskRuntime(
        storage=storage, turn_handler=handler, event_emitter=emit,
        running_heartbeat_interval_s=None, max_resident_tasks=1,
    )
    driver: asyncio.Task[None] | None = None
    try:
        handle = await runtime.enqueue(
            RouteEnvelope(
                source_kind=SourceKind.WEB,
                source_name="synthetic",
                agent_id="main",
                session_key="agent:main:webchat:terminal-contention",
            ),
            "synthetic test input",
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        before = await storage.get_agent_task(handle.task_id)
        assert before is not None
        driver = runtime._tasks[handle.task_id].asyncio_task
        assert driver is not None
        await storage._operation_lock.acquire()
        gate_held = True
        fail_turn.set()

        # Completion, including the one compensation attempt, remains bounded
        # even when the gate stays held for the entire settlement.
        await asyncio.wait_for(asyncio.shield(driver), timeout=2)
        record = await asyncio.wait_for(runtime.status(handle.task_id), timeout=2)
        assert record.status == AgentTaskStatus.FAILED
        assert record.error_message == "synthetic turn failure"
        assert record.created_at == before.created_at
        assert record.started_at == before.started_at
        assert record.finished_at is not None
        assert record.details is not None
        assert record.details["turn_outcome"]["kind"] != "completed"
        assert len(terminal_events) == 1
        assert terminal_events[0]["task_id"] == handle.task_id
        assert await runtime.active_task_id(record.session_key) is None
        stale_projection = _task_state_summary([before])
        await _overlay_runtime_task_snapshot(
            SimpleNamespace(task_runtime=runtime), record.session_key, stale_projection,
        )
        assert stale_projection["active_task"] is None
        assert stale_projection["last_task"]["status"] == "failed"

        if gate_held:
            storage._operation_lock.release()
            gate_held = False
        durable = await storage.get_agent_task(handle.task_id)
        assert durable is not None
        if release_on_terminal:
            assert durable.status == AgentTaskStatus.FAILED
            assert durable.finished_at == record.finished_at
            assert handle.task_id not in runtime._terminal_fallback_records
            assert runtime._resident_count == 0
        else:
            # Persistent storage failure is reported honestly; do not pretend
            # an in-memory terminal result is a durable write.
            assert durable.status == AgentTaskStatus.RUNNING
            assert handle.task_id in runtime._terminal_fallback_records
            assert runtime._terminal_retry_task is not None
            # The in-memory task is gone, but the unresolved durable write
            # still owns its resident slot until the retry settles.
            assert runtime._resident_count == 1
            with pytest.raises(TaskResidentBusyError):
                await runtime.enqueue(
                    RouteEnvelope(
                        source_kind=SourceKind.WEB,
                        source_name="synthetic",
                        agent_id="main",
                        session_key="agent:main:webchat:terminal-contention-other",
                    ),
                    "must wait for terminal settlement",
                )
            await asyncio.wait_for(asyncio.shield(runtime._terminal_retry_task), timeout=2)
            durable = await storage.get_agent_task(handle.task_id)
            assert durable is not None and durable.status == AgentTaskStatus.FAILED
            assert durable.finished_at == record.finished_at
            assert handle.task_id not in runtime._terminal_fallback_records
            assert runtime._resident_count == 0
        assert len(terminal_events) == 1
    finally:
        fail_turn.set()
        if gate_held:
            storage._operation_lock.release()
            gate_held = False
        try:
            if driver is not None:
                await asyncio.wait_for(asyncio.shield(driver), timeout=2)
        finally:
            await runtime.shutdown(timeout=1)
            await storage.close()
    restarted = await SessionStorage.open(str(tmp_path / "terminal-contention.db"))
    try:
        recovered = await restarted.get_agent_task(handle.task_id)
        assert recovered is not None
        assert recovered.status == AgentTaskStatus.FAILED
        assert recovered.finished_at == record.finished_at
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_task", [False, True])
async def test_shutdown_reports_unresolved_terminal_write_without_leaking_retry(
    tmp_path, delete_task, operational_logs,
):
    storage = await SessionStorage.open(str(tmp_path / "shutdown-contention.db"))
    storage._busy_budget_seconds = 0.01
    started, finish = asyncio.Event(), asyncio.Event()

    async def handler(_run):
        started.set()
        await finish.wait()
        raise RuntimeError("synthetic failure")

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        running_heartbeat_interval_s=None,
        max_resident_tasks=1,
    )
    held = False
    try:
        handle = await runtime.enqueue(RouteEnvelope(
            source_kind=SourceKind.WEB, source_name="synthetic", agent_id="main",
            session_key="agent:main:webchat:shutdown-contention",
        ), "synthetic")
        await asyncio.wait_for(started.wait(), timeout=2)
        driver = runtime._tasks[handle.task_id].asyncio_task
        await storage._operation_lock.acquire()
        held = True
        finish.set()
        await asyncio.wait_for(asyncio.shield(driver), timeout=2)
        assert any(
            event.get("event") == "task_runtime.activation_permit_release_failed"
            and event.get("exception_type") == "StorageBusyError"
            and event.get("log_level") == "warning"
            for event in operational_logs
        )
        assert runtime._resident_count == 1
        if delete_task:
            # Delete while still holding the gate, before the retry can run.
            await storage.conn.execute(
                "DELETE FROM agent_tasks WHERE task_id = ?", (handle.task_id,),
            )
            storage._operation_lock.release()
            held = False
            await asyncio.wait_for(asyncio.shield(runtime._terminal_retry_task), timeout=2)
        result = await asyncio.wait_for(runtime.shutdown(cancel=False, timeout=0.05), timeout=2)
        assert result.clean == delete_task
        assert result.remaining_auxiliary_count == (0 if delete_task else 1)
        assert runtime._resident_count == (0 if delete_task else 1)
        assert runtime._terminal_retry_task is not None and runtime._terminal_retry_task.done()
        if delete_task:
            assert handle.task_id not in runtime._terminal_fallback_records
        else:
            assert (await runtime.status(handle.task_id)).status == AgentTaskStatus.FAILED
    finally:
        finish.set()
        if held:
            storage._operation_lock.release()
        await runtime.shutdown(timeout=1)
        await storage.close()


@pytest.mark.asyncio
async def test_terminal_compensation_does_not_overwrite_later_audit_details(
    tmp_path, monkeypatch,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "terminal-audit-race.db"))
    storage._busy_budget_seconds = 0.02
    started = asyncio.Event()
    fail_turn = asyncio.Event()
    timeline: list[str] = []
    gate_held = False
    driver: asyncio.Task[None] | None = None
    original_get = storage.get_agent_task
    original_update = storage.update_agent_task

    async def handler(_run: Any) -> None:
        started.set()
        await fail_turn.wait()
        raise RuntimeError("synthetic turn failure")

    async def read(_task_id: str) -> AgentTaskRecord | None:
        raise AssertionError("terminal merge must not read details before its transaction")

    async def emit(_session: str, event: str, payload: dict[str, Any]) -> None:
        nonlocal gate_held
        if event == "task.failed":
            timeline.append("terminal_event")
            storage._operation_lock.release()
            gate_held = False
            before_compensation = await original_get(payload["task_id"])
            assert before_compensation is not None
            assert before_compensation.status == AgentTaskStatus.RUNNING
            assert before_compensation.details is not None
            await original_update(
                payload["task_id"],
                details={
                    **before_compensation.details,
                    "synthetic_audit": {"revision": 3},
                    "metadata": {
                        **before_compensation.details.get("metadata", {}),
                        "synthetic_audit": {"revision": 3},
                        "plan_result": {"status": "submitted"},
                    },
                },
            )

    runtime = TaskRuntime(
        storage=storage, turn_handler=handler, event_emitter=emit,
        running_heartbeat_interval_s=None,
    )
    try:
        handle = await runtime.enqueue(
            RouteEnvelope(
                source_kind=SourceKind.WEB,
                source_name="synthetic",
                agent_id="main",
                session_key="agent:main:webchat:terminal-audit-race",
            ),
            "synthetic test input",
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        driver = runtime._tasks[handle.task_id].asyncio_task
        assert driver is not None
        record = await original_get(handle.task_id)
        assert record is not None
        await original_update(
            handle.task_id,
            details={**(record.details or {}), "synthetic_audit": {"revision": 2}},
        )
        monkeypatch.setattr(storage, "get_agent_task", read)
        await storage._operation_lock.acquire()
        gate_held = True
        fail_turn.set()

        await asyncio.wait_for(asyncio.shield(driver), timeout=2)
        durable = await original_get(handle.task_id)
        assert durable is not None
        assert durable.status == AgentTaskStatus.FAILED
        assert durable.details is not None
        assert durable.details["synthetic_audit"] == {"revision": 3}
        assert durable.details["metadata"]["synthetic_audit"] == {"revision": 3}
        assert "plan_result" not in durable.details["metadata"]
        assert durable.details["turn_outcome"]["kind"] != "completed"
        assert timeline == ["terminal_event"]
        assert handle.task_id not in runtime._terminal_fallback_records
    finally:
        fail_turn.set()
        if gate_held:
            storage._operation_lock.release()
            gate_held = False
        try:
            if driver is not None:
                await asyncio.wait_for(asyncio.shield(driver), timeout=2)
        finally:
            await runtime.shutdown(timeout=1)
            await storage.close()
