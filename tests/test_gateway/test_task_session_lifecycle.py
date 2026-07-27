from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.gateway.boot import _make_task_session_lifecycle_listener
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.session_events import build_sessions_changed_payload
from opensquilla.gateway.session_lifecycle import (
    TaskLifecycleEvent,
    apply_task_lifecycle_to_session,
    session_status_for_task_status,
)
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionNode,
    SessionStatus,
)


def _make_envelope(session_key: str = "agent-1::sess-1") -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="agent-1",
        session_key=session_key,
        input_provenance={"kind": "test"},
        metadata={},
    )


def _make_task_storage() -> Any:
    storage = MagicMock()
    task_db: dict[str, AgentTaskRecord] = {}

    async def create(record: AgentTaskRecord) -> None:
        task_db[record.task_id] = record

    async def update(task_id: str, **kwargs: Any) -> None:
        rec = task_db.get(task_id)
        if rec is None:
            return
        for key, value in kwargs.items():
            if hasattr(rec, key):
                object.__setattr__(rec, key, value)

    async def get(task_id: str) -> AgentTaskRecord | None:
        return task_db.get(task_id)

    storage.create_agent_task = create
    storage.update_agent_task = update
    storage.get_agent_task = get
    return storage


class _SessionManager:
    def __init__(self, node: SessionNode) -> None:
        self.node = node
        self.finish_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    async def get_session(self, session_key: str) -> SessionNode | None:
        if session_key == self.node.session_key:
            return self.node
        return None

    async def update(self, session_key: str, **fields: Any) -> SessionNode:
        if session_key != self.node.session_key:
            raise KeyError(session_key)
        self.update_calls.append((session_key, dict(fields)))
        for key, value in fields.items():
            if hasattr(self.node, key):
                setattr(self.node, key, value)
        return self.node

    async def finish(self, session_key: str, status: str = SessionStatus.DONE) -> SessionNode:
        if session_key != self.node.session_key:
            raise KeyError(session_key)
        self.finish_calls.append((session_key, status))
        self.node.status = status
        self.node.ended_at = 2000
        self.node.runtime_ms = 1000
        return self.node


def _make_session(
    session_key: str = "agent-1::sess-1",
    *,
    status: str = SessionStatus.RUNNING,
) -> SessionNode:
    return SessionNode(
        session_key=session_key,
        session_id="session-id",
        agent_id="agent-1",
        created_at=1000,
        updated_at=1000,
        started_at=1000,
        status=status,
    )


def test_sessions_changed_payload_has_shared_schema_fields() -> None:
    assert build_sessions_changed_payload("agent:main:test", "turn_complete") == {
        "schema_version": 1,
        "key": "agent:main:test",
        "reason": "turn_complete",
        "run_status": "idle",
    }


def _make_runtime(
    turn_handler: Callable[..., Awaitable[Any]],
    *,
    session_manager: _SessionManager,
    events: list[tuple[str, str, dict[str, Any]]],
) -> TaskRuntime:
    async def _emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, event_name, payload))

    return TaskRuntime(
        storage=_make_task_storage(),
        turn_handler=turn_handler,
        event_emitter=_emit,
        lifecycle_listener=_make_task_session_lifecycle_listener(
            session_manager=session_manager,
            event_emitter=_emit,
        ),
    )


@pytest.mark.asyncio
async def test_task_timeout_terminalizes_running_session_and_broadcasts_change() -> None:
    session = _make_session()
    manager = _SessionManager(session)
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _timeout_handler(_run: Any) -> None:
        raise TimeoutError("Gateway task timeout: Stream idle for more than 180.0s")

    runtime = _make_runtime(_timeout_handler, session_manager=manager, events=events)
    handle = await runtime.enqueue(_make_envelope(), "hello")

    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert record.status == "timeout"
    assert session.status == SessionStatus.TIMEOUT
    assert [event_name for _, event_name, _ in events] == [
        "task.queued",
        "sessions.changed",
        "task.running",
        "sessions.changed",
        "task.timeout",
        "sessions.changed",
    ]
    assert events[0] == (
        session.session_key,
        "task.queued",
        {
            "task_id": handle.task_id,
            "turn_id": handle.task_id,
            "session_key": session.session_key,
            "queue_depth": 1,
            "queue_position": 1,
        },
    )
    assert events[1] == (
        session.session_key,
        "sessions.changed",
        {
            "schema_version": 1,
            "key": session.session_key,
            "reason": "task_queued",
            "run_status": "queued",
            "active_task": {"task_id": handle.task_id, "status": "queued"},
        },
    )
    assert events[2] == (
        session.session_key,
        "task.running",
        {
            "task_id": handle.task_id,
            "turn_id": handle.task_id,
            "session_key": session.session_key,
        },
    )
    assert events[3] == (
        session.session_key,
        "sessions.changed",
        {
            "schema_version": 1,
            "key": session.session_key,
            "reason": "task_running",
            "run_status": "running",
            "active_task": {"task_id": handle.task_id, "status": "running"},
        },
    )
    assert events[4] == (
        session.session_key,
        "task.timeout",
        {
            "task_id": handle.task_id,
            "turn_id": handle.task_id,
            "session_key": session.session_key,
            "terminal_reason": "timeout",
            "terminal_message": "The task timed out before it could finish.",
        },
    )
    assert events[-1] == (
        session.session_key,
        "sessions.changed",
        {
            "schema_version": 1,
            "key": session.session_key,
            "reason": "task_terminal",
            "status": "timeout",
            "run_status": "timeout",
            "last_task": {
                "task_id": handle.task_id,
                "status": "timeout",
                "terminal_reason": "timeout",
                "terminal_message": "The task timed out before it could finish.",
            },
        },
    )
    assert manager.finish_calls == []
    assert manager.update_calls[0] == (session.session_key, {})
    assert manager.update_calls[1] == (session.session_key, {})
    assert manager.update_calls[2][1]["status"] == SessionStatus.TIMEOUT
    assert manager.update_calls[2][1]["ended_at"] > 0
    assert manager.update_calls[2][1]["runtime_ms"] >= 0


def test_task_terminal_status_mapping_matches_session_lifecycle() -> None:
    assert session_status_for_task_status(AgentTaskStatus.SUCCEEDED) == SessionStatus.DONE
    assert session_status_for_task_status(AgentTaskStatus.FAILED) == SessionStatus.FAILED
    assert session_status_for_task_status(AgentTaskStatus.CANCELLED) == SessionStatus.KILLED
    assert session_status_for_task_status(AgentTaskStatus.TIMEOUT) == SessionStatus.TIMEOUT
    assert session_status_for_task_status(AgentTaskStatus.ABANDONED) == SessionStatus.FAILED
    assert session_status_for_task_status(AgentTaskStatus.RUNNING) is None


@pytest.mark.asyncio
async def test_terminal_lifecycle_is_idempotent_for_already_terminal_session() -> None:
    session = _make_session(status=SessionStatus.TIMEOUT)
    manager = _SessionManager(session)

    changed = await apply_task_lifecycle_to_session(
        TaskLifecycleEvent(
            phase="terminal",
            session_key=session.session_key,
            task_id="task-1",
            task_status=AgentTaskStatus.TIMEOUT,
            run_kind="default",
            terminal_reason="timeout",
        ),
        session_manager=manager,
    )

    assert changed is False
    assert manager.finish_calls == []
    assert session.status == SessionStatus.TIMEOUT


@pytest.mark.asyncio
async def test_boot_lifecycle_listener_skips_subagent_tasks() -> None:
    session = _make_session()
    manager = _SessionManager(session)
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, event_name, payload))

    listener = _make_task_session_lifecycle_listener(
        session_manager=manager,
        event_emitter=_emit,
    )

    await listener(
        TaskLifecycleEvent(
            phase="terminal",
            session_key=session.session_key,
            task_id="task-1",
            task_status=AgentTaskStatus.TIMEOUT,
            run_kind="subagent",
            terminal_reason="timeout",
        )
    )

    assert manager.finish_calls == []
    assert events == []
    assert session.status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_task_running_broadcasts_change_for_already_running_session() -> None:
    session = _make_session(status=SessionStatus.RUNNING)
    manager = _SessionManager(session)
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, event_name, payload))

    listener = _make_task_session_lifecycle_listener(
        session_manager=manager,
        event_emitter=_emit,
    )

    await listener(
        TaskLifecycleEvent(
            phase="running",
            session_key=session.session_key,
            task_id="task-active",
            task_status=AgentTaskStatus.RUNNING,
            run_kind="default",
        )
    )

    assert manager.update_calls == [(session.session_key, {})]
    assert events == [
        (
            session.session_key,
            "sessions.changed",
            {
                "schema_version": 1,
                "key": session.session_key,
                "reason": "task_running",
                "run_status": "running",
                "active_task": {"task_id": "task-active", "status": "running"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_task_queued_broadcasts_change_for_waiting_session() -> None:
    session = _make_session(status=SessionStatus.RUNNING)
    manager = _SessionManager(session)
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, event_name, payload))

    listener = _make_task_session_lifecycle_listener(
        session_manager=manager,
        event_emitter=_emit,
    )

    await listener(
        TaskLifecycleEvent(
            phase="queued",
            session_key=session.session_key,
            task_id="task-waiting",
            task_status=AgentTaskStatus.QUEUED,
            run_kind="default",
        )
    )

    assert manager.update_calls == [(session.session_key, {})]
    assert events == [
        (
            session.session_key,
            "sessions.changed",
            {
                "schema_version": 1,
                "key": session.session_key,
                "reason": "task_queued",
                "run_status": "queued",
                "active_task": {"task_id": "task-waiting", "status": "queued"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_task_running_reactivates_terminal_session_before_next_turn() -> None:
    session = _make_session(status=SessionStatus.TIMEOUT)
    session.ended_at = 2000
    session.runtime_ms = 1000
    manager = _SessionManager(session)
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _success_handler(_run: Any) -> None:
        return None

    runtime = _make_runtime(_success_handler, session_manager=manager, events=events)
    handle = await runtime.enqueue(_make_envelope(), "hello again")

    await runtime.wait(handle.task_id, timeout=2.0)

    assert session.status == SessionStatus.DONE
    assert [event_name for _, event_name, _ in events] == [
        "task.queued",
        "sessions.changed",
        "task.running",
        "sessions.changed",
        "task.succeeded",
        "sessions.changed",
    ]
    assert events[0] == (
        session.session_key,
        "task.queued",
        {
            "task_id": handle.task_id,
            "turn_id": handle.task_id,
            "session_key": session.session_key,
            "queue_depth": 1,
            "queue_position": 1,
        },
    )
    assert events[1] == (
        session.session_key,
        "sessions.changed",
        {
            "schema_version": 1,
            "key": session.session_key,
            "reason": "task_queued",
            "run_status": "queued",
            "active_task": {"task_id": handle.task_id, "status": "queued"},
        },
    )
    assert events[2] == (
        session.session_key,
        "task.running",
        {
            "task_id": handle.task_id,
            "turn_id": handle.task_id,
            "session_key": session.session_key,
        },
    )
    assert events[3] == (
        session.session_key,
        "sessions.changed",
        {
            "schema_version": 1,
            "key": session.session_key,
            "reason": "task_running",
            "run_status": "running",
            "active_task": {"task_id": handle.task_id, "status": "running"},
        },
    )
    assert events[-1] == (
        session.session_key,
        "sessions.changed",
        {
            "schema_version": 1,
            "key": session.session_key,
            "reason": "task_terminal",
            "status": "done",
            "run_status": "idle",
            "last_task": {
                "task_id": handle.task_id,
                "status": "succeeded",
                "terminal_reason": "completed",
            },
        },
    )
    assert events[4] == (
        session.session_key,
        "task.succeeded",
        {
            "task_id": handle.task_id,
            "turn_id": handle.task_id,
            "session_key": session.session_key,
            "terminal_reason": "completed",
        },
    )
    assert manager.finish_calls == []
    assert manager.update_calls[0] == (session.session_key, {})
    assert manager.update_calls[1][1]["status"] == SessionStatus.RUNNING
    assert manager.update_calls[1][1]["started_at"] > 0
    assert manager.update_calls[-1][1]["status"] == SessionStatus.DONE


@pytest.mark.asyncio
async def test_task_runtime_persists_agent_task_timestamps_as_epoch_ms() -> None:
    session = _make_session()
    manager = _SessionManager(session)
    events: list[tuple[str, str, dict[str, Any]]] = []
    storage = _make_task_storage()

    async def _success_handler(_run: Any) -> None:
        return None

    async def _emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, event_name, payload))

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_success_handler,
        event_emitter=_emit,
        lifecycle_listener=_make_task_session_lifecycle_listener(
            session_manager=manager,
            event_emitter=_emit,
        ),
    )

    before_ms = int(time.time() * 1000) - 1000
    handle = await runtime.enqueue(_make_envelope(), "hello")
    await runtime.wait(handle.task_id, timeout=2.0)
    after_ms = int(time.time() * 1000) + 1000

    record = await storage.get_agent_task(handle.task_id)

    assert record is not None
    assert record.started_at is not None
    assert record.finished_at is not None
    assert before_ms <= record.started_at <= after_ms
    assert before_ms <= record.finished_at <= after_ms
    assert record.finished_at >= record.started_at
