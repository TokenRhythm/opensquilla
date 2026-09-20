from __future__ import annotations

import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.channels.delivery_store import ChannelDeliveryStore, install_outbox
from opensquilla.channels.discord import DiscordChannel, DiscordChannelConfig
from opensquilla.channels.types import (
    ChannelArtifactDeliveryRequest,
    IncomingMessage,
    IngressProvenance,
)


def _artifact(tmp_path: Path) -> ChannelArtifactDeliveryRequest:
    path = tmp_path / "report.txt"
    path.write_bytes(b"synthetic report")
    return ChannelArtifactDeliveryRequest(
        inbound=IncomingMessage(
            sender_id="user-1", channel_id="thread-1", content="report",
            metadata={"native_message_id": "message-1", "native_thread_id": "thread-1"},
            provenance=IngressProvenance(
                provider="discord", account_id="account-1", event_id="message-1"
            ),
        ),
        artifact_id="artifact-1", file_path=str(path), name="report.txt",
        mime_type="text/plain", size=path.stat().st_size, session_id="session-1",
    )


def _multipart(request: httpx.Request) -> tuple[dict[str, Any], bytes]:
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
        + request.content
    )
    parts = {
        part.get_param("name", header="content-disposition"): part
        for part in message.iter_parts()
    }
    payload = json.loads(parts["payload_json"].get_payload(decode=True))
    return payload, parts["files[0]"].get_payload(decode=True)


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
async def test_discord_unknown_file_send_is_not_replayed(
    tmp_path: Path, failure: str
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("response lost", request=request)
            return httpx.Response(503, json={"message": "response lost"})
        return httpx.Response(200, json={"id": "duplicate-message"})

    path = tmp_path / "outbox.sqlite"
    store = ChannelDeliveryStore(path)
    channel = DiscordChannel(DiscordChannelConfig(token="synthetic-token"))
    channel._client = httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(handler)
    )
    channel._delivery_store = store
    channel._delivery_channel_name = "discord-test"
    install_outbox(channel)
    artifact = _artifact(tmp_path)
    try:
        with pytest.raises(httpx.HTTPError):
            await channel.deliver_artifact(artifact)
        assert len(requests) == 1
        assert store.diagnostics("discord-test")["outbox"]["unknown"]["count"] == 1
        store.close()
        store = ChannelDeliveryStore(path)
        channel._delivery_store = store
        replay = await channel.deliver_artifact(artifact)
        assert not replay.is_delivered()
        assert not replay.retryable
        assert "unknown" in replay.reason
        assert len(requests) == 1
    finally:
        await channel.stop()
        store.close()


async def test_discord_rejected_send_reuses_nonce_and_success_is_deduped(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"retry_after": 1})
        return httpx.Response(200, json={"id": "result-1"})

    store = ChannelDeliveryStore(tmp_path / "outbox.sqlite")
    channel = DiscordChannel(DiscordChannelConfig(token="synthetic-token"))
    channel._client = httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(handler)
    )
    channel._delivery_store = store
    channel._delivery_channel_name = "discord-test"
    install_outbox(channel)
    artifact = _artifact(tmp_path)
    try:
        rejected = await channel.deliver_artifact(artifact)
        assert rejected.retryable and not rejected.is_delivered()
        accepted = await channel.deliver_artifact(artifact)
        assert accepted.is_delivered()
        assert (await channel.deliver_artifact(artifact)) == accepted
        assert len(requests) == 2
        payload, body = _multipart(requests[0])
        assert _multipart(requests[1]) == (payload, body)
        assert body == b"synthetic report"
        assert len(payload["nonce"]) <= 25
        assert payload["enforce_nonce"] is True
        assert payload["message_reference"] == {"message_id": "message-1"}
        assert payload["allowed_mentions"] == {"replied_user": False}
        assert all(req.url.path == "/api/v10/channels/thread-1/messages" for req in requests)
        assert store.diagnostics("discord-test")["outbox"]["sent"]["count"] == 1
    finally:
        await channel.stop()
        store.close()


async def test_discord_legacy_file_signature_sends_one_multipart_request(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "result-1"})

    channel = DiscordChannel(DiscordChannelConfig(token="synthetic-token"))
    channel._client = httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(handler)
    )
    try:
        result = await channel.send_file("channel-1", _artifact(tmp_path).file_path, "Report")
        assert result.is_delivered()
        assert result.provider_message_id == "result-1"
        assert _multipart(requests[0]) == ({"content": "Report"}, b"synthetic report")
        assert len(requests) == 1
    finally:
        await channel.stop()
