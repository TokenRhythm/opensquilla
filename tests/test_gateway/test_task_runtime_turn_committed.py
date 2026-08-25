from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.contracts.gateway_transport import TURN_COMMITTED_EVENT
from opensquilla.engine.types import DoneEvent, ErrorEvent
from opensquilla.gateway import task_runtime as task_runtime_module
from opensquilla.gateway.boot import TaskRuntimeStreamError, dispatch_task_runtime_turn
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus


def _envelope() -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="main",
        session_key="agent:main:turn-committed",
        session_id="session-turn-committed",
        input_provenance={"kind": "test"},
        metadata={
            "client_message_id": "client-message-1",
            "surface_id": "webui:chat",
        },
    )


class _Storage:
    def __init__(
        self,
        *,
        terminal_failures: int = 0,
        terminal_entered: asyncio.Event | None = None,
        release_terminal: asyncio.Event | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self.records: dict[str, AgentTaskRecord] = {}
        self.terminal_failures = terminal_failures
        self.terminal_attempts = 0
        self.terminal_entered = terminal_entered
        self.release_terminal = release_terminal
        self.timeline = timeline if timeline is not None else []

    async def create_agent_task(self, record: AgentTaskRecord) -> None:
        self.records[record.task_id] = record

    async def get_agent_task(self, task_id: str) -> AgentTaskRecord | None:
        return self.records.get(task_id)

    async def update_agent_task(self, task_id: str, **updates: Any) -> None:
        is_terminal = updates.get("finished_at") is not None
        if is_terminal:
            self.terminal_attempts += 1
            attempt = self.terminal_attempts
            self.timeline.append(f"terminal.attempt.{attempt}")
            if self.terminal_entered is not None:
                self.terminal_entered.set()
            if self.release_terminal is not None:
                await self.release_terminal.wait()
            if attempt <= self.terminal_failures:
                self.timeline.append(f"terminal.failed.{attempt}")
                raise sqlite3.OperationalError("database is locked")
            self.timeline.append(f"terminal.persisted.{attempt}")
        record = self.records.get(task_id)
        if record is None:
            return
        for field_name, value in updates.items():
            if hasattr(record, field_name):
                object.__setattr__(record, field_name, value)


async def _receipt_handler(run: Any) -> None:
    assert run.finalizer_receipt_sink is not None
    run.finalizer_receipt_sink()


@pytest.mark.asyncio
async def test_terminal_settlement_honors_installed_task_factory() -> None:
    loop = asyncio.get_running_loop()
    original_factory = loop.get_task_factory()
    factory_calls = 0

    def _factory(
        factory_loop: asyncio.AbstractEventLoop,
        coroutine: Any,
        context: Any = None,
    ) -> asyncio.Task[Any]:
        nonlocal factory_calls
        factory_calls += 1
        return asyncio.Task(coroutine, loop=factory_loop, context=context)

    async def _settle() -> str:
        return "settled"

    loop.set_task_factory(_factory)
    try:
        result = await task_runtime_module._complete_terminal_settlement(_settle())
    finally:
        loop.set_task_factory(original_factory)

    assert result == "settled"
    assert factory_calls == 1


def _dispatch_run(receipt_sink: Any) -> SimpleNamespace:
    envelope = _envelope()
    return SimpleNamespace(
        agent_id="main",
        task_id="task-dispatch-receipt",
        session_key=envelope.session_key,
        message="hello",
        envelope=envelope,
        attachments=[],
        input_provenance={"kind": "test"},
        run_kind="interactive",
        no_memory_capture=False,
        fresh_user_session=False,
        ingress_pipeline_steps=(),
        semantic_message=None,
        stream_event_sink=None,
        finalizer_receipt_sink=receipt_sink,
    )


def _gateway_config(tmp_path: Path) -> GatewayConfig:
    return GatewayConfig(
        workspace_dir=str(tmp_path),
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )


@pytest.mark.asyncio
async def test_dispatch_sets_receipt_only_after_normal_stream_exhaustion(
    tmp_path: Path,
) -> None:
    after_done = asyncio.Event()
    release_stream = asyncio.Event()
    receipts: list[str] = []
    emitted: list[str] = []

    class _TurnRunner:
        async def run(self, _message: str, _session_key: str, **_kwargs: Any):
            yield DoneEvent()
            after_done.set()
            await release_stream.wait()

    async def _emit(
        _session_key: str,
        event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        emitted.append(event_name)

    dispatch = asyncio.create_task(
        dispatch_task_runtime_turn(
            _dispatch_run(lambda: receipts.append("received")),
            config=_gateway_config(tmp_path),
            session_manager=None,
            turn_runner=_TurnRunner(),
            event_emitter=_emit,
        )
    )

    await asyncio.wait_for(after_done.wait(), timeout=2.0)
    assert "session.event.done" in emitted
    assert receipts == []

    release_stream.set()
    await asyncio.wait_for(dispatch, timeout=2.0)
    assert receipts == ["received"]


@pytest.mark.asyncio
async def test_dispatch_stream_error_does_not_set_receipt(tmp_path: Path) -> None:
    receipts: list[str] = []

    class _TurnRunner:
        async def run(self, _message: str, _session_key: str, **_kwargs: Any):
            yield ErrorEvent(message="provider failed", code="provider_error")

    async def _emit(
        _session_key: str,
        _event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        return None

    with pytest.raises(TaskRuntimeStreamError):
        await dispatch_task_runtime_turn(
            _dispatch_run(lambda: receipts.append("received")),
            config=_gateway_config(tmp_path),
            session_manager=None,
            turn_runner=_TurnRunner(),
            event_emitter=_emit,
        )

    assert receipts == []


@pytest.mark.asyncio
async def test_finalizer_failure_after_done_marks_failed_without_commit(
    tmp_path: Path,
) -> None:
    emitted: list[str] = []

    class _TurnRunner:
        async def run(self, _message: str, _session_key: str, **_kwargs: Any):
            yield DoneEvent()
            raise OSError("assistant transcript append failed")

    async def _emit(
        _session_key: str,
        event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        emitted.append(event_name)

    async def _handler(run: Any) -> None:
        await dispatch_task_runtime_turn(
            run,
            config=_gateway_config(tmp_path),
            session_manager=None,
            turn_runner=_TurnRunner(),
            event_emitter=_emit,
        )

    runtime = TaskRuntime(
        storage=_Storage(),
        turn_handler=_handler,
        event_emitter=_emit,
    )
    handle = await runtime.enqueue(_envelope(), "hello")
    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert record.status is AgentTaskStatus.FAILED
    assert record.error_class == "OSError"
    assert "session.event.done" in emitted
    assert "task.failed" in emitted
    assert TURN_COMMITTED_EVENT not in emitted


@pytest.mark.asyncio
async def test_success_emits_one_commit_with_durable_identity_payload() -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    storage = _Storage()

    async def _emit(
        _session_key: str,
        event_name: str,
        payload: dict[str, Any],
    ) -> None:
        emitted.append((event_name, payload))

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_receipt_handler,
        event_emitter=_emit,
    )
    handle = await runtime.enqueue(
        _envelope(),
        "hello",
        persisted_user_message_id="user-message-1",
    )
    record = await runtime.wait(handle.task_id, timeout=2.0)

    committed = [payload for name, payload in emitted if name == TURN_COMMITTED_EVENT]
    assert record.status is AgentTaskStatus.SUCCEEDED
    assert committed == [
        {
            "schema_version": 1,
            "session_key": _envelope().session_key,
            "session_id": "session-turn-committed",
            "task_id": handle.task_id,
            "turn_id": handle.task_id,
            "status": "succeeded",
            "terminal_reason": "completed",
            "finished_at": record.finished_at,
            "client_message_id": "client-message-1",
            "user_message_id": "user-message-1",
            "surface_id": "webui:chat",
        }
    ]
    event_names = [name for name, _payload in emitted]
    assert event_names.index("task.succeeded") < event_names.index(TURN_COMMITTED_EVENT)


@pytest.mark.asyncio
async def test_success_without_receipt_never_emits_commit() -> None:
    emitted: list[str] = []

    async def _handler(_run: Any) -> None:
        return None

    async def _emit(
        _session_key: str,
        event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        emitted.append(event_name)

    runtime = TaskRuntime(
        storage=_Storage(),
        turn_handler=_handler,
        event_emitter=_emit,
    )
    handle = await runtime.enqueue(_envelope(), "hello")
    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert record.status is AgentTaskStatus.SUCCEEDED
    assert emitted.count("task.succeeded") == 1
    assert TURN_COMMITTED_EVENT not in emitted


@pytest.mark.asyncio
async def test_commit_emitter_failure_does_not_skip_lifecycle_or_cleanup() -> None:
    emitted: list[str] = []
    lifecycle_phases: list[str] = []

    async def _emit(
        _session_key: str,
        event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        emitted.append(event_name)
        if event_name == TURN_COMMITTED_EVENT:
            raise RuntimeError("optional receipt delivery failed")

    async def _lifecycle(event: Any) -> None:
        lifecycle_phases.append(event.phase)

    runtime = TaskRuntime(
        storage=_Storage(),
        turn_handler=_receipt_handler,
        event_emitter=_emit,
        lifecycle_listener=_lifecycle,
    )
    handle = await runtime.enqueue(_envelope(), "hello")
    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert record.status is AgentTaskStatus.SUCCEEDED
    assert emitted.count("task.succeeded") == 1
    assert emitted.count(TURN_COMMITTED_EVENT) == 1
    assert lifecycle_phases[-1] == "terminal"
    assert handle.task_id not in runtime._tasks
    assert _envelope().session_key not in runtime._running_by_session


@pytest.mark.asyncio
async def test_commit_waits_for_successful_compensation() -> None:
    timeline: list[str] = []
    storage = _Storage(terminal_failures=1, timeline=timeline)

    async def _emit(
        _session_key: str,
        event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        timeline.append(event_name)

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_receipt_handler,
        event_emitter=_emit,
    )
    handle = await runtime.enqueue(_envelope(), "hello")
    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert record.status is AgentTaskStatus.SUCCEEDED
    assert storage.terminal_attempts == 2
    assert timeline.index("task.succeeded") < timeline.index("terminal.persisted.2")
    assert timeline.index("terminal.persisted.2") < timeline.index(TURN_COMMITTED_EVENT)
    assert timeline.count(TURN_COMMITTED_EVENT) == 1


@pytest.mark.asyncio
async def test_double_terminal_write_failure_keeps_success_without_commit() -> None:
    emitted: list[str] = []
    storage = _Storage(terminal_failures=2)

    async def _emit(
        _session_key: str,
        event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        emitted.append(event_name)

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_receipt_handler,
        event_emitter=_emit,
    )
    handle = await runtime.enqueue(_envelope(), "hello")
    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert record.status is AgentTaskStatus.SUCCEEDED
    assert storage.terminal_attempts == 2
    assert emitted.count("task.succeeded") == 1
    assert TURN_COMMITTED_EVENT not in emitted


@pytest.mark.asyncio
async def test_repeated_cancel_during_terminal_write_settles_once() -> None:
    terminal_entered = asyncio.Event()
    release_terminal = asyncio.Event()
    emitted: list[str] = []
    storage = _Storage(
        terminal_entered=terminal_entered,
        release_terminal=release_terminal,
    )

    async def _emit(
        _session_key: str,
        event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        emitted.append(event_name)

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_receipt_handler,
        event_emitter=_emit,
    )
    handle = await runtime.enqueue(_envelope(), "hello")
    await asyncio.wait_for(terminal_entered.wait(), timeout=2.0)
    runtime_task = runtime._tasks[handle.task_id]
    driver = runtime_task.asyncio_task
    assert driver is not None

    driver.cancel()
    await asyncio.sleep(0)
    driver.cancel()
    await asyncio.sleep(0)
    assert not runtime_task.done.is_set()

    release_terminal.set()
    record = await runtime.wait(handle.task_id, timeout=2.0)
    assert record.status is AgentTaskStatus.SUCCEEDED
    assert emitted.count("task.succeeded") == 1
    assert emitted.count(TURN_COMMITTED_EVENT) == 1
    assert handle.task_id not in runtime._tasks
    assert _envelope().session_key not in runtime._running_by_session

    await runtime._mark_terminal(
        runtime_task,
        AgentTaskStatus.SUCCEEDED,
        terminal_reason="completed",
    )
    assert emitted.count("task.succeeded") == 1
    assert emitted.count(TURN_COMMITTED_EVENT) == 1


@pytest.mark.asyncio
async def test_duplicate_terminal_caller_cannot_signal_done_for_owner() -> None:
    terminal_entered = asyncio.Event()
    release_terminal = asyncio.Event()
    storage = _Storage(
        terminal_entered=terminal_entered,
        release_terminal=release_terminal,
    )
    runtime = TaskRuntime(storage=storage, turn_handler=_receipt_handler)
    handle = await runtime.enqueue(_envelope(), "hello")
    await asyncio.wait_for(terminal_entered.wait(), timeout=2.0)
    runtime_task = runtime._tasks[handle.task_id]
    driver = runtime_task.asyncio_task
    assert driver is not None

    class _RecordingEvent:
        def __init__(self) -> None:
            self.event = asyncio.Event()
            self.setters: list[asyncio.Task[Any] | None] = []

        def is_set(self) -> bool:
            return self.event.is_set()

        def set(self) -> None:
            self.setters.append(asyncio.current_task())
            self.event.set()

        async def wait(self) -> None:
            await self.event.wait()

    done = _RecordingEvent()
    runtime_task.done = done  # type: ignore[assignment]
    duplicate = asyncio.create_task(
        runtime._mark_terminal(
            runtime_task,
            AgentTaskStatus.SUCCEEDED,
            terminal_reason="completed",
        )
    )
    await asyncio.sleep(0)

    release_terminal.set()
    await runtime.wait(handle.task_id, timeout=2.0)
    await asyncio.wait_for(duplicate, timeout=2.0)
    await asyncio.wait_for(driver, timeout=2.0)

    assert done.setters == [driver]


@pytest.mark.asyncio
async def test_shutdown_reports_residual_while_terminal_settlement_is_blocked() -> None:
    terminal_entered = asyncio.Event()
    release_terminal = asyncio.Event()
    emitted: list[str] = []
    storage = _Storage(
        terminal_entered=terminal_entered,
        release_terminal=release_terminal,
    )

    async def _emit(
        _session_key: str,
        event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        emitted.append(event_name)

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_receipt_handler,
        event_emitter=_emit,
    )
    handle = await runtime.enqueue(_envelope(), "hello")
    await asyncio.wait_for(terminal_entered.wait(), timeout=2.0)
    runtime_task = runtime._tasks[handle.task_id]
    driver = runtime_task.asyncio_task
    assert driver is not None

    try:
        result = await asyncio.wait_for(
            runtime.shutdown(
                graceful=True,
                graceful_timeout=0.01,
                timeout=0.01,
            ),
            timeout=1.0,
        )

        assert result.clean is False
        assert result.remaining_driver_count == 1
        assert driver.done() is False
        assert driver in runtime._driver_tasks_by_session[_envelope().session_key]
        assert TURN_COMMITTED_EVENT not in emitted
    finally:
        release_terminal.set()
        await asyncio.wait_for(driver, timeout=2.0)

    settled = await runtime.status(handle.task_id)
    assert settled.status is AgentTaskStatus.SUCCEEDED
    assert emitted.count("task.succeeded") == 1
    assert emitted.count(TURN_COMMITTED_EVENT) == 1
    assert runtime._driver_tasks_by_session == {}
