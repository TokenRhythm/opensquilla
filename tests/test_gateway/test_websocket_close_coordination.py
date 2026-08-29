"""WebSocket shutdown races must converge on one clean transport close."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import structlog
from starlette.websockets import WebSocketState

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
