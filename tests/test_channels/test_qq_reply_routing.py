from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.channels.qq import QQChannel, QQChannelConfig


def _make_channel() -> QQChannel:
    return QQChannel(QQChannelConfig(name="qq", app_id="app-id", app_secret="app-secret"))


def _raw_c2c(msg_id: str, openid: str, content: str) -> Any:
    return SimpleNamespace(
        id=msg_id,
        author=SimpleNamespace(user_openid=openid),
        content=content,
    )


def _raw_group(msg_id: str, member_openid: str, group_openid: str, content: str) -> Any:
    return SimpleNamespace(
        id=msg_id,
        author=SimpleNamespace(member_openid=member_openid),
        group_openid=group_openid,
        content=content,
    )


async def test_qq_streaming_reply_kwargs_pin_c2c_target() -> None:
    channel = _make_channel()
    channel._enqueue_message(_raw_c2c("m-1", "openid-1", "hi"), is_group=False)

    msg = await channel.receive()

    assert channel.streaming_reply_kwargs(msg) == {
        "chat_type": "c2c",
        "target": "openid-1",
        "msg_id": "m-1",
    }


async def test_qq_streaming_reply_kwargs_pin_group_target() -> None:
    channel = _make_channel()
    channel._enqueue_message(_raw_group("m-2", "member-1", "group-1", "hi"), is_group=True)

    msg = await channel.receive()

    assert channel.streaming_reply_kwargs(msg) == {
        "chat_type": "group",
        "target": "group-1",
        "msg_id": "m-2",
    }


async def test_qq_streamed_reply_targets_sender_even_after_newer_inbound() -> None:
    channel = _make_channel()
    channel.api = SimpleNamespace(post_c2c_message=AsyncMock(), post_group_message=AsyncMock())

    channel._enqueue_message(_raw_c2c("m-a", "openid-a", "question from a"), is_group=False)
    msg_a = await channel.receive()

    mid_stream = asyncio.Event()
    release = asyncio.Event()

    async def chunks() -> Any:
        yield "answer for a, part 1. "
        mid_stream.set()
        await release.wait()
        yield "part 2."

    stream_task = asyncio.create_task(
        channel.send_streaming(chunks(), **channel.streaming_reply_kwargs(msg_a))
    )
    await mid_stream.wait()

    # Another user's message is received while A's answer is still streaming.
    channel._enqueue_message(_raw_c2c("m-b", "openid-b", "unrelated"), is_group=False)
    await channel.receive()

    release.set()
    await asyncio.wait_for(stream_task, timeout=5)

    assert channel.api.post_c2c_message.await_count == 1
    kwargs = channel.api.post_c2c_message.await_args.kwargs
    assert "answer for a" in kwargs["content"]
    assert kwargs["openid"] == "openid-a"
    assert kwargs["msg_id"] == "m-a"
    assert kwargs["msg_seq"] == 1
    assert isinstance(kwargs["msg_seq"], int)


@pytest.mark.parametrize("chat_type", ["c2c", "group"])
@pytest.mark.parametrize("mime_type,file_type", [
    ("image/png", 1), ("image/jpeg", 1), ("video/mp4", 2), ("audio/silk", 3),
])
async def test_qq_media_reply_uses_sdk_url_upload_and_original_target(
    chat_type: str, mime_type: str, file_type: int,
) -> None:
    from opensquilla.channels.types import Attachment

    channel = _make_channel()
    channel.api = SimpleNamespace(
        post_c2c_file=AsyncMock(return_value={"file_info": "c2c-file"}),
        post_group_file=AsyncMock(return_value={"file_info": "group-file"}),
        post_c2c_message=AsyncMock(),
        post_group_message=AsyncMock(),
    )
    is_group = chat_type == "group"
    raw = (
        _raw_group("m-original", "user-original", "group-original", "request")
        if is_group else _raw_c2c("m-original", "user-original", "request")
    )
    channel._enqueue_message(raw, is_group=is_group)
    original = await channel.receive()
    channel._enqueue_message(_raw_c2c("m-other", "user-other", "other"), is_group=False)
    await channel.receive()
    message = channel.build_reply_message("media reply", original)
    message.attachments = [Attachment(
        name="media", mime_type=mime_type, url="https://media.example.test/media",
    )]

    await channel.send(message)

    target_key = "group_openid" if is_group else "openid"
    target = "group-original" if is_group else "user-original"
    upload = channel.api.post_group_file if is_group else channel.api.post_c2c_file
    send = channel.api.post_group_message if is_group else channel.api.post_c2c_message
    upload.assert_awaited_once_with(
        **{target_key: target}, file_type=file_type,
        url="https://media.example.test/media", srv_send_msg=False,
    )
    send.assert_awaited_once_with(
        **{target_key: target}, msg_type=7, content="media reply",
        media={"file_info": f"{chat_type}-file"}, msg_id="m-original", msg_seq=1,
    )


@pytest.mark.parametrize("attachment", [
    {"name": "report.pdf", "mime_type": "application/pdf", "url": "https://example.test/report.pdf"},
    {"name": "image.png", "mime_type": "image/png", "data": b"image"},
    {"name": "image.png", "mime_type": "image/png", "url": "file:///private/image.png"},
    {"name": "image.png", "mime_type": "image/png", "url": "http://127.0.0.1/image.png"},
    {"name": "image.png", "mime_type": "image/png", "url": "https://u:p@example.test/image.png"},
])
async def test_qq_unsupported_attachment_does_not_send_partial_text(
    attachment: dict[str, Any],
) -> None:
    from opensquilla.channels.types import Attachment, OutgoingMessage, UnsupportedChannelOperation

    channel = _make_channel()
    channel.api = SimpleNamespace(post_c2c_message=AsyncMock())
    message = OutgoingMessage(
        content="file ready",
        metadata={"chat_type": "c2c", "openid": "user-original", "msg_id": "m-original"},
        attachments=[Attachment(**attachment)],
    )
    with pytest.raises(UnsupportedChannelOperation):
        await channel.send(message)
    channel.api.post_c2c_message.assert_not_awaited()


async def test_qq_media_upload_must_return_file_info_before_message_send() -> None:
    from opensquilla.channels.types import Attachment, OutgoingMessage

    channel = _make_channel()
    channel.api = SimpleNamespace(
        post_c2c_file=AsyncMock(return_value={}), post_c2c_message=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="missing file_info"):
        await channel.send(OutgoingMessage(
            content="image",
            metadata={"chat_type": "c2c", "openid": "user-original", "msg_id": "m-original"},
            attachments=[Attachment(
                name="image.png", mime_type="image/png", url="https://example.test/image.png",
            )],
        ))
    channel.api.post_c2c_message.assert_not_awaited()


@pytest.mark.parametrize("suffix", ["csv", "xlsx", "pptx", "pdf", "png"])
async def test_qq_local_artifact_is_explicitly_unsupported_without_public_hosting(
    suffix: str,
) -> None:
    from opensquilla.channels.contract import ChannelSendStatus
    from opensquilla.channels.types import ChannelArtifactDeliveryRequest, IncomingMessage

    channel = _make_channel()
    channel.api = SimpleNamespace()
    result = await channel.deliver_artifact(ChannelArtifactDeliveryRequest(
        inbound=IncomingMessage(
            sender_id="user-original", channel_id="group-original", content="file"
        ),
        artifact_id="artifact-1", file_path=f"/nonexistent/report.{suffix}",
        name=f"report.{suffix}", mime_type="application/octet-stream", size=100,
    ))
    assert result.status is ChannelSendStatus.UNSUPPORTED
    assert result.target_id == "group-original"
    assert result.retryable is False


async def test_qq_artifact_outbox_records_one_safe_unsupported_delivery(tmp_path: Path) -> None:
    from opensquilla.channels.contract import ChannelSendStatus
    from opensquilla.channels.delivery_store import ChannelDeliveryStore, install_outbox
    from opensquilla.channels.types import ChannelArtifactDeliveryRequest, IncomingMessage

    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")
    channel = _make_channel()
    channel.api = SimpleNamespace()
    channel._delivery_store = store
    channel._delivery_channel_name = "qq-test"
    install_outbox(channel)
    path = tmp_path / "private-workspace" / "report.pdf"
    request = ChannelArtifactDeliveryRequest(
        inbound=IncomingMessage(sender_id="sender", channel_id="group", content="file"),
        artifact_id="artifact-1", file_path=str(path), name="report.pdf",
        mime_type="application/pdf", size=100, delivery_id="delivery-1",
    )

    try:
        result = await channel.deliver_artifact(request)
        assert result.status is ChannelSendStatus.UNSUPPORTED
        with sqlite3.connect(store.path) as connection:
            rows = connection.execute(
                "SELECT send_id, state, message_json, error_message FROM channel_outbox"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][:2] == ("delivery-1", "unsupported")
        assert str(path) not in rows[0][2]
        assert rows[0][3] == ""
    finally:
        store.close()
