"""Exercise the installed Feishu SDK against a loopback WebSocket endpoint."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from opensquilla.channels.feishu import FeishuChannelConfig, FeishuWebSocketTransport


@pytest.mark.asyncio
async def test_real_feishu_sdk_reconnects_and_stops_its_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Substitute endpoint discovery only. Connection setup, receive-loop failure,
    # reconnect, close, and the OpenSquilla worker all use the installed SDK.
    from lark_oapi import ws as sdk_ws
    from lark_oapi.ws import client as sdk

    connections: list[ServerConnection] = []
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

    async def accept(connection: ServerConnection) -> None:
        connections.append(connection)
        await connection.wait_closed()

    async def ignore_event(_event: Any) -> None:
        pass

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
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
            try:
                await transport.stop()
            finally:
                for task in cache_tasks:
                    task.cancel()
                await asyncio.gather(*cache_tasks, return_exceptions=True)
        assert worker is not None and not worker.is_alive()
        assert transport._thread is None
