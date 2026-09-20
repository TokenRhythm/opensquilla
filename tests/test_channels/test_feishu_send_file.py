from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from opensquilla.channels.feishu import FeishuChannel, FeishuChannelConfig, _TokenState
from opensquilla.channels.stream_policy import resolve_channel_stream_policy
from opensquilla.channels.types import (
    ChannelArtifactDeliveryRequest,
    IncomingMessage,
    OutgoingMessage,
)
from opensquilla.contracts.attachments import OPAQUE_ATTACHMENT_BYTES


@pytest.mark.asyncio
async def test_send_file_sends_image_to_private_open_id(tmp_path: Path) -> None:
    image_path = tmp_path / "result.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        if request.url.path == "/open-apis/im/v1/images":
            assert b'image_type"\r\n\r\nmessage' in body
            return httpx.Response(200, json={"code": 0, "data": {"image_key": "img-key"}})
        if request.url.path == "/open-apis/im/v1/messages":
            assert request.url.params["receive_id_type"] == "open_id"
            payload = json.loads(body)
            assert payload["receive_id"] == "ou_private_user"
            assert payload["msg_type"] == "image"
            assert json.loads(payload["content"]) == {"image_key": "img-key"}
            return httpx.Response(200, json={"code": 0})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    try:
        await channel.send_file("ou_private_user", str(image_path))
    finally:
        await channel.stop()

    assert [request.url.path for request in requests] == [
        "/open-apis/im/v1/images",
        "/open-apis/im/v1/messages",
    ]


def test_feishu_uses_final_only_channel_stream_policy() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )

    policy = resolve_channel_stream_policy(channel)

    assert policy.mode == "final_only"
    assert policy.relay_stream is False


def test_parse_event_maps_image_and_file_messages_to_attachments() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )

    image_msg = channel.parse_event(
        {
            "header": {"event_id": "evt-image"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_image",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "image",
                    "content": json.dumps({"image_key": "img-key"}),
                },
            },
        }
    )
    file_msg = channel.parse_event(
        {
            "header": {"event_id": "evt-file"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_file",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "file",
                    "content": json.dumps(
                        {"file_key": "file-key", "file_name": "report.pdf", "file_size": 12}
                    ),
                },
            },
        }
    )

    assert image_msg.content == "[image]"
    assert image_msg.attachments[0].name == "image.png"
    assert image_msg.attachments[0].mime_type == "image/png"
    assert image_msg.attachments[0].metadata["feishu_resource_key"] == "img-key"
    assert image_msg.attachments[0].metadata["feishu_resource_type"] == "image"
    assert image_msg.attachments[0].metadata["feishu_message_id"] == "om_image"

    assert file_msg.content == "[file]"
    assert file_msg.attachments[0].name == "report.pdf"
    assert file_msg.attachments[0].mime_type == "application/pdf"
    assert file_msg.attachments[0].size == 12
    assert file_msg.attachments[0].metadata["feishu_resource_key"] == "file-key"
    assert file_msg.attachments[0].metadata["feishu_resource_type"] == "file"


def test_parse_event_maps_post_image_elements_to_attachments() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )

    msg = channel.parse_event(
        {
            "header": {"event_id": "evt-post-image"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_post",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "post",
                    "content": json.dumps(
                        {
                            "content": [
                                [
                                    {"tag": "text", "text": "what is this"},
                                    {"tag": "img", "image_key": "img-key"},
                                ]
                            ]
                        }
                    ),
                },
            },
        }
    )

    assert msg.content == "what is this[image]"
    assert len(msg.attachments) == 1
    assert msg.attachments[0].name == "image.png"
    assert msg.attachments[0].mime_type == "image/png"
    assert msg.attachments[0].metadata["feishu_resource_key"] == "img-key"
    assert msg.attachments[0].metadata["feishu_resource_type"] == "image"
    assert msg.attachments[0].metadata["feishu_message_type"] == "post"
    assert msg.attachments[0].metadata["feishu_message_id"] == "om_post"


def test_parse_event_maps_media_audio_and_sticker_to_attachments() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )

    media_msg = channel.parse_event(
        {
            "header": {"event_id": "evt-media"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_media",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "media",
                    "content": json.dumps(
                        {"file_key": "media-key", "file_name": "clip.mp4", "file_size": 34}
                    ),
                },
            },
        }
    )
    audio_msg = channel.parse_event(
        {
            "header": {"event_id": "evt-audio"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_audio",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "audio",
                    "content": json.dumps({"file_key": "audio-key", "file_size": 56}),
                },
            },
        }
    )
    sticker_msg = channel.parse_event(
        {
            "header": {"event_id": "evt-sticker"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_sticker",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "sticker",
                    "content": json.dumps({"image_key": "sticker-key"}),
                },
            },
        }
    )

    assert media_msg is not None
    assert media_msg.content == "[media]"
    assert media_msg.attachments[0].name == "clip.mp4"
    assert media_msg.attachments[0].mime_type == "video/mp4"
    assert media_msg.attachments[0].size == 34
    assert media_msg.attachments[0].metadata["feishu_resource_key"] == "media-key"
    assert media_msg.attachments[0].metadata["feishu_resource_type"] == "media"
    assert media_msg.attachments[0].metadata["feishu_message_id"] == "om_media"

    assert audio_msg is not None
    assert audio_msg.content == "[audio]"
    assert audio_msg.attachments[0].name == "audio.ogg"
    assert audio_msg.attachments[0].mime_type == "audio/ogg"
    assert audio_msg.attachments[0].size == 56
    assert audio_msg.attachments[0].metadata["feishu_resource_key"] == "audio-key"
    assert audio_msg.attachments[0].metadata["feishu_resource_type"] == "audio"
    assert audio_msg.attachments[0].metadata["feishu_message_id"] == "om_audio"

    assert sticker_msg is not None
    assert sticker_msg.content == "[sticker]"
    assert sticker_msg.attachments[0].name == "sticker.png"
    assert sticker_msg.attachments[0].mime_type == "image/png"
    assert sticker_msg.attachments[0].metadata["feishu_resource_key"] == "sticker-key"
    assert sticker_msg.attachments[0].metadata["feishu_resource_type"] == "image"
    assert sticker_msg.attachments[0].metadata["feishu_message_type"] == "sticker"
    assert sticker_msg.attachments[0].metadata["feishu_message_id"] == "om_sticker"


@pytest.mark.asyncio
async def test_resolve_inbound_attachment_downloads_feishu_resource() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/open-apis/im/v1/messages/om_file/resources/file-key"
        assert request.url.params["type"] == "file"
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF-1.4\n",
        )

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    try:
        resolved = await channel.resolve_inbound_attachment(
            channel.parse_event(
                {
                    "event": {
                        "sender": {"sender_id": {"open_id": "ou_user"}},
                        "message": {
                            "message_id": "om_file",
                            "chat_id": "oc_chat",
                            "message_type": "file",
                            "content": json.dumps(
                                {
                                    "file_key": "file-key",
                                    "file_name": "report.pdf",
                                    "file_size": 12,
                                }
                            ),
                        },
                    }
                }
            ).attachments[0]
        )
    finally:
        await channel.stop()

    assert resolved.name == "report.pdf"
    assert resolved.mime_type == "application/pdf"
    assert resolved.data == b"%PDF-1.4\n"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_resolve_inbound_attachment_rejects_oversize_header_before_body_read() -> None:
    class FakeResponse:
        # Opaque inbound files use the staged opaque ceiling; the header must
        # exceed it for the pre-body rejection to fire.
        headers = {"content-length": str(OPAQUE_ATTACHMENT_BYTES + 1)}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            raise AssertionError("oversize Feishu resource body should not be read")
            yield b""

    class StreamOnlyClient:
        def get(self, *args, **kwargs):
            raise AssertionError("Feishu resources must not be downloaded with buffered get()")

        def stream(self, method: str, url: str, **kwargs):
            assert method == "GET"
            assert url == "/im/v1/messages/om_file/resources/file-key"
            assert kwargs["params"] == {"type": "file"}
            return FakeResponse()

        async def aclose(self) -> None:
            return None

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = StreamOnlyClient()

    try:
        with pytest.raises(ValueError, match="exceeds"):
            await channel.resolve_inbound_attachment(
                channel.parse_event(
                    {
                        "event": {
                            "sender": {"sender_id": {"open_id": "ou_user"}},
                            "message": {
                                "message_id": "om_file",
                                "chat_id": "oc_chat",
                                "message_type": "file",
                                "content": json.dumps(
                                    {
                                        "file_key": "file-key",
                                        "file_name": "huge.bin",
                                        "file_size": OPAQUE_ATTACHMENT_BYTES,
                                    }
                                ),
                            },
                        }
                    }
                ).attachments[0]
            )
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_send_file_uses_chat_id_for_group_file(tmp_path: Path) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF-1.4\n")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        if request.url.path == "/open-apis/im/v1/files":
            assert b'file_type"\r\n\r\npdf' in body
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "file-key"}})
        if request.url.path == "/open-apis/im/v1/messages":
            assert request.url.params["receive_id_type"] == "chat_id"
            payload = json.loads(body)
            assert payload["receive_id"] == "oc_group_chat"
            assert payload["msg_type"] == "file"
            assert json.loads(payload["content"]) == {"file_key": "file-key"}
            return httpx.Response(200, json={"code": 0})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    try:
        await channel.send_file("oc_group_chat", str(file_path))
    finally:
        await channel.stop()

    assert [request.url.path for request in requests] == [
        "/open-apis/im/v1/files",
        "/open-apis/im/v1/messages",
    ]


@pytest.mark.asyncio
async def test_contextual_file_delivery_reuses_uuid_and_replies_in_original_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF-1.4\n")
    provider_uuids: list[str] = []
    upload_count = 0

    async def no_delay(_delay: float) -> None:
        return None

    monkeypatch.setattr("opensquilla.channels._util.asyncio.sleep", no_delay)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_count
        if request.url.path == "/open-apis/im/v1/files":
            upload_count += 1
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "file-key"}})
        assert request.url.path == "/open-apis/im/v1/messages/om_origin/reply"
        payload = json.loads(await request.aread())
        provider_uuids.append(payload["uuid"])
        assert "uuid" not in request.url.params
        assert payload["reply_in_thread"] is True
        assert payload["msg_type"] == "file"
        assert "receive_id" not in payload
        if len(provider_uuids) == 1:
            raise httpx.ReadTimeout("synthetic lost reply", request=request)
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_file"}})

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis", transport=httpx.MockTransport(handler)
    )
    request = ChannelArtifactDeliveryRequest(
        inbound=IncomingMessage(
            sender_id="ou_user", channel_id="oc_group", content="",
            metadata={"reply_target_id": "om_origin", "native_thread_id": "omt_thread"},
        ),
        artifact_id="artifact-1", file_path=str(file_path), name="report.pdf",
        mime_type="application/pdf", size=file_path.stat().st_size,
        delivery_id="stable-file-delivery", session_id="session-1",
    )
    try:
        result = await channel.deliver_artifact(request)
    finally:
        await channel.stop()
    assert result.provider_message_id == "om_file"
    assert result.target_id == "oc_group"
    assert provider_uuids == ["stable-file-delivery", "stable-file-delivery"]
    assert upload_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "reply", "card", "legacy_text", "legacy_reply"])
async def test_message_uuid_is_in_json_body_and_stable_after_lost_response(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feishu reads idempotency from JSON for both create and reply requests."""
    bodies: list[dict] = []
    is_reply = mode in {"reply", "legacy_reply"}

    async def no_delay(_delay: float) -> None:
        return None

    monkeypatch.setattr("opensquilla.channels._util.asyncio.sleep", no_delay)

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(await request.aread())
        bodies.append(payload)
        assert "uuid" not in request.url.params
        assert isinstance(payload["uuid"], str) and payload["uuid"]
        if is_reply:
            assert request.url.path == "/open-apis/im/v1/messages/om_origin/reply"
            assert not request.url.params
            assert "receive_id" not in payload
        else:
            assert request.url.path == "/open-apis/im/v1/messages"
            assert request.url.params["receive_id_type"] == "open_id"
            assert payload["receive_id"] == "ou_private_user"
        if mode == "card":
            assert payload["msg_type"] == "interactive"
            assert json.loads(payload["content"]) == {"elements": []}
        else:
            assert payload["msg_type"] == "text"
            assert json.loads(payload["content"]) == {"text": "test reply"}
        if len(bodies) == 1:
            raise httpx.ReadTimeout("synthetic lost response", request=request)
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_sent"}})

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis", transport=httpx.MockTransport(handler)
    )
    try:
        if mode == "legacy_text":
            assert await channel.send_text("ou_private_user", "test reply") == "om_sent"
        elif mode == "legacy_reply":
            assert await channel.reply_text("om_origin", "test reply") == "om_sent"
        else:
            metadata: dict = {"delivery_id": "stable-text-delivery"}
            if is_reply:
                metadata["reply_message_id"] = "om_origin"
            if mode == "card":
                metadata["card"] = {"elements": []}
            await channel.send(OutgoingMessage(
                content="test reply", reply_to="ou_private_user", metadata=metadata,
            ))
            assert bodies[0]["uuid"] == "stable-text-delivery"
    finally:
        await channel.stop()
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]
