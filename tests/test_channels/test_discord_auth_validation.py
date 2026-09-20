from __future__ import annotations

import httpx
import pytest

from opensquilla.channels.discord import DiscordChannel, DiscordChannelConfig
from opensquilla.onboarding.channel_specs import get_channel_setup_spec


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_discord_gateway_url_fetch_uses_bot_token_auth_header() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v10/gateway/bot"
        assert request.headers["Authorization"] == "Bot bot-token"
        return httpx.Response(200, json={"url": "wss://gateway.discord.gg/"})

    channel = DiscordChannel(DiscordChannelConfig(token="bot-token"))
    channel._client = httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    )

    try:
        url = await channel._fetch_gateway_url()
    finally:
        await channel.stop()

    assert url == "wss://gateway.discord.gg/?v=10&encoding=json"
    assert len(requests) == 1


@pytest.mark.anyio
async def test_discord_sdk_receives_configured_gateway_intents() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="bot-token", intents=513))
    client = channel._create_gateway_client()
    try:
        assert client.intents.value == 513
    finally:
        await client.close()


def test_discord_gateway_spec_does_not_accept_interactions_public_key_as_auth() -> None:
    fields = {field.name: field for field in get_channel_setup_spec("discord").fields}

    assert fields["token"].secret is True
    assert fields["application_id"].secret is False
    assert "public_key" not in fields
