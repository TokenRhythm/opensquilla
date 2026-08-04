from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call

import pytest

from opensquilla.contracts.gateway_transport import (
    GATEWAY_CLIENT_MAX_MESSAGE_BYTES,
    GATEWAY_CLIENT_MAX_QUEUE,
    GATEWAY_HISTORY_MAX_RESPONSE_BUDGET_BYTES,
    GATEWAY_HISTORY_RESPONSE_BUDGET_BYTES,
)
from opensquilla.gateway_client import GatewayRPCClient, GatewayRPCError, normalize_gateway_url


class _SilentWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        self.closed = True


class _HandshakeWebSocket(_SilentWebSocket):
    def __init__(self, methods: list[str] | None) -> None:
        super().__init__()
        hello: dict[str, Any] = {"type": "hello-ok", "policy": {}}
        if methods is not None:
            hello["features"] = {"methods": methods}
        self._recv_frames = [
            {"type": "event", "event": "connect.challenge"},
            hello,
        ]

    async def recv(self) -> str:
        return json.dumps(self._recv_frames.pop(0))

    def __aiter__(self) -> _HandshakeWebSocket:
        return self

    async def __anext__(self) -> str:
        await asyncio.Future()
        raise StopAsyncIteration


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
async def test_gateway_rpc_error_preserves_server_details() -> None:
    client = GatewayRPCClient()
    ws = _SilentWebSocket()
    client._ws = ws  # noqa: SLF001

    call_task = asyncio.create_task(client.call("chat.history.v2", {}))
    while not ws.sent:
        await asyncio.sleep(0)
    request_id = ws.sent[0]["id"]
    client._pending[request_id].set_result(  # noqa: SLF001
        {
            "ok": False,
            "error": {
                "code": "RESPONSE_BUDGET_EXCEEDED",
                "message": "response exceeds the requested byte budget",
                "details": {"byte_budget": 64 * 1024, "wire_bytes": 70 * 1024},
            },
        }
    )

    with pytest.raises(GatewayRPCError) as raised:
        await call_task

    assert raised.value.data == {
        "byte_budget": 64 * 1024,
        "wire_bytes": 70 * 1024,
    }


@pytest.mark.asyncio
async def test_session_history_supports_optional_cursors_without_changing_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayRPCClient()
    client._server_methods = frozenset()  # noqa: SLF001
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
async def test_session_history_prefers_advertised_bounded_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayRPCClient()
    client._server_methods = frozenset({"chat.history.v2"})  # noqa: SLF001
    rpc_call = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(client, "call", rpc_call)

    await client.session_history(
        "agent:main:test",
        limit=25,
        after="12|12",
        include_canonical=True,
        include_summaries=False,
    )

    rpc_call.assert_awaited_once_with(
        "chat.history.v2",
        {
            "sessionKey": "agent:main:test",
            "limit": 25,
            "after": "12|12",
            "includeCanonical": True,
            "includeSummaries": False,
            "maxResponseBytes": GATEWAY_HISTORY_RESPONSE_BUDGET_BYTES,
        },
    )


@pytest.mark.asyncio
async def test_session_history_explicit_noncanonical_view_uses_legacy_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayRPCClient()
    client._server_methods = frozenset({"chat.history.v2"})  # noqa: SLF001
    rpc_call = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(client, "call", rpc_call)

    await client.session_history(
        "agent:main:test",
        limit=25,
        after="12|12",
        include_canonical=False,
        include_summaries=False,
    )

    rpc_call.assert_awaited_once_with(
        "chat.history",
        {
            "sessionKey": "agent:main:test",
            "limit": 25,
            "after": "12|12",
            "includeCanonical": False,
            "includeSummaries": False,
        },
    )
    assert client._server_method_support("chat.history.v2") is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_session_history_retries_v2_at_bounded_max_without_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayRPCClient()
    rpc_call = AsyncMock(
        side_effect=[
            GatewayRPCError(
                "chat.history.v2",
                code="RESPONSE_BUDGET_EXCEEDED",
                message="retry with a larger bounded response",
            ),
            {"messages": []},
        ]
    )
    monkeypatch.setattr(client, "call", rpc_call)

    await client.session_history("agent:main:test", limit=25)

    assert rpc_call.await_args_list == [
        call(
            "chat.history.v2",
            {
                "sessionKey": "agent:main:test",
                "limit": 25,
                "maxResponseBytes": GATEWAY_HISTORY_RESPONSE_BUDGET_BYTES,
            },
        ),
        call(
            "chat.history.v2",
            {
                "sessionKey": "agent:main:test",
                "limit": 25,
                "maxResponseBytes": GATEWAY_HISTORY_MAX_RESPONSE_BUDGET_BYTES,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_session_history_unknown_capability_falls_back_once_and_remembers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayRPCClient()
    rpc_call = AsyncMock(
        side_effect=[
            GatewayRPCError(
                "chat.history.v2",
                code="METHOD_NOT_FOUND",
                message="Method not found: chat.history.v2",
            ),
            {"messages": []},
            {"messages": []},
        ]
    )
    monkeypatch.setattr(client, "call", rpc_call)

    await client.session_history("agent:main:test", limit=5)
    await client.session_history("agent:main:test", limit=10)

    assert client._server_methods is None  # noqa: SLF001
    assert client._server_method_support("chat.history.v2") is False  # noqa: SLF001
    assert rpc_call.await_args_list == [
        call(
            "chat.history.v2",
            {
                "sessionKey": "agent:main:test",
                "limit": 5,
                "maxResponseBytes": GATEWAY_HISTORY_RESPONSE_BUDGET_BYTES,
            },
        ),
        call("chat.history", {"sessionKey": "agent:main:test", "limit": 5}),
        call("chat.history", {"sessionKey": "agent:main:test", "limit": 10}),
    ]


@pytest.mark.asyncio
async def test_session_history_does_not_fallback_on_non_capability_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayRPCClient()
    error = GatewayRPCError("chat.history.v2", code="STORAGE_BUSY", message="try later")
    rpc_call = AsyncMock(side_effect=error)
    monkeypatch.setattr(client, "call", rpc_call)

    with pytest.raises(GatewayRPCError) as raised:
        await client.session_history("agent:main:test")

    assert raised.value is error
    assert client._server_method_support("chat.history.v2") is None  # noqa: SLF001
    rpc_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_connect_saves_advertised_methods_and_close_resets_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    methods = ["chat.history.v2"]
    ws = _HandshakeWebSocket(methods)

    async def connect(_url: str, **_kwargs: Any) -> _HandshakeWebSocket:
        return ws

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    client = GatewayRPCClient()

    await client.connect()

    assert client._server_methods == frozenset(methods)  # noqa: SLF001
    assert client._server_method_support("chat.history.v2") is True  # noqa: SLF001

    await client.close()

    assert client._server_methods is None  # noqa: SLF001
    assert client._known_missing_server_methods == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_gateway_connect_closes_socket_after_bad_handshake(monkeypatch) -> None:
    class BadHandshakeWebSocket(_SilentWebSocket):
        async def recv(self) -> str:
            return json.dumps({"type": "event", "event": "unexpected"})

    ws = BadHandshakeWebSocket()
    observed_connect: dict[str, Any] = {}

    async def connect(url: str, **kwargs: Any):
        observed_connect.update({"url": url, **kwargs})
        return ws

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    client = GatewayRPCClient()
    client._server_methods = frozenset({"chat.history.v2"})  # noqa: SLF001
    client._known_missing_server_methods.add("sessions.bootstrap.v2")  # noqa: SLF001

    with pytest.raises(RuntimeError, match="Unexpected gateway handshake frame"):
        await client.connect("ws://127.0.0.1:18791/ws")

    assert ws.closed is True
    assert client._ws is None
    assert client._server_methods is None  # noqa: SLF001
    assert client._known_missing_server_methods == set()  # noqa: SLF001
    assert observed_connect == {
        "url": "ws://127.0.0.1:18791/ws",
        "max_size": GATEWAY_CLIENT_MAX_MESSAGE_BYTES,
        "max_queue": GATEWAY_CLIENT_MAX_QUEUE,
    }
