"""A slow request must not block controls or retire sibling requests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from starlette.websockets import WebSocketDisconnect, WebSocketState

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.protocol import make_ok_res
from opensquilla.gateway.websocket import WsConnection, handle_ws_connection


class _Socket:
    client = SimpleNamespace(host="127.0.0.1", port=12345)

    def __init__(self) -> None:
        self.client_state = self.application_state = WebSocketState.CONNECTED
        self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.changed = asyncio.Condition()
        self.close_codes: list[int] = []

    async def accept(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        async with self.changed:
            self.sent.append(json.loads(text))
            self.changed.notify_all()

    async def receive_text(self) -> str:
        frame = await self.incoming.get()
        if frame is None:
            raise WebSocketDisconnect(1000)
        return json.dumps(frame)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_codes.append(code)
        self.client_state = self.application_state = WebSocketState.DISCONNECTED
        self.incoming.put_nowait(None)

    async def wait_frame(self, **fields: Any) -> dict[str, Any]:
        async with asyncio.timeout(2):
            async with self.changed:
                while True:
                    for frame in self.sent:
                        if all(frame.get(key) == value for key, value in fields.items()):
                            return frame
                    await self.changed.wait()

    def request(self, request_id: str, method: str) -> None:
        self.incoming.put_nowait({"type": "req", "id": request_id, "method": method, "params": {}})


class _Dispatcher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.order: list[str] = []

    def list_methods(self) -> list[str]:
        return ["slow", "next", "explode"]

    async def dispatch(self, req_id: str, method: str, params: Any, ctx: Any) -> Any:
        self.order.append(req_id)
        if method == "slow":
            self.started.set()
            await self.release.wait()
            self.finished.set()
        if method == "explode":
            raise ValueError("not for the client")
        return make_ok_res(req_id, {"method": method})


async def _start(ws: _Socket, dispatcher: _Dispatcher) -> asyncio.Task[None]:
    ws.incoming.put_nowait(
        {
            "type": "req",
            "id": "connect",
            "method": "connect",
            "params": {"minProtocol": 3, "maxProtocol": 3, "caps": ["transport.probe.v1"]},
        }
    )
    task = asyncio.create_task(handle_ws_connection(ws, GatewayConfig(), dispatcher=dispatcher))  # type: ignore[arg-type]
    hello = await ws.wait_frame(type="hello-ok")
    assert hello["policy"]["transport_probe_nonce"] is True
    assert hello["policy"]["client_ws_keepalive_timeout_ms"] == 0
    return task


async def test_probe_is_processed_during_slow_fifo_request_without_reordering() -> None:
    ws, dispatcher = _Socket(), _Dispatcher()
    task = await _start(ws, dispatcher)
    try:
        ws.request("slow", "slow")
        await dispatcher.started.wait()
        ws.request("next", "next")
        ws.incoming.put_nowait({"type": "ping", "nonce": "wake-1"})
        await ws.wait_frame(type="pong", nonce="wake-1")
        assert dispatcher.order == ["slow"]
        dispatcher.release.set()
        await ws.wait_frame(type="res", id="next")
        assert dispatcher.order == ["slow", "next"]
        assert ws.close_codes == []
    finally:
        dispatcher.release.set()
        ws.incoming.put_nowait(None)
        await asyncio.wait_for(task, 2)


async def test_fifo_full_rejects_before_execution_and_still_answers_probe() -> None:
    ws, dispatcher = _Socket(), _Dispatcher()
    task = await _start(ws, dispatcher)
    try:
        ws.request("slow", "slow")
        await dispatcher.started.wait()
        for index in range(9):
            ws.request(f"queued-{index}", "next")
        rejected = await ws.wait_frame(type="res", id="queued-8")
        assert rejected["ok"] is False
        assert rejected["error"]["accepted"] is False
        ws.incoming.put_nowait({"type": "ping", "nonce": "still-here"})
        await ws.wait_frame(type="pong", nonce="still-here")
        assert dispatcher.order == ["slow"]
    finally:
        dispatcher.release.set()
        ws.incoming.put_nowait(None)
        await asyncio.wait_for(task, 2)


async def test_disconnect_does_not_start_queued_work_or_cancel_running_mutation() -> None:
    ws, dispatcher = _Socket(), _Dispatcher()
    task = await _start(ws, dispatcher)
    ws.request("slow", "slow")
    await dispatcher.started.wait()
    ws.request("queued", "next")
    ws.incoming.put_nowait(None)
    await asyncio.wait_for(task, 2)
    assert dispatcher.order == ["slow"]
    assert not dispatcher.finished.is_set()
    dispatcher.release.set()
    await asyncio.wait_for(dispatcher.finished.wait(), 2)
    await asyncio.sleep(0)
    assert dispatcher.order == ["slow"]
    assert not any(frame.get("id") == "slow" for frame in ws.sent)


async def test_handler_failure_is_request_local_and_next_request_still_succeeds() -> None:
    ws, dispatcher = _Socket(), _Dispatcher()
    task = await _start(ws, dispatcher)
    try:
        ws.request("bad", "explode")
        bad = await ws.wait_frame(type="res", id="bad")
        assert bad["error"]["message"] == "Request failed"
        ws.request("next", "next")
        await ws.wait_frame(type="res", id="next", ok=True)
        assert ws.close_codes == []
    finally:
        ws.incoming.put_nowait(None)
        await asyncio.wait_for(task, 2)


def test_connection_transport_reservations_are_shared_and_cleanup_is_idempotent() -> None:
    from opensquilla.gateway.transport_flow import CONNECTION_BUFFER_BYTES, get_transport_budget

    budget = get_transport_budget()
    before = budget.used
    conn = WsConnection("budget", _Socket())  # type: ignore[arg-type]
    assert conn.reserve_transport_bytes(CONNECTION_BUFFER_BYTES)
    assert not conn.reserve_transport_bytes(1)
    conn.add_transport_cleanup(lambda: conn.release_transport_bytes(CONNECTION_BUFFER_BYTES))
    conn._cleanup_transport()
    conn._cleanup_transport()
    assert budget.used == before
