from __future__ import annotations

import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.channels.dingtalk import DingTalkChannel, DingTalkChannelConfig
from opensquilla.channels.types import IngressVerification


def _sdk_message(msg_id: str, conversation_id: str, sender_id: str, webhook: str) -> Any:
    return SimpleNamespace(
        message_id=msg_id,
        message_type="text",
        text=SimpleNamespace(content="hello"),
        sender_staff_id=sender_id,
        sender_nick=sender_id,
        conversation_id=conversation_id,
        conversation_type="1",
        session_webhook=webhook,
    )


async def test_dingtalk_reply_targets_triggering_message_not_latest_inbound() -> None:
    channel = DingTalkChannel(DingTalkChannelConfig(name="dingtalk"))

    raw_a = _sdk_message("msg-a", "conv-a", "staff-a", "https://example.invalid/hook-a")
    raw_b = _sdk_message("msg-b", "conv-b", "staff-b", "https://example.invalid/hook-b")

    incoming_a = channel.parse_message(raw_a)
    assert incoming_a is not None
    channel._last_incoming = raw_a

    # A message from another conversation arrives before A's reply is sent.
    assert channel.parse_message(raw_b) is not None
    channel._last_incoming = raw_b

    delivered: list[tuple[str, Any]] = []
    channel._handler = SimpleNamespace(
        reply_text=lambda content, incoming_message: delivered.append((content, incoming_message))
    )

    reply = channel.build_reply_message("answer for a", incoming_a)
    assert reply.metadata["dingtalk_reply_msg_id"] == "msg-a"

    await channel.send(reply)

    assert delivered == [("answer for a", raw_a)]


def test_dingtalk_streaming_reply_kwargs_pin_triggering_message() -> None:
    channel = DingTalkChannel(DingTalkChannelConfig(name="dingtalk"))

    incoming = channel.parse_message(
        _sdk_message("msg-1", "conv-1", "staff-1", "https://example.invalid/hook-1")
    )

    assert incoming is not None
    assert channel.streaming_reply_kwargs(incoming) == {"reply_msg_id": "msg-1"}
    assert incoming.provenance.verification == IngressVerification.SDK_SESSION
    assert incoming.provenance.principal is not None
    assert incoming.provenance.principal.subject_id == "staff-1"


async def test_dingtalk_session_webhook_expiry_fails_closed() -> None:
    channel = DingTalkChannel(DingTalkChannelConfig(name="dingtalk"))
    raw = _sdk_message(
        "msg-expired", "conv-expired", "staff", "https://example.invalid/expired"
    )
    raw.sessionWebhookExpiredTime = int((time.time() - 1) * 1000)
    incoming = channel.parse_message(raw)
    assert incoming is not None
    channel._handler = SimpleNamespace(reply_text=lambda *_args: None)

    with pytest.raises(RuntimeError, match="reply context.*expired"):
        await channel.send(channel.build_reply_message("late", incoming))


async def test_dingtalk_expired_explicit_reply_context_fails_closed() -> None:
    channel = DingTalkChannel(DingTalkChannelConfig(name="dingtalk"))
    raw_original = _sdk_message(
        "msg-original", "conv-original", "staff-original", "https://example.invalid/original"
    )
    incoming_original = channel.parse_message(raw_original)
    assert incoming_original is not None

    latest = raw_original
    for index in range(257):
        latest = _sdk_message(
            f"msg-{index}",
            f"conv-{index}",
            f"staff-{index}",
            f"https://example.invalid/{index}",
        )
        assert channel.parse_message(latest) is not None
    channel._last_incoming = latest

    delivered: list[tuple[str, Any]] = []
    channel._handler = SimpleNamespace(
        reply_text=lambda content, incoming_message: delivered.append((content, incoming_message))
    )
    reply = channel.build_reply_message("late answer", incoming_original)

    with pytest.raises(RuntimeError, match="reply context.*expired"):
        await channel.send(reply)

    assert delivered == []


async def test_dingtalk_streaming_expired_explicit_reply_context_fails_closed() -> None:
    channel = DingTalkChannel(DingTalkChannelConfig(name="dingtalk"))
    channel._client = object()
    channel._last_incoming = _sdk_message(
        "msg-latest", "conv-latest", "staff-latest", "https://example.invalid/latest"
    )

    async def _chunks() -> AsyncIterator[str]:
        yield "must not be sent"

    with pytest.raises(RuntimeError, match="reply context.*expired"):
        await channel.send_streaming(_chunks(), reply_msg_id="msg-expired")
