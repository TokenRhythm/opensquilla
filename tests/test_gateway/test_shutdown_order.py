"""Tests for graceful shutdown ordering (AC-M3)."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opensquilla.gateway.boot import GatewayServer, ServiceContainer
from opensquilla.gateway.config import GatewayConfig


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
