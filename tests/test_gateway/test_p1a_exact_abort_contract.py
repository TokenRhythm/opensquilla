"""Adversarial contracts for task-scoped chat Stop.

These tests deliberately exercise runtimes outside the in-tree TaskRuntime
shape.  An exact Stop may skip an advisory list preflight, but it must never
widen to session cancellation when the runtime cannot atomically bind a task
id to its session.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskStatus
from tests.test_gateway.test_rpc_sessions import FakeSession, FakeSessionManager, make_ctx
from tests.test_gateway.test_task_runtime_terminal_cleanup import (
    _make_envelope,
    _make_storage,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("list_failure", ["raises", "timeout"])
async def test_exact_abort_still_uses_atomic_cancel_when_runtime_list_fails(
    monkeypatch: pytest.MonkeyPatch,
    list_failure: str,
) -> None:
    session = FakeSession(session_key=f"agent:main:webchat:list-{list_failure}")

    class Runtime:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, Any]] = []

        async def list(self, session_key: str | None = None):
            assert session_key == session.session_key
            if list_failure == "raises":
                raise RuntimeError("advisory list unavailable")
            await asyncio.Event().wait()

        async def cancel(
            self,
            *,
            task_id: str | None = None,
            session_key: str | None = None,
            source: str | None = None,
            reason: str | None = None,
        ) -> int:
            self.cancel_calls.append({
                "task_id": task_id,
                "session_key": session_key,
                "source": source,
                "reason": reason,
            })
            return int(task_id == "task-A" and session_key == session.session_key)

        async def wait(self, task_id: str):
            return SimpleNamespace(task_id=task_id, status="cancelled")

    runtime = Runtime()
    context = make_ctx(
        session_manager=FakeSessionManager([session]),
        task_runtime=runtime,
    )
    # A broken advisory list must not make this test (or a real Stop) wait for
    # the normal multi-second drain budget.
    monkeypatch.setattr(rpc_sessions, "_ABORT_RUNTIME_CANCEL_DRAIN_SECONDS", 0.05)

    response = await get_dispatcher().dispatch(
        f"abort-{list_failure}",
        "chat.abort",
        {
            "sessionKey": session.session_key,
            "taskId": "task-A",
            "scope": "task",
            "source": "webui_stop",
        },
        context,
    )

    assert response.ok is True
    assert response.payload["aborted"] is True
    assert runtime.cancel_calls == [{
        "task_id": "task-A",
        "session_key": session.session_key,
        "source": "webui_stop",
        "reason": "user_abort",
    }]


@pytest.mark.asyncio
async def test_task_scoped_abort_never_falls_back_to_legacy_session_cancel() -> None:
    session = FakeSession(session_key="agent:main:webchat:legacy-runtime")

    class LegacySessionRuntime:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, Any]] = []

        async def list(self, session_key: str | None = None):
            assert session_key == session.session_key
            return [SimpleNamespace(task_id="task-A", status="running")]

        async def cancel(
            self,
            *,
            session_key: str | None = None,
            source: str | None = None,
            reason: str | None = None,
        ) -> int:
            self.cancel_calls.append({
                "session_key": session_key,
                "source": source,
                "reason": reason,
            })
            return 1

    runtime = LegacySessionRuntime()
    context = make_ctx(
        session_manager=FakeSessionManager([session]),
        task_runtime=runtime,
    )

    response = await get_dispatcher().dispatch(
        "abort-legacy-runtime",
        "chat.abort",
        {
            "sessionKey": session.session_key,
            "taskId": "task-A",
            "scope": "task",
            "source": "webui_stop",
        },
        context,
    )

    assert response.ok is True
    assert response.payload["aborted"] is False
    assert response.payload["reason"] == "task_scope_unsupported"
    assert runtime.cancel_calls == []


@pytest.mark.asyncio
async def test_task_scoped_abort_without_runtime_does_not_cancel_legacy_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy session-only registry cannot safely implement an exact Stop."""

    session = FakeSession(session_key="agent:main:webchat:legacy-registry")
    running_task = object()

    class LegacyRegistry:
        def __init__(self) -> None:
            self.cancel_calls: list[str] = []

        def get(self, session_key: str) -> object | None:
            return running_task if session_key == session.session_key else None

        def cancel(self, session_key: str) -> bool:
            self.cancel_calls.append(session_key)
            return True

    registry = LegacyRegistry()
    monkeypatch.setattr(rpc_sessions, "get_agent_task_registry", lambda: registry)
    context = make_ctx(
        session_manager=FakeSessionManager([session]),
        task_runtime=None,
    )

    response = await get_dispatcher().dispatch(
        "abort-legacy-registry",
        "chat.abort",
        {
            "sessionKey": session.session_key,
            "taskId": "task-A",
            "scope": "task",
            "source": "webui_stop",
        },
        context,
    )

    assert response.ok is True
    assert response.payload["aborted"] is False
    assert response.payload["reason"] == "task_scope_unsupported"
    assert registry.get(session.session_key) is running_task
    assert registry.cancel_calls == []


@pytest.mark.asyncio
async def test_cancel_exact_waits_for_committed_reservation_activation_before_cancelling() -> None:
    """A Stop racing durable admission must cancel the accepted task before its turn runs."""

    session_key = "agent:main:webchat:exact-admission-race"
    envelope = _make_envelope(session_key)
    storage = _make_storage()
    handler_calls: list[str] = []

    async def handler(run: Any) -> None:
        handler_calls.append(run.task_id)

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    committed = asyncio.Event()
    allow_activation = asyncio.Event()
    reservation_box: list[Any] = []

    async def commit_then_activate() -> Any:
        async with runtime.collect_admission(session_key):
            reservation = await runtime.reserve(envelope, "accepted before Stop")
            reservation_box.append(reservation)
            await storage.create_agent_task(reservation.task_record)
            committed.set()
            await allow_activation.wait()
            return await runtime.activate(
                reservation,
                defer_queued_notification=True,
            )

    ingress = asyncio.create_task(commit_then_activate())
    await asyncio.wait_for(committed.wait(), timeout=2.0)
    task_id = reservation_box[0].task_id
    exact_stop = asyncio.create_task(
        runtime.cancel_exact(
            task_id=task_id,
            session_key=session_key,
            source="webui_stop",
            reason="user_abort",
        )
    )
    await asyncio.sleep(0)
    assert not exact_stop.done()

    # Keep the turn handler behind the per-session execution fence while the
    # admission owner activates and the already-waiting exact Stop takes over.
    execution_lock = runtime._session_execution_locks.setdefault(
        session_key,
        asyncio.Lock(),
    )
    async with execution_lock:
        allow_activation.set()
        handle = await asyncio.wait_for(ingress, timeout=2.0)
        assert handle.task_id == task_id
        assert await asyncio.wait_for(exact_stop, timeout=2.0) == 1

    try:
        record = await runtime.wait(task_id, timeout=2.0)
    finally:
        await runtime.shutdown()

    assert record.status is AgentTaskStatus.CANCELLED
    assert handler_calls == []


@pytest.mark.asyncio
async def test_exact_abort_timeout_is_unknown_then_same_identity_retry_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission contention is unknown, never an inactive-task false negative."""

    session_key = "agent:main:webchat:exact-admission-timeout"
    session = FakeSession(session_key=session_key)
    envelope = _make_envelope(session_key)
    storage = _make_storage()
    handler_calls: list[str] = []

    async def handler(run: Any) -> None:
        handler_calls.append(run.task_id)

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    context = make_ctx(
        session_manager=FakeSessionManager([session]),
        task_runtime=runtime,
    )
    monkeypatch.setattr(rpc_sessions, "_ABORT_RUNTIME_CANCEL_DRAIN_SECONDS", 0.05)

    reservation = await runtime.reserve(envelope, "committed but not active")
    await storage.create_agent_task(reservation.task_record)
    task_id = reservation.task_id
    execution_lock = runtime._session_execution_locks.setdefault(
        session_key,
        asyncio.Lock(),
    )

    async with execution_lock:
        async with runtime.collect_admission(session_key):
            first = await get_dispatcher().dispatch(
                "abort-admission-timeout",
                "chat.abort",
                {
                    "sessionKey": session_key,
                    "taskId": task_id,
                    "scope": "task",
                    "source": "webui_stop",
                },
                context,
            )
            assert first.ok is True
            assert first.payload["aborted"] is False
            assert first.payload["key"] == session_key
            assert first.payload["reason"] == "task_cancel_unknown"
            handle = await runtime.activate(
                reservation,
                defer_queued_notification=True,
            )
            assert handle.task_id == task_id

        # Release the admission fence so the exact retry can acquire it, but
        # retain the execution fence until cancellation is durably requested.
        second = await get_dispatcher().dispatch(
            "abort-admission-retry",
            "chat.abort",
            {
                "sessionKey": session_key,
                "taskId": task_id,
                "scope": "task",
                "source": "webui_stop",
            },
            context,
        )
    try:
        record = await runtime.wait(task_id, timeout=2.0)
    finally:
        await runtime.shutdown()

    assert second.ok is True
    assert second.payload["aborted"] is True
    assert record.status is AgentTaskStatus.CANCELLED
    assert handler_calls == []
