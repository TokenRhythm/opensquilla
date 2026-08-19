"""Tests for graceful shutdown ordering (AC-M3)."""

from __future__ import annotations

import asyncio
import threading
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
async def test_runtime_drained_before_channel_stop() -> None:
    """task_runtime.shutdown() must complete before channel_manager.stop_all()."""
    call_order: list[str] = []

    async def mock_runtime_shutdown(**kwargs: object) -> None:
        call_order.append("task_runtime.shutdown")

    async def mock_stop_all() -> None:
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
    stop + serve-task join + pid-lock release must run first — otherwise the
    uvicorn serve task is leaked ("Task was destroyed but it is pending").
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

    # The teardown step raises — close() must still stop the server and release
    # the pid lock (in finally) before the error propagates.
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
    mock_services.close.assert_awaited_once()
    mock_pid_lock.release.assert_called_once()  # pid lock always released
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
        mock_pid_lock.release.assert_called_once()
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
    mock_pid_lock.release.assert_called_once()


@pytest.mark.parametrize(
    "writer_field",
    ["meta_run_writer", "router_decision_writer", "turn_error_writer"],
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
