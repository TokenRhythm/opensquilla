"""Exercise the installed Feishu SDK against a loopback WebSocket endpoint."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Request, Response

from opensquilla.channels.feishu import (
    FeishuChannelConfig,
    FeishuWebSocketTransport,
    _adapt_feishu_sdk_handshake_errors,
)


@pytest.fixture
async def real_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Any]:
    from lark_oapi import ws as sdk_ws
    from lark_oapi.ws import client as sdk

    cache_tasks: set[asyncio.Task[Any]] = set()
    real_client = sdk.Client

    def track_client_cache(*args: Any, **kwargs: Any) -> Any:
        client = real_client(*args, **kwargs)
        # The real SDK constructs its cache on the caller loop before the
        # transport starts its worker. Own that exact task even if start fails
        # before endpoint discovery; keep the real Client type/module intact.
        cache_tasks.add(client._cache._cron)
        return client

    monkeypatch.setattr(sdk_ws, "Client", track_client_cache)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    try:
        yield sdk
    finally:
        for task in cache_tasks:
            task.cancel()
        await asyncio.gather(*cache_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_feishu_sdk_reconnects_and_stops_its_worker(
    monkeypatch: pytest.MonkeyPatch, real_sdk: Any,
) -> None:
    # Substitute endpoint discovery only. Connection setup, receive-loop failure,
    # reconnect, close, and the OpenSquilla worker all use the installed SDK.
    sdk = real_sdk
    connections: list[ServerConnection] = []

    async def accept(connection: ServerConnection) -> None:
        connections.append(connection)
        await connection.wait_closed()

    async def ignore_event(_event: Any) -> None:
        pass

    async with serve(accept, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        def endpoint(client: Any) -> str:
            # Remove the vendor's randomized reconnect delay from this local
            # transport test; no production retry budget changes.
            client._reconnect_nonce = 0
            client._reconnect_interval = 0.01
            return f"ws://127.0.0.1:{port}/callback?device_id=local&service_id=1"

        monkeypatch.setattr(sdk.Client, "_get_conn_url", endpoint)
        transport = FeishuWebSocketTransport(
            FeishuChannelConfig(
                app_id="local-test-app", app_secret="local-test-secret",
                connection_mode="websocket",
            )
        )
        worker = None
        try:
            async with asyncio.timeout(10):
                await transport.start(ignore_event)
                worker = transport._thread
                assert worker is not None and worker.is_alive()
                assert (await transport.health_check()).connected
                assert len(connections) == 1

                # Break the socket without a close handshake. The real SDK must
                # release its connection lock and establish a replacement.
                connections[0].transport.abort()
                while len(connections) < 2 or not (await transport.health_check()).connected:
                    await asyncio.sleep(0.01)

                await transport.stop()
                assert not worker.is_alive()
                assert not (await transport.health_check()).connected

                # A later start must bind a fresh SDK loop, not reuse the closed
                # loop from the first worker.
                await transport.start(ignore_event)
                assert len(connections) >= 3
                assert (await transport.health_check()).connected
                worker = transport._thread
        finally:
            await transport.stop()
        assert worker is not None and not worker.is_alive()
        assert transport._thread is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "terminal"),
    [
        pytest.param(401, {}, False, id="http-unauthorized-without-vendor-code"),
        pytest.param(403, {}, False, id="http-forbidden-without-vendor-code"),
        pytest.param(
            503, {"handshake-status": "403", "handshake-msg": "local-test-secret"},
            True, id="vendor-forbidden",
        ),
        pytest.param(
            503, {"handshake-status": "514", "handshake-msg": "vendor rejection",
                  "handshake-autherrcode": "1000040350"},
            True, id="vendor-connection-limit",
        ),
        pytest.param(
            503, {"handshake-status": "1", "handshake-msg": "server busy"},
            False, id="vendor-transient",
        ),
        pytest.param(503, {}, False, id="missing-vendor-headers"),
        pytest.param(
            503, {"handshake-status": "invalid", "handshake-msg": "bad metadata"},
            False, id="malformed-vendor-headers",
        ),
        pytest.param(
            503, {"handshake-status": "403"}, False, id="missing-vendor-message",
        ),
        pytest.param(
            503, {"handshake-status": "514", "handshake-msg": "bad metadata",
                  "handshake-autherrcode": "invalid"},
            False, id="malformed-vendor-auth-code",
        ),
        pytest.param(
            503, [("handshake-status", "403"), ("handshake-status", "1"),
                  ("handshake-msg", "ambiguous metadata")],
            False, id="duplicate-vendor-status",
        ),
        pytest.param(
            503, [("handshake-status", "403"), ("handshake-msg", "first"),
                  ("handshake-msg", "second")],
            False, id="duplicate-vendor-message",
        ),
    ],
)
async def test_real_feishu_sdk_classifies_handshake_rejections(
    monkeypatch: pytest.MonkeyPatch, real_sdk: Any,
    caplog: pytest.LogCaptureFixture,
    status: int, headers: dict[str, str] | list[tuple[str, str]], terminal: bool,
) -> None:
    attempts = 0
    accepted: list[ServerConnection] = []

    def reject_first(connection: ServerConnection, _request: Request) -> Response | None:
        nonlocal attempts
        attempts += 1
        if not terminal and attempts > 1:
            return None
        response = connection.respond(HTTPStatus(status), "synthetic handshake rejection")
        response.headers.update(headers)
        return response

    async def accept(connection: ServerConnection) -> None:
        accepted.append(connection)
        await connection.wait_closed()

    async def ignore_event(_event: Any) -> None:
        pass

    async with serve(accept, "127.0.0.1", 0, process_request=reject_first) as server:
        port = server.sockets[0].getsockname()[1]

        def endpoint(client: Any) -> str:
            client._reconnect_nonce = 0
            client._reconnect_interval = 0.01
            return f"ws://127.0.0.1:{port}/callback?device_id=local&service_id=1"

        monkeypatch.setattr(real_sdk.Client, "_get_conn_url", endpoint)
        transport = FeishuWebSocketTransport(
            FeishuChannelConfig(
                app_id="local-test-app", app_secret="local-test-secret",
                connection_mode="websocket",
            )
        )
        worker = None
        try:
            async with asyncio.timeout(10):
                if terminal:
                    with pytest.raises(RuntimeError):
                        await transport.start(ignore_event)
                    health = await transport.health_check()
                    assert not health.connected
                    assert health.extra["last_error"]["error_class"] == "auth_invalid"
                    assert health.extra["last_error"]["retryable"] is False
                    assert "local-test-secret" not in health.extra["last_error"]["message"]
                    assert "local-test-secret" not in caplog.text
                    assert attempts == 1
                    assert accepted == []
                    assert transport._thread is None
                else:
                    await transport.start(ignore_event)
                    worker = transport._thread
                    assert worker is not None and worker.is_alive()
                    assert (await transport.health_check()).connected
                    assert attempts == 2
                    assert len(accepted) == 1
        finally:
            await transport.stop()
        assert transport._thread is None
        assert worker is None or not worker.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "duplicate_key", ["handshake-status", "handshake-msg", "handshake-autherrcode"],
)
async def test_sdk_adapter_preserves_ambiguous_handshake_error(
    monkeypatch: pytest.MonkeyPatch, real_sdk: Any, duplicate_key: str,
) -> None:
    from lark_oapi import ws as sdk_ws

    headers = Headers({
        "handshake-status": "514", "handshake-msg": "rejected",
        "handshake-autherrcode": "1000040350",
    })
    headers[duplicate_key] = "ambiguous"
    original = InvalidStatus(Response(503, "Service Unavailable", headers))

    async def reject(_client: Any) -> None:
        raise original

    monkeypatch.setattr(real_sdk.Client, "_connect", reject)
    client = sdk_ws.Client("local-test-app", "local-test-secret")
    failures: list[Exception] = []
    _adapt_feishu_sdk_handshake_errors(client, failures.append)
    with pytest.raises(InvalidStatus) as caught:
        await client._connect()
    assert caught.value is original
    assert failures == []


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor_headers", [
    {"handshake-status": "403", "handshake-msg": "local-test-secret"},
    {"handshake-status": "514", "handshake-msg": "local-test-secret",
     "handshake-autherrcode": "1000040350"},
], ids=["forbidden", "connection-limit"])
async def test_real_feishu_sdk_terminal_reconnect_stops_and_can_restart(
    monkeypatch: pytest.MonkeyPatch, real_sdk: Any, vendor_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    connections: list[ServerConnection] = []
    attempts = 0
    reject = False

    def reject_reconnect(connection: ServerConnection, _request: Request) -> Response | None:
        nonlocal attempts
        attempts += 1
        if not reject:
            return None
        response = connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "vendor rejection")
        response.headers.update(vendor_headers)
        return response

    async def accept(connection: ServerConnection) -> None:
        connections.append(connection)
        await connection.wait_closed()

    async def ignore_event(_event: Any) -> None:
        pass

    async with serve(accept, "127.0.0.1", 0, process_request=reject_reconnect) as server:
        port = server.sockets[0].getsockname()[1]

        def endpoint(client: Any) -> str:
            client._reconnect_nonce = 0
            client._reconnect_interval = 0.01
            return f"ws://127.0.0.1:{port}/callback?device_id=local&service_id=1"

        monkeypatch.setattr(real_sdk.Client, "_get_conn_url", endpoint)
        transport = FeishuWebSocketTransport(
            FeishuChannelConfig(
                app_id="local-test-app", app_secret="local-test-secret",
                connection_mode="websocket",
            )
        )
        try:
            async with asyncio.timeout(10):
                await transport.start(ignore_event)
                first_worker = transport._thread
                assert first_worker is not None and first_worker.is_alive()
                first_loop = transport._worker_loop
                assert first_loop is not None
                assert (await transport.health_check()).connected
                reject = True
                connections[0].transport.abort()
                while first_worker.is_alive():
                    await asyncio.sleep(0.01)
                health = await transport.health_check()
                assert not health.connected
                assert health.extra["connection_phase"] == "stopped"
                assert health.extra["last_error"]["error_class"] == "auth_invalid"
                assert health.extra["last_error"]["retryable"] is False
                assert "local-test-secret" not in health.extra["last_error"]["message"]
                assert "local-test-secret" not in caplog.text
                assert "Task exception was never retrieved" not in caplog.text
                assert first_loop.is_closed()
                assert not asyncio.all_tasks(first_loop)
                assert transport._ws_client._conn is None
                assert attempts == 2
                await transport.stop()
                assert transport._thread is None

                reject = False
                await transport.start(ignore_event)
                assert (await transport.health_check()).connected
                assert "last_error" not in (await transport.health_check()).extra
                assert transport._thread is not first_worker
                assert attempts == 3
        finally:
            await transport.stop()
        assert transport._thread is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_first", [False, True], ids=["initial-connect", "reconnect"])
async def test_real_feishu_sdk_stop_between_loop_phases_keeps_disconnect_on_worker(
    monkeypatch: pytest.MonkeyPatch, real_sdk: Any, reject_first: bool,
) -> None:
    paused = threading.Event()
    resume = threading.Event()
    disconnect_loops: list[asyncio.AbstractEventLoop] = []
    attempts = 0

    def handshake(connection: ServerConnection, _request: Request) -> Response | None:
        nonlocal attempts
        attempts += 1
        if reject_first and attempts == 1:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "try again")
        return None

    async def accept(connection: ServerConnection) -> None:
        await connection.wait_closed()

    async def ignore_event(_event: Any) -> None:
        pass

    disconnect = real_sdk.Client._disconnect

    async def observe_disconnect(client: Any) -> None:
        disconnect_loops.append(asyncio.get_running_loop())
        await disconnect(client)

    monkeypatch.setattr(real_sdk.Client, "_disconnect", observe_disconnect)
    async with serve(accept, "127.0.0.1", 0, process_request=handshake) as server:
        port = server.sockets[0].getsockname()[1]

        def endpoint(client: Any) -> str:
            client._reconnect_nonce = 0
            client._reconnect_interval = 0.01
            return f"ws://127.0.0.1:{port}/callback?device_id=local&service_id=1"

        monkeypatch.setattr(real_sdk.Client, "_get_conn_url", endpoint)
        transport = FeishuWebSocketTransport(
            FeishuChannelConfig(
                app_id="local-test-app", app_secret="local-test-secret",
                connection_mode="websocket",
            )
        )
        bind = transport._bind_sdk_event_loop

        def bind_and_pause_between_phases(loop: asyncio.AbstractEventLoop) -> None:
            bind(loop)
            run = loop.run_until_complete

            def run_then_pause(future: Any) -> Any:
                value = run(future)
                if transport._ws_client._conn is not None and not paused.is_set():
                    # The real SDK briefly stops its loop between connect (or
                    # reconnect) and _select. Make that ownership race exact.
                    paused.set()
                    assert resume.wait(timeout=5), "test did not release SDK phase boundary"
                return value

            monkeypatch.setattr(loop, "run_until_complete", run_then_pause)

        monkeypatch.setattr(transport, "_bind_sdk_event_loop", bind_and_pause_between_phases)
        stopping: asyncio.Task[None] | None = None
        try:
            async with asyncio.timeout(10):
                await transport.start(ignore_event)
                while not paused.is_set():
                    await asyncio.sleep(0.01)
                worker = transport._thread
                worker_loop = transport._worker_loop
                assert worker is not None and worker.is_alive()
                assert worker_loop is not None and not worker_loop.is_running()
                stopping = asyncio.create_task(transport.stop())
                await asyncio.sleep(0)
                # Even a temporarily idle loop still owns its socket/lock.
                assert all(loop is worker_loop for loop in disconnect_loops)
                resume.set()
                await stopping
                assert not worker.is_alive()
                assert worker_loop.is_closed()
                assert not asyncio.all_tasks(worker_loop)
                assert transport._ws_client._conn is None
                assert transport._thread is None
                assert "last_error" not in (await transport.health_check()).extra
                assert disconnect_loops and all(loop is worker_loop for loop in disconnect_loops)

                await transport.start(ignore_event)
                assert (await transport.health_check()).connected
                assert transport._thread is not worker
        finally:
            resume.set()
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            await transport.stop()


@pytest.mark.asyncio
async def test_real_feishu_sdk_cancelled_handshake_stops_and_can_restart(
    monkeypatch: pytest.MonkeyPatch, real_sdk: Any,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_handshake(
        _connection: ServerConnection, _request: Request,
    ) -> None:
        entered.set()
        await release.wait()

    async def accept(connection: ServerConnection) -> None:
        await connection.wait_closed()

    async def ignore_event(_event: Any) -> None:
        pass

    async with serve(accept, "127.0.0.1", 0, process_request=hold_handshake) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(
            real_sdk.Client, "_get_conn_url",
            lambda _client: f"ws://127.0.0.1:{port}/callback?device_id=local&service_id=1",
        )
        transport = FeishuWebSocketTransport(
            FeishuChannelConfig(
                app_id="local-test-app", app_secret="local-test-secret",
                connection_mode="websocket",
            )
        )
        startup = asyncio.create_task(transport.start(ignore_event))
        try:
            async with asyncio.timeout(10):
                await entered.wait()
                first_worker = transport._thread
                assert first_worker is not None and first_worker.is_alive()
                startup.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await startup
                await transport.stop()
                assert not first_worker.is_alive()
                assert transport._thread is None

                release.set()
                await transport.start(ignore_event)
                assert (await transport.health_check()).connected
                second_worker = transport._thread
                assert second_worker is not None and second_worker.is_alive()
                assert second_worker is not first_worker
                await transport.stop()
                assert not second_worker.is_alive()
        finally:
            release.set()
            if not startup.done():
                startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
            await transport.stop()
        assert transport._thread is None
