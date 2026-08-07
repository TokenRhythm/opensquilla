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


@pytest.mark.asyncio
async def test_qq_send_file_uploads_and_sends_media_message(tmp_path) -> None:
    """QQ send_file uploads local bytes via file_data then sends msg_type=7."""

    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from opensquilla.channels.qq import QQChannel, QQChannelConfig

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))
    uploaded: dict = {}

    async def fake_upload(route, **kwargs):
        uploaded["json"] = kwargs.get("json", {})
        return {"file_info": "fi-123"}

    channel.api = SimpleNamespace(
        _http=SimpleNamespace(request=fake_upload),
        post_c2c_message=AsyncMock(),
        post_group_message=AsyncMock(),
    )

    tmp = tmp_path / "qq-test-file.txt"
    tmp.write_text("hello qq", encoding="utf-8")
    result = await channel.send_file("openid-1", str(tmp), chat_type="c2c", file_name="qq-test-file.txt")

    assert uploaded["json"]["file_type"] == 4
    assert uploaded["json"]["file_name"] == "qq-test-file.txt"
    assert "hello qq" in __import__("base64").b64decode(uploaded["json"]["file_data"]).decode()
    channel.api.post_c2c_message.assert_awaited_once()
    kwargs = channel.api.post_c2c_message.await_args.kwargs
    assert kwargs["msg_type"] == 7
    assert kwargs["media"] == {"file_info": "fi-123"}
    assert result.target_id == "openid-1"


@pytest.mark.asyncio
async def test_qq_send_with_attachment_delivers_file_not_degrade_text() -> None:
    """QQ send() with attachments routes through send_file, not a text notice."""

    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from opensquilla.channels.qq import QQChannel, QQChannelConfig

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))
    uploads: list[dict] = []

    async def fake_upload(route, **kwargs):
        uploads.append(kwargs.get("json", {}))
        return {"file_info": "fi-456"}

    channel.api = SimpleNamespace(
        _http=SimpleNamespace(request=fake_upload),
        post_c2c_message=AsyncMock(),
        post_group_message=AsyncMock(),
    )
    await channel.send(
        OutgoingMessage(
            content="",
            metadata={"chat_type": "c2c", "openid": "openid-1", "msg_id": "m-1"},
            attachments=[
                Attachment(
                    name="a.txt",
                    mime_type="text/plain",
                    data=b"payload",
                )
            ],
        )
    )

    assert uploads, "expected a file upload"
    # The media message is sent via post_c2c_message (msg_type=7), not a text notice.
    channel.api.post_c2c_message.assert_awaited_once()
    send_kwargs = channel.api.post_c2c_message.await_args.kwargs
    assert send_kwargs["msg_type"] == 7
    assert "[attachment" not in str(send_kwargs)
    channel.api.post_group_message.assert_not_awaited()


def test_qq_artifact_delivery_gate_is_unlocked_by_capability_profile() -> None:
    """QQ send_file must not be gated off by its capability profile.

    Regression guard: the adapter implements the official rich-media send_file,
    so ``can_deliver_channel_files`` must return True. When this gate was left
    False, artifacts were only shown in the WebUI and the channel got the
    ``available in WebUI`` fallback line instead of the file.
    """

    from opensquilla.channels.artifact_delivery import can_deliver_channel_files
    from opensquilla.channels.qq import QQChannel, QQChannelConfig

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))

    assert can_deliver_channel_files(channel) is True
