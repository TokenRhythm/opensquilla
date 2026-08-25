from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.gateway.boot import _make_task_session_lifecycle_listener
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc_sessions import _active_task_summary
from opensquilla.gateway.session_events import build_sessions_changed_payload
from opensquilla.gateway.session_lifecycle import (
    SessionTaskSnapshot,
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


def test_live_snapshot_matches_running_first_hydration_projection() -> None:
    session_key = "agent-1::sess-1"
    snapshot = SessionTaskSnapshot(
        running_task_id="task-running",
        queued_task_ids=("task-newer-queued",),
    )
    hydrated = _active_task_summary(
        [
            AgentTaskRecord(
                task_id="task-running",
                session_key=session_key,
                status=AgentTaskStatus.RUNNING,
                created_at=1000,
            ),
            AgentTaskRecord(
                task_id="task-newer-queued",
                session_key=session_key,
                status=AgentTaskStatus.QUEUED,
                created_at=2000,
            ),
        ]
    )

    assert hydrated is not None
    assert {
        "task_id": hydrated["task_id"],
        "status": hydrated["status"],
    } == snapshot.active_task


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
                "steer_capability": {
                    "mode": "same_turn",
                    "expected_turn_id": handle.task_id,
                    "input_kinds": ["text"],
                    "reason": None,
                },
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
async def test_terminal_projection_waits_for_authoritative_task_persistence() -> None:
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
            phase="terminal",
            session_key=session.session_key,
            task_id="task-terminal-write-failed",
            task_status=AgentTaskStatus.SUCCEEDED,
            run_kind="goal",
            terminal_reason="completed",
            terminal_persisted=False,
        )
    )

    assert manager.finish_calls == []
    assert manager.update_calls == []
    assert events == []
    assert session.status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_terminal_handoff_keeps_session_queued_without_idle_projection() -> None:
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
            phase="terminal",
            session_key=session.session_key,
            task_id="task-old",
            task_status=AgentTaskStatus.SUCCEEDED,
            run_kind="default",
            terminal_reason="completed",
            continuation_task_id="task-next",
            task_snapshot=SessionTaskSnapshot(
                running_task_id=None,
                queued_task_ids=("task-next",),
            ),
        )
    )

    assert session.status == SessionStatus.RUNNING
    assert session.ended_at is None
    assert session.runtime_ms is None
    assert manager.update_calls == [
        (
            session.session_key,
            {
                "status": SessionStatus.RUNNING,
                "ended_at": None,
                "runtime_ms": None,
            },
        )
    ]
    assert events == [
        (
            session.session_key,
            "sessions.changed",
            {
                "schema_version": 1,
                "key": session.session_key,
                "reason": "task_terminal",
                "status": "running",
                "run_status": "queued",
                "last_task": {
                    "task_id": "task-old",
                    "status": "succeeded",
                    "terminal_reason": "completed",
                },
                "active_task": {"task_id": "task-next", "status": "queued"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_durable_continuation_projects_queued_when_not_runtime_active() -> None:
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
            phase="terminal",
            session_key=session.session_key,
            task_id="task-old",
            task_status=AgentTaskStatus.ABANDONED,
            run_kind="default",
            terminal_reason="shutdown_timeout",
            continuation_task_id="task-durable-next",
            task_snapshot=SessionTaskSnapshot(
                running_task_id=None,
                queued_task_ids=(),
            ),
        )
    )

    assert session.status == SessionStatus.RUNNING
    assert events[0][2]["status"] == "running"
    assert events[0][2]["run_status"] == "queued"
    assert events[0][2]["active_task"] == {
        "task_id": "task-durable-next",
        "status": "queued",
    }
    assert events[0][2]["run_status"] == "queued"


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
            task_snapshot=SessionTaskSnapshot(
                running_task_id="task-active",
                queued_task_ids=(),
            ),
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
            task_snapshot=SessionTaskSnapshot(
                running_task_id=None,
                queued_task_ids=("task-waiting",),
            ),
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
async def test_running_task_wins_when_followup_queues_and_terminal_hands_off() -> None:
    session = _make_session(status=SessionStatus.RUNNING)
    manager = _SessionManager(session)
    events: list[tuple[str, str, dict[str, Any]]] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    run_count = 0

    async def _controlled_handler(_run: Any) -> None:
        nonlocal run_count
        run_count += 1
        if run_count == 1:
            first_started.set()
            await release_first.wait()
            return
        second_started.set()
        await release_second.wait()

    runtime = _make_runtime(
        _controlled_handler,
        session_manager=manager,
        events=events,
    )
    first = await runtime.enqueue(_make_envelope(), "first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)

    # Cancellation intent must not let a queued follower take foreground
    # ownership before the running task reaches its terminal boundary.
    async with runtime._state_lock:
        runtime._tasks[first.task_id].cancel_requested = True
    snapshot = await runtime.session_task_snapshot(session.session_key)
    assert snapshot.running_task_id == first.task_id
    assert snapshot.cancel_requested_task_ids == (first.task_id,)
    async with runtime._state_lock:
        runtime._tasks[first.task_id].cancel_requested = False

    second = await runtime.enqueue(_make_envelope(), "second")
    snapshot = await runtime.session_task_snapshot(session.session_key)
    assert snapshot == SessionTaskSnapshot(
        running_task_id=first.task_id,
        queued_task_ids=(second.task_id,),
    )

    second_queued_change = next(
        payload
        for _, event_name, payload in events
        if event_name == "sessions.changed"
        and payload.get("reason") == "task_queued"
        and payload.get("changed_task", {}).get("task_id") == second.task_id
    )
    assert second_queued_change["run_status"] == "running"
    assert second_queued_change["active_task"] == {
        "task_id": first.task_id,
        "status": "running",
    }
    assert second_queued_change["changed_task"] == {
        "task_id": second.task_id,
        "status": "queued",
    }

    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=2.0)

    first_terminal_change = next(
        payload
        for _, event_name, payload in events
        if event_name == "sessions.changed"
        and payload.get("reason") == "task_terminal"
        and payload.get("last_task", {}).get("task_id") == first.task_id
    )
    assert first_terminal_change["status"] == "running"
    assert first_terminal_change["run_status"] == "queued"
    assert first_terminal_change["active_task"] == {
        "task_id": second.task_id,
        "status": "queued",
    }

    # A delayed terminal callback is projected against current state. Once B
    # is running it must not regress to queued merely because A's callback is
    # delivered again.
    await runtime._notify_task_lifecycle(
        TaskLifecycleEvent(
            phase="terminal",
            session_key=session.session_key,
            task_id=first.task_id,
            task_status=AgentTaskStatus.SUCCEEDED,
            run_kind="default",
            terminal_reason="completed",
        )
    )
    delayed_terminal_change = events[-1][2]
    assert delayed_terminal_change["run_status"] == "running"
    assert delayed_terminal_change["active_task"] == {
        "task_id": second.task_id,
        "status": "running",
    }

    release_second.set()
    await runtime.wait(first.task_id, timeout=2.0)
    await runtime.wait(second.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_delayed_queued_callback_uses_current_running_snapshot() -> None:
    session = _make_session(status=SessionStatus.RUNNING)
    manager = _SessionManager(session)
    events: list[tuple[str, str, dict[str, Any]]] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def _controlled_handler(_run: Any) -> None:
        started.set()
        await release.wait()

    runtime = _make_runtime(
        _controlled_handler,
        session_manager=manager,
        events=events,
    )
    handle = await runtime.enqueue(_make_envelope(), "hello")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    await runtime._notify_task_lifecycle(
        TaskLifecycleEvent(
            phase="queued",
            session_key=session.session_key,
            task_id=handle.task_id,
            task_status=AgentTaskStatus.QUEUED,
            run_kind="default",
        )
    )

    assert events[-1] == (
        session.session_key,
        "sessions.changed",
        {
            "schema_version": 1,
            "key": session.session_key,
            "reason": "task_queued",
            "run_status": "running",
            "active_task": {"task_id": handle.task_id, "status": "running"},
        },
    )

    release.set()
    await runtime.wait(handle.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_delayed_queued_callback_cannot_resurrect_terminal_task() -> None:
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
            task_id="task-already-terminal",
            task_status=AgentTaskStatus.QUEUED,
            run_kind="default",
            task_snapshot=SessionTaskSnapshot(
                running_task_id=None,
                queued_task_ids=(),
            ),
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
            },
        )
    ]


@pytest.mark.asyncio
async def test_lifecycle_projection_fails_safe_when_runtime_snapshot_fails() -> None:
    session = _make_session(status=SessionStatus.RUNNING)
    manager = _SessionManager(session)
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, event_name, payload))

    async def _handler(_run: Any) -> None:
        return None

    async def _snapshot_failure(*_args: Any, **_kwargs: Any) -> SessionTaskSnapshot:
        raise RuntimeError("snapshot unavailable")

    runtime = TaskRuntime(
        storage=_make_task_storage(),
        turn_handler=_handler,
        event_emitter=_emit,
        lifecycle_listener=_make_task_session_lifecycle_listener(
            session_manager=manager,
            event_emitter=_emit,
        ),
    )
    runtime.session_task_snapshot = _snapshot_failure  # type: ignore[method-assign]

    await runtime._notify_task_lifecycle(
        TaskLifecycleEvent(
            phase="queued",
            session_key=session.session_key,
            task_id="task-changed",
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
                "changed_task": {"task_id": "task-changed", "status": "queued"},
            },
        )
    ]

    # A can reach its terminal callback after B has acquired the same-session
    # execution lock.  If the state-locked snapshot fails here, A's terminal
    # status must not overwrite B's still-running session lifecycle.
    await runtime._notify_task_lifecycle(
        TaskLifecycleEvent(
            phase="terminal",
            session_key=session.session_key,
            task_id="task-old-running-owner",
            task_status=AgentTaskStatus.CANCELLED,
            run_kind="default",
            terminal_reason="cancelled",
        )
    )

    assert session.status == SessionStatus.RUNNING
    assert manager.update_calls == [
        (session.session_key, {}),
        (session.session_key, {}),
    ]
    assert events[-1] == (
        session.session_key,
        "sessions.changed",
        {
            "schema_version": 1,
            "key": session.session_key,
            "reason": "task_terminal",
            "changed_task": {
                "task_id": "task-old-running-owner",
                "status": "cancelled",
                "terminal_reason": "cancelled",
                "terminal_message": "The task was cancelled before it finished.",
            },
        },
    )


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
                "steer_capability": {
                    "mode": "same_turn",
                    "expected_turn_id": handle.task_id,
                    "input_kinds": ["text"],
                    "reason": None,
                },
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
