from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from opensquilla.channels.discord import DiscordChannel, DiscordChannelConfig
from opensquilla.channels.slack import SlackChannel


async def _two_chunks() -> AsyncIterator[str]:
    yield "first chunk"
    yield "second chunk"


async def test_discord_send_streaming_surfaces_rejected_edit() -> None:
    config = DiscordChannelConfig(token="token", default_channel_id="123")
    channel = DiscordChannel(config=config)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(400, json={"code": 50035, "message": "Invalid Form Body"})

    channel._client = httpx.AsyncClient(
        base_url=config.api_base, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await channel.send_streaming(_two_chunks(), update_interval_ms=0)
    finally:
        await channel._client.aclose()


async def test_slack_send_streaming_surfaces_edit_api_error() -> None:
    channel = SlackChannel(token="xoxb-dummy", slack_channel_id="C123")

    class Client:
        async def api_call(self, method: str, **_kwargs: object) -> dict[str, object]:
            if method == "chat.postMessage":
                return {"ok": True, "ts": "111.222"}
            return {"ok": False, "error": "msg_too_long"}

    channel._client = Client()
    with pytest.raises(RuntimeError, match="msg_too_long"):
        await channel.send_streaming(_two_chunks(), update_interval_ms=0)
