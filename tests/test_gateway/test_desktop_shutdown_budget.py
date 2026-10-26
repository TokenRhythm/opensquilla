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
        details_patch = fields.pop("details_patch", None)
        if details_patch is not None:
            record = self.records[task_id]
            record.details = {**(record.details or {}), **details_patch}
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
    await asyncio.wait_for(started.wait(), 1)
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
    await asyncio.wait_for(started.wait(), 1)
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
    await asyncio.wait_for(started.wait(), 1)
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
    await asyncio.wait_for(started.get(), 1)
    await runtime.enqueue(_envelope("agent:main:second"), "second")
    await asyncio.wait_for(started.get(), 1)
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
@pytest.mark.parametrize("runtime_residual", [False, True], ids=["goal-only", "runtime-residual"])
async def test_blocked_goal_pause_does_not_delay_quit_cancellation(monkeypatch, runtime_residual):
    from opensquilla.gateway import boot

    cancelled = asyncio.Event()
    started = asyncio.Event()
    goal_entered = asyncio.Event()
    goal_release = asyncio.Event()
    write_blocked = asyncio.Event()
    write_release = asyncio.Event()
    runtime_finished = asyncio.Event()
    services_closed = asyncio.Event()
    runtime_results = []
    close_tasks = []

    async def handler(_run):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    storage = _Storage()
    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    await runtime.enqueue(_envelope(), "synthetic work")
    await asyncio.wait_for(started.wait(), 1)
    update = storage.update_agent_task

    async def controlled_update(task_id, **fields):
        if runtime_residual and "status" in fields:
            write_blocked.set()
            await write_release.wait()
        await update(task_id, **fields)

    async def pause_goals():
        goal_entered.set()
        await goal_release.wait()

    runtime_shutdown = runtime.shutdown

    async def observe_runtime_shutdown(**kwargs):
        result = await runtime_shutdown(**kwargs)
        runtime_results.append(result)
        runtime_finished.set()
        return result

    monkeypatch.setattr(storage, "update_agent_task", controlled_update)
    monkeypatch.setattr(runtime, "shutdown", observe_runtime_shutdown)
    # Infinity is only the unarmed fixture phase, bounded by the 1 s setup
    # barrier below. The real shared 50 ms deadline starts after we establish
    # whether the goal alone is blocked or runtime settlement is blocked too.
    request = ShutdownRequest("quit", float("inf"))
    runtime.close_admission(request)
    services = SimpleNamespace(
        task_runtime=runtime,
        goal_service=SimpleNamespace(prepare_shutdown=AsyncMock(side_effect=pause_goals)),
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
    close_impl = server._close_impl

    async def observe_close_impl(*args, **kwargs):
        close_tasks.append(asyncio.current_task())
        return await close_impl(*args, **kwargs)

    monkeypatch.setattr(server, "_close_impl", observe_close_impl)
    closing = asyncio.create_task(server.close("desktop_quit"))
    try:
        async with asyncio.timeout(1):
            await goal_entered.wait()
            if runtime_residual:
                await write_blocked.wait()
            else:
                await runtime_finished.wait()
                assert runtime_results[0].clean
        assert cancelled.is_set()
        assert not goal_release.is_set()
        request.update("quit", 50)
        result = await asyncio.wait_for(asyncio.shield(closing), 0.5)
        assert not result.clean
        services.close.assert_not_awaited()
        server._pid_lock.release.assert_not_called()
        await asyncio.wait_for(runtime_finished.wait(), 1)
        assert runtime_results[0].clean is (not runtime_residual)
        goal_release.set()
        write_release.set()
        await asyncio.wait_for(asyncio.shield(close_tasks[0]), 1)
        # Once a protected goal exceeded its phase cutoff, transports may
        # close but shared dependencies remain owned until process exit.
        services.close.assert_not_awaited()
        server._pid_lock.release.assert_not_called()
    finally:
        goal_release.set()
        write_release.set()
        # Wake every unarmed wait even when an admission assertion failed.
        request.update("quit", 0)
        pending = [closing, *close_tasks]
        if runtime._shutdown_task is not None:
            pending.append(runtime._shutdown_task)
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 1)


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
    await asyncio.wait_for(started.wait(), 1)
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
    monkeypatch.setattr(
        process_tree, "has_persisted_processes_for_task", AsyncMock(return_value=False),
    )
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


@pytest.mark.asyncio
@pytest.mark.parametrize("residual", [False, True, OSError("synthetic registry unavailable")])
async def test_quit_requires_verified_persisted_process_cleanup(monkeypatch, tmp_path, residual):
    from opensquilla import process_tree
    from opensquilla.tools.builtin import shell

    monkeypatch.setattr(shell, "active_background_process_task_owners", lambda: ())
    monkeypatch.setattr(shell, "cancel_background_processes_for_task", AsyncMock(return_value=0))
    # Zero can mean either no owned tree or failed exact-owner termination.
    monkeypatch.setattr(
        process_tree, "cancel_persisted_processes_for_task", AsyncMock(return_value=0),
    )
    remaining = AsyncMock(
        **({"side_effect": residual} if isinstance(residual, Exception)
           else {"return_value": residual}),
    )
    monkeypatch.setattr(process_tree, "has_persisted_processes_for_task", remaining)
    server = GatewayServer(
        app=SimpleNamespace(), config=GatewayConfig(state_dir=str(tmp_path)),
        _services=SimpleNamespace(task_runtime=SimpleNamespace(
            shutdown_task_owners=lambda: {("synthetic-session", "synthetic-task")},
        )),
    )
    assert await server._stop_owned_processes() is (residual is False)
    remaining.assert_awaited_once_with(str(tmp_path), "synthetic-session", "synthetic-task")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["channels", "goals", "broadcast", "mcp", "services"])
async def test_gateway_nonruntime_phase_observes_tighter_request(monkeypatch, phase):
    from opensquilla.gateway import boot

    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    close_tasks = []
    channel_budgets = []

    async def blocked(**kwargs):
        if "timeout" in kwargs:
            channel_budgets.append(kwargs["timeout"])
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    services = SimpleNamespace(
        task_runtime=None, skill_management_service=None, optional_start_task=None,
        deferred_warmup_task=None, goal_service=None, close=AsyncMock(),
    )
    registry = SimpleNamespace(broadcast=AsyncMock(), all=lambda: [])
    manager = SimpleNamespace(stop_all=AsyncMock())
    mcp = AsyncMock()
    if phase == "channels":
        manager.stop_all = blocked
    elif phase == "goals":
        services.goal_service = SimpleNamespace(prepare_shutdown=blocked)
    elif phase == "broadcast":
        async def broadcast(*args):
            await blocked()
        registry.broadcast = broadcast
    elif phase == "mcp":
        mcp = blocked
    else:
        services.close = blocked

    request = ShutdownRequest("drain", time.monotonic() + 60)
    listener = SimpleNamespace(should_exit=False)
    pid_lock = MagicMock()
    server = GatewayServer(
        app=SimpleNamespace(state=SimpleNamespace(shutdown_request=request)),
        config=GatewayConfig(), _services=services, _channel_manager=manager,
        _server=listener, _pid_lock=pid_lock,
    )
    original = server._close_impl

    async def observe(*args, **kwargs):
        close_tasks.append(asyncio.current_task())
        return await original(*args, **kwargs)

    monkeypatch.setattr(server, "_close_impl", observe)
    monkeypatch.setattr(server, "_stop_owned_processes", AsyncMock(return_value=True))
    monkeypatch.setattr(boot, "get_registry", lambda: registry)
    monkeypatch.setattr(
        "opensquilla.gateway.terminal_sessions.get_terminal_manager",
        lambda: SimpleNamespace(close_all=AsyncMock()),
    )
    monkeypatch.setattr("opensquilla.mcp.discovery.close_active_clients", mcp)
    closing = asyncio.create_task(server.close("desktop_drain"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # Leave enough final cleanup time for Windows scheduling when asserting
        # that a best-effort notification alone does not make shutdown dirty.
        request.update("quit", 1000 if phase == "broadcast" else 100)
        result = await asyncio.wait_for(asyncio.shield(closing), 2)
        assert listener.should_exit is True
        if phase == "broadcast":
            assert result is None or result.clean
            assert cancelled.is_set()
        else:
            assert result is not None and not result.clean
            assert not cancelled.is_set()
            pid_lock.release.assert_not_called()
        if phase in {"channels", "goals", "mcp"}:
            services.close.assert_not_awaited()
        if phase == "channels":
            # The first budget is valid for the original drain, but the same
            # in-flight stop must react when that request is later tightened.
            assert 40 < channel_budgets[0] < 60
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(
            closing, *close_tasks, *server._shutdown_phase_tasks,
            return_exceptions=True,
        ), 1)


@pytest.mark.asyncio
async def test_short_quit_bounds_channel_budget_and_preserves_settlement_window(monkeypatch):
    from opensquilla.gateway import boot

    release = asyncio.Event()
    transports = asyncio.Event()
    entered = asyncio.Event()
    saved = asyncio.Event()
    observed_budgets = []
    close_tasks = []

    async def stop_channels(*, timeout):
        observed_budgets.append(timeout)
        entered.set()
        await release.wait()
        saved.set()

    request = ShutdownRequest("quit", time.monotonic() + 0.5)
    services = SimpleNamespace(
        task_runtime=None, skill_management_service=None, optional_start_task=None,
        deferred_warmup_task=None, goal_service=None, close=AsyncMock(),
    )
    server = GatewayServer(
        app=SimpleNamespace(state=SimpleNamespace(shutdown_request=request)),
        config=GatewayConfig(), _services=services,
        _channel_manager=SimpleNamespace(stop_all=stop_channels), _pid_lock=MagicMock(),
    )
    original = server._close_impl

    async def observe(*args, **kwargs):
        close_tasks.append(asyncio.current_task())
        return await original(*args, **kwargs)

    async def close_websockets(*args, **kwargs):
        transports.set()

    monkeypatch.setattr(server, "_close_impl", observe)
    monkeypatch.setattr(server, "_stop_owned_processes", AsyncMock(return_value=True))
    monkeypatch.setattr(boot, "_close_gateway_websocket_connections", close_websockets)
    monkeypatch.setattr(
        boot, "get_registry", lambda: SimpleNamespace(broadcast=AsyncMock(), all=lambda: []),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.terminal_sessions.get_terminal_manager",
        lambda: SimpleNamespace(close_all=AsyncMock()),
    )
    closing = asyncio.create_task(server.close("desktop_quit"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(transports.wait(), 1)
        # Subtracting absolute monotonic deadlines can round just above 0.4.
        assert 0 < observed_budgets[0] <= 0.4 + 1e-6
        assert not closing.done()
        assert not saved.is_set()
        services.close.assert_not_awaited()
        release.set()
        result = await asyncio.wait_for(closing, 1)
        assert saved.is_set()
        assert result is not None and not result.clean
        server._pid_lock.release.assert_not_called()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(
            closing, *close_tasks, *server._shutdown_phase_tasks,
            return_exceptions=True,
        ), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [False, True], ids=["server", "preview-server"])
async def test_resistant_serve_owner_retains_shared_dependencies(monkeypatch, preview):
    from opensquilla.gateway import boot

    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    close_tasks = []

    async def serve():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            # Model an ASGI owner still finishing a request after cancellation.
            await release.wait()

    serve_task = asyncio.create_task(serve())
    await started.wait()
    services = SimpleNamespace(
        task_runtime=None, skill_management_service=None, optional_start_task=None,
        deferred_warmup_task=None, goal_service=None, close=AsyncMock(),
    )
    request = ShutdownRequest("quit", time.monotonic() + 0.2)
    listener = SimpleNamespace(should_exit=False)
    server = GatewayServer(
        app=SimpleNamespace(state=SimpleNamespace(shutdown_request=request)),
        config=GatewayConfig(), _services=services, _pid_lock=MagicMock(),
        _server=None if preview else listener,
        _task=None if preview else serve_task,
        _preview_server=listener if preview else None,
        _preview_task=serve_task if preview else None,
    )
    original = server._close_impl

    async def observe(*args, **kwargs):
        close_tasks.append(asyncio.current_task())
        return await original(*args, **kwargs)

    monkeypatch.setattr(server, "_close_impl", observe)
    monkeypatch.setattr(server, "_stop_owned_processes", AsyncMock(return_value=True))
    monkeypatch.setattr(
        boot, "get_registry", lambda: SimpleNamespace(broadcast=AsyncMock(), all=lambda: []),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.terminal_sessions.get_terminal_manager",
        lambda: SimpleNamespace(close_all=AsyncMock()),
    )
    mcp_close = AsyncMock()
    monkeypatch.setattr("opensquilla.mcp.discovery.close_active_clients", mcp_close)
    try:
        result = await asyncio.wait_for(server.close("desktop_quit"), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert result is not None and not result.clean
        assert listener.should_exit is True
        assert not serve_task.done()
        assert server._shutdown_phase_tasks
        mcp_close.assert_not_awaited()
        services.close.assert_not_awaited()
        server._pid_lock.release.assert_not_called()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(
            serve_task, *close_tasks, *server._shutdown_phase_tasks,
            return_exceptions=True,
        ), 1)
