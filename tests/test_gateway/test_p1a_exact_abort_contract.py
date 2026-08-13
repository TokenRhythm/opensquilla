"""Adversarial contracts for task-scoped chat Stop.

These tests deliberately exercise runtimes outside the in-tree TaskRuntime
shape.  An exact Stop may skip an advisory list preflight, but it must never
widen to session cancellation when the runtime cannot atomically bind a task
id to its session.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskStatus
from opensquilla.tools.builtin import shell
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
async def test_exact_abort_starts_process_cleanup_before_slow_completion_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession(session_key="agent:main:webchat:parallel-cleanup")
    completion_started = asyncio.Event()
    completion_cancelled = asyncio.Event()
    process_started = asyncio.Event()
    release_process = asyncio.Event()
    process_finished = asyncio.Event()

    class Runtime:
        async def cancel(self, **_kwargs: Any) -> int:
            return 1

        async def list(self, session_key: str | None = None):
            assert session_key in {None, session.session_key}
            return []

        async def wait(self, _task_id: str):
            return SimpleNamespace(status="cancelled")

    async def slow_completion(_session_key: str, _task_id: str) -> int:
        completion_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            completion_cancelled.set()

    async def process_cleanup(_session_key: str, _task_id: str) -> int:
        process_started.set()
        await release_process.wait()
        process_finished.set()
        return 1

    monkeypatch.setattr(rpc_sessions, "_ABORT_RUNTIME_CANCEL_DRAIN_SECONDS", 0.02)
    monkeypatch.setattr(rpc_sessions, "_ABORT_OWNED_CLEANUP_SECONDS", 0.08)
    monkeypatch.setattr(
        "opensquilla.gateway.subagent_announce.cancel_background_completion_for_task",
        slow_completion,
    )
    monkeypatch.setattr(
        "opensquilla.tools.builtin.shell.cancel_background_processes_for_task",
        process_cleanup,
    )

    started_at = asyncio.get_running_loop().time()
    response = await get_dispatcher().dispatch(
        "abort-parallel-cleanup",
        "chat.abort",
        {
            "sessionKey": session.session_key,
            "taskId": "task-A",
            "scope": "task",
            "source": "webui_stop",
        },
        make_ctx(
            session_manager=FakeSessionManager([session]),
            task_runtime=Runtime(),
        ),
    )
    elapsed = asyncio.get_running_loop().time() - started_at

    assert response.ok is True
    assert response.payload["aborted"] is True
    assert elapsed < 0.12
    assert completion_started.is_set()
    assert process_started.is_set()
    assert process_finished.is_set() is False

    release_process.set()
    await asyncio.wait_for(process_finished.wait(), timeout=0.2)
    await asyncio.wait_for(completion_cancelled.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_slow_exact_runtime_cancel_cannot_delay_starting_any_safety_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession(session_key="agent:main:webchat:slow-runtime-cleanup")
    release = asyncio.Event()
    started = {
        name: asyncio.Event()
        for name in ("runtime", "completion", "process", "descendants")
    }
    finished = {name: asyncio.Event() for name in started}

    class Runtime:
        async def cancel(self, **_kwargs: Any) -> int:
            started["runtime"].set()
            try:
                await release.wait()
                return 1
            finally:
                finished["runtime"].set()

        async def list(self, session_key: str | None = None):
            assert session_key in {None, session.session_key}
            started["descendants"].set()
            try:
                await release.wait()
                return []
            finally:
                finished["descendants"].set()

        async def wait(self, _task_id: str):
            return SimpleNamespace(status="cancelled")

    async def slow_completion(_session_key: str, _task_id: str) -> int:
        started["completion"].set()
        try:
            await release.wait()
            return 1
        finally:
            finished["completion"].set()

    async def slow_process_cleanup(_session_key: str, _task_id: str) -> int:
        started["process"].set()
        try:
            await release.wait()
            return 1
        finally:
            finished["process"].set()

    monkeypatch.setattr(rpc_sessions, "_ABORT_RUNTIME_CANCEL_DRAIN_SECONDS", 0.02)
    monkeypatch.setattr(rpc_sessions, "_ABORT_OWNED_CLEANUP_SECONDS", 0.5)
    monkeypatch.setattr(
        "opensquilla.gateway.subagent_announce.cancel_background_completion_for_task",
        slow_completion,
    )
    monkeypatch.setattr(
        "opensquilla.tools.builtin.shell.cancel_background_processes_for_task",
        slow_process_cleanup,
    )

    response = await asyncio.wait_for(
        get_dispatcher().dispatch(
            "abort-slow-runtime-cleanup",
            "chat.abort",
            {
                "sessionKey": session.session_key,
                "taskId": "task-A",
                "scope": "task",
                "source": "webui_stop",
            },
            make_ctx(
                session_manager=FakeSessionManager([session]),
                task_runtime=Runtime(),
            ),
        ),
        timeout=0.15,
    )

    assert response.ok is True
    assert response.payload["aborted"] is False
    assert response.payload["reason"] == "task_cancel_unknown"
    assert all(event.is_set() for event in started.values())
    assert not any(event.is_set() for event in finished.values())

    release.set()
    await asyncio.gather(
        *(asyncio.wait_for(event.wait(), timeout=0.2) for event in finished.values())
    )


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

        # Releasing admission lets the first, still-live exact cancel finish.
        # A same-identity retry remains safe and may observe it as inactive.
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
    assert second.payload["aborted"] is False
    assert second.payload["reason"] == "task_not_active"
    assert record.status is AgentTaskStatus.CANCELLED
    assert handler_calls == []


@pytest.mark.skipif(os.name != "posix", reason="real process-group check is POSIX-specific")
@pytest.mark.asyncio
async def test_exact_stop_cancels_child_tool_process_and_runtime_stays_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise RPC -> task lineage -> real tool process cancellation end to end."""

    root_key = "agent:main:webchat:process-tree-e2e"
    child_key = "agent:worker:subagent:process-tree-e2e"
    storage = _make_storage()
    child_started = asyncio.Event()
    child_task_id: str | None = None
    created_processes: list[asyncio.subprocess.Process] = []
    original_create = shell._create_host_shell_subprocess

    async def capture_process(*args, **kwargs):
        process = await original_create(*args, **kwargs)
        created_processes.append(process)
        child_started.set()
        return process

    monkeypatch.setattr(shell, "_create_host_shell_subprocess", capture_process)

    async def handler(run: Any) -> None:
        nonlocal child_task_id
        if run.message == "after-stop":
            return
        if run.envelope.metadata.get("parent_task_id"):
            await shell._run_host_shell_command(
                " ".join(
                    shlex.quote(part)
                    for part in (sys.executable, "-c", "import time; time.sleep(30)")
                ),
                cwd=None,
                env=dict(os.environ),
                stdin_bytes=None,
                effective_timeout=30.0,
            )
            return
        child_envelope = replace(
            _make_envelope(child_key),
            metadata={
                "parent_task_id": run.task_id,
                "parent_session_key": root_key,
            },
        )
        child = await runtime.enqueue(
            child_envelope,
            "run child tool",
            mode="followup",
            run_kind="subagent",
        )
        child_task_id = child.task_id
        await asyncio.Event().wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    root = await runtime.enqueue(_make_envelope(root_key), "spawn child")
    await asyncio.wait_for(child_started.wait(), timeout=3.0)
    assert child_task_id is not None

    context = make_ctx(
        session_manager=FakeSessionManager([FakeSession(session_key=root_key)]),
        task_runtime=runtime,
    )
    response = await get_dispatcher().dispatch(
        "process-tree-stop",
        "chat.abort",
        {
            "sessionKey": root_key,
            "taskId": root.task_id,
            "scope": "task",
            "source": "webui_stop",
        },
        context,
    )

    root_record = await runtime.wait(root.task_id, timeout=3.0)
    child_record = await runtime.wait(child_task_id, timeout=3.0)
    assert response.ok is True
    assert response.payload["aborted"] is True
    assert root_record.status is AgentTaskStatus.CANCELLED
    assert child_record.status is AgentTaskStatus.CANCELLED
    assert created_processes and created_processes[0].returncode is not None

    # The same runtime/event loop accepts and completes another turn after Stop.
    next_task = await runtime.enqueue(_make_envelope(root_key), "after-stop")
    try:
        next_record = await runtime.wait(next_task.task_id, timeout=3.0)
    finally:
        await runtime.shutdown()
    assert next_record.status is AgentTaskStatus.SUCCEEDED
