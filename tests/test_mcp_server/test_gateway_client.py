from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from opensquilla.gateway_client import GatewayRPCClient, normalize_gateway_url


class _SilentWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        self.closed = True


def test_normalize_gateway_url_preserves_query_and_fragment() -> None:
    assert (
        normalize_gateway_url("https://gateway.example.com/ws?token=abc#trace")
        == "wss://gateway.example.com/ws?token=abc#trace"
    )


def test_normalize_gateway_url_adds_ws_path_without_dropping_query() -> None:
    assert normalize_gateway_url("gateway.example.com?token=abc") == "ws://gateway.example.com/ws?token=abc"


@pytest.mark.asyncio
async def test_gateway_rpc_call_times_out_and_clears_pending_request() -> None:
    client = GatewayRPCClient(request_timeout_s=0.01)
    client._ws = _SilentWebSocket()

    with pytest.raises(TimeoutError, match="sessions.list timed out"):
        await client.call("sessions.list", {"limit": 1})

    assert client._pending == {}


@pytest.mark.asyncio
async def test_session_history_supports_optional_cursors_without_changing_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayRPCClient()
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {"messages": []}

    monkeypatch.setattr(client, "call", fake_call)

    await client.session_history("agent:main:test", limit=5)
    await client.session_history(
        "agent:main:test",
        limit=25,
        before="12|12",
        include_canonical=True,
        include_summaries=False,
    )

    assert calls == [
        ("chat.history", {"sessionKey": "agent:main:test", "limit": 5}),
        (
            "chat.history",
            {
                "sessionKey": "agent:main:test",
                "limit": 25,
                "before": "12|12",
                "includeCanonical": True,
                "includeSummaries": False,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_gateway_connect_closes_socket_after_bad_handshake(monkeypatch) -> None:
    class BadHandshakeWebSocket(_SilentWebSocket):
        async def recv(self) -> str:
            return json.dumps({"type": "event", "event": "unexpected"})

    ws = BadHandshakeWebSocket()

    async def connect(_url: str):
        return ws

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    client = GatewayRPCClient()

    with pytest.raises(RuntimeError, match="Unexpected gateway handshake frame"):
        await client.connect("ws://127.0.0.1:18791/ws")

    assert ws.closed is True
    assert client._ws is None
