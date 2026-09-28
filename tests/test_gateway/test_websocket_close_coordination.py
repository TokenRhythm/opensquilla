"""WebSocket shutdown races must converge on one clean transport close."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from starlette.websockets import WebSocketState

import opensquilla.gateway.websocket as websocket_module
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.protocol import make_ok_res
from opensquilla.gateway.websocket import handle_ws_connection

_CONNECT_FRAME = json.dumps(
    {
        "type": "req",
        "id": "connect",
        "method": "connect",
        "params": {"minProtocol": 1, "role": "operator", "auth": {}},
    }
)
_NOOP_FRAME = json.dumps(
    {
        "type": "req",
        "id": "slow-response",
        "method": "noop",
        "params": {},
    }
)


class _EchoDispatcher:
    def list_methods(self) -> list[str]:
        return ["noop"]

    async def dispatch(self, req_id: str, method: str, params: Any, ctx: Any) -> Any:
        return make_ok_res(req_id, {"method": method, "params": params})


class _TimeoutDuringWriterWebSocket:
    """Expose the wire-order race between a keepalive close and an active send."""

    client = SimpleNamespace(host="127.0.0.1", port=12345)

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self._frames = [_CONNECT_FRAME, _NOOP_FRAME]
        self._writer_started = asyncio.Event()
        self._socket_closed = asyncio.Event()
        self.sent: list[str] = []
        self.close_codes: list[int] = []
        self.close_reasons: list[str] = []
        self.lifecycle: list[str] = []

    async def accept(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        frame = json.loads(text)
        if frame.get("type") == "res" and frame.get("id") == "slow-response":
            self.lifecycle.append("writer_started")
            self._writer_started.set()
            try:
                await self._socket_closed.wait()
            except asyncio.CancelledError:
                self.lifecycle.append("writer_cancelled")
                raise
            self.lifecycle.append("writer_send_after_close")
            raise RuntimeError('Cannot call "send" once a close message has been sent.')
        self.sent.append(text)

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        await self._writer_started.wait()
        await asyncio.Future()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.lifecycle.append("socket_close")
        self.close_codes.append(code)
        self.close_reasons.append(reason)
        self.client_state = WebSocketState.DISCONNECTED
        self.application_state = WebSocketState.DISCONNECTED
        self._socket_closed.set()
        await asyncio.sleep(0)


class _WriterFailureClosesReceiveWebSocket:
    """Turn a writer failure into Starlette's close-state receive error."""

    client = SimpleNamespace(host="127.0.0.1", port=12345)

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self._frames = [_CONNECT_FRAME, _NOOP_FRAME]
        self._receive_waiting = asyncio.Event()
        self._socket_closed = asyncio.Event()
        self.sent: list[str] = []
        self.close_codes: list[int] = []
        self.close_reasons: list[str] = []

    async def accept(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        frame = json.loads(text)
        if frame.get("type") == "res" and frame.get("id") == "slow-response":
            await self._receive_waiting.wait()
            raise RuntimeError("synthetic writer send failure")
        self.sent.append(text)

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        self._receive_waiting.set()
        await self._socket_closed.wait()
        raise RuntimeError('WebSocket is not connected. Need to call "accept" first.')

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_codes.append(code)
        self.close_reasons.append(reason)
        self.client_state = WebSocketState.DISCONNECTED
        self.application_state = WebSocketState.DISCONNECTED
        self._socket_closed.set()


class _ConnectedReceiveFailureWebSocket:
    """Raise a receive error without entering any transport closing state."""

    client = SimpleNamespace(host="127.0.0.1", port=12345)

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self._frames = [_CONNECT_FRAME]
        self.sent: list[str] = []

    async def accept(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        raise RuntimeError("synthetic connected receive failure")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason
        self.client_state = WebSocketState.DISCONNECTED
        self.application_state = WebSocketState.DISCONNECTED


async def test_keepalive_timeout_stops_active_writer_before_socket_close() -> None:
    ws = _TimeoutDuringWriterWebSocket()
    config = GatewayConfig(
        client_ws_keepalive_timeout_s=0.03,
        ws_writer_queue_enabled=True,
    )

    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(
            handle_ws_connection(ws, config, dispatcher=_EchoDispatcher()),
            timeout=1.0,
        )

    events = [entry.get("event") for entry in logs]
    assert ws.close_codes == [1011]
    assert ws.close_reasons == [""]
    assert ws.lifecycle.index("writer_cancelled") < ws.lifecycle.index("socket_close")
    assert events.count("gateway.client_ws_keepalive_timeout") == 1
    assert "gateway.ws_writer_send_failed" not in events
    assert "ws.error" not in events


async def test_writer_close_state_receive_error_is_normal_teardown() -> None:
    ws = _WriterFailureClosesReceiveWebSocket()
    config = GatewayConfig(
        client_ws_keepalive_timeout_s=1.0,
        ws_writer_queue_enabled=True,
    )

    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(
            handle_ws_connection(ws, config, dispatcher=_EchoDispatcher()),
            timeout=1.0,
        )

    events = [entry.get("event") for entry in logs]
    assert ws.close_codes == [1011]
    assert ws.close_reasons == ["writer_send_failed"]
    assert events.count("gateway.ws_writer_send_failed") == 1
    assert "ws.error" not in events


async def test_connected_receive_runtime_error_remains_visible() -> None:
    ws = _ConnectedReceiveFailureWebSocket()
    config = GatewayConfig(
        client_ws_keepalive_timeout_s=1.0,
        ws_writer_queue_enabled=True,
    )

    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(
            handle_ws_connection(ws, config, dispatcher=_EchoDispatcher()),
            timeout=1.0,
        )

    errors = [entry for entry in logs if entry.get("event") == "ws.error"]
    assert len(errors) == 1
    assert errors[0].get("error") == "synthetic connected receive failure"


@pytest.mark.parametrize("fallback", ["capacity", "timeout"])
async def test_handler_close_fallback_preserves_awaited_teardown(
    monkeypatch: pytest.MonkeyPatch,
    fallback: str,
) -> None:
    """A fallback from the reader itself cannot cancel its own cleanup await."""
    finished = asyncio.Event()

    class SlowDispatcher(_EchoDispatcher):
        async def dispatch(self, req_id: str, method: str, params: Any, ctx: Any) -> Any:
            await asyncio.sleep(0.08)
            finished.set()
            return make_ok_res(req_id, {})

    class BlockedCloseSocket(_TimeoutDuringWriterWebSocket):
        async def close(self, code: int = 1000, reason: str = "") -> None:
            self.close_codes.append(code)
            await asyncio.Future()

    ws = BlockedCloseSocket()
    registry = websocket_module.ConnectionRegistry()
    monkeypatch.setattr(websocket_module, "get_registry", lambda: registry)
    monkeypatch.setattr(websocket_module, "_DIRECT_CLOSE_TIMEOUT_SECONDS", 0.01)
    current = asyncio.current_task()
    assert current is not None
    monkeypatch.setattr(websocket_module, "_MAX_WRITER_TASKS", 1)
    monkeypatch.setattr(
        websocket_module, "_SOCKET_CLOSE_TASKS", {current} if fallback == "capacity" else set(),
    )
    cleaned: list[str] = []
    removed: list[str] = []
    original_cleanup = websocket_module.WsConnection._cleanup_transport

    def record_cleanup(conn: websocket_module.WsConnection) -> None:
        original_cleanup(conn)
        cleaned.append(conn.conn_id)
        assert conn._transport_bytes == 0

    monkeypatch.setattr(websocket_module.WsConnection, "_cleanup_transport", record_cleanup)
    task = asyncio.create_task(handle_ws_connection(
        ws,
        GatewayConfig(client_ws_keepalive_timeout_s=0.01, ws_writer_queue_enabled=True),
        dispatcher=SlowDispatcher(),
        subscription_manager=SimpleNamespace(remove_connection=removed.append),
    ))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)

    assert finished.is_set()
    assert registry.all() == []
    assert len(cleaned) == 1
    assert removed == cleaned
    assert task.cancelled()


@pytest.mark.parametrize("fallback", ["capacity", "timeout"])
async def test_direct_send_timeout_retires_handler_and_tracks_resistant_close(
    monkeypatch: pytest.MonkeyPatch,
    fallback: str,
) -> None:
    """A legacy worker timeout must retire the idle reader and its registry entry."""
    release_close = asyncio.Event()

    class BlockedDirectSocket(_WriterFailureClosesReceiveWebSocket):
        async def send_text(self, text: str) -> None:
            frame = json.loads(text)
            if frame.get("id") == "slow-response":
                await self._receive_waiting.wait()
                await asyncio.Future()
            self.sent.append(text)

        async def close(self, code: int = 1000, reason: str = "") -> None:
            self.close_codes.append(code)
            self.close_reasons.append(reason)
            try:
                await release_close.wait()
            except asyncio.CancelledError:
                await release_close.wait()

    before = websocket_module.get_transport_budget().used
    registry = websocket_module.ConnectionRegistry()
    monkeypatch.setattr(websocket_module, "get_registry", lambda: registry)
    monkeypatch.setattr(websocket_module, "_DIRECT_SEND_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(websocket_module, "_DIRECT_CLOSE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(websocket_module, "_MAX_WRITER_TASKS", 1)
    current = asyncio.current_task()
    assert current is not None
    occupied = {current} if fallback == "capacity" else set()
    monkeypatch.setattr(websocket_module, "_SOCKET_CLOSE_TASKS", occupied)
    removed: list[str] = []
    ws = BlockedDirectSocket()
    task = asyncio.create_task(handle_ws_connection(
        ws,
        GatewayConfig(ws_writer_queue_enabled=False, client_ws_keepalive_timeout_s=0),
        dispatcher=_EchoDispatcher(),
        subscription_manager=SimpleNamespace(remove_connection=removed.append),
    ))
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
        assert task.cancelled()
        assert registry.all() == []
        assert len(removed) == 1
        assert websocket_module.get_transport_budget().used == before
        if fallback == "capacity":
            assert websocket_module._SOCKET_CLOSE_TASKS == {current}
            assert ws.close_reasons == []
        else:
            assert ws.close_reasons == ["direct_send_timeout"]
            assert len(websocket_module._SOCKET_CLOSE_TASKS) == 1
    finally:
        release_close.set()
        close_tasks = websocket_module._SOCKET_CLOSE_TASKS - {current}
        await asyncio.gather(
            *close_tasks,
            return_exceptions=True,
        )
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert websocket_module._SOCKET_CLOSE_TASKS == ({current} if fallback == "capacity" else set())
