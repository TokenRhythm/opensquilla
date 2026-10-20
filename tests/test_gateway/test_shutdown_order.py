"""Tests for graceful shutdown ordering (AC-M3)."""

from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opensquilla.gateway.boot import GatewayServer, ServiceContainer
from opensquilla.gateway.config import GatewayConfig


class _CancellationResistantConnection:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def close(self) -> None:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        finally:
            self.finished.set()


class _ImmediateConnection:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _SelfCancellingConnection:
    async def close(self) -> None:
        raise asyncio.CancelledError


def _gateway_server_for_ws_close_test() -> tuple[
    GatewayServer,
    MagicMock,
    MagicMock,
    MagicMock,
]:
    fake_server = MagicMock()
    fake_server.should_exit = False

    mock_services = MagicMock()
    mock_services.goal_service = None
    mock_services.task_runtime = None
    mock_services.close = AsyncMock()

    mock_pid_lock = MagicMock()
    serve_task = asyncio.create_task(asyncio.sleep(0))
    server = GatewayServer(
        app=MagicMock(),
        config=GatewayConfig(),
        _server=fake_server,
        _task=serve_task,
        _services=mock_services,
        _pid_lock=mock_pid_lock,
    )
    return server, fake_server, mock_services, mock_pid_lock


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["terminals", "goals", "mcp", "services"])
async def test_required_cleanup_exception_retains_ownership(
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    server, listener, services, pid_lock = _gateway_server_for_ws_close_test()
    failing = AsyncMock(side_effect=RuntimeError("synthetic cleanup failure"))
    terminal_close = failing if phase == "terminals" else AsyncMock()
    mcp_close = failing if phase == "mcp" else AsyncMock()
    if phase == "goals":
        services.goal_service = SimpleNamespace(prepare_shutdown=failing)
    if phase == "services":
        services.close = failing
    monkeypatch.setattr(
        "opensquilla.gateway.terminal_sessions.get_terminal_manager",
        lambda: SimpleNamespace(close_all=terminal_close),
    )
    monkeypatch.setattr("opensquilla.mcp.discovery.close_active_clients", mcp_close)
    monkeypatch.setattr(
        "opensquilla.gateway.boot.get_registry",
        lambda: SimpleNamespace(broadcast=AsyncMock(), all=lambda: []),
    )

    if phase == "mcp":
        with pytest.raises(RuntimeError, match="synthetic cleanup failure"):
            await server.close()
    else:
        result = await server.close()
        assert result is not None and not result.clean
    assert listener.should_exit is True
    assert server._task.done()
    pid_lock.release.assert_not_called()
    if phase in {"goals", "mcp"}:
        services.close.assert_not_awaited()
    else:
        services.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_drained_before_channel_stop() -> None:
    """task_runtime.shutdown() must complete before channel_manager.stop_all()."""
    call_order: list[str] = []

    async def mock_runtime_shutdown(**kwargs: object) -> None:
        call_order.append("task_runtime.shutdown")

    async def mock_stop_all(*, timeout: float | None = None) -> None:
        call_order.append("channel_manager.stop_all")

    # Build a minimal GatewayServer with mocked internals
    server = GatewayServer.__new__(GatewayServer)
    server._server = None
    server._task = None

    # Mock services with a task_runtime that records call order
    mock_services = MagicMock()
    mock_task_runtime = MagicMock()
    mock_task_runtime.shutdown = AsyncMock(side_effect=mock_runtime_shutdown)
    mock_services.task_runtime = mock_task_runtime
    # Make services.close() a no-op so the duplicate shutdown call is harmless
    mock_services.close = AsyncMock()
    server._services = mock_services

    # Mock channel_manager
    mock_channel_manager = MagicMock()
    mock_channel_manager.stop_all = AsyncMock(side_effect=mock_stop_all)
    server._channel_manager = mock_channel_manager

    # Patch registry to avoid real WS/broadcast logic
    mock_registry = MagicMock()
    mock_registry.broadcast = AsyncMock()
    mock_registry.all = MagicMock(return_value=[])

    with patch("opensquilla.gateway.boot.get_registry", return_value=mock_registry):
        await server.close(reason="test")

    assert call_order[0] == "task_runtime.shutdown", (
        f"Expected task_runtime.shutdown first, got: {call_order}"
    )
    assert call_order[1] == "channel_manager.stop_all", (
        f"Expected channel_manager.stop_all second, got: {call_order}"
    )


@pytest.mark.asyncio
async def test_close_stops_server_even_if_teardown_raises() -> None:
    """A failing teardown step must not leave the serve task pending.

    close() is now invoked on every shutdown (signal or HTTP), so the serve task
    is typically still running when it runs. If a teardown step (channel stop, WS
    broadcast) raises, the error still propagates to the caller, but the server
    stop + serve-task join must run first. A failed resource owner retains its
    shared dependencies and pid lock until process exit.
    """
    server = GatewayServer.__new__(GatewayServer)

    fake_server = MagicMock()
    fake_server.should_exit = False
    server._server = fake_server

    async def _serve() -> None:
        return None

    server._task = asyncio.ensure_future(_serve())

    mock_services = MagicMock()
    mock_services.task_runtime = None
    mock_services.close = AsyncMock()
    server._services = mock_services

    # The teardown step raises — close() must still stop the server before the
    # error propagates, without declaring ownership safely released.
    mock_channel_manager = MagicMock()
    mock_channel_manager.stop_all = AsyncMock(side_effect=RuntimeError("boom"))
    server._channel_manager = mock_channel_manager

    mock_pid_lock = MagicMock()
    server._pid_lock = mock_pid_lock

    mock_registry = MagicMock()
    mock_registry.broadcast = AsyncMock()
    mock_registry.all = MagicMock(return_value=[])

    with patch("opensquilla.gateway.boot.get_registry", return_value=mock_registry):
        with pytest.raises(RuntimeError, match="boom"):
            await server.close(reason="test")

    assert fake_server.should_exit is True  # server stop ran despite the failure
    mock_services.close.assert_not_awaited()
    mock_pid_lock.release.assert_not_called()
    assert server._task.done()  # serve task awaited, not leaked


@pytest.mark.asyncio
async def test_close_bounds_cancellation_resistant_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot

    monkeypatch.setattr(boot, "_WS_SHUTDOWN_CLOSE_TIMEOUT_S", 0.02)
    monkeypatch.setattr(boot, "_WS_SHUTDOWN_CANCEL_GRACE_S", 0.01)
    connection = _CancellationResistantConnection()
    server, fake_server, mock_services, mock_pid_lock = _gateway_server_for_ws_close_test()
    mock_registry = MagicMock()
    mock_registry.broadcast = AsyncMock()
    mock_registry.all.return_value = [connection]

    try:
        with patch("opensquilla.gateway.boot.get_registry", return_value=mock_registry):
            await asyncio.wait_for(server.close(reason="test"), timeout=0.5)

        assert connection.started.is_set()
        assert connection.cancelled.is_set()
        assert fake_server.should_exit is True
        mock_services.close.assert_awaited_once()
        mock_pid_lock.release.assert_called_once()
    finally:
        connection.release.set()
        if connection.started.is_set():
            await asyncio.wait_for(connection.finished.wait(), timeout=0.5)
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_close_uses_one_shared_websocket_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot

    monkeypatch.setattr(boot, "_WS_SHUTDOWN_CLOSE_TIMEOUT_S", 0.25)
    monkeypatch.setattr(boot, "_WS_SHUTDOWN_CANCEL_GRACE_S", 0.01)
    blocked_connections = [_CancellationResistantConnection() for _ in range(10)]
    immediate_connection = _ImmediateConnection()
    server, fake_server, mock_services, mock_pid_lock = _gateway_server_for_ws_close_test()
    mock_registry = MagicMock()
    mock_registry.broadcast = AsyncMock()
    mock_registry.all.return_value = [*blocked_connections, immediate_connection]
    close_task: asyncio.Task[None] | None = None

    try:
        with patch("opensquilla.gateway.boot.get_registry", return_value=mock_registry):
            close_task = asyncio.create_task(server.close(reason="test"))
            await asyncio.wait_for(
                asyncio.gather(*(conn.started.wait() for conn in blocked_connections)),
                timeout=1.0,
            )
            await asyncio.wait_for(close_task, timeout=2.0)

        assert immediate_connection.closed is True
        assert all(conn.cancelled.is_set() for conn in blocked_connections)
        assert fake_server.should_exit is True
        mock_services.close.assert_awaited_once()
        mock_pid_lock.release.assert_called_once()
    finally:
        for connection in blocked_connections:
            connection.release.set()
        if close_task is not None and not close_task.done():
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
        started_connections = [
            connection for connection in blocked_connections if connection.started.is_set()
        ]
        if started_connections:
            await asyncio.wait_for(
                asyncio.gather(
                    *(connection.finished.wait() for connection in started_connections)
                ),
                timeout=1.0,
            )
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_close_cancellation_detaches_websocket_tasks_and_runs_finally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot

    monkeypatch.setattr(boot, "_WS_SHUTDOWN_CLOSE_TIMEOUT_S", 10.0)
    connection = _CancellationResistantConnection()
    server, fake_server, mock_services, mock_pid_lock = _gateway_server_for_ws_close_test()
    mock_registry = MagicMock()
    mock_registry.broadcast = AsyncMock()
    mock_registry.all.return_value = [connection]
    close_task: asyncio.Task[None] | None = None

    try:
        with patch("opensquilla.gateway.boot.get_registry", return_value=mock_registry):
            close_task = asyncio.create_task(server.close(reason="test"))
            await asyncio.wait_for(connection.started.wait(), timeout=0.5)
            close_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close_task

        assert connection.cancelled.is_set()
        assert fake_server.should_exit is True
        mock_services.close.assert_awaited_once()
        mock_pid_lock.release.assert_not_called()
    finally:
        connection.release.set()
        if close_task is not None and not close_task.done():
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
        if connection.started.is_set():
            await asyncio.wait_for(connection.finished.wait(), timeout=0.5)
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_close_preserves_connection_cancellation_semantics() -> None:
    connection = _SelfCancellingConnection()
    server, fake_server, mock_services, mock_pid_lock = _gateway_server_for_ws_close_test()
    mock_registry = MagicMock()
    mock_registry.broadcast = AsyncMock()
    mock_registry.all.return_value = [connection]

    with patch("opensquilla.gateway.boot.get_registry", return_value=mock_registry):
        with pytest.raises(asyncio.CancelledError):
            await server.close(reason="test")

    assert fake_server.should_exit is True
    mock_services.close.assert_awaited_once()
    mock_pid_lock.release.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_present", [False, True])
async def test_channel_timeout_keeps_live_dependencies_and_process_ownership(
    runtime_present: bool,
) -> None:
    from opensquilla.gateway.task_runtime import TaskRuntimeShutdownResult

    server, fake_server, services, pid_lock = _gateway_server_for_ws_close_test()
    previous = TaskRuntimeShutdownResult(
        clean=True,
        elapsed_ms=123,
        abandoned_task_count=2,
        remaining_driver_count=0,
        remaining_reservation_count=0,
        remaining_auxiliary_count=0,
    )
    if runtime_present:
        services.task_runtime = SimpleNamespace(shutdown=AsyncMock(return_value=previous))
    server._channel_manager = SimpleNamespace(
        stop_all=AsyncMock(side_effect=TimeoutError("worker still owns SQL"))
    )
    registry = MagicMock()
    registry.broadcast = AsyncMock()
    registry.all.return_value = []
    with (
        patch("opensquilla.gateway.boot.get_registry", return_value=registry),
        patch("opensquilla.mcp.discovery.close_active_clients", new_callable=AsyncMock) as mcp,
    ):
        result = await server.close(reason="test")

    assert result is not None and result.clean is False
    if runtime_present:
        assert result.elapsed_ms == 123
        assert result.abandoned_task_count == 2
        assert previous.clean is True
    services.close.assert_not_awaited()
    mcp.assert_not_awaited()
    pid_lock.release.assert_not_called()
    assert server._pid_lock is pid_lock
    assert fake_server.should_exit is True
    assert server._task.done()
    registry.broadcast.assert_awaited_once_with("shutdown", {"reason": "test"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("total", "runtime_elapsed", "background_elapsed", "expected_background", "expected_channel"),
    [(75.0, 20.0, 10.0, 30.0, 32.95), (20.0, 18.0, 2.0, 2.0, 0.0)],
)
async def test_channel_drain_uses_remaining_gateway_deadline(
    monkeypatch: pytest.MonkeyPatch,
    total: float,
    runtime_elapsed: float,
    background_elapsed: float,
    expected_background: float,
    expected_channel: float,
) -> None:
    from opensquilla.gateway import boot

    # Advance a local logical clock at completed stages. Do not patch the
    # event loop's clock or infer the contract from short wall-clock sleeps.
    clock = [100.0]
    monkeypatch.setattr(boot, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(boot, "gateway_shutdown_deadline", lambda: total)
    monkeypatch.setattr(boot, "gateway_graceful_timeout", lambda: 30.0)
    server, _, services, _ = _gateway_server_for_ws_close_test()

    async def runtime_shutdown(*, graceful: bool, graceful_timeout: float) -> None:
        assert graceful is True
        assert graceful_timeout == min(30.0, total)
        clock[0] += runtime_elapsed

    async def background_close(*, timeout: float) -> None:
        assert timeout == expected_background
        clock[0] += background_elapsed

    services.task_runtime = SimpleNamespace(shutdown=runtime_shutdown)
    server._background_completion_manager = SimpleNamespace(close=background_close)
    manager = SimpleNamespace(stop_all=AsyncMock())
    server._channel_manager = manager
    registry = MagicMock()
    registry.broadcast = AsyncMock()
    registry.all.return_value = []
    with (
        patch("opensquilla.gateway.boot.get_registry", return_value=registry),
        patch(
            "opensquilla.gateway.boot._close_gateway_websocket_connections",
            new_callable=AsyncMock,
        ) as close_ws,
    ):
        await server.close(reason="test")

    manager.stop_all.assert_awaited_once_with(timeout=pytest.approx(expected_channel))
    close_ws.assert_awaited_once_with([], deadline=100.0 + total)


@pytest.mark.parametrize(
    "writer_field",
    ["router_decision_writer", "turn_error_writer"],
)
@pytest.mark.asyncio
async def test_service_close_does_not_block_event_loop_on_sidecar_writer(
    writer_field: str,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class _BlockingWriter:
        def close(self) -> None:
            started.set()
            release.wait(timeout=1.0)

    services = ServiceContainer(
        config=GatewayConfig(),
        **{writer_field: _BlockingWriter()},
    )
    release_timer = threading.Timer(0.4, release.set)
    close_task = asyncio.create_task(services.close())
    release_timer.start()
    try:
        await asyncio.sleep(0.05)
        assert started.is_set()
        assert not close_task.done()
        release.set()
        await asyncio.wait_for(close_task, timeout=1.0)
    finally:
        release_timer.cancel()
        release.set()
        await close_task


@pytest.mark.asyncio
async def test_service_close_cancels_deferred_warmup_before_catalog_coordinator() -> None:
    warmup_started = asyncio.Event()
    warmup_cancelled = asyncio.Event()
    close_observations: list[bool] = []

    async def _warmup() -> None:
        warmup_started.set()
        try:
            await asyncio.Future()
        finally:
            warmup_cancelled.set()

    class _Coordinator:
        async def close(self) -> None:
            close_observations.append(warmup_cancelled.is_set())

    warmup_task = asyncio.create_task(_warmup())
    await warmup_started.wait()
    services = ServiceContainer(
        config=GatewayConfig(),
        model_catalog_refresh_coordinator=_Coordinator(),
        deferred_warmup_task=warmup_task,
    )

    await services.close()

    assert warmup_task.cancelled()
    assert close_observations == [True]


@pytest.mark.asyncio
async def test_service_close_drains_installer_before_shared_catalog(tmp_path: Path) -> None:
    from opensquilla.skills.hub.operations import run_operation_worker
    from tests.test_skills.test_install_operation_io import _service

    management = _service(tmp_path)
    operations = management.install_operations
    entered, release = threading.Event(), threading.Event()
    order: list[str] = []
    operation_id = str(uuid.uuid4())

    def work():
        entered.set()
        assert release.wait(5)
        order.append("worker_finished")

    async def install():
        await run_operation_worker(work)
        return {"success": True}

    class Catalog:
        async def close(self):
            receipt = operations.store.read_existing("owner", operation_id)
            assert receipt["state"] == "cancelled"
            order.append("catalog_closed")

    services = ServiceContainer(
        config=GatewayConfig(), skill_management_service=management,
        model_catalog_refresh_coordinator=Catalog(),
    )
    waiter = asyncio.create_task(operations.run("owner", operation_id, {}, install))
    try:
        while not entered.is_set():
            await asyncio.sleep(.001)
        closing = asyncio.create_task(services.close())
        await asyncio.sleep(.01)
        assert not closing.done() and not order
    finally:
        release.set()
    await closing
    assert (await waiter)["cancelled"]
    assert order == ["worker_finished", "catalog_closed"]
    await services.close()


@pytest.mark.asyncio
async def test_gateway_keeps_resources_and_profile_lease_for_unsettled_install(
    tmp_path: Path,
) -> None:
    from tests.test_skills.test_install_operation_io import _service

    server, _, services, _ = _gateway_server_for_ws_close_test()
    management = _service(tmp_path)
    management.close = AsyncMock(return_value=False)
    services.skill_management_service = management
    server._pid_lock = MagicMock()
    registry = MagicMock()
    registry.broadcast = AsyncMock()
    registry.all.return_value = []
    with patch("opensquilla.gateway.boot.get_registry", return_value=registry):
        result = await server.close()
    assert result is not None and not result.clean
    services.close.assert_not_awaited()
    server._pid_lock.release.assert_not_called()
    management.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_gateway_close_keeps_install_profile_lease(tmp_path: Path) -> None:
    from tests.test_skills.test_install_operation_io import _service

    server, _, services, _ = _gateway_server_for_ws_close_test()
    management = _service(tmp_path)
    entered = asyncio.Event()

    async def blocked_close(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    management.close = blocked_close
    services.skill_management_service = management
    server._pid_lock = MagicMock()
    closer = asyncio.create_task(server.close())
    await asyncio.wait_for(entered.wait(), timeout=5)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    services.close.assert_not_awaited()
    server._pid_lock.release.assert_not_called()
