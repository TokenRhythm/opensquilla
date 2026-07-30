from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
import websockets.exceptions

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


@pytest.mark.anyio
async def test_first_gateway_heartbeat_uses_negotiated_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    channel._connected = True
    channel._state.heartbeat_interval_ms = 40_000
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        channel._connected = False

    monkeypatch.setattr("opensquilla.channels.discord.random.random", lambda: 0.25)
    monkeypatch.setattr("opensquilla.channels.discord.asyncio.sleep", fake_sleep)

    await channel._heartbeat_loop()

    assert delays == [10.0]


@pytest.mark.anyio
async def test_start_schedules_heartbeat_after_hello_and_requires_ready() -> None:
    channel = DiscordChannel(
        DiscordChannelConfig(token="token", reconnect_max_retries=0)
    )
    frames = [
        {"op": 10, "d": {"heartbeat_interval": 40_000}},
        {
            "op": 0,
            "t": "READY",
            "s": 7,
            "d": {
                "session_id": "session-1",
                "resume_gateway_url": "wss://resume.example.test",
                "user": {"id": "bot-1", "username": "bot"},
                "guilds": [],
            },
        },
    ]
    sent: list[dict] = []

    async def fake_connect(_url: str) -> object:
        return object()

    async def fake_recv() -> dict:
        return frames.pop(0)

    async def fake_send(payload: dict) -> None:
        sent.append(payload)
        if payload.get("op") == 2:
            assert channel._heartbeat_task is not None
            assert not channel._connected

    channel._connect_ws = fake_connect  # type: ignore[method-assign]
    channel._ws_recv = fake_recv  # type: ignore[method-assign]
    channel._ws_send = fake_send  # type: ignore[method-assign]
    try:
        await channel.start()

        assert channel._connected is True
        assert channel.bot_user_id == "bot-1"
        assert channel._state.sequence == 7
        assert sent[0]["op"] == 2
    finally:
        await channel.stop()


@pytest.mark.anyio
async def test_start_retries_early_gateway_reconnect_before_marking_connected() -> None:
    channel = DiscordChannel(
        DiscordChannelConfig(
            token="token",
            reconnect_max_retries=1,
            reconnect_base_delay_s=0,
        )
    )
    frames = [
        {"op": 10, "d": {"heartbeat_interval": 40_000}},
        {"op": 7, "d": None},
        {"op": 10, "d": {"heartbeat_interval": 40_000}},
        {
            "op": 0,
            "t": "READY",
            "s": 8,
            "d": {
                "session_id": "session-2",
                "user": {"id": "bot-2", "username": "bot"},
                "guilds": [],
            },
        },
    ]
    connections: list[str] = []

    async def fake_connect(url: str) -> object:
        connections.append(url)
        return object()

    async def fake_recv() -> dict:
        return frames.pop(0)

    async def fake_gateway_url() -> str:
        return "wss://gateway-retry.example.test"

    async def fake_send(_payload: dict) -> None:
        return None

    channel._connect_ws = fake_connect  # type: ignore[method-assign]
    channel._ws_recv = fake_recv  # type: ignore[method-assign]
    channel._ws_send = fake_send  # type: ignore[method-assign]
    channel._fetch_gateway_url = fake_gateway_url  # type: ignore[method-assign]
    try:
        await channel.start()

        assert channel._connected is True
        assert connections == [
            channel.config.gateway_url,
            "wss://gateway-retry.example.test",
        ]
    finally:
        await channel.stop()


@pytest.mark.anyio
async def test_early_invalid_session_clears_resume_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    channel._state.session_id = "stale-session"
    channel._state.sequence = 9

    async def fake_recv() -> dict:
        return {"op": 9, "d": False}

    async def fake_sleep(_delay: float) -> None:
        return None

    channel._ws_recv = fake_recv  # type: ignore[method-assign]
    monkeypatch.setattr("opensquilla.channels.discord.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="invalidated"):
        await channel._await_ready_dispatch()

    assert channel._state.session_id is None
    assert channel._state.sequence is None


@pytest.mark.anyio
async def test_dispatch_loop_keeps_reading_after_reconnect() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    channel._connected = True
    frames = [
        {"op": 7, "d": None},
        {"op": 0, "t": "RESUMED", "s": 9, "d": {}},
    ]
    reconnects = 0
    reads = 0

    async def fake_recv() -> dict:
        nonlocal reads
        frame = frames[reads]
        reads += 1
        if reads == len(frames):
            channel._connected = False
        return frame

    async def fake_reconnect() -> None:
        nonlocal reconnects
        reconnects += 1

    channel._ws_recv = fake_recv  # type: ignore[method-assign]
    channel._reconnect = fake_reconnect  # type: ignore[method-assign]

    await channel._dispatch_loop()

    assert reads == 2
    assert reconnects == 1
    assert channel._state.sequence == 9


@pytest.mark.anyio
async def test_ws_recv_reports_closed_connection_when_socket_is_torn_down() -> None:
    """A reconnect-in-flight nulls the socket; readers must see ConnectionClosed."""
    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    assert channel._ws is None

    with pytest.raises(websockets.exceptions.ConnectionClosed):
        await channel._ws_recv()


@pytest.mark.anyio
async def test_dispatch_loop_waits_for_in_flight_reconnect_and_resumes_reading() -> None:
    """A heartbeat-timeout reconnect must not kill the dispatch loop.

    While the heartbeat task's reconnect is in flight the socket is ``None``;
    the dispatch loop must wait for that reconnect to finish and then resume
    reading from the fresh socket instead of dying on ``None.recv()``.
    """
    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    channel._connected = True
    channel._ws = None  # the heartbeat task's reconnect tore the socket down

    await channel._reconnect_lock.acquire()  # reconnect still in flight
    channel._reconnecting = True

    dispatch = asyncio.create_task(channel._dispatch_loop())
    for _ in range(5):
        await asyncio.sleep(0)
    # Pre-fix the loop died here with an unobserved AttributeError.
    assert not dispatch.done()

    class _FreshSocket:
        async def recv(self) -> str:
            channel._connected = False
            return json.dumps({"op": 0, "t": "RESUMED", "s": 5, "d": {}})

    channel._ws = _FreshSocket()
    channel._reconnecting = False
    channel._reconnect_lock.release()

    await asyncio.wait_for(dispatch, timeout=2)

    assert channel._state.sequence == 5


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
