from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

from opensquilla.channels.discord import DiscordChannel, DiscordChannelConfig
from opensquilla.channels.matrix import MatrixChannel, MatrixChannelConfig
from opensquilla.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig
from opensquilla.channels.slack import SLACK_API_BASE, SlackChannel
from opensquilla.channels.types import IncomingMessage


async def _one_chunk() -> AsyncIterator[str]:
    yield "preview"


@pytest.mark.asyncio
async def test_slack_terminal_operations_accept_the_stream_creation_channel() -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    channel = SlackChannel(
        token="xoxb-dummy",
        slack_channel_id="configured-channel",
    )
    channel._client = httpx.AsyncClient(
        base_url=SLACK_API_BASE,
        transport=httpx.MockTransport(handler),
    )
    try:
        await channel.edit("111.222", "canonical", channel="origin-channel")
        await channel.delete("111.222", channel="origin-channel")
    finally:
        await channel._client.aclose()

    assert [(path.removeprefix("/api"), payload) for path, payload in requests] == [
        (
            "/chat.update",
            {"channel": "origin-channel", "ts": "111.222", "text": "canonical"},
        ),
        ("/chat.delete", {"channel": "origin-channel", "ts": "111.222"}),
    ]


@pytest.mark.asyncio
async def test_discord_stream_and_terminal_operations_share_dynamic_channel() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "message-1"})
        return httpx.Response(200, json={})

    config = DiscordChannelConfig(
        token="dummy",
        default_channel_id="configured-channel",
    )
    channel = DiscordChannel(config)
    channel._client = httpx.AsyncClient(
        base_url=config.api_base,
        transport=httpx.MockTransport(handler),
    )
    try:
        message_id = await channel.send_streaming(
            _one_chunk(),
            channel_id="origin-channel",
        )
        assert message_id == "message-1"
        assert channel._sent_messages["message-1"] == "origin-channel"

        await channel.edit(
            "message-1",
            "canonical",
            channel_id="origin-channel",
        )
        await channel.delete("message-1", channel_id="origin-channel")
    finally:
        await channel._client.aclose()

    assert paths
    assert all(
        path.startswith("/api/v10/channels/origin-channel/messages")
        for path in paths
    )
    assert not any("configured-channel" in path for path in paths)


class _MatrixClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.redacted: list[tuple[str, str]] = []

    async def room_send(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return SimpleNamespace(event_id="$event-1")

    async def room_redact(self, *, room_id: str, event_id: str) -> None:
        self.redacted.append((room_id, event_id))


@pytest.mark.asyncio
async def test_matrix_raw_stream_event_id_is_replaced_in_pinned_room() -> None:
    channel = MatrixChannel(MatrixChannelConfig())
    client = _MatrixClient()
    channel._client = client
    inbound = IncomingMessage(
        sender_id="@user:example.test",
        channel_id="!origin:example.test",
        content="hello",
    )

    assert channel.streaming_reply_kwargs(inbound) == {
        "room_id": "!origin:example.test"
    }
    assert channel.build_reply_message("canonical", inbound).reply_to == (
        "!origin:example.test"
    )

    await channel.edit(
        "$event-1",
        "canonical",
        room_id="!origin:example.test",
    )
    await channel.delete("$event-1", room_id="!origin:example.test")

    assert client.sent[-1]["room_id"] == "!origin:example.test"
    assert client.sent[-1]["content"]["m.relates_to"]["event_id"] == "$event-1"
    assert client.redacted == [("!origin:example.test", "$event-1")]


@pytest.mark.asyncio
async def test_matrix_rejects_conflicting_composite_and_explicit_routes() -> None:
    channel = MatrixChannel(MatrixChannelConfig())
    channel._client = _MatrixClient()

    with pytest.raises(RuntimeError, match="does not match"):
        await channel.edit(
            "!other:example.test|$event-1",
            "canonical",
            room_id="!origin:example.test",
        )


@pytest.mark.asyncio
async def test_msteams_terminal_operations_use_matching_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Activity:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    botbuilder = ModuleType("botbuilder")
    botbuilder.__path__ = []  # type: ignore[attr-defined]
    schema = ModuleType("botbuilder.schema")
    schema.Activity = Activity  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botbuilder", botbuilder)
    monkeypatch.setitem(sys.modules, "botbuilder.schema", schema)

    class TurnContext:
        def __init__(self) -> None:
            self.updated: list[Any] = []
            self.deleted: list[str] = []

        async def update_activity(self, activity: Any) -> None:
            self.updated.append(activity)

        async def delete_activity(self, message_id: str) -> None:
            self.deleted.append(message_id)

    class Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, TurnContext]] = []

        async def continue_conversation(
            self,
            reference: Any,
            callback: Any,
            *,
            bot_id: str | None,
        ) -> None:
            del bot_id
            context = TurnContext()
            self.calls.append((reference, context))
            await callback(context)

    channel = MSTeamsChannel(MSTeamsChannelConfig())
    adapter = Adapter()
    channel._adapter = adapter
    channel._references.update(
        {
            "conversation-a": object(),
            "conversation-b": object(),
        }
    )
    inbound = IncomingMessage(
        sender_id="user-b",
        channel_id="conversation-b",
        content="hello",
    )

    assert channel.streaming_reply_kwargs(inbound) == {"reply_to": "conversation-b"}
    assert channel.build_reply_message("canonical", inbound).reply_to == "conversation-b"

    await channel.edit(
        "message-1",
        "canonical",
        reply_to="conversation-b",
    )
    await channel.delete("message-1", reply_to="conversation-b")

    expected_reference = channel._references["conversation-b"]
    assert [reference for reference, _ in adapter.calls] == [
        expected_reference,
        expected_reference,
    ]
    assert adapter.calls[0][1].updated[0].text == "canonical"
    assert adapter.calls[1][1].deleted == ["message-1"]
