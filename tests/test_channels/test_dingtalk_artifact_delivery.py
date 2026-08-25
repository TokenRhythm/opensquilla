from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.channels.contract import (
    ChannelCapabilities,
    ChannelSendResult,
    ChannelSendStatus,
)
from opensquilla.channels.delivery_store import ChannelDeliveryStore, install_outbox
from opensquilla.channels.dingtalk import (
    DingTalkChannel,
    DingTalkChannelConfig,
    DingTalkDeliveryUncertainError,
)
from opensquilla.channels.registry import build_managed_channel
from opensquilla.channels.types import (
    ChannelArtifactDeliveryRequest,
    IncomingMessage,
)
from opensquilla.gateway.config import DingTalkChannelEntry


def _channel(*, cool_app_code: str = "") -> DingTalkChannel:
    return DingTalkChannel(
        DingTalkChannelConfig(
            client_id="app-key",
            client_secret="app-secret",
            robot_code="robot-code",
            cool_app_code=cool_app_code,
        )
    )


def _request(
    tmp_path: Path,
    *,
    name: str,
    content: bytes = b"artifact-body",
    group: bool = True,
    metadata: dict[str, Any] | None = None,
) -> ChannelArtifactDeliveryRequest:
    path = tmp_path / "artifact-source"
    path.write_bytes(content)
    if group:
        inbound_metadata: dict[str, Any] = {
            "conversation_kind": "group",
            "conversation_type": "2",
            "conversation_id": "cid-group-1",
            "native_chat_id": "cid-group-1",
            "is_group": True,
            "sender_staff_id": "staff-ignored-for-group",
        }
        channel_id = "cid-group-1"
    else:
        inbound_metadata = {
            "conversation_kind": "dm",
            "conversation_type": "1",
            "conversation_id": "cid-dm-1",
            "native_chat_id": "cid-dm-1",
            "is_group": False,
            "sender_staff_id": "staff-1",
        }
        channel_id = "cid-dm-1"
    if metadata:
        inbound_metadata.update(metadata)
    inbound = IncomingMessage(
        sender_id="sender-union-id",
        channel_id=channel_id,
        content="generate it",
        metadata=inbound_metadata,
    )
    return ChannelArtifactDeliveryRequest(
        inbound=inbound,
        artifact_id="artifact-1",
        file_path=str(path),
        name=name,
        mime_type="application/octet-stream",
        size=len(content),
    )


def test_gateway_entry_wires_native_artifact_credentials_to_adapter() -> None:
    adapter = build_managed_channel(
        DingTalkChannelEntry(
            name="dingtalk-main",
            client_id="app-key",
            client_secret="app-secret",
            robot_code="robot-code",
            cool_app_code="cool-app-code",
        )
    )

    assert isinstance(adapter, DingTalkChannel)
    assert adapter.config.robot_code == "robot-code"
    assert adapter.config.cool_app_code == "cool-app-code"
    assert adapter.capability_profile.artifact_delivery is True


@pytest.mark.asyncio
async def test_access_token_is_cached_within_provider_expiry() -> None:
    token_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        assert request.url.path == "/v1.0/oauth2/accessToken"
        token_requests += 1
        return httpx.Response(200, json={"accessToken": "token-1", "expireIn": 7200})

    channel = _channel()
    channel._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await channel._get_access_token() == "token-1"
        assert await channel._get_access_token() == "token-1"
    finally:
        await channel._http_client.aclose()

    assert token_requests == 1


@pytest.mark.asyncio
async def test_group_image_uses_official_upload_and_sample_image_contract(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        calls.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            assert json.loads(body) == {"appKey": "app-key", "appSecret": "app-secret"}
            return httpx.Response(200, json={"accessToken": "token-1", "expireIn": 7200})
        if request.url.path == "/media/upload":
            assert request.url.params["access_token"] == "token-1"
            assert b'name="type"' in body
            assert b"image" in body
            assert b'filename="chart.png"' in body
            assert b"artifact-body" in body
            return httpx.Response(200, json={"errcode": 0, "media_id": "media-1"})
        assert request.url.path == "/v1.0/robot/groupMessages/send"
        assert request.headers["x-acs-dingtalk-access-token"] == "token-1"
        payload = json.loads(body)
        assert payload == {
            "robotCode": "robot-code",
            "msgKey": "sampleImageMsg",
            "msgParam": json.dumps(
                {"photoURL": "media-1"}, ensure_ascii=False, separators=(",", ":")
            ),
            "openConversationId": "cid-group-1",
            "coolAppCode": "cool-app-code",
        }
        return httpx.Response(200, json={"processQueryKey": "process-1"})

    channel = _channel(cool_app_code="cool-app-code")
    channel._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await channel.deliver_artifact(_request(tmp_path, name="chart.png"))
    finally:
        await channel._http_client.aclose()

    assert result.status == ChannelSendStatus.SENT
    assert result.provider_file_id == "media-1"
    assert result.provider_message_id == "process-1"
    assert calls == [
        "/v1.0/oauth2/accessToken",
        "/media/upload",
        "/v1.0/robot/groupMessages/send",
    ]


@pytest.mark.asyncio
async def test_dm_file_uses_staff_id_and_official_sample_file_contract(tmp_path: Path) -> None:
    payloads: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "token-1", "expireIn": 7200})
        if request.url.path == "/media/upload":
            assert b"file" in body
            assert b'filename="report.xlsx"' in body
            return httpx.Response(200, json={"errcode": 0, "media_id": "media-file"})
        assert request.url.path == "/v1.0/robot/oToMessages/batchSend"
        payloads.append(json.loads(body))
        return httpx.Response(
            200,
            json={
                "processQueryKey": "process-dm",
                "invalidStaffIdList": [],
                "flowControlledStaffIdList": [],
            },
        )

    channel = _channel(cool_app_code="cool-app-only-for-group")
    channel._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await channel.deliver_artifact(
            _request(tmp_path, name="report.xlsx", group=False)
        )
    finally:
        await channel._http_client.aclose()

    assert result.status == ChannelSendStatus.SENT
    assert payloads == [
        {
            "robotCode": "robot-code",
            "msgKey": "sampleFile",
            "msgParam": json.dumps(
                {
                    "mediaId": "media-file",
                    "fileName": "report.xlsx",
                    "fileType": "xlsx",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "userIds": ["staff-1"],
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["notes.txt", "photo.jpeg"])
async def test_unsupported_extension_is_a_single_file_zip(
    tmp_path: Path,
    name: str,
) -> None:
    channel = _channel()
    observed: dict[str, Any] = {}

    async def fake_upload_media(**kwargs: Any) -> tuple[str, str]:
        observed.update(kwargs)
        prepared_path = kwargs["path"]
        observed["prepared_path"] = str(prepared_path)
        with zipfile.ZipFile(prepared_path) as archive:
            observed["members"] = archive.namelist()
            observed["content"] = archive.read(name)
        return "token-1", "media-zip"

    async def fake_send_uploaded_media(**kwargs: Any) -> ChannelSendResult:
        observed.update({f"send_{key}": value for key, value in kwargs.items()})
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
            target_id=kwargs["target_id"],
            provider_message_id="process-zip",
            provider_file_id=kwargs["media_id"],
        )

    channel._upload_media = fake_upload_media  # type: ignore[method-assign]
    channel._send_uploaded_media = fake_send_uploaded_media  # type: ignore[method-assign]

    result = await channel.deliver_artifact(_request(tmp_path, name=name))

    assert result.status == ChannelSendStatus.SENT
    assert observed["filename"] == f"{name}.zip"
    assert observed["send_file_type"] == "zip"
    assert observed["media_type"] == "file"
    assert observed["members"] == [name]
    assert observed["content"] == b"artifact-body"
    assert not Path(observed["prepared_path"]).exists()


@pytest.mark.asyncio
async def test_missing_dm_staff_context_fails_before_network(tmp_path: Path) -> None:
    network_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("network must not be called")

    channel = _channel()
    channel._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = _request(
        tmp_path,
        name="report.pdf",
        group=False,
        metadata={"sender_staff_id": None},
    )
    try:
        result = await channel.deliver_artifact(request)
    finally:
        await channel._http_client.aclose()

    assert result.status == ChannelSendStatus.UNSUPPORTED
    assert network_calls == 0


@pytest.mark.asyncio
async def test_group_does_not_guess_target_from_generic_channel_id(tmp_path: Path) -> None:
    channel = _channel()
    request = _request(
        tmp_path,
        name="report.pdf",
        metadata={"native_chat_id": None, "conversation_id": None},
    )

    result = await channel.deliver_artifact(request)

    assert result.status == ChannelSendStatus.UNSUPPORTED
    assert channel._http_client is None


@pytest.mark.asyncio
async def test_oversize_artifact_fails_before_network(tmp_path: Path) -> None:
    path = tmp_path / "large.pdf"
    with path.open("wb") as handle:
        handle.truncate(20 * 1024 * 1024 + 1)
    request = _request(tmp_path, name="placeholder.pdf")
    request = ChannelArtifactDeliveryRequest(
        inbound=request.inbound,
        artifact_id=request.artifact_id,
        file_path=str(path),
        name="large.pdf",
        mime_type="application/pdf",
        size=path.stat().st_size,
    )
    channel = _channel()

    result = await channel.deliver_artifact(request)

    assert result.status == ChannelSendStatus.UNSUPPORTED
    assert result.reason == "DingTalk artifacts must not exceed 20 MB"
    assert channel._http_client is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upload_response", "expected_reason"),
    [
        (
            {"errcode": 0},
            "DingTalk media upload was rejected code=missing_media_id",
        ),
        (
            {"media_id": "media-without-success-code"},
            "DingTalk media upload was rejected code=missing_success_errcode",
        ),
    ],
)
async def test_invalid_upload_success_response_never_attempts_visible_send(
    tmp_path: Path,
    upload_response: dict[str, Any],
    expected_reason: str,
) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "token-1", "expireIn": 7200})
        if request.url.path == "/media/upload":
            return httpx.Response(200, json=upload_response)
        raise AssertionError("visible send must not run without media_id")

    channel = _channel()
    channel._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await channel.deliver_artifact(_request(tmp_path, name="report.pdf"))
    finally:
        await channel._http_client.aclose()

    assert result.status == ChannelSendStatus.FAILED
    assert result.reason == expected_reason
    assert calls == ["/v1.0/oauth2/accessToken", "/media/upload"]


@pytest.mark.asyncio
async def test_explicit_invalid_upload_token_refreshes_once(tmp_path: Path) -> None:
    token_requests = 0
    uploads = 0
    sends = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests, uploads, sends
        if request.url.path == "/v1.0/oauth2/accessToken":
            token_requests += 1
            return httpx.Response(
                200,
                json={"accessToken": f"token-{token_requests}", "expireIn": 7200},
            )
        if request.url.path == "/media/upload":
            uploads += 1
            if uploads == 1:
                return httpx.Response(
                    200,
                    json={"errcode": 40014, "errmsg": "invalid access token"},
                )
            assert request.url.params["access_token"] == "token-2"
            return httpx.Response(200, json={"errcode": 0, "media_id": "media-1"})
        sends += 1
        assert request.headers["x-acs-dingtalk-access-token"] == "token-2"
        return httpx.Response(200, json={"processQueryKey": "process-1"})

    channel = _channel()
    channel._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await channel.deliver_artifact(_request(tmp_path, name="report.pdf"))
    finally:
        await channel._http_client.aclose()

    assert result.status == ChannelSendStatus.SENT
    assert (token_requests, uploads, sends) == (2, 2, 1)


@pytest.mark.asyncio
async def test_visible_transport_failure_is_not_retried_and_outbox_is_unknown(
    tmp_path: Path,
) -> None:
    delivery_store = ChannelDeliveryStore(tmp_path / "delivery.sqlite")
    visible_send_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal visible_send_calls
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "secret-token", "expireIn": 7200})
        if request.url.path == "/media/upload":
            return httpx.Response(200, json={"errcode": 0, "media_id": "media-1"})
        visible_send_calls += 1
        raise httpx.ConnectError("synthetic connection failure", request=request)

    channel = _channel()
    channel._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    channel._delivery_store = delivery_store
    channel._delivery_channel_name = "dingtalk-main"
    install_outbox(channel)
    request = _request(tmp_path, name="report.pdf")
    try:
        with pytest.raises(DingTalkDeliveryUncertainError) as exc_info:
            await channel.deliver_artifact(request)
    finally:
        await channel._http_client.aclose()

    assert visible_send_calls == 1
    assert "secret-token" not in str(exc_info.value)
    assert request.file_path not in str(exc_info.value)
    with sqlite3.connect(delivery_store.path) as connection:
        row = connection.execute(
            "SELECT state, error_message, message_json FROM channel_outbox"
        ).fetchone()
    assert row is not None
    assert row[0] == "unknown"
    assert row[1] == (
        "DingTalkDeliveryUncertainError: contextual artifact delivery failed"
    )
    assert "secret-token" not in row[2]
    assert request.file_path not in row[2]
    delivery_store.close()


@pytest.mark.parametrize(
    "provider_code",
    [
        "send.byToken.tooFast",
        "send.too.fast",
        "too.many.group",
        "too.many.people",
    ],
)
@pytest.mark.asyncio
async def test_explicit_rate_limit_is_failed_retryable_without_replay(
    tmp_path: Path,
    provider_code: str,
) -> None:
    visible_send_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal visible_send_calls
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "token-1", "expireIn": 7200})
        if request.url.path == "/media/upload":
            return httpx.Response(200, json={"errcode": 0, "media_id": "media-1"})
        visible_send_calls += 1
        return httpx.Response(400, json={"code": provider_code})

    channel = _channel()
    channel._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await channel.deliver_artifact(_request(tmp_path, name="report.pdf"))
    finally:
        await channel._http_client.aclose()

    assert result.status == ChannelSendStatus.FAILED
    assert result.retryable is True
    assert visible_send_calls == 1


@pytest.mark.asyncio
async def test_media_upload_access_token_is_redacted_from_httpx_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(
                200,
                json={"accessToken": "secret-query-token", "expireIn": 7200},
            )
        if request.url.path == "/media/upload":
            assert request.url.params["access_token"] == "secret-query-token"
            return httpx.Response(200, json={"errcode": 0, "media_id": "media-1"})
        return httpx.Response(200, json={"processQueryKey": "process-1"})

    channel = _channel()
    channel._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with caplog.at_level("INFO", logger="httpx"):
        try:
            result = await channel.deliver_artifact(_request(tmp_path, name="report.pdf"))
        finally:
            await channel._http_client.aclose()

    assert result.status == ChannelSendStatus.SENT
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-query-token" not in rendered_logs
    assert "access_token=<redacted>" in rendered_logs


@pytest.mark.asyncio
async def test_dm_flow_control_list_is_not_reported_as_sent(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "token-1", "expireIn": 7200})
        if request.url.path == "/media/upload":
            return httpx.Response(200, json={"errcode": 0, "media_id": "media-1"})
        return httpx.Response(
            200,
            json={
                "processQueryKey": "process-1",
                "invalidStaffIdList": [],
                "flowControlledStaffIdList": ["staff-1"],
            },
        )

    channel = _channel()
    channel._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await channel.deliver_artifact(
            _request(tmp_path, name="report.pdf", group=False)
        )
    finally:
        await channel._http_client.aclose()

    assert result.status == ChannelSendStatus.FAILED
    assert result.retryable is True
    assert "flowControlledStaffIdList" in result.reason


@pytest.mark.asyncio
async def test_missing_process_query_key_is_an_unknown_visible_result(tmp_path: Path) -> None:
    visible_send_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal visible_send_calls
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "token-1", "expireIn": 7200})
        if request.url.path == "/media/upload":
            return httpx.Response(200, json={"errcode": 0, "media_id": "media-1"})
        visible_send_calls += 1
        return httpx.Response(200, json={})

    channel = _channel()
    channel._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(DingTalkDeliveryUncertainError, match="processQueryKey"):
            await channel.deliver_artifact(_request(tmp_path, name="report.pdf"))
    finally:
        await channel._http_client.aclose()

    assert visible_send_calls == 1


@pytest.mark.asyncio
async def test_missing_robot_code_preserves_text_only_upgrade_behavior(tmp_path: Path) -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="app-key", client_secret="app-secret")
    )
    request = _request(tmp_path, name="report.pdf")

    result = await channel.deliver_artifact(request)

    assert result.status == ChannelSendStatus.UNSUPPORTED
    assert channel._http_client is None
    assert not channel.capability_profile.artifact_delivery
    assert callable(channel.send)
