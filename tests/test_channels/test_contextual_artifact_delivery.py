from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.artifacts import ArtifactStore
from opensquilla.channels.artifact_delivery import (
    can_deliver_channel_files,
    deliver_artifacts_as_channel_files,
)
from opensquilla.channels.contract import (
    ChannelCapabilities,
    ChannelCapabilityProfile,
    ChannelPlatformCategories,
    ChannelSendResult,
    channel_capability_evidence,
    channel_platform_manifest,
)
from opensquilla.channels.delivery_store import ChannelDeliveryStore, install_outbox
from opensquilla.channels.types import (
    ChannelArtifactDeliveryRequest,
    IncomingMessage,
    OutgoingMessage,
)


def _config(media_root: Path) -> SimpleNamespace:
    return SimpleNamespace(attachments=SimpleNamespace(media_root=str(media_root)))


def _inbound() -> IncomingMessage:
    return IncomingMessage(
        sender_id="user-1",
        channel_id="chat-1",
        content="generate a report",
        metadata={"is_group": False, "private_context": "do-not-persist"},
    )


@pytest.mark.asyncio
async def test_contextual_artifact_delivery_is_preferred_and_carries_verified_ref(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"report body",
        session_id="session-1",
        session_key="agent:main:channel:session-1",
        name="report.txt",
        mime="text/plain",
        source="test",
    )
    inbound = _inbound()
    received: list[ChannelArtifactDeliveryRequest] = []

    class ContextualChannel:
        capability_profile = ChannelCapabilityProfile(
            channel_type="contextual",
            artifact_delivery=True,
        )

        async def deliver_artifact(
            self,
            request: ChannelArtifactDeliveryRequest,
        ) -> ChannelSendResult:
            assert request.inbound is inbound
            assert request.artifact_id == ref.id
            assert request.name == "report.txt"
            assert request.mime_type == "text/plain"
            assert request.size == len(b"report body")
            assert Path(request.file_path).name == "report.txt"
            assert Path(request.file_path).read_bytes() == b"report body"
            received.append(request)
            return ChannelSendResult.sent(
                capability=ChannelCapabilities.ARTIFACT_DELIVERY,
                target_id=request.inbound.channel_id,
                provider_message_id="message-1",
                provider_file_id="file-1",
            )

        async def send_file(self, *_args: object) -> None:
            raise AssertionError("legacy send_file must not run when deliver_artifact exists")

    channel = ContextualChannel()

    assert can_deliver_channel_files(channel) is True
    undelivered = await deliver_artifacts_as_channel_files(
        channel,
        inbound,
        [ref.to_dict()],
        _config(tmp_path),
    )

    assert undelivered == []
    assert len(received) == 1


@pytest.mark.asyncio
async def test_artifact_delivery_keeps_legacy_send_file_signature(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"legacy",
        session_id="session-1",
        session_key="agent:main:channel:session-1",
        name="legacy.txt",
        mime="text/plain",
        source="test",
    )
    calls: list[tuple[str, str, bytes]] = []

    class LegacyChannel:
        capability_profile = ChannelCapabilityProfile(
            channel_type="legacy",
            native_file_upload=True,
        )

        async def send_file(self, channel_id: str, file_path: str) -> None:
            calls.append((channel_id, Path(file_path).name, Path(file_path).read_bytes()))

    undelivered = await deliver_artifacts_as_channel_files(
        LegacyChannel(),
        _inbound(),
        [ref.to_dict()],
        _config(tmp_path),
    )

    assert undelivered == []
    assert calls == [("chat-1", "legacy.txt", b"legacy")]


def test_contextual_method_backs_manifest_and_capability_evidence() -> None:
    class ContextualChannel:
        capability_profile = ChannelCapabilityProfile(
            channel_type="contextual",
            native_file_upload=True,
            artifact_delivery=True,
        )

        async def deliver_artifact(self, request: ChannelArtifactDeliveryRequest) -> None:
            del request

    channel = ContextualChannel()
    manifest = channel_platform_manifest(channel)
    evidence = channel_capability_evidence(channel)

    assert manifest is not None
    assert manifest.supports(ChannelPlatformCategories.FILES)
    assert evidence[ChannelCapabilities.ARTIFACT_DELIVERY]["implemented"] is True
    assert evidence[ChannelCapabilities.ARTIFACT_DELIVERY]["methods"] == [
        "deliver_artifact"
    ]
    assert evidence[ChannelCapabilities.NATIVE_FILE_UPLOAD]["implemented"] is True


@pytest.mark.asyncio
async def test_contextual_artifact_outbox_persists_only_safe_summary(tmp_path: Path) -> None:
    delivery_store = ChannelDeliveryStore(tmp_path / "channel_delivery.sqlite")

    class ContextualChannel:
        _delivery_store = delivery_store
        _delivery_channel_name = "contextual-main"
        capability_profile = ChannelCapabilityProfile(
            channel_type="contextual",
            artifact_delivery=True,
        )

        async def send(self, message: OutgoingMessage) -> None:
            del message

        async def deliver_artifact(
            self,
            request: ChannelArtifactDeliveryRequest,
        ) -> ChannelSendResult:
            return ChannelSendResult.sent(
                capability=ChannelCapabilities.ARTIFACT_DELIVERY,
                target_id=request.inbound.channel_id,
                provider_message_id="provider-message-1",
                provider_file_id="provider-file-1",
            )

    channel = ContextualChannel()
    install_outbox(channel)
    request = ChannelArtifactDeliveryRequest(
        inbound=_inbound(),
        artifact_id="artifact-1",
        file_path="/private/local/generated/report.txt",
        name="../../report.txt",
        mime_type="text/plain",
        size=42,
    )

    await channel.deliver_artifact(request)

    with sqlite3.connect(delivery_store.path) as connection:
        row = connection.execute(
            "SELECT target_id, message_json, provider_message_id, provider_file_id "
            "FROM channel_outbox"
        ).fetchone()

    assert row is not None
    target_id, message_json, provider_message_id, provider_file_id = row
    payload = json.loads(message_json)
    assert target_id == "chat-1"
    assert payload["metadata"].items() >= {
        "outbox_operation": "deliver_artifact",
        "artifact_id": "artifact-1",
        "artifact_name": "report.txt",
        "artifact_mime_type": "text/plain",
        "artifact_size": 42,
    }.items()
    assert "/private/local/generated/report.txt" not in message_json
    assert "do-not-persist" not in message_json
    assert "generate a report" not in message_json
    assert provider_message_id == "provider-message-1"
    assert provider_file_id == "provider-file-1"
    delivery_store.close()


@pytest.mark.asyncio
async def test_contextual_artifact_outbox_redacts_exception_request(tmp_path: Path) -> None:
    delivery_store = ChannelDeliveryStore(tmp_path / "channel_delivery.sqlite")

    class FailingChannel:
        _delivery_store = delivery_store
        _delivery_channel_name = "contextual-main"
        capability_profile = ChannelCapabilityProfile(
            channel_type="contextual",
            artifact_delivery=True,
        )

        async def send(self, message: OutgoingMessage) -> None:
            del message

        async def deliver_artifact(self, request: ChannelArtifactDeliveryRequest) -> None:
            raise RuntimeError(f"failed request: {request!r}")

    channel = FailingChannel()
    install_outbox(channel)
    request = ChannelArtifactDeliveryRequest(
        inbound=_inbound(),
        artifact_id="artifact-1",
        file_path="/private/local/generated/report.txt",
        name="report.txt",
        mime_type="text/plain",
        size=42,
    )

    with pytest.raises(RuntimeError, match="failed request"):
        await channel.deliver_artifact(request)

    with sqlite3.connect(delivery_store.path) as connection:
        error_message = connection.execute(
            "SELECT error_message FROM channel_outbox"
        ).fetchone()[0]

    assert error_message == "RuntimeError: contextual artifact delivery failed"
    assert request.file_path not in error_message
    assert "do-not-persist" not in error_message
    delivery_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_keyword", [False, True])
async def test_malformed_contextual_artifact_call_never_persists_raw_arguments(
    tmp_path: Path,
    use_keyword: bool,
) -> None:
    delivery_store = ChannelDeliveryStore(tmp_path / "channel_delivery.sqlite")
    malformed = {
        "file_path": "/private/local/generated/secret-report.txt",
        "access_token": "secret-provider-token",
        "inbound": {"metadata": {"private_context": "private-user-prompt"}},
    }

    class FailingChannel:
        _delivery_store = delivery_store
        _delivery_channel_name = "contextual-main"
        capability_profile = ChannelCapabilityProfile(
            channel_type="contextual",
            artifact_delivery=True,
        )

        async def send(self, message: OutgoingMessage) -> None:
            del message

        async def deliver_artifact(self, request: object) -> None:
            raise RuntimeError(f"malformed request: {request!r}")

    channel = FailingChannel()
    install_outbox(channel)

    with pytest.raises((RuntimeError, TypeError)):
        if use_keyword:
            await channel.deliver_artifact(  # type: ignore[call-arg]
                request=malformed,
                target_id="/private/local/generated/sensitive-target",
            )
        else:
            await channel.deliver_artifact(malformed)  # type: ignore[arg-type]

    with sqlite3.connect(delivery_store.path) as connection:
        target_id, message_json, error_message = connection.execute(
            "SELECT target_id, message_json, error_message FROM channel_outbox"
        ).fetchone()

    persisted = f"{target_id}\n{message_json}\n{error_message}"
    assert target_id == ""
    assert "secret-report.txt" not in persisted
    assert "secret-provider-token" not in persisted
    assert "private-user-prompt" not in persisted
    assert "sensitive-target" not in persisted
    expected_error_type = "TypeError" if use_keyword else "RuntimeError"
    assert error_message == (
        f"{expected_error_type}: contextual artifact delivery failed"
    )
    delivery_store.close()


@pytest.mark.asyncio
async def test_malformed_contextual_artifact_result_reason_is_not_persisted(
    tmp_path: Path,
) -> None:
    delivery_store = ChannelDeliveryStore(tmp_path / "channel_delivery.sqlite")
    secret_path = "/private/local/generated/secret-report.txt"

    class FailingChannel:
        _delivery_store = delivery_store
        _delivery_channel_name = "contextual-main"
        capability_profile = ChannelCapabilityProfile(
            channel_type="contextual",
            artifact_delivery=True,
        )

        async def send(self, message: OutgoingMessage) -> None:
            del message

        async def deliver_artifact(self, request: object) -> ChannelSendResult:
            return ChannelSendResult.failed(
                capability=ChannelCapabilities.ARTIFACT_DELIVERY,
                reason=f"invalid local path: {request!r}",
            )

    channel = FailingChannel()
    install_outbox(channel)

    result = await channel.deliver_artifact({"file_path": secret_path})  # type: ignore[arg-type]

    assert result.status.value == "failed"
    with sqlite3.connect(delivery_store.path) as connection:
        target_id, message_json, error_message = connection.execute(
            "SELECT target_id, message_json, error_message FROM channel_outbox"
        ).fetchone()
    assert target_id == ""
    assert secret_path not in message_json
    assert error_message == ""
    delivery_store.close()
