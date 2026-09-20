"""Durable artifact sends cannot replay ambiguous provider side effects."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.artifacts import ArtifactStore
from opensquilla.channels.artifact_delivery import deliver_artifacts_as_channel_files
from opensquilla.channels.contract import ChannelCapabilityProfile, ChannelSendResult
from opensquilla.channels.delivery_store import ChannelDeliveryStore, install_outbox
from opensquilla.channels.types import (
    ChannelArtifactDeliveryRequest,
    IncomingMessage,
    IngressProvenance,
    OutgoingMessage,
)


def _request() -> ChannelArtifactDeliveryRequest:
    return ChannelArtifactDeliveryRequest(
        inbound=IncomingMessage(
            sender_id="user-1",
            channel_id="chat-1",
            content="private synthetic request",
            metadata={"native_thread_id": "thread-1", "private_context": "private detail"},
            provenance=IngressProvenance(
                provider="test", account_id="account-1", event_id="event-1"
            ),
        ),
        artifact_id="artifact-1",
        file_path="/private/local/output/report.txt",
        name="report.txt",
        mime_type="text/plain",
        size=6,
        session_id="session-1",
    )


class _Channel:
    capability_profile = ChannelCapabilityProfile(
        channel_type="test", artifact_delivery=True
    )

    def __init__(self, store: ChannelDeliveryStore) -> None:
        self._delivery_store = store
        self._delivery_channel_name = "test-main"
        self.requests: list[ChannelArtifactDeliveryRequest] = []

    async def send(self, message: OutgoingMessage) -> None:
        del message

    async def deliver_artifact(self, request: ChannelArtifactDeliveryRequest) -> ChannelSendResult:
        self.requests.append(request)
        return ChannelSendResult.sent(
            capability="artifact_delivery",
            target_id=request.inbound.channel_id,
            provider_message_id="message-1",
            provider_file_id="file-1",
        )


async def test_successful_artifact_replay_survives_store_restart(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite"
    store = ChannelDeliveryStore(path)
    channel = _Channel(store)
    install_outbox(channel)
    request = _request()
    first = await channel.deliver_artifact(request)
    assert len(channel.requests) == 1
    delivery_id = channel.requests[0].delivery_id
    assert delivery_id
    store.close()

    reopened = ChannelDeliveryStore(path)
    channel = _Channel(reopened)
    install_outbox(channel)
    # Temporary staging paths change between attempts and must not change the
    # identity of the stored artifact or cause another upload.
    replay = await channel.deliver_artifact(replace(request, file_path="/another/temp/report.txt"))
    assert replay == first
    assert channel.requests == []
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT send_id, message_json FROM channel_outbox").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == delivery_id
    assert "/private/local" not in rows[0][1]
    assert "private synthetic request" not in rows[0][1]
    assert "private detail" not in rows[0][1]
    reopened.close()


async def test_unknown_artifact_outcome_cannot_be_blindly_retried(tmp_path: Path) -> None:
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")

    class UncertainChannel(_Channel):
        async def deliver_artifact(self, request: ChannelArtifactDeliveryRequest) -> None:
            self.requests.append(request)
            raise TimeoutError("provider accepted upload, message response lost")

    channel = UncertainChannel(store)
    install_outbox(channel)
    request = _request()
    with pytest.raises(TimeoutError):
        await channel.deliver_artifact(request)
    replay = await channel.deliver_artifact(request)
    assert not replay.is_delivered()
    assert replay.retryable is False
    assert "unknown" in replay.reason
    assert len(channel.requests) == 1
    assert store.diagnostics("test-main")["outbox"]["unknown"]["count"] == 1
    store.close()


async def test_concurrent_artifact_attempt_does_not_start_second_upload(tmp_path: Path) -> None:
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")
    started = asyncio.Event()
    finish = asyncio.Event()

    class SlowChannel(_Channel):
        async def deliver_artifact(
            self, request: ChannelArtifactDeliveryRequest
        ) -> ChannelSendResult:
            self.requests.append(request)
            started.set()
            await finish.wait()
            return ChannelSendResult.sent(capability="artifact_delivery")

    channel = SlowChannel(store)
    install_outbox(channel)
    first = asyncio.create_task(channel.deliver_artifact(_request()))
    await started.wait()
    try:
        duplicate = await channel.deliver_artifact(_request())
        assert duplicate.retryable is False
        assert "pending" in duplicate.reason
        assert len(channel.requests) == 1
    finally:
        finish.set()
        await first
        store.close()


@pytest.mark.parametrize("field", ["account", "channel", "thread", "sender", "session", "artifact"])
async def test_reused_delivery_id_cannot_cross_artifact_or_reply_binding(
    tmp_path: Path, field: str
) -> None:
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")
    channel = _Channel(store)
    install_outbox(channel)
    request = replace(_request(), delivery_id="same-delivery")
    await channel.deliver_artifact(request)
    if field == "session":
        changed = replace(request, session_id="session-2")
    elif field == "artifact":
        changed = replace(request, artifact_id="artifact-2")
    else:
        inbound = request.inbound.model_copy(deep=True)
        if field == "account":
            inbound.provenance = replace(inbound.provenance, account_id="account-2")
        elif field == "channel":
            inbound.channel_id = "chat-2"
        elif field == "thread":
            inbound.metadata["native_thread_id"] = "thread-2"
        else:
            inbound.sender_id = "user-2"
        changed = replace(request, inbound=inbound)
    result = await channel.deliver_artifact(changed)
    assert not result.is_delivered()
    assert "does not match" in result.reason
    assert len(channel.requests) == 1
    assert store.diagnostics("test-main")["outbox"]["sent"]["count"] == 1
    store.close()


async def test_explicit_retryable_rejection_keeps_provider_delivery_id(tmp_path: Path) -> None:
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")

    class RejectedChannel(_Channel):
        async def deliver_artifact(
            self, request: ChannelArtifactDeliveryRequest
        ) -> ChannelSendResult:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ChannelSendResult.failed(capability="artifact_delivery", retryable=True)
            return ChannelSendResult.sent(capability="artifact_delivery")

    channel = RejectedChannel(store)
    install_outbox(channel)
    assert not (await channel.deliver_artifact(_request())).is_delivered()
    assert (await channel.deliver_artifact(_request())).is_delivered()
    assert len(channel.requests) == 2
    assert channel.requests[0].delivery_id == channel.requests[1].delivery_id
    assert store.diagnostics("test-main")["outbox"]["sent"]["count"] == 1
    store.close()


async def test_shared_pipeline_dedupes_legacy_file_delivery(tmp_path: Path) -> None:
    artifact_store = ArtifactStore(tmp_path / "media")
    ref = artifact_store.publish_bytes(
        b"report", session_id="session-1", session_key="test-session", name="report.txt",
        mime="text/plain", source="test",
    )
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")

    class LegacyChannel:
        capability_profile = ChannelCapabilityProfile(
            channel_type="legacy", native_file_upload=True
        )
        _delivery_store = store
        _delivery_channel_name = "test-main"
        calls = 0

        async def send(self, message: OutgoingMessage) -> None:
            del message

        async def send_file(self, channel_id: str, file_path: str) -> ChannelSendResult:
            self.calls += 1
            assert Path(file_path).read_bytes() == b"report"
            return ChannelSendResult.sent(capability="native_file_upload", target_id=channel_id)

    channel = LegacyChannel()
    install_outbox(channel)
    config = SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path / "media")))
    for _ in range(2):
        assert await deliver_artifacts_as_channel_files(
            channel, _request().inbound, [ref.to_dict()], config, expected_session_id="session-1"
        ) == []
    assert channel.calls == 1
    assert store.diagnostics("test-main")["outbox"]["sent"]["count"] == 1
    store.close()


@pytest.mark.parametrize("spoof_session", [False, True])
async def test_shared_pipeline_rejects_other_sessions_artifact_before_any_send(
    tmp_path: Path, spoof_session: bool
) -> None:
    ref = ArtifactStore(tmp_path / "media").publish_bytes(
        b"private material", session_id="session-a", session_key="test-a",
        name="private.txt", mime="text/plain", source="test",
    )
    store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")
    channel = _Channel(store)
    install_outbox(channel)
    artifact = ref.to_dict()
    artifact["channel_download_url"] = "https://example.test/private-signed-link"
    if spoof_session:
        artifact["session_id"] = "session-b"
    result = await deliver_artifacts_as_channel_files(
        channel, _request().inbound, [artifact],
        SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path / "media"))),
        expected_session_id="session-b",
    )
    assert result == []  # No foreign name or signed URL is returned as a text fallback.
    assert channel.requests == []
    assert store.diagnostics("test-main")["outbox"] == {}
    store.close()
