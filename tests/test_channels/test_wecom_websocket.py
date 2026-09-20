from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import opensquilla.channels.wecom as wecom_module
from opensquilla.channels.contract import (
    ChannelCapabilities,
    ChannelPlatformCapabilityStatus,
    ChannelPlatformCategories,
)
from opensquilla.channels.registry import parse_channel_entry
from opensquilla.channels.types import (
    ChannelArtifactDeliveryRequest,
    IncomingMessage,
    OutgoingMessage,
)
from opensquilla.channels.wecom import WeComApiError, WeComChannel, WeComChannelConfig
from opensquilla.gateway.config import WeComChannelEntry


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._subscribe_acked = False
        self._queue: asyncio.Queue[str | BaseException] = asyncio.Queue()

    @property
    def state(self) -> SimpleNamespace:
        return SimpleNamespace(name="CLOSED" if self.closed else "OPEN")

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        return await self.recv()

    def feed(self, payload: dict[str, Any]) -> None:
        self._queue.put_nowait(json.dumps(payload))

    def fail(self, exc: BaseException) -> None:
        self.closed = True
        self._queue.put_nowait(exc)

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self._subscribe_acked and self.sent:
            subscribe = self.sent[0]
            self._subscribe_acked = True
            return json.dumps(
                {
                    "cmd": "aibot_subscribe",
                    "headers": {"req_id": subscribe["headers"]["req_id"]},
                    "errcode": 0,
                }
            )
        item = await self._queue.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self, *_args: object) -> None:
        self.closed = True


def _install_fake_websockets(
    monkeypatch: pytest.MonkeyPatch, ws: _FakeWebSocket | list[_FakeWebSocket]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    sockets = list(ws) if isinstance(ws, list) else [ws]

    async def connect(url: str, **kwargs: Any) -> _FakeWebSocket:
        calls.append({"url": url, "kwargs": kwargs})
        return sockets[min(len(calls) - 1, len(sockets) - 1)]

    # Exercise the actual SDK, replacing only its network boundary.
    import websockets.asyncio.client

    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)
    return calls


async def _wait_until(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met")


def _inbound_text(req_id: str = "inbound-1") -> dict[str, Any]:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": req_id},
        "body": {
            "msgid": "msg-1",
            "chatid": "chat-1",
            "chattype": "group",
            "msgtype": "text",
            "from": {"userid": "user-1"},
            "text": {"content": "hello"},
        },
    }


@pytest.mark.asyncio
async def test_wecom_websocket_subscribes_to_ai_bot_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWebSocket()
    calls = _install_fake_websockets(monkeypatch, ws)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    await channel.start()
    try:
        assert calls == [
            {
                "url": "wss://openws.work.weixin.qq.com",
                "kwargs": {"ping_interval": None, "ping_timeout": None, "close_timeout": 5},
            }
        ]
        assert "wsagent" not in calls[0]["url"]
        assert "access_token" not in calls[0]["url"]
        assert ws.sent[0]["cmd"] == "aibot_subscribe"
        assert ws.sent[0]["body"] == {"bot_id": "bot-id", "secret": "bot-secret"}
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wecom_websocket_inbound_callback_can_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _FakeWebSocket()
    _install_fake_websockets(monkeypatch, ws)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    await channel.start()
    try:
        ws.feed(_inbound_text())
        incoming = await asyncio.wait_for(channel.receive(), timeout=1)
        assert incoming.content == "hello"
        assert incoming.channel_id == "chat-1"
        assert incoming.metadata["wecom_protocol"] == "aibot"
        assert incoming.metadata["wecom_req_id"] == "inbound-1"

        send_task = asyncio.create_task(channel.send(OutgoingMessage(content="world")))
        while len(ws.sent) < 2:
            await asyncio.sleep(0)
        assert ws.sent[1] == {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": "inbound-1"},
            "body": {"msgtype": "markdown", "markdown": {"content": "world"}},
        }
        ws.feed({"cmd": "aibot_respond_msg", "headers": {"req_id": "inbound-1"}, "errcode": 0})
        await asyncio.wait_for(send_task, timeout=1)

        send_task = asyncio.create_task(
            channel.send(OutgoingMessage(content="later", reply_to="chat-1"))
        )
        await _wait_until(lambda: len(ws.sent) >= 3)
        assert ws.sent[2]["cmd"] == "aibot_send_msg"
        assert ws.sent[2]["body"]["chatid"] == "chat-1"
        ws.feed(
            {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": ws.sent[2]["headers"]["req_id"]},
                "errcode": 0,
            }
        )
        await asyncio.wait_for(send_task, timeout=1)
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wecom_websocket_streaming_reply_preserves_callback_req_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWebSocket()
    _install_fake_websockets(monkeypatch, ws)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    async def chunks() -> Any:
        yield "streamed"

    await channel.start()
    try:
        ws.feed(_inbound_text())
        incoming = await asyncio.wait_for(channel.receive(), timeout=1)
        kwargs = channel.streaming_reply_kwargs(incoming)
        send_task = asyncio.create_task(channel.send_streaming(chunks(), **kwargs))
        await _wait_until(lambda: len(ws.sent) >= 2)
        assert ws.sent[1] == {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": "inbound-1"},
            "body": {"msgtype": "markdown", "markdown": {"content": "streamed"}},
        }
        ws.feed({"cmd": "aibot_respond_msg", "headers": {"req_id": "inbound-1"}, "errcode": 0})
        await asyncio.wait_for(send_task, timeout=1)
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wecom_websocket_explicit_chat_target_uses_proactive_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWebSocket()
    _install_fake_websockets(monkeypatch, ws)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    await channel.start()
    try:
        ws.feed(_inbound_text())
        await asyncio.wait_for(channel.receive(), timeout=1)
        send_task = asyncio.create_task(
            channel.send(OutgoingMessage(content="proactive", reply_to="chat-1"))
        )
        await _wait_until(lambda: len(ws.sent) >= 2)
        assert ws.sent[1]["cmd"] == "aibot_send_msg"
        assert ws.sent[1]["body"]["chatid"] == "chat-1"
        ws.feed(
            {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": ws.sent[1]["headers"]["req_id"]},
                "errcode": 0,
            }
        )
        await asyncio.wait_for(send_task, timeout=1)
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wecom_websocket_expired_callback_req_id_uses_proactive_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wecom_module, "_WEBSOCKET_REPLY_REQ_ID_TTL_S", 0.01)
    ws = _FakeWebSocket()
    _install_fake_websockets(monkeypatch, ws)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    await channel.start()
    try:
        ws.feed(_inbound_text())
        await asyncio.wait_for(channel.receive(), timeout=1)
        await asyncio.sleep(0.02)
        send_task = asyncio.create_task(channel.send(OutgoingMessage(content="late")))
        await _wait_until(lambda: len(ws.sent) >= 2)
        assert ws.sent[1]["cmd"] == "aibot_send_msg"
        assert ws.sent[1]["body"]["chatid"] == "chat-1"
        ws.feed(
            {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": ws.sent[1]["headers"]["req_id"]},
                "errcode": 0,
            }
        )
        await asyncio.wait_for(send_task, timeout=1)
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wecom_websocket_event_callbacks_are_not_enqueued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWebSocket()
    _install_fake_websockets(monkeypatch, ws)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    await channel.start()
    try:
        ws.feed(
            {
                "cmd": "aibot_event_callback",
                "headers": {"req_id": "event-1"},
                "body": {"event": {"eventtype": "enter_chat"}, "chatid": "chat-1"},
            }
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(channel.receive(), timeout=0.05)
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wecom_websocket_voice_callback_reads_transcribed_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWebSocket()
    _install_fake_websockets(monkeypatch, ws)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    await channel.start()
    try:
        ws.feed(
            {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "voice-1"},
                "body": {
                    "msgid": "msg-voice",
                    "chatid": "chat-1",
                    "msgtype": "voice",
                    "from": {"userid": "user-1"},
                    "voice": {"content": "spoken text"},
                },
            }
        )
        incoming = await asyncio.wait_for(channel.receive(), timeout=1)
        assert incoming.content == "spoken text"
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wecom_websocket_sends_application_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wecom_module, "_WEBSOCKET_APP_PING_INTERVAL_S", 0.01)
    ws = _FakeWebSocket()
    _install_fake_websockets(monkeypatch, ws)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    await channel.start()
    try:
        await _wait_until(lambda: len(ws.sent) >= 2)
        ping = ws.sent[1]
        assert ping["cmd"] == "ping"
        assert ping.get("body", {}) == {}
        ws.feed({"cmd": "pong", "headers": {"req_id": ping["headers"]["req_id"]}, "errcode": 0})
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wecom_websocket_reconnects_after_receive_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wecom_module, "_WEBSOCKET_RECONNECT_INITIAL_S", 0.01)
    monkeypatch.setattr(wecom_module, "_WEBSOCKET_RECONNECT_MAX_S", 0.01)
    first_ws = _FakeWebSocket()
    second_ws = _FakeWebSocket()
    calls = _install_fake_websockets(monkeypatch, [first_ws, second_ws])
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    await channel.start()
    try:
        first_ws.fail(RuntimeError("connection dropped"))
        await _wait_until(lambda: len(calls) >= 2)
        assert first_ws.closed is True
        second_ws.feed(_inbound_text("inbound-2"))
        incoming = await asyncio.wait_for(channel.receive(), timeout=1)
        assert incoming.content == "hello"
        assert incoming.metadata["wecom_req_id"] == "inbound-2"
    finally:
        await channel.stop()


def test_wecom_websocket_capabilities_advertise_sdk_media_delivery() -> None:
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    assert channel.capability_profile.supports(ChannelCapabilities.WEBSOCKET)
    assert channel.capability_profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)
    assert channel.capability_profile.supports(ChannelCapabilities.ARTIFACT_DELIVERY)
    assert (
        channel.platform_capability_manifest.get(ChannelPlatformCategories.FILES).status
        == ChannelPlatformCapabilityStatus.SUPPORTED
    )


def test_wecom_websocket_config_requires_bot_credentials() -> None:
    with pytest.raises(ValueError, match="bot_id and bot_secret"):
        parse_channel_entry(
            {
                "type": "wecom",
                "name": "wecom",
                "connection_mode": "websocket",
                "corp_id": "corp",
                "corp_secret": "corp-secret",
                "agent_id_int": 1001,
            }
        )

    entry = parse_channel_entry(
        {
            "type": "wecom",
            "name": "wecom",
            "connection_mode": "websocket",
            "bot_id": "bot",
            "bot_secret": "secret",
        }
    )
    assert isinstance(entry, WeComChannelEntry)
    assert entry.websocket_url == "wss://openws.work.weixin.qq.com"


def test_wecom_webhook_streaming_reply_kwargs_pin_inbound_sender() -> None:
    channel = WeComChannel(WeComChannelConfig(name="wecom", agent_id_int=1))
    assert channel.config.connection_mode == "webhook"

    inbound = IncomingMessage(
        sender_id="user-1",
        channel_id="user-1",
        content="hello",
        metadata={"toparty": "party-1"},
    )

    assert channel.streaming_reply_kwargs(inbound) == {
        "reply_to": "user-1",
        "metadata": {"toparty": "party-1"},
    }


def test_wecom_webhook_build_reply_message_targets_inbound_sender() -> None:
    channel = WeComChannel(WeComChannelConfig(name="wecom", agent_id_int=1))
    inbound = IncomingMessage(
        sender_id="user-1",
        channel_id="user-1",
        content="hello",
        metadata={"toparty": "party-1", "totag": "tag-1"},
    )

    reply = channel.build_reply_message("answer", inbound)

    assert reply.reply_to == "user-1"
    assert reply.metadata == {"toparty": "party-1", "totag": "tag-1"}
    assert channel._build_send_payload(reply) == {  # noqa: SLF001
        "touser": "user-1",
        "toparty": "party-1",
        "totag": "tag-1",
        "msgtype": "text",
        "agentid": 1,
        "text": {"content": "answer"},
        "safe": 0,
    }


def test_wecom_webhook_send_payload_rejects_missing_target() -> None:
    channel = WeComChannel(WeComChannelConfig(name="wecom", agent_id_int=1))

    with pytest.raises(WeComApiError, match="explicit .* target is required"):
        channel._build_send_payload(OutgoingMessage(content="never broadcast"))  # noqa: SLF001


def test_wecom_webhook_config_remains_supported() -> None:
    entry = parse_channel_entry(
        {
            "type": "wecom",
            "name": "wecom-callback",
            "connection_mode": "webhook",
            "corp_id": "corp",
            "corp_secret": "corp-secret",
            "agent_id_int": 1001,
            "token": "token",
            "encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        }
    )
    assert isinstance(entry, WeComChannelEntry)
    assert entry.connection_mode == "webhook"


async def test_wecom_sdk_uploads_file_and_replies_to_original_inbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MediaWebSocket(_FakeWebSocket):
        async def send(self, raw: str) -> None:
            await super().send(raw)
            request = json.loads(raw)
            cmd = request["cmd"]
            if cmd == "aibot_subscribe":
                return
            body = {}
            if cmd == "aibot_upload_media_init":
                body = {"upload_id": "upload-synthetic"}
            elif cmd == "aibot_upload_media_finish":
                body = {"media_id": "media-synthetic", "type": "file"}
            self.feed({"headers": request["headers"], "errcode": 0, "body": body})

    ws = MediaWebSocket()
    _install_fake_websockets(monkeypatch, ws)
    channel = WeComChannel(WeComChannelConfig(
        connection_mode="websocket", bot_id="bot-id", bot_secret="bot-secret",
    ))
    file = tmp_path / "report.csv"
    file.write_bytes(b"name,value\nsynthetic,42\n")
    await channel.start()
    try:
        ws.feed(_inbound_text("original-req"))
        inbound = await asyncio.wait_for(channel.receive(), timeout=1)
        later = _inbound_text("later-req")
        later["body"].update(msgid="msg-other", chatid="other-room")
        ws.feed(later)
        await asyncio.wait_for(channel.receive(), timeout=1)
        result = await channel.deliver_artifact(ChannelArtifactDeliveryRequest(
            inbound=inbound, artifact_id="artifact-synthetic", file_path=str(file),
            name=file.name, mime_type="text/csv", size=file.stat().st_size,
            session_id="session-synthetic", delivery_id="delivery-synthetic",
        ))
        assert result.is_delivered()
        assert result.target_id == "chat-1"
        assert result.provider_file_id == "media-synthetic"
        frames = ws.sent[1:]
        assert [frame["cmd"] for frame in frames] == [
            "aibot_upload_media_init", "aibot_upload_media_chunk",
            "aibot_upload_media_finish", "aibot_respond_msg",
        ]
        assert base64.b64decode(frames[1]["body"]["base64_data"]) == file.read_bytes()
        assert frames[-1]["headers"]["req_id"] == "original-req"
        assert frames[-1]["body"] == {
            "msgtype": "file", "file": {"media_id": "media-synthetic"},
        }
    finally:
        await channel.stop()
    assert not (await channel.health_check()).connected


async def test_wecom_sdk_failed_auth_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    class RejectedWebSocket(_FakeWebSocket):
        async def recv(self) -> str:
            response = json.loads(await super().recv())
            if response.get("cmd") == "aibot_subscribe":
                response["errcode"] = 40001
                response["errmsg"] = "invalid credential"
            return json.dumps(response)

    ws = RejectedWebSocket()
    _install_fake_websockets(monkeypatch, ws)
    channel = WeComChannel(WeComChannelConfig(
        connection_mode="websocket", bot_id="bot-id", bot_secret="bot-secret",
    ))
    with pytest.raises(wecom_module.WeComAuthError, match="SDK connection failed"):
        await channel.start()
    assert ws.closed
    assert channel._ws_sdk is None
    assert not (await channel.health_check()).connected


async def test_wecom_sdk_empty_target_does_not_read_file(tmp_path: Path) -> None:
    channel = WeComChannel(WeComChannelConfig(connection_mode="websocket"))
    with pytest.raises(ValueError, match="target is required"):
        await channel.send_file("", str(tmp_path / "absent.csv"))


async def test_wecom_corp_artifact_has_one_contextual_outbox_record(tmp_path: Path) -> None:
    import sqlite3
    from unittest.mock import AsyncMock

    from opensquilla.channels.delivery_store import ChannelDeliveryStore, install_outbox

    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    calls: list[str] = []

    class Response:
        def __init__(self, value: dict[str, Any]) -> None:
            self.value = value

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return self.value

    class Client:
        async def post(self, path: str, **kwargs: Any) -> Response:
            calls.append(path)
            if path == "/cgi-bin/media/upload":
                return Response({"errcode": 0, "media_id": "media-1"})
            assert kwargs["json"]["touser"] == "original-sender"
            return Response({"errcode": 0, "msgid": "message-1"})

    channel = WeComChannel(WeComChannelConfig(agent_id_int=1001))
    channel._client = Client()  # type: ignore[assignment]
    channel._get_token = AsyncMock(return_value="synthetic-token")  # type: ignore[method-assign]
    db_path = tmp_path / "outbox.sqlite"
    store = ChannelDeliveryStore(db_path)
    channel._delivery_store = store
    channel._delivery_channel_name = "wecom-test"
    install_outbox(channel)
    request = ChannelArtifactDeliveryRequest(
        inbound=IncomingMessage(
            sender_id="original-sender", channel_id="original-sender", content="generate report",
            metadata={"message_id": "inbound-1"},
        ),
        artifact_id="artifact-1", file_path=str(file_path), name=file_path.name,
        mime_type="text/plain", size=6, session_id="session-1",
    )
    try:
        first = await channel.deliver_artifact(request)
        assert await channel.deliver_artifact(request) == first
        assert calls == ["/cgi-bin/media/upload", "/cgi-bin/message/send"]
        with sqlite3.connect(db_path) as connection:
            records = connection.execute("SELECT message_json FROM channel_outbox").fetchall()
        assert len(records) == 1
        assert str(tmp_path) not in records[0][0]
    finally:
        store.close()
