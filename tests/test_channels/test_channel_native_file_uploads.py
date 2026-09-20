from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from opensquilla.channels.contract import ChannelCapabilities, ChannelSendStatus
from opensquilla.channels.matrix import MatrixChannel, MatrixChannelConfig
from opensquilla.channels.slack import SlackChannel
from opensquilla.channels.telegram import TelegramChannel, TelegramChannelConfig
from opensquilla.channels.wecom import WeComChannel, WeComChannelConfig, _TokenState


class _FakeResponse:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self._payload = payload or {"ok": True}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.mark.asyncio
async def test_slack_send_file_uses_external_upload_flow(tmp_path: Path) -> None:
    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    requests: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
            requests.append(("files_upload_v2", kwargs))
            assert Path(kwargs["file"]).read_bytes() == b"report"
            return {"ok": True, "files": [{"id": "F1"}]}

    channel = SlackChannel(token="xoxb-token", slack_channel_id="C-default")
    channel._client = FakeClient()

    result = await channel.send_file("C-target", str(file_path), content="done")

    assert result.status == ChannelSendStatus.SENT
    assert result.capability == ChannelCapabilities.NATIVE_FILE_UPLOAD
    assert result.target_id == "C-target"
    assert result.provider_file_id == "F1"
    assert requests == [
        (
            "files_upload_v2",
            {
                "file": str(file_path),
                "filename": "report.txt",
                "title": "report.txt",
                "channel": "C-target",
                "initial_comment": "done",
            },
        )
    ]


@pytest.mark.asyncio
async def test_telegram_send_file_posts_document_upload(tmp_path: Path) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF")
    requests: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        async def post(self, path: str, **kwargs: Any) -> _FakeResponse:
            requests.append((path, kwargs))
            return _FakeResponse(
                {"ok": True, "result": {"message_id": 42, "document": {"file_id": "doc-1"}}}
            )

    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    channel._client = FakeClient()  # type: ignore[assignment]

    result = await channel.send_file("12345", str(file_path), content="done")

    assert result.status == ChannelSendStatus.SENT
    assert result.target_id == "12345"
    assert result.provider_message_id == "42"
    assert result.provider_file_id == "doc-1"
    assert requests[0][0] == "/bottoken/sendDocument"
    assert requests[0][1]["data"] == {"chat_id": "12345", "caption": "done"}


@pytest.mark.asyncio
async def test_matrix_send_file_uploads_media_then_sends_room_message(tmp_path: Path) -> None:
    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    sent: list[dict[str, Any]] = []

    class UploadResponse:
        content_uri = "mxc://server/media"

    class FakeClient:
        async def upload(self, file: Any, *, content_type: str, filename: str) -> UploadResponse:
            assert file.read() == b"report"
            assert content_type == "text/plain"
            assert filename == "report.txt"
            return UploadResponse()

        async def room_send(
            self,
            *,
            room_id: str,
            message_type: str,
            content: dict[str, Any],
        ) -> Any:
            sent.append(
                {"room_id": room_id, "message_type": message_type, "content": content}
            )
            return type("SendResponse", (), {"event_id": "$event"})()

    channel = MatrixChannel(MatrixChannelConfig())
    channel._client = FakeClient()

    result = await channel.send_file("!room:server", str(file_path))

    assert result.status == ChannelSendStatus.SENT
    assert result.provider_message_id == "$event"
    assert result.provider_file_id == "mxc://server/media"
    assert sent == [
        {
            "room_id": "!room:server",
            "message_type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "report.txt",
                "filename": "report.txt",
                "url": "mxc://server/media",
                "info": {"mimetype": "text/plain", "size": 6},
            },
        }
    ]


@pytest.mark.asyncio
async def test_wecom_send_file_uploads_media_then_sends_file_message(tmp_path: Path) -> None:
    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    requests: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        async def post(self, path: str, **kwargs: Any) -> _FakeResponse:
            requests.append((path, kwargs))
            if path == "/cgi-bin/media/upload":
                return _FakeResponse({"errcode": 0, "media_id": "media-1"})
            if path == "/cgi-bin/message/send":
                return _FakeResponse({"errcode": 0, "msgid": "msg-1"})
            raise AssertionError(path)

    channel = WeComChannel(WeComChannelConfig(agent_id_int=1001))
    channel._client = FakeClient()  # type: ignore[assignment]
    channel._token_state = _TokenState("token", time.monotonic() + 3600)

    result = await channel.send_file("user-1", str(file_path))

    assert result.status == ChannelSendStatus.SENT
    assert result.target_id == "user-1"
    assert result.provider_message_id == "msg-1"
    assert result.provider_file_id == "media-1"
    assert requests[0][0] == "/cgi-bin/media/upload"
    assert requests[1] == (
        "/cgi-bin/message/send",
        {
            "params": {"access_token": "token"},
            "json": {
                "touser": "user-1",
                "msgtype": "file",
                "agentid": 1001,
                "file": {"media_id": "media-1"},
                "safe": 0,
            },
        },
    )
