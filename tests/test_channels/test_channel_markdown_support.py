"""Rich-text (markdown) outbound support across channel adapters."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from opensquilla.channels.dingtalk import DingTalkChannel
from opensquilla.channels.discord import DiscordChannel
from opensquilla.channels.feishu import FeishuChannel, FeishuChannelConfig, _TokenState
from opensquilla.channels.matrix import MatrixChannel
from opensquilla.channels.qq import QQChannel
from opensquilla.channels.slack import SlackChannel
from opensquilla.channels.telegram import (
    TelegramApiError,
    TelegramChannel,
    TelegramChannelConfig,
)
from opensquilla.channels.types import OutgoingMessage
from opensquilla.channels.wecom import WeComChannel, WeComChannelConfig


def test_markdown_capable_declarations_match_rendering_support() -> None:
    """Every adapter declares whether it can render markdown natively."""

    assert FeishuChannel.markdown_capable is True
    assert WeComChannel.markdown_capable is True
    assert DingTalkChannel.markdown_capable is True
    assert TelegramChannel.markdown_capable is True
    assert SlackChannel.markdown_capable is True
    assert DiscordChannel.markdown_capable is True
    assert MatrixChannel.markdown_capable is True
    assert QQChannel.markdown_capable is True


@pytest.mark.asyncio
async def test_feishu_send_markdown_posts_rich_message() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        payload = json.loads(body)
        assert payload["msg_type"] == "post"
        post = json.loads(payload["content"])["zh_cn"]
        assert post["content"][0][0]["tag"] == "md"
        assert post["content"][0][0]["text"] == "**bold** text"
        return httpx.Response(200, json={"code": 0})

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    try:
        await channel.send(
            OutgoingMessage(
                content="**bold** text",
                reply_to="ou_user",
                format="markdown",
            )
        )
    finally:
        await channel.stop()

    assert len(requests) == 1
    assert requests[0].url.path == "/open-apis/im/v1/messages"


@pytest.mark.asyncio
async def test_feishu_send_text_stays_plain_when_format_is_text() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        payload = json.loads(body)
        assert payload["msg_type"] == "text"
        assert json.loads(payload["content"]) == {"text": "plain"}
        return httpx.Response(200, json={"code": 0})

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    try:
        await channel.send(
            OutgoingMessage(content="plain", reply_to="ou_user", format="text")
        )
    finally:
        await channel.stop()

    assert len(requests) == 1


def test_wecom_webhook_builds_markdown_payload() -> None:
    channel = WeComChannel(WeComChannelConfig(name="wecom", agent_id_int=1))
    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(
            content="**bold** text",
            reply_to="user-1",
            format="markdown",
        )
    )
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"] == {"content": "**bold** text"}


def test_wecom_webhook_builds_plain_text_payload_by_default() -> None:
    channel = WeComChannel(WeComChannelConfig(name="wecom", agent_id_int=1))
    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(content="plain", reply_to="user-1")
    )
    assert payload["msgtype"] == "text"
    assert payload["text"] == {"content": "plain"}


def test_telegram_builds_markdownv2_payload_for_markdown() -> None:
    channel = TelegramChannel(
        TelegramChannelConfig(token="bot-token", connection_mode="webhook")
    )
    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(
            content="*bold* text",
            metadata={"chat_id": "12345"},
            format="markdown",
        )
    )
    assert payload["parse_mode"] == "MarkdownV2"
    assert payload["text"] == "*bold* text"


def test_telegram_builds_plain_payload_without_parse_mode() -> None:
    channel = TelegramChannel(
        TelegramChannelConfig(token="bot-token", connection_mode="webhook")
    )
    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(
            content="plain",
            metadata={"chat_id": "12345"},
        )
    )
    assert "parse_mode" not in payload


def test_runtime_reply_defaults_to_markdown_format() -> None:
    """Agent replies are rendered as markdown by default across adapters."""

    from types import SimpleNamespace

    from opensquilla.channels.qq import QQChannel, QQChannelConfig
    from opensquilla.channels.types import IncomingMessage
    from opensquilla.gateway.channel_dispatch import _build_runtime_reply_message

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))
    inbound = IncomingMessage(
        sender_id="openid-1",
        channel_id="chat-1",
        content="hi",
        metadata={"chat_type": "c2c", "openid": "openid-1", "msg_id": "m-1"},
    )
    route_envelope = SimpleNamespace(
        channel_id="chat-1",
        thread_id=None,
        channel_name="qq",
        metadata={"chat_type": "c2c", "openid": "openid-1"},
    )

    reply = _build_runtime_reply_message(
        channel,
        "**bold** answer",
        inbound,
        route_envelope,
    )

    assert reply.format == "markdown"
    assert reply.content == "**bold** answer"


@pytest.mark.asyncio
async def test_qq_markdown_uses_official_markdown_message() -> None:
    """QQ markdown replies use msg_type=2 + markdown.content."""

    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from opensquilla.channels.qq import QQChannelConfig

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))
    channel.api = SimpleNamespace(
        post_c2c_message=AsyncMock(),
        post_group_message=AsyncMock(),
    )
    await channel.send(
        OutgoingMessage(
            content="**bold** list",
            format="markdown",
            metadata={"chat_type": "c2c", "openid": "openid-1", "msg_id": "m-1"},
        )
    )
    channel.api.post_c2c_message.assert_awaited_once()
    kwargs = channel.api.post_c2c_message.await_args.kwargs
    assert kwargs["msg_type"] == 2
    assert kwargs["content"] is None
    assert kwargs["markdown"]["content"] == "**bold** list"


@pytest.mark.asyncio
async def test_telegram_markdown_failure_falls_back_to_plain_text() -> None:
    """MarkdownV2 rejection falls back to plain text instead of dropping."""

    channel = TelegramChannel(
        TelegramChannelConfig(token="bot-token", connection_mode="webhook")
    )
    sent: list[dict] = []

    async def fake_api(method: str, payload: dict | None = None) -> Any:
        sent.append(dict(payload or {}))
        if method == "sendMessage" and payload.get("parse_mode") == "MarkdownV2":
            raise TelegramApiError("Bad Request: can't parse entities")
        return {"ok": True, "result": {"message_id": 1}}

    channel._api = fake_api  # type: ignore[method-assign]
    await channel.send(
        OutgoingMessage(
            content="unescaped _underscore_",
            format="markdown",
            metadata={"chat_id": "12345"},
        )
    )

    assert len(sent) == 2
    assert sent[0]["parse_mode"] == "MarkdownV2"
    assert "parse_mode" not in sent[1]
    assert sent[1]["text"] == "unescaped _underscore_"
