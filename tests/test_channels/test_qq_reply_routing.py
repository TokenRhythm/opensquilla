from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

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
