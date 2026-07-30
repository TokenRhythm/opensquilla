from __future__ import annotations

import asyncio
import json
import sys
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
from opensquilla.channels.types import IncomingMessage, OutgoingMessage
from opensquilla.channels.wecom import WeComApiError, WeComChannel, WeComChannelConfig
from opensquilla.gateway.config import WeComChannelEntry


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._subscribe_acked = False
        self._queue: asyncio.Queue[str | BaseException] = asyncio.Queue()

    def feed(self, payload: dict[str, Any]) -> None:
        self._queue.put_nowait(json.dumps(payload))

    def fail(self, exc: BaseException) -> None:
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

    async def close(self) -> None:
        self.closed = True


def _install_fake_websockets(
    monkeypatch: pytest.MonkeyPatch, ws: _FakeWebSocket | list[_FakeWebSocket]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    sockets = list(ws) if isinstance(ws, list) else [ws]

    async def connect(url: str, **kwargs: Any) -> _FakeWebSocket:
        calls.append({"url": url, "kwargs": kwargs})
        return sockets[min(len(calls) - 1, len(sockets) - 1)]

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
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
                "kwargs": {"ping_interval": None},
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
                "body": {"event": "enter_chat", "chatid": "chat-1"},
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
        assert ping["body"] == {}
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


def test_wecom_websocket_capabilities_do_not_advertise_corp_app_file_upload() -> None:
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    assert channel.capability_profile.supports(ChannelCapabilities.WEBSOCKET)
    assert not channel.capability_profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)
    assert (
        channel.platform_capability_manifest.get(ChannelPlatformCategories.FILES).status
        == ChannelPlatformCapabilityStatus.UNSUPPORTED
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
