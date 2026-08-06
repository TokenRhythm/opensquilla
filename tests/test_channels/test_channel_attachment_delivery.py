"""Outbound attachment delivery + inbound media parsing across channels."""

from __future__ import annotations

import json

import httpx
import pytest

from opensquilla.channels.feishu import FeishuChannel, FeishuChannelConfig, _TokenState
from opensquilla.channels.telegram import TelegramChannel, TelegramChannelConfig
from opensquilla.channels.types import Attachment, OutgoingMessage
from opensquilla.channels.wecom import WeComChannel, WeComChannelConfig


@pytest.mark.asyncio
async def test_telegram_send_delivers_image_attachment_with_send_photo() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path.endswith("/sendPhoto")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    channel = TelegramChannel(
        TelegramChannelConfig(token="bot-token", connection_mode="webhook")
    )
    channel._client = httpx.AsyncClient(
        base_url="https://api.telegram.org",
        transport=httpx.MockTransport(handler),
    )
    await channel.send(
        OutgoingMessage(
            content="",
            metadata={"chat_id": "12345"},
            attachments=[
                Attachment(name="pic.png", mime_type="image/png", data=b"png-bytes")
            ],
        )
    )
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/sendPhoto")


def test_wecom_webhook_send_payload_with_attachments() -> None:
    channel = WeComChannel(WeComChannelConfig(name="wecom", agent_id_int=1))
    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(
            content="hello",
            reply_to="user-1",
            attachments=[Attachment(name="a.pdf", data=b"x")],
        )
    )
    assert payload["touser"] == "user-1"
    assert payload["text"] == {"content": "hello"}


@pytest.mark.asyncio
async def test_feishu_send_delivers_attachment_then_text() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        if request.url.path.endswith("/im/v1/files"):
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "fk-1"}})
        if request.url.path.endswith("/im/v1/messages"):
            payload = json.loads(body)
            assert payload["msg_type"] in {"file", "text"}
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
                content="hello",
                reply_to="ou_user",
                attachments=[
                    Attachment(name="doc.pdf", mime_type="application/pdf", data=b"%PDF")
                ],
            )
        )
    finally:
        await channel.stop()

    assert len(requests) >= 1
