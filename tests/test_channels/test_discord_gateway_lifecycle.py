from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from opensquilla.channels.discord import (
    GATEWAY_INTENTS,
    DiscordChannel,
    DiscordChannelConfig,
)
from opensquilla.channels.types import IncomingMessage
from opensquilla.gateway.config import DiscordChannelEntry
from opensquilla.onboarding.channel_specs import get_channel_setup_spec


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_discord_intent_defaults_are_consistent_across_config_surfaces() -> None:
    spec = get_channel_setup_spec("discord")
    intents_field = next(field for field in spec.fields if field.name == "intents")

    assert DiscordChannelConfig(token="token").intents == GATEWAY_INTENTS
    assert DiscordChannelEntry(name="discord", token="token").intents == GATEWAY_INTENTS
    assert intents_field.default == GATEWAY_INTENTS
    assert GATEWAY_INTENTS & (1 << 13)  # DIRECT_MESSAGE_REACTIONS


def _ready_frame() -> str:
    return json.dumps({
        "op": 0, "t": "READY", "s": 7,
        "d": {"session_id": "session-1", "user": {"id": "bot-1", "username": "bot"},
              "guilds": []},
    })


@pytest.mark.anyio
async def test_sdk_ready_bridge_preserves_order_and_authenticated_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import discord

    received: list[tuple[str, bool, int]] = []
    closed = asyncio.Event()

    async def start(client: Any, token: str, *, reconnect: bool) -> None:
        received.append((token, reconnect, client.intents.value))
        client.dispatch("socket_raw_receive", _ready_frame())
        for kind, data in [
            ("THREAD_CREATE", {"id": "thread-1", "type": 11, "parent_id": "parent-1"}),
            ("MESSAGE_CREATE", {"id": "message-1", "channel_id": "thread-1",
                                "author": {"id": "user-1"}, "content": "hello"}),
        ]:
            client.dispatch("socket_raw_receive", json.dumps({"op": 0, "t": kind, "d": data}))
        await asyncio.Event().wait()

    async def close(_client: Any) -> None:
        closed.set()

    monkeypatch.setattr(discord.Client, "start", start)
    monkeypatch.setattr(discord.Client, "close", close)
    channel = DiscordChannel(DiscordChannelConfig(token="token", application_id="app-1"))
    try:
        await channel.start()
        assert channel.is_connected()
        assert channel.bot_user_id == "bot-1"
        message = await asyncio.wait_for(channel.receive(), timeout=1)
        assert message.metadata["native_thread_id"] == "thread-1"
        assert message.metadata["native_parent_channel_id"] == "parent-1"
        assert message.provenance.authenticated
        assert message.provenance.account_id == "app-1"
        assert received == [("token", True, GATEWAY_INTENTS)]
        assert (await channel.health_check()).extra["gateway_sdk"] == "discord.py"
        # SDK owns reconnection; the adapter only reflects its lifecycle.
        channel._gateway_client.dispatch("disconnect")
        await asyncio.sleep(0)
        assert not channel.is_connected()
        channel._gateway_client.dispatch(
            "socket_raw_receive", json.dumps({"op": 0, "t": "RESUMED", "d": {}})
        )
        await asyncio.sleep(0)
        assert channel.is_connected()
    finally:
        await channel.stop()
    assert closed.is_set()
    assert not channel.is_connected()
    assert channel._gateway_task is None
    assert channel._dispatch_task is None


@pytest.mark.anyio
async def test_sdk_login_failure_is_reported_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import discord

    async def start(_client: Any, _token: str, *, reconnect: bool) -> None:
        raise discord.LoginFailure("synthetic invalid token")

    close = AsyncMock()
    monkeypatch.setattr(discord.Client, "start", start)
    monkeypatch.setattr(discord.Client, "close", close)
    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    with pytest.raises(discord.LoginFailure):
        await channel.start()
    assert close.await_count >= 1
    assert channel._gateway_client is None
    assert not channel.is_connected()


@pytest.mark.anyio
async def test_cancel_before_ready_closes_sdk_and_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    import discord

    started = asyncio.Event()

    async def start(_client: Any, _token: str, *, reconnect: bool) -> None:
        started.set()
        await asyncio.Event().wait()

    close = AsyncMock()
    monkeypatch.setattr(discord.Client, "start", start)
    monkeypatch.setattr(discord.Client, "close", close)
    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    startup = asyncio.create_task(channel.start())
    await asyncio.wait_for(started.wait(), timeout=1)
    assert not channel.is_connected()
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup
    assert close.await_count >= 1
    assert channel._gateway_task is None
    assert channel._dispatch_task is None


@pytest.mark.anyio
async def test_sdk_terminal_failure_marks_health_disconnected_and_closes_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import discord

    fail = asyncio.Event()

    async def start(client: Any, _token: str, *, reconnect: bool) -> None:
        client.dispatch("socket_raw_receive", _ready_frame())
        await fail.wait()
        raise RuntimeError("synthetic terminal error")

    close = AsyncMock()
    monkeypatch.setattr(discord.Client, "start", start)
    monkeypatch.setattr(discord.Client, "close", close)
    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    try:
        await channel.start()
        fail.set()
        task = channel._gateway_task
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert not (await channel.health_check()).connected
        assert close.await_count >= 1
    finally:
        await channel.stop()


@pytest.mark.anyio
async def test_sdk_custom_endpoints_are_instance_scoped() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    client = channel._create_gateway_client()
    request = AsyncMock(return_value={"ok": True})
    connect = AsyncMock(return_value=object())
    client.http.request = request
    client.http.ws_connect = connect
    channel.config.api_base = "https://api.example.test/v10"
    channel.config.gateway_url = "wss://gateway.example.test/socket"
    channel._configure_sdk_endpoints(client)
    route = SimpleNamespace(BASE="https://discord.com/api/v10",
                            url="https://discord.com/api/v10/users/@me")
    try:
        await client.http.request(route)
        assert request.call_args.args[0].url == "https://api.example.test/v10/users/@me"
        assert route.url == "https://discord.com/api/v10/users/@me"
        await client.http.ws_connect("wss://gateway.discord.gg/?v=10&compress=zlib-stream")
        assert connect.call_args.args[0] == (
            "wss://gateway.example.test/socket?v=10&compress=zlib-stream"
        )
        await client.http.ws_connect("wss://resume.example.test/?v=10")
        assert connect.call_args.args[0] == "wss://resume.example.test/?v=10"
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("enabled", "bypass", "rest_proxy", "gateway_proxy", "resume_proxy"),
    [
        (False, "", False, False, False),
        (True, "", True, True, True),
        (True, "gateway.discord.gg", True, False, True),
        (True, "discord.com,resume.example.test", False, True, False),
        (True, "*", False, False, False),
    ],
)
async def test_sdk_proxy_policy_applies_per_rest_gateway_and_resume_url(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    bypass: str,
    rest_proxy: bool,
    gateway_proxy: bool,
    resume_proxy: bool,
) -> None:
    from contextlib import asynccontextmanager

    import aiohttp

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WS_PROXY", "WSS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    proxy = "http://proxy.example.test:8080"
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("NO_PROXY", bypass)
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1" if enabled else "0")
    calls: list[tuple[str, str | None]] = []

    @asynccontextmanager
    async def request(_session: Any, _method: str, url: str, **kwargs: Any) -> Any:
        calls.append((url, kwargs.get("proxy")))
        yield SimpleNamespace(
            status=200,
            headers={"content-type": "application/json"},
            text=AsyncMock(return_value='{"id":"bot-1"}'),
        )

    async def connect(_session: Any, url: str, **kwargs: Any) -> Any:
        calls.append((url, kwargs.get("proxy")))
        return object()

    monkeypatch.setattr(aiohttp.ClientSession, "request", request)
    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", connect)
    channel = DiscordChannel(DiscordChannelConfig(token="synthetic-token"))
    client = channel._create_gateway_client()
    try:
        # Exercise the actual SDK login/session construction and REST method;
        # replace only the final aiohttp transport, so no network is used.
        await client.http.static_login("synthetic-token")
        await client.http.ws_connect("wss://gateway.discord.gg/?v=10")
        await client.http.ws_connect("wss://resume.example.test/?v=10")
        assert calls == [
            ("https://discord.com/api/v10/users/@me", proxy if rest_proxy else None),
            ("wss://gateway.discord.gg/?v=10", proxy if gateway_proxy else None),
            ("wss://resume.example.test/?v=10", proxy if resume_proxy else None),
        ]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_sdk_proxy_bridge_fails_explicitly_for_missing_sdk_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8080")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.delenv("no_proxy", raising=False)
    channel = DiscordChannel(DiscordChannelConfig(token="synthetic-token"))
    client = channel._create_gateway_client()
    try:
        with pytest.raises(RuntimeError, match="SDK session is unavailable"):
            await client.http.ws_connect("wss://gateway.discord.gg/?v=10")
    finally:
        await client.close()


@pytest.mark.anyio
async def test_gateway_interaction_is_deferred_and_original_response_is_resolved() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/callback"):
            return httpx.Response(204)
        if request.url.path.endswith("/messages/@original"):
            return httpx.Response(200, json={"id": "response-1"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    channel = DiscordChannel(DiscordChannelConfig(token="token", application_id="app-1"))
    channel._client = httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    )
    try:
        await channel._handle_dispatch(
            "INTERACTION_CREATE",
            {
                "id": "interaction-1",
                "token": "interaction-token",
                "application_id": "app-1",
                "channel_id": "channel-1",
                "user": {"id": "user-1"},
                "data": {"name": "help", "options": []},
            },
        )
        inbound = await channel.receive()
        assert inbound.metadata["interaction_deferred"] is True

        reply = channel.build_reply_message("Done", inbound)
        result = await channel.send(reply)
    finally:
        await channel.stop()

    assert result.provider_message_id == "response-1"
    assert [request.method for request in requests] == ["POST", "PATCH"]
    assert requests[0].url.path.endswith("/interactions/interaction-1/interaction-token/callback")
    assert json.loads(requests[0].content) == {"type": 5}
    assert requests[1].url.path.endswith("/webhooks/app-1/interaction-token/messages/@original")
    assert json.loads(requests[1].content) == {"content": "Done"}


@pytest.mark.anyio
async def test_interaction_stream_updates_the_deferred_original() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "response-1"})

    async def chunks() -> AsyncIterator[str]:
        yield "one"
        yield " two"

    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    channel._client = httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    )
    try:
        await channel.send_streaming(
            chunks(),
            channel_id="channel-1",
            application_id="app-1",
            interaction_token="interaction-token",
            update_interval_ms=0,
        )
    finally:
        await channel.stop()

    assert requests
    assert all(request.method == "PATCH" for request in requests)
    assert all(
        request.url.path.endswith("/webhooks/app-1/interaction-token/messages/@original")
        for request in requests
    )


def test_interaction_reply_metadata_is_only_added_after_successful_defer() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token", application_id="app-1"))
    message = IncomingMessage(
        sender_id="user-1",
        channel_id="channel-1",
        content="/help",
        metadata={
            "native_message_id": "interaction-1",
            "interaction_token": "token-1",
            "interaction_deferred": False,
        },
    )

    reply = channel.build_reply_message("No defer", message)

    assert "interaction_token" not in reply.metadata
