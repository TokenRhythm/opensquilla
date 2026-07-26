from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from opensquilla.channels.discord import DiscordChannel, DiscordChannelConfig
from opensquilla.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig
from opensquilla.channels.slack import SlackChannel
from opensquilla.channels.types import IncomingMessage


class _BodyRequest:
    def __init__(self, payload: dict, *, signing_secret: str | None = None) -> None:
        self._body = json.dumps(payload).encode()
        self.headers: dict[str, str] = {}
        if signing_secret is not None:
            timestamp = str(int(time.time()))
            signature = hmac.new(
                signing_secret.encode(),
                f"v0:{timestamp}:".encode() + self._body,
                hashlib.sha256,
            ).hexdigest()
            self.headers = {
                "X-Slack-Request-Timestamp": timestamp,
                "X-Slack-Signature": f"v0={signature}",
            }

    async def body(self) -> bytes:
        return self._body


@pytest.mark.asyncio
async def test_slack_webhook_dedupes_retried_event_callback() -> None:
    signing_secret = "signing-secret"
    channel = SlackChannel(
        token="xoxb-test",
        slack_channel_id="C1",
        signing_secret=signing_secret,
    )
    payload = {
        "type": "event_callback",
        "event_id": "Ev123",
        "event": {
            "type": "message",
            "user": "U1",
            "channel": "C1",
            "text": "draw an image",
            "ts": "1710000000.000100",
            "channel_type": "im",
        },
    }

    await channel._handle_webhook(  # noqa: SLF001
        _BodyRequest(payload, signing_secret=signing_secret)
    )
    await channel._handle_webhook(  # noqa: SLF001
        _BodyRequest(payload, signing_secret=signing_secret)
    )

    assert channel._queue.qsize() == 1  # noqa: SLF001
    assert (await channel.receive()).content == "draw an image"


@pytest.mark.asyncio
async def test_slack_webhook_without_signing_secret_fails_closed() -> None:
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C1")
    payload = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "user": "U1",
            "channel": "C1",
            "text": "unsigned input",
            "ts": "1710000000.000100",
        },
    }

    response = await channel._handle_webhook(_BodyRequest(payload))  # noqa: SLF001

    assert response.status_code == 503
    assert channel._queue.empty()  # noqa: SLF001


@pytest.mark.asyncio
async def test_msteams_enqueue_dedupes_retried_activity_id() -> None:
    channel = MSTeamsChannel(MSTeamsChannelConfig())
    msg = IncomingMessage(
        sender_id="u1",
        channel_id="conv1",
        content="make a deck",
        metadata={"activity_id": "activity-1"},
    )

    channel.enqueue(msg)
    channel.enqueue(msg)

    assert channel._queue.qsize() == 1  # noqa: SLF001
    assert (await channel.receive()).content == "make a deck"


@pytest.mark.asyncio
async def test_discord_gateway_dedupes_replayed_message_create() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    payload = {
        "id": "message-1",
        "channel_id": "channel-1",
        "content": "hello",
        "author": {"id": "user-1"},
        "mentions": [],
        "attachments": [],
    }

    await channel._handle_dispatch("MESSAGE_CREATE", payload)  # noqa: SLF001
    await channel._handle_dispatch("MESSAGE_CREATE", payload)  # noqa: SLF001

    assert channel._queue.qsize() == 1  # noqa: SLF001
    assert (await channel.receive()).content == "hello"
