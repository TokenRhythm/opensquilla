from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.datastructures import URL, Headers
from starlette.websockets import WebSocketDisconnect, WebSocketState

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.websocket import handle_ws_connection


class _DisconnectingChallengeWebSocket:
    client_state = WebSocketState.CONNECTED
    client = SimpleNamespace(host="127.0.0.1", port=12345)

    def __init__(self) -> None:
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, _text: str) -> None:
        raise WebSocketDisconnect(code=1006)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


class _OriginWebSocket(_DisconnectingChallengeWebSocket):
    def __init__(self, origin: str | None) -> None:
        super().__init__()
        self.headers = Headers({"Origin": origin} if origin is not None else {})
        self.url = URL("ws://127.0.0.1:18791/ws")
        self.challenge_attempted = False
        self.close_code: int | None = None

    async def send_text(self, _text: str) -> None:
        self.challenge_attempted = True
        raise WebSocketDisconnect(code=1006)


@pytest.mark.asyncio
async def test_websocket_handshake_ignores_disconnect_while_sending_challenge() -> None:
    ws = _DisconnectingChallengeWebSocket()

    await handle_ws_connection(ws, GatewayConfig(), dispatcher=object())

    assert ws.accepted is True


@pytest.mark.parametrize(
    "origin",
    [
        "https://attacker.example",
        "null",
        "http://127.0.0.1:8080",
        "http://user@127.0.0.1:18791",
        "http://127.0.0.1:18791/path",
        "not an origin",
    ],
)
@pytest.mark.asyncio
async def test_websocket_rejects_invalid_origin_before_accept_or_challenge(
    origin: str,
) -> None:
    ws = _OriginWebSocket(origin)

    await handle_ws_connection(ws, GatewayConfig(), dispatcher=object())

    assert ws.accepted is False
    assert ws.challenge_attempted is False
    assert ws.close_code == 1008


@pytest.mark.asyncio
async def test_websocket_accepts_http_origin_matching_ws_endpoint() -> None:
    ws = _OriginWebSocket("http://127.0.0.1:18791")

    await handle_ws_connection(ws, GatewayConfig(), dispatcher=object())

    assert ws.accepted is True
    assert ws.challenge_attempted is True
    assert ws.close_code is None


@pytest.mark.asyncio
async def test_websocket_accepts_exact_configured_origin_but_not_wildcard() -> None:
    allowed_ws = _OriginWebSocket("https://frontend.example")
    wildcard_ws = _OriginWebSocket("https://attacker.example")
    allowed_config = GatewayConfig()
    allowed_config.cors.allowed_origins = ["https://frontend.example"]
    wildcard_config = GatewayConfig()
    wildcard_config.cors.allowed_origins = ["*"]

    await handle_ws_connection(allowed_ws, allowed_config, dispatcher=object())
    await handle_ws_connection(wildcard_ws, wildcard_config, dispatcher=object())

    assert allowed_ws.accepted is True
    assert allowed_ws.challenge_attempted is True
    assert wildcard_ws.accepted is False
    assert wildcard_ws.challenge_attempted is False
    assert wildcard_ws.close_code == 1008


@pytest.mark.asyncio
async def test_websocket_matching_host_and_origin_cannot_rebind_loopback_gateway() -> None:
    ws = _OriginWebSocket("http://evil.example:18791")
    ws.url = URL("ws://evil.example:18791/ws")

    await handle_ws_connection(ws, GatewayConfig(), dispatcher=object())

    assert ws.accepted is False
    assert ws.challenge_attempted is False
    assert ws.close_code == 1008


@pytest.mark.asyncio
async def test_websocket_wildcard_bind_rejects_unconfigured_hostname_from_remote_peer() -> None:
    ws = _OriginWebSocket("http://rebinding.example:18791")
    ws.url = URL("ws://rebinding.example:18791/ws")
    ws.client = SimpleNamespace(host="192.0.2.20", port=12345)
    ws.headers = Headers(
        {
            "Origin": "http://rebinding.example:18791",
            "X-Forwarded-For": "127.0.0.1",
        }
    )
    config = GatewayConfig()
    config.host = "0.0.0.0"

    await handle_ws_connection(ws, config, dispatcher=object())

    assert ws.accepted is False
    assert ws.challenge_attempted is False
    assert ws.close_code == 1008


@pytest.mark.asyncio
async def test_websocket_wildcard_bind_accepts_same_origin_ip_authority() -> None:
    ws = _OriginWebSocket("http://192.0.2.10:18791")
    ws.url = URL("ws://192.0.2.10:18791/ws")
    ws.client = SimpleNamespace(host="192.0.2.20", port=12345)
    config = GatewayConfig()
    config.host = "0.0.0.0"

    await handle_ws_connection(ws, config, dispatcher=object())

    assert ws.accepted is True
    assert ws.challenge_attempted is True
    assert ws.close_code is None
