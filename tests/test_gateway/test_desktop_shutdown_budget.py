from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from opensquilla.gateway.background_completion import BackgroundCompletionManager
from opensquilla.gateway.boot import GatewayServer, _GatewayShutdownRelay
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.shutdown import ShutdownRequest
from opensquilla.gateway.task_runtime import TaskRuntime, TaskRuntimeShuttingDownError


class _Storage:
    def __init__(self):
        self.records = {}

    async def create_agent_task(self, record):
        self.records[record.task_id] = record

    async def get_agent_task(self, task_id):
        return self.records.get(task_id)

    async def update_agent_task(self, task_id, **fields):
        for key, value in fields.items():
            setattr(self.records[task_id], key, value)


def _envelope(key="agent:main:desktop-budget"):
    return RouteEnvelope(
        source_kind=SourceKind.WEB, source_name="synthetic-test",
        agent_id="main", session_key=key,
    )


def test_shutdown_relay_preserves_early_quit_and_never_extends_deadline():
    requests = []
    relay = _GatewayShutdownRelay(on_request=requests.append)
    first = relay.desktop("drain", 10000)
    initial_deadline = relay.request.deadline
    second = relay.desktop("quit", 20000)
    relay.desktop("drain", 5000)
    reasons = []
    relay.install(reasons.append)

    assert first["accepted_mode"] == "drain"
    assert second["accepted_mode"] == "quit"
    assert relay.request.deadline <= initial_deadline - 5
    assert relay.request.termination_deadline == initial_deadline
    assert reasons == ["desktop_quit"]
    assert all(request is relay.request for request in requests)


def test_late_drain_upgrade_reserves_existing_force_stop_window(monkeypatch):
    from opensquilla.gateway import shutdown

    monkeypatch.setattr(shutdown.time, "monotonic", lambda: 59.75)
    request = ShutdownRequest("drain", 60.0)
    request.update("quit", 10000)
    assert request.acknowledgement() == {
        "accepted_mode": "quit", "remaining_ms": 0, "total_remaining_ms": 250,
    }
    request.update("drain", 60000)
    assert request.mode == "quit"
    assert request.termination_deadline == 60.0


@pytest.mark.asyncio
@pytest.mark.parametrize("start_with_drain", [False, True])
async def test_desktop_quit_cancels_and_preserves_system_reason(start_with_drain):
    storage = _Storage()
    started = asyncio.Event()

    async def handler(_run):
        started.set()
        await asyncio.sleep(60)

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    handle = await runtime.enqueue(_envelope(), "synthetic work")
    await started.wait()
    request = ShutdownRequest(
        "drain" if start_with_drain else "quit", time.monotonic() + 10,
    )
    closing = asyncio.create_task(runtime.shutdown(
        graceful=True, graceful_timeout=60, request=request,
    ))
    if start_with_drain:
        await asyncio.sleep(0)
        assert not closing.done()
        request.update("quit", 1000)
        runtime.close_admission(request)
    result = await asyncio.wait_for(closing, 0.5)
    assert result.clean
    assert storage.records[handle.task_id].details["cancellation"] == {
        "source": "gateway_shutdown", "reason": "desktop_quit",
    }
    with pytest.raises(TaskRuntimeShuttingDownError):
        await runtime.reserve(_envelope(), "late work")


@pytest.mark.asyncio
async def test_quit_fences_reservation_committed_before_shutdown():
    storage = _Storage()
    handler = AsyncMock()
    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    reservation = await runtime.reserve(_envelope(), "accepted work")
    await storage.create_agent_task(reservation.task_record)
    request = ShutdownRequest("quit", time.monotonic() + 1)
    runtime.close_admission(request)
    handle = await runtime.activate(reservation)
    result = await runtime.shutdown(request=request)
    assert result.clean
    handler.assert_not_awaited()
    assert storage.records[handle.task_id].details["cancellation"] == {
        "source": "gateway_shutdown", "reason": "desktop_quit",
    }


@pytest.mark.asyncio
async def test_quit_fences_fair_slot_waiter_before_handler():
    started = asyncio.Event()
    calls = []

    async def handler(run):
        calls.append(run.task_id)
        started.set()
        await asyncio.sleep(60)

    runtime = TaskRuntime(storage=_Storage(), turn_handler=handler, max_concurrency=1)
    first = await runtime.enqueue(_envelope("agent:main:first"), "first")
    await started.wait()
    await runtime.enqueue(_envelope("agent:main:second"), "waiting")
    request = ShutdownRequest("quit", time.monotonic() + 1)
    runtime.close_admission(request)
    assert (await runtime.shutdown(request=request)).clean
    assert calls == [first.task_id]


@pytest.mark.asyncio
async def test_desktop_cancellation_stops_driver_before_blocked_intent_write():
    storage = _Storage()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def handler(_run):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    handle = await runtime.enqueue(_envelope(), "synthetic work")
    await started.wait()
    task = runtime._tasks[handle.task_id]
    update = storage.update_agent_task

    async def slow_update(task_id, **fields):
        if "cancellation_requested" in fields.get("details", {}):
            blocked.set()
            await release.wait()
        await update(task_id, **fields)

    storage.update_agent_task = slow_update
    closing = asyncio.create_task(runtime._cancel_runtime_tasks(
        [task], source="gateway_shutdown", reason="desktop_quit",
    ))
    await asyncio.wait_for(blocked.wait(), 0.5)
    await asyncio.wait_for(cancelled.wait(), 0.5)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 1)
    await asyncio.wait_for(task.asyncio_task, 1)
    assert storage.records[handle.task_id].details["cancellation"] == {
        "source": "gateway_shutdown", "reason": "desktop_quit",
    }


@pytest.mark.asyncio
async def test_quit_cancels_other_tasks_while_one_intent_write_is_blocked():
    storage = _Storage()
    blocked = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    started = asyncio.Queue()

    async def handler(run):
        await started.put(run.task_id)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    first = await runtime.enqueue(_envelope("agent:main:first"), "first")
    await started.get()
    await runtime.enqueue(_envelope("agent:main:second"), "second")
    await started.get()
    update = storage.update_agent_task

    async def slow_update(task_id, **fields):
        if task_id == first.task_id and "cancellation_requested" in fields.get("details", {}):
            blocked.set()
            await release.wait()
        await update(task_id, **fields)

    storage.update_agent_task = slow_update
    request = ShutdownRequest("quit", time.monotonic() + 0.05)
    result = await asyncio.wait_for(runtime.shutdown(request=request), 0.5)
    assert blocked.is_set()
    assert cancelled.is_set()
    assert not result.clean
    release.set()
    await asyncio.wait_for(runtime._shutdown_task, 1)


@pytest.mark.asyncio
async def test_blocked_goal_pause_does_not_delay_quit_cancellation(monkeypatch):
    from opensquilla.gateway import boot

    cancelled = asyncio.Event()
    started = asyncio.Event()
    goal_release = asyncio.Event()
    services_closed = asyncio.Event()

    async def handler(_run):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runtime = TaskRuntime(storage=_Storage(), turn_handler=handler)
    await runtime.enqueue(_envelope(), "synthetic work")
    await started.wait()
    request = ShutdownRequest("quit", time.monotonic() + 0.05)
    runtime.close_admission(request)
    services = SimpleNamespace(
        task_runtime=runtime,
        goal_service=SimpleNamespace(prepare_shutdown=AsyncMock(side_effect=goal_release.wait)),
        close=AsyncMock(side_effect=lambda: services_closed.set()),
    )
    server = GatewayServer(
        app=SimpleNamespace(state=SimpleNamespace(shutdown_request=request)),
        config=GatewayConfig(), _services=services, _pid_lock=MagicMock(),
    )
    monkeypatch.setattr(boot, "get_registry", lambda: SimpleNamespace(
        broadcast=AsyncMock(), all=lambda: [],
    ))
    monkeypatch.setattr(server, "_stop_owned_processes", AsyncMock())
    result = await asyncio.wait_for(server.close("desktop_quit"), 0.5)
    assert cancelled.is_set()
    assert not result.clean
    services.close.assert_not_awaited()
    server._pid_lock.release.assert_not_called()
    goal_release.set()
    await asyncio.wait_for(services_closed.wait(), 1)


@pytest.mark.asyncio
async def test_completion_quit_fences_new_work_and_bounds_resistant_watcher():
    manager = BackgroundCompletionManager(session_manager=SimpleNamespace())
    started = asyncio.Event()
    release = asyncio.Event()

    async def watcher():
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    task = asyncio.create_task(watcher())
    manager._watch_tasks.add(task)
    await started.wait()
    assert manager.shutdown_activity_count() == 1
    assert not await asyncio.wait_for(manager.close(cancel=True, timeout=0.02), 0.5)
    assert not await manager._begin_group_admission("parent", "group")
    assert task in manager._watch_tasks
    release.set()
    await task


@pytest.mark.asyncio
async def test_quit_stops_only_exact_runtime_and_background_process_owners(monkeypatch):
    from opensquilla import process_tree
    from opensquilla.tools.builtin import shell

    background = AsyncMock()
    persisted = AsyncMock()
    monkeypatch.setattr(shell, "active_background_process_task_owners", lambda: (
        ("background-session", "completed-task"),
    ))
    monkeypatch.setattr(shell, "cancel_background_processes_for_task", background)
    monkeypatch.setattr(process_tree, "cancel_persisted_processes_for_task", persisted)
    runtime = TaskRuntime(storage=_Storage(), turn_handler=AsyncMock())
    assert runtime.shutdown_activity()["auxiliary"] == 1
    server = GatewayServer(
        app=SimpleNamespace(), config=GatewayConfig(),
        _services=SimpleNamespace(task_runtime=SimpleNamespace(
            shutdown_task_owners=lambda: {("active-session", "active-task")},
        )),
    )
    await server._stop_owned_processes()
    assert {call.args for call in background.await_args_list} == {
        ("background-session", "completed-task"), ("active-session", "active-task"),
    }
    assert {call.args[1:] for call in persisted.await_args_list} == {
        ("background-session", "completed-task"), ("active-session", "active-task"),
    }
