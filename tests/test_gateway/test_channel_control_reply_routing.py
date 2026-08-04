"""Control-plane replies must route like normal turn replies on every channel.

Command, approval, pairing, busy, and delivery-failure replies used to be
built from the route envelope with only interaction metadata preserved. QQ's
``send()`` routes purely on ``metadata['chat_type']`` / ``openid``, so every
slash-command reply (and its failure notice) was rejected with a ValueError.
The shared fix routes these replies through each adapter's own
``build_reply_message`` so every channel lands them in the same conversation
as a normal reply.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.channels.dingtalk import DingTalkChannel, DingTalkChannelConfig
from opensquilla.channels.discord import DiscordChannel, DiscordChannelConfig
from opensquilla.channels.feishu import FeishuChannel, FeishuChannelConfig
from opensquilla.channels.matrix import MatrixChannel, MatrixChannelConfig
from opensquilla.channels.qq import QQChannel, QQChannelConfig
from opensquilla.channels.slack import SlackChannel
from opensquilla.channels.telegram import TelegramChannel, TelegramChannelConfig
from opensquilla.channels.types import IncomingMessage, OutgoingMessage
from opensquilla.channels.wecom import WeComChannel, WeComChannelConfig
from opensquilla.gateway.channel_dispatch import (
    _deliver_reply_or_notify,
    _route_control_reply,
)


def _route() -> SimpleNamespace:
    return SimpleNamespace(
        channel_id="chat-1",
        thread_id=None,
        channel_name="qq",
        metadata={
            "chat_type": "c2c",
            "openid": "openid-1",
            "msg_id": "m-1",
            "author_id": "openid-1",
        },
    )


def _inbound_c2c() -> IncomingMessage:
    return IncomingMessage(
        sender_id="openid-1",
        channel_id="chat-1",
        content="/help",
        metadata={
            "chat_type": "c2c",
            "openid": "openid-1",
            "msg_id": "m-1",
            "author_id": "openid-1",
        },
    )


def _inbound_group() -> IncomingMessage:
    return IncomingMessage(
        sender_id="member-1",
        channel_id="group-1",
        content="/usage",
        metadata={
            "chat_type": "group",
            "group_openid": "group-1",
            "msg_id": "m-2",
            "author_id": "member-1",
        },
    )


def _command_reply() -> OutgoingMessage:
    return OutgoingMessage(
        content="**Available commands**\n\n- `/help` — Show commands.",
        metadata={
            "command": "help",
            "method": "commands.list_for_surface",
            "denied": False,
        },
    )


def _adapters() -> list[tuple[str, Any]]:
    return [
        ("qq", QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))),
        ("telegram", TelegramChannel(TelegramChannelConfig(name="telegram", token="t"))),
        ("slack", SlackChannel(token="x", slack_channel_id="")),
        ("discord", DiscordChannel(DiscordChannelConfig(name="discord", token="t"))),
        ("feishu", FeishuChannel(FeishuChannelConfig(name="feishu", app_id="a", app_secret="s"))),
        ("wecom", WeComChannel(WeComChannelConfig(name="wecom"))),
        (
            "matrix",
            MatrixChannel(
                MatrixChannelConfig(
                    name="matrix",
                    homeserver="https://example.com",
                    access_token="t",
                )
            ),
        ),
        (
            "dingtalk",
            DingTalkChannel(
                DingTalkChannelConfig(
                    name="dingtalk",
                    client_id="c",
                    client_secret="s",
                )
            ),
        ),
    ]


@pytest.mark.parametrize("name,channel", _adapters())
def test_command_reply_routes_like_normal_reply_on_every_adapter(
    name: str,
    channel: Any,
) -> None:
    """A slash-command reply keeps the adapter's normal reply target."""

    reply = _command_reply()
    msg = _inbound_c2c()
    routed = _route_control_reply(channel, reply, msg, _route())
    expected = channel.build_reply_message(reply.content, msg)

    # Transport routing matches the normal turn reply path exactly.
    assert routed.reply_to == expected.reply_to, name
    for key, value in (expected.metadata or {}).items():
        assert routed.metadata.get(key) == value, f"{name}: lost transport key {key}"
    # Control-plane markers survive, content and rich-text hint are preserved.
    assert routed.metadata["command"] == "help"
    assert routed.metadata["method"] == "commands.list_for_surface"
    assert routed.metadata["denied"] is False
    assert routed.content == reply.content


def test_qq_slash_command_reply_reaches_post_c2c_message() -> None:
    """The real QQ adapter: a /help reply must be delivered to the sender."""

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))
    channel.api = SimpleNamespace(
        post_c2c_message=AsyncMock(),
        post_group_message=AsyncMock(),
    )
    msg = _inbound_c2c()
    routed = _route_control_reply(channel, _command_reply(), msg, _route())

    import asyncio

    asyncio.run(channel.send(routed))

    channel.api.post_c2c_message.assert_awaited_once()
    kwargs = channel.api.post_c2c_message.await_args.kwargs
    assert kwargs["openid"] == "openid-1"
    assert kwargs["msg_id"] == "m-1"
    assert kwargs["msg_seq"] == 1
    assert "Available commands" in kwargs["content"]
    channel.api.post_group_message.assert_not_awaited()


def test_qq_slash_command_reply_reaches_post_group_message() -> None:
    """Group variant: the reply targets the group openid, not a fallback."""

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))
    channel.api = SimpleNamespace(
        post_c2c_message=AsyncMock(),
        post_group_message=AsyncMock(),
    )
    msg = _inbound_group()
    routed = _route_control_reply(channel, _command_reply(), msg, _route())

    import asyncio

    asyncio.run(channel.send(routed))

    channel.api.post_group_message.assert_awaited_once()
    kwargs = channel.api.post_group_message.await_args.kwargs
    assert kwargs["group_openid"] == "group-1"
    assert kwargs["msg_id"] == "m-2"
    assert kwargs["msg_seq"] == 1
    channel.api.post_c2c_message.assert_not_awaited()


async def test_delivery_failure_notice_routes_through_adapter_builder() -> None:
    """The failure notice is itself deliverable on a metadata-routed channel."""

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))
    channel.api = SimpleNamespace(
        post_c2c_message=AsyncMock(),
        post_group_message=AsyncMock(),
    )
    msg = _inbound_c2c()

    class _FailReplyThenDelegate:
        def __init__(self, delegate: Any) -> None:
            self.delegate = delegate

        def build_reply_message(self, content: str, inbound: IncomingMessage) -> OutgoingMessage:
            # The adapter's own builder is what the shared router consults.
            return self.delegate.build_reply_message(content, inbound)

        async def send(self, message: OutgoingMessage) -> None:
            if message.metadata.get("delivery_failure_notice"):
                await self.delegate.send(message)
                return
            raise ValueError("transient")

    delivered = await _deliver_reply_or_notify(
        _FailReplyThenDelegate(channel),
        _command_reply(),
        route_envelope=_route(),
        session_key="agent:main:qq:direct:openid-1",
        msg=msg,
    )
    assert delivered is False

    channel.api.post_c2c_message.assert_awaited_once()
    kwargs = channel.api.post_c2c_message.await_args.kwargs
    assert kwargs["openid"] == "openid-1"
    assert "could not deliver" in kwargs["content"]
