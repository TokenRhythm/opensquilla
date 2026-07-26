"""Malformed client frames must produce INVALID_REQUEST, not kill the connection.

Regression surface: a ``req`` frame whose ``id`` is a JSON number used to pass
straight into ``ResFrame(id=...)`` construction, raise a pydantic
``ValidationError`` after the handler had already run, and tear down the whole
WebSocket connection (``ws.error`` + close) instead of answering the one bad
frame.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from starlette.websockets import WebSocketDisconnect, WebSocketState

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.protocol import make_ok_res
from opensquilla.gateway.websocket import WsConnection, handle_ws_connection

_CONNECT_FRAME = json.dumps(
    {
        "type": "req",
        "id": "h",
        "method": "connect",
        "params": {"minProtocol": 1, "role": "operator", "auth": {}},
    }
)


class _ScriptedWebSocket:
    client_state = WebSocketState.CONNECTED
    client = SimpleNamespace(host="127.0.0.1", port=12345)

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.sent: list[str] = []
        self.close_codes: list[int] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def receive_text(self) -> str:
        if not self._frames:
            raise WebSocketDisconnect(code=1000)
        return self._frames.pop(0)

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)

    def responses(self) -> list[dict[str, Any]]:
        return [f for f in (json.loads(s) for s in self.sent) if f.get("type") == "res"]


class _EchoDispatcher:
    def list_methods(self) -> list[str]:
        return ["noop"]

    async def dispatch(self, req_id: str, method: str, params: Any, ctx: Any) -> Any:
        return make_ok_res(req_id, {"method": method})


def _config() -> GatewayConfig:
    # Direct-send path keeps response ordering deterministic in the fake.
    return GatewayConfig(ws_writer_queue_enabled=False)


async def _run(frames: list[str]) -> _ScriptedWebSocket:
    ws = _ScriptedWebSocket(frames)
    await handle_ws_connection(ws, _config(), dispatcher=_EchoDispatcher())
    return ws


async def test_non_string_req_id_gets_error_res_and_connection_survives() -> None:
    ws = await _run(
        [
            _CONNECT_FRAME,
            json.dumps({"type": "req", "id": 1, "method": "noop", "params": {}}),
            json.dumps({"type": "req", "id": "ok1", "method": "noop"}),
        ]
    )

    responses = ws.responses()
    errors = [r for r in responses if not r["ok"]]
    assert len(errors) == 1
    assert errors[0]["id"] == "1"
    assert errors[0]["error"]["code"] == "INVALID_REQUEST"

    # The connection outlived the bad frame: the next request still dispatched.
    assert any(r["ok"] and r["id"] == "ok1" for r in responses)
    assert ws.close_codes == []


async def test_non_string_method_gets_error_res_and_connection_survives() -> None:
    ws = await _run(
        [
            _CONNECT_FRAME,
            json.dumps({"type": "req", "id": "x", "method": 5}),
            json.dumps({"type": "req", "id": "ok2", "method": "noop"}),
        ]
    )

    responses = ws.responses()
    errors = [r for r in responses if not r["ok"]]
    assert len(errors) == 1
    assert errors[0]["id"] == "x"
    assert errors[0]["error"]["code"] == "INVALID_REQUEST"
    assert any(r["ok"] and r["id"] == "ok2" for r in responses)
    assert ws.close_codes == []


async def test_non_object_frame_gets_error_res_and_connection_survives() -> None:
    ws = await _run(
        [
            _CONNECT_FRAME,
            json.dumps([1, 2, 3]),
            json.dumps({"type": "req", "id": "ok3", "method": "noop"}),
        ]
    )

    responses = ws.responses()
    errors = [r for r in responses if not r["ok"]]
    assert len(errors) == 1
    assert errors[0]["id"] == ""
    assert errors[0]["error"]["code"] == "INVALID_REQUEST"
    assert any(r["ok"] and r["id"] == "ok3" for r in responses)
    assert ws.close_codes == []


async def test_oversized_int_literal_gets_error_res_and_connection_survives() -> None:
    # CPython's int-conversion limit makes json.loads raise a plain
    # ValueError (not JSONDecodeError) for a 5000-digit number literal.
    raw = '{"type": "req", "id": ' + "9" * 5000 + ', "method": "noop"}'
    ws = await _run(
        [
            _CONNECT_FRAME,
            raw,
            json.dumps({"type": "req", "id": "ok4", "method": "noop"}),
        ]
    )

    responses = ws.responses()
    errors = [r for r in responses if not r["ok"]]
    assert len(errors) == 1
    assert errors[0]["error"]["code"] == "INVALID_REQUEST"
    assert any(r["ok"] and r["id"] == "ok4" for r in responses)
    assert ws.close_codes == []


async def test_deeply_nested_frame_gets_error_res_and_connection_survives() -> None:
    # Valid JSON, but deeper than the parser's recursion budget.
    raw = "[" * 100_000 + "]" * 100_000
    ws = await _run(
        [
            _CONNECT_FRAME,
            raw,
            json.dumps({"type": "req", "id": "ok5", "method": "noop"}),
        ]
    )

    responses = ws.responses()
    errors = [r for r in responses if not r["ok"]]
    assert len(errors) == 1
    assert errors[0]["error"]["code"] == "INVALID_REQUEST"
    assert any(r["ok"] and r["id"] == "ok5" for r in responses)
    assert ws.close_codes == []


async def test_lone_surrogate_id_rejected_and_connection_survives() -> None:
    # A lone surrogate survives json round-trips but cannot be re-serialized
    # into a response frame, so it must be rejected, not echoed.
    ws = await _run(
        [
            _CONNECT_FRAME,
            json.dumps({"type": "req", "id": "\ud800", "method": "noop"}),
            json.dumps({"type": "req", "id": "ok6", "method": "noop"}),
        ]
    )

    responses = ws.responses()
    errors = [r for r in responses if not r["ok"]]
    assert len(errors) == 1
    assert errors[0]["id"] == ""
    assert errors[0]["error"]["code"] == "INVALID_REQUEST"
    assert any(r["ok"] and r["id"] == "ok6" for r in responses)
    assert ws.close_codes == []


async def test_lone_surrogate_frame_type_error_still_serializes() -> None:
    ws = await _run(
        [
            _CONNECT_FRAME,
            json.dumps({"type": "\ud800"}),
            json.dumps({"type": "req", "id": "ok7", "method": "noop"}),
        ]
    )

    responses = ws.responses()
    errors = [r for r in responses if not r["ok"]]
    assert len(errors) == 1
    assert "Unknown frame type" in errors[0]["error"]["message"]
    assert any(r["ok"] and r["id"] == "ok7" for r in responses)
    assert ws.close_codes == []


async def test_unserializable_outbound_frame_closes_connection_not_zombifies() -> None:
    # With the writer queue enabled (the production default), a frame whose
    # payload cannot be serialized used to kill the writer task silently:
    # the socket stayed open, requests kept executing, and no response ever
    # left. The writer must close the connection instead.
    ws = _ScriptedWebSocket([])
    conn = WsConnection(conn_id="poisoned", ws=ws)
    conn._start_writer(maxsize=8, enabled=True)

    await conn.send_res(make_ok_res("x", {"v": "\ud800"}))
    for _ in range(200):
        if ws.close_codes:
            break
        await asyncio.sleep(0.005)

    assert ws.close_codes == [1011]
    await conn._stop_writer()


async def test_handshake_tolerates_non_dict_params_and_auth() -> None:
    connect = json.dumps(
        {
            "type": "req",
            "id": "h",
            "method": "connect",
            "params": {"auth": "bogus", "role": 7, "client": None},
        }
    )
    ws = await _run([connect, json.dumps({"type": "req", "id": "ok8", "method": "noop"})])

    # The handshake completed on defaults and the loop dispatched the
    # follow-up request instead of crashing on params_raw.get(...).
    assert any(r["ok"] and r["id"] == "ok8" for r in ws.responses())
    assert ws.close_codes == []

    connect_scalar_params = json.dumps(
        {"type": "req", "id": "h", "method": "connect", "params": "x"}
    )
    ws2 = await _run(
        [connect_scalar_params, json.dumps({"type": "req", "id": "ok9", "method": "noop"})]
    )
    assert any(r["ok"] and r["id"] == "ok9" for r in ws2.responses())
    assert ws2.close_codes == []


async def test_handshake_rejects_non_integer_protocol_bounds() -> None:
    connect = json.dumps(
        {
            "type": "req",
            "id": "h",
            "method": "connect",
            "params": {"minProtocol": "3", "maxProtocol": None},
        }
    )
    ws = await _run([connect])

    responses = ws.responses()
    assert len(responses) == 1
    assert responses[0]["ok"] is False
    assert responses[0]["id"] == "h"
    assert responses[0]["error"]["code"] == "INVALID_REQUEST"
    assert len(ws.close_codes) == 1


async def test_handshake_survives_oversized_int_literal() -> None:
    raw = '{"type": "req", "id": ' + "9" * 5000 + ', "method": "connect"}'
    ws = await _run([raw])

    responses = ws.responses()
    assert len(responses) == 1
    assert responses[0]["ok"] is False
    assert responses[0]["id"] == "handshake"
    assert responses[0]["error"]["code"] == "INVALID_REQUEST"
    assert len(ws.close_codes) == 1


async def test_handshake_rejects_non_connect_frame_with_numeric_id_gracefully() -> None:
    ws = await _run([json.dumps({"type": "req", "id": 7, "method": "chat.send"})])

    responses = ws.responses()
    assert len(responses) == 1
    assert responses[0]["ok"] is False
    assert responses[0]["id"] == "7"
    assert responses[0]["error"]["code"] == "INVALID_REQUEST"
    # Graceful protocol-level close, not an unhandled exception path.
    assert len(ws.close_codes) == 1


async def test_handshake_rejects_non_object_connect_frame() -> None:
    ws = await _run([json.dumps("not-an-object")])

    responses = ws.responses()
    assert len(responses) == 1
    assert responses[0]["ok"] is False
    assert responses[0]["id"] == "handshake"
    assert responses[0]["error"]["code"] == "INVALID_REQUEST"
    assert len(ws.close_codes) == 1
