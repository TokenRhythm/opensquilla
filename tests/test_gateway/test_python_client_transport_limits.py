"""Real-WebSocket coverage for bounded Python Gateway client messages."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
import websockets

import opensquilla.cli.gateway_client as tui_gateway_client_module
import opensquilla.gateway_client as generic_gateway_client_module
from opensquilla.cli.gateway_client import GatewayClient as TuiGatewayClient
from opensquilla.gateway_client import GatewayRPCClient

LEGACY_WEBSOCKETS_MAX_MESSAGE_BYTES = 1024 * 1024
TEST_MESSAGE_CAP_BYTES = 64 * 1024

GatewayPythonClient = TuiGatewayClient | GatewayRPCClient
GatewayHandler = Callable[[Any], Awaitable[None]]


@pytest.fixture(params=("tui", "generic"))
def gateway_client(request: pytest.FixtureRequest) -> GatewayPythonClient:
    if request.param == "tui":
        return TuiGatewayClient()
    return GatewayRPCClient(request_timeout_s=30.0)


def _json_frame(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"))


def _response(request_id: str, payload: dict[str, Any]) -> str:
    return _json_frame(
        {
            "type": "res",
            "id": request_id,
            "ok": True,
            "payload": payload,
        }
    )


def _response_with_wire_size(request_id: str, wire_size: int) -> str:
    payload: dict[str, Any] = {"blob": ""}
    empty_response = _response(request_id, payload)
    padding_size = wire_size - len(empty_response.encode("utf-8"))
    assert padding_size >= 0
    payload["blob"] = "x" * padding_size
    response = _response(request_id, payload)
    assert len(response.encode("utf-8")) == wire_size
    return response


async def _handshake(websocket: Any) -> None:
    await websocket.send(
        _json_frame(
            {
                "type": "event",
                "event": "connect.challenge",
                "payload": {"nonce": "loopback-test"},
            }
        )
    )
    request = json.loads(await websocket.recv())
    assert request["type"] == "req"
    assert request["method"] == "connect"
    await websocket.send(
        _json_frame(
            {
                "type": "hello-ok",
                "features": {"methods": ["sessions.bootstrap"]},
                "policy": {"client_ws_keepalive_timeout_ms": 120_000},
            }
        )
    )


async def _receive_request(websocket: Any, method: str) -> dict[str, Any]:
    request = json.loads(await websocket.recv())
    assert request["type"] == "req"
    assert request["method"] == method
    return request


@asynccontextmanager
async def _loopback_gateway(handler: GatewayHandler) -> AsyncIterator[str]:
    handler_errors: list[BaseException] = []

    async def checked_handler(websocket: Any) -> None:
        try:
            await handler(websocket)
        except BaseException as exc:
            handler_errors.append(exc)
            raise

    server = await websockets.serve(
        checked_handler,
        "127.0.0.1",
        0,
        compression=None,
    )
    try:
        assert server.sockets
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}/ws"
    finally:
        server.close()
        await server.wait_closed()
    if handler_errors:
        raise handler_errors[0]


async def _bootstrap(client: GatewayPythonClient) -> dict[str, Any]:
    if isinstance(client, TuiGatewayClient):
        return await client.bootstrap_session("agent:main:large", limit=200)
    result = await client.call(
        "sessions.bootstrap",
        {"key": "agent:main:large", "limit": 200},
    )
    assert isinstance(result, dict)
    return result


def _use_test_message_cap(
    monkeypatch: pytest.MonkeyPatch,
    client: GatewayPythonClient,
) -> None:
    module = (
        tui_gateway_client_module
        if isinstance(client, TuiGatewayClient)
        else generic_gateway_client_module
    )
    monkeypatch.setattr(module, "GATEWAY_CLIENT_MAX_MESSAGE_BYTES", TEST_MESSAGE_CAP_BYTES)


@pytest.mark.asyncio
async def test_python_gateway_clients_accept_bootstrap_over_one_mib(
    gateway_client: GatewayPythonClient,
) -> None:
    wire_size = LEGACY_WEBSOCKETS_MAX_MESSAGE_BYTES + 4096

    async def handler(websocket: Any) -> None:
        await _handshake(websocket)
        bootstrap = await _receive_request(websocket, "sessions.bootstrap")
        await websocket.send(_response_with_wire_size(bootstrap["id"], wire_size))
        follow_up = await _receive_request(websocket, "health")
        await websocket.send(_response(follow_up["id"], {"status": "ok"}))
        await websocket.wait_closed()

    async with _loopback_gateway(handler) as url:
        await gateway_client.connect(url)
        try:
            result = await asyncio.wait_for(_bootstrap(gateway_client), timeout=30.0)
            assert len(result["blob"]) > LEGACY_WEBSOCKETS_MAX_MESSAGE_BYTES
            assert await asyncio.wait_for(
                gateway_client.call("health"),
                timeout=5.0,
            ) == {"status": "ok"}
        finally:
            await gateway_client.close()


@pytest.mark.asyncio
async def test_python_gateway_clients_accept_message_at_cap_and_remain_usable(
    monkeypatch: pytest.MonkeyPatch,
    gateway_client: GatewayPythonClient,
) -> None:
    _use_test_message_cap(monkeypatch, gateway_client)

    async def handler(websocket: Any) -> None:
        await _handshake(websocket)
        bootstrap = await _receive_request(websocket, "sessions.bootstrap")
        await websocket.send(
            _response_with_wire_size(
                bootstrap["id"],
                TEST_MESSAGE_CAP_BYTES,
            )
        )
        follow_up = await _receive_request(websocket, "health")
        await websocket.send(_response(follow_up["id"], {"status": "ok"}))
        await websocket.wait_closed()

    async with _loopback_gateway(handler) as url:
        await gateway_client.connect(url)
        try:
            result = await asyncio.wait_for(_bootstrap(gateway_client), timeout=30.0)
            assert isinstance(result["blob"], str)
            assert await asyncio.wait_for(gateway_client.call("health"), timeout=5.0) == {
                "status": "ok"
            }
        finally:
            await gateway_client.close()


@pytest.mark.asyncio
async def test_python_gateway_clients_reject_cap_plus_one_then_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    gateway_client: GatewayPythonClient,
) -> None:
    _use_test_message_cap(monkeypatch, gateway_client)
    connection_count = 0

    async def handler(websocket: Any) -> None:
        nonlocal connection_count
        connection_index = connection_count
        connection_count += 1
        await _handshake(websocket)
        request = await _receive_request(websocket, "sessions.bootstrap")
        if connection_index == 0:
            await websocket.send(
                _response_with_wire_size(
                    request["id"],
                    TEST_MESSAGE_CAP_BYTES + 1,
                )
            )
        else:
            await websocket.send(_response(request["id"], {"reconnected": True}))
        await websocket.wait_closed()

    async with _loopback_gateway(handler) as url:
        await gateway_client.connect(url)
        try:
            with pytest.raises(ConnectionError) as exc_info:
                await asyncio.wait_for(_bootstrap(gateway_client), timeout=60.0)

            error_text = str(exc_info.value).lower()
            assert any(marker in error_text for marker in ("1009", "too big", "exceeds limit"))
            assert gateway_client._pending == {}  # noqa: SLF001
            assert gateway_client._connection_error is not None  # noqa: SLF001
            tasks = (
                gateway_client._listener_task,  # noqa: SLF001
                gateway_client._heartbeat_task,  # noqa: SLF001
            )
            await asyncio.wait_for(
                asyncio.gather(
                    *(task for task in tasks if task is not None),
                    return_exceptions=True,
                ),
                timeout=5.0,
            )
            assert all(task is None or task.done() for task in tasks)

            await gateway_client.connect(url)
            result = await asyncio.wait_for(_bootstrap(gateway_client), timeout=5.0)
            assert result == {"reconnected": True}
        finally:
            await gateway_client.close()

    assert connection_count == 2
