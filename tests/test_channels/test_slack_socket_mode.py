"""Slack Socket Mode transport, auto-target replies, and self-echo filtering."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opensquilla.channels.manager import ChannelManager
from opensquilla.channels.slack import SlackAuthError, SlackChannel
from opensquilla.channels.types import IncomingMessage, OutgoingMessage


def _mk(**kwargs: Any) -> SlackChannel:
    kwargs.setdefault("slack_channel_id", "")
    ch = SlackChannel(token="xoxb-test", **kwargs)
    ch.bot_user_id = "UBOT"
    return ch


class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None, Any]] = []
        self.socket_open_payload: dict[str, Any] = {
            "ok": True,
            "url": "wss://socket.slack.test/session",
        }

    async def post(
        self, path: str, json: dict[str, Any] | None = None, headers: Any = None
    ) -> _FakeResp:
        self.calls.append((path, json, headers))
        if path == "/auth.test":
            return _FakeResp({"ok": True, "user_id": "UBOT"})
        if path == "/apps.connections.open":
            return _FakeResp(self.socket_open_payload)
        return _FakeResp({"ok": True, "ts": "1700000000.000100"})


def test_transport_name_follows_connection_mode() -> None:
    assert _mk().transport_name == "webhook"
    assert _mk(connection_mode="socket").transport_name == "websocket"


async def test_webhook_mode_requires_signing_secret_before_authentication() -> None:
    ch = _mk(connection_mode="webhook")
    fake = _FakeClient()
    ch._get_client = lambda: fake  # type: ignore[method-assign]

    with pytest.raises(SlackAuthError, match="requires signing_secret"):
        await ch.start()

    assert fake.calls == []
    assert ch.is_connected() is False


async def test_socket_mode_requires_app_token() -> None:
    ch = _mk(connection_mode="socket")  # no app_token
    ch._get_client = lambda: _FakeClient()  # type: ignore[method-assign]
    with pytest.raises(SlackAuthError):
        await ch.start()


async def test_socket_mode_validates_app_token_before_reporting_started() -> None:
    ch = _mk(connection_mode="socket", app_token="xapp-valid")
    fake = _FakeClient()
    fake.socket_open_payload = {"ok": False, "error": "invalid_auth"}
    ch._get_client = lambda: fake  # type: ignore[method-assign]
    with pytest.raises(SlackAuthError, match="apps.connections.open failed"):
        await ch.start()
    assert ch.is_connected() is False


async def test_socket_mode_start_passes_opened_url_to_background_loop() -> None:
    ch = _mk(connection_mode="socket", app_token="xapp-valid")
    fake = _FakeClient()
    seen: list[str | None] = []

    async def _fake_socket_loop(initial_socket_url: str | None = None) -> None:
        seen.append(initial_socket_url)
        await ch._socket_stop.wait()  # type: ignore[union-attr]

    ch._get_client = lambda: fake  # type: ignore[method-assign]
    ch._run_socket_loop = _fake_socket_loop  # type: ignore[method-assign]

    await ch.start()
    await asyncio.sleep(0)
    await ch.stop()

    assert seen == ["wss://socket.slack.test/session"]
    open_call = next(c for c in fake.calls if c[0] == "/apps.connections.open")
    assert open_call[2] == {"Authorization": "Bearer xapp-valid"}


def test_ingest_accepts_plain_user_message() -> None:
    ch = _mk()
    ch._ingest_event_callback(
        {
            "event_id": "Ev1",
            "event": {
                "type": "message",
                "user": "UUSER",
                "channel": "D123",
                "text": "hi",
                "ts": "1.1",
            },
        }
    )
    assert ch._queue.qsize() == 1
    msg = ch._queue.get_nowait()
    assert msg.channel_id == "D123"
    assert msg.content == "hi"


def test_ingest_accepts_app_mention_event() -> None:
    ch = _mk()
    ch._ingest_event_callback(
        {
            "event_id": "EvMention",
            "event": {
                "type": "app_mention",
                "user": "UUSER",
                "channel": "C123",
                "text": "<@UBOT> hi",
                "ts": "2.1",
            },
        }
    )

    assert ch._queue.qsize() == 1
    msg = ch._queue.get_nowait()
    assert msg.channel_id == "C123"
    assert msg.content == "<@UBOT> hi"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "message", "bot_id": "B1", "channel": "D1", "text": "x", "ts": "2"},
        {"type": "message", "user": "UBOT", "channel": "D1", "text": "x", "ts": "3"},
        {"type": "message", "subtype": "bot_message", "channel": "D1", "text": "x", "ts": "4"},
        {"type": "message", "subtype": "message_changed", "channel": "D1", "ts": "5"},
        {"type": "message", "subtype": "message_deleted", "channel": "D1", "ts": "6"},
    ],
)
def test_ingest_drops_self_echoes_and_non_user_subtypes(event: dict[str, Any]) -> None:
    ch = _mk()
    ch._ingest_event_callback({"event_id": f"e-{event.get('ts')}", "event": event})
    assert ch._queue.qsize() == 0


def test_ingest_dedupes_replayed_event() -> None:
    ch = _mk()
    payload = {
        "event_id": "Dup1",
        "event": {"type": "message", "user": "U", "channel": "D1", "text": "hi", "ts": "9"},
    }
    ch._ingest_event_callback(payload)
    ch._ingest_event_callback(payload)
    assert ch._queue.qsize() == 1


def test_ingest_dedupes_app_mention_and_message_pair() -> None:
    ch = _mk()
    ch._ingest_event_callback(
        {
            "event_id": "EvMention",
            "event": {
                "type": "app_mention",
                "user": "UUSER",
                "channel": "C123",
                "text": "<@UBOT> hi",
                "ts": "10.1",
            },
        }
    )
    ch._ingest_event_callback(
        {
            "event_id": "EvMessage",
            "event": {
                "type": "message",
                "user": "UUSER",
                "channel": "C123",
                "text": "<@UBOT> hi",
                "ts": "10.1",
            },
        }
    )

    assert ch._queue.qsize() == 1


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


async def test_socket_frame_acks_and_dispatches_events_api_payload() -> None:
    ch = _mk()
    ws = _FakeSocket()
    await ch._handle_socket_frame(
        ws,
        (
            '{"envelope_id":"env-1","type":"events_api","payload":'
            '{"type":"event_callback","event_id":"Ev1","event":'
            '{"type":"message","user":"UUSER","channel":"D123","text":"hi","ts":"1.1"}}}'
        ),
    )

    assert ws.sent == ['{"envelope_id": "env-1"}']
    assert ch._queue.qsize() == 1


async def test_socket_frame_disconnect_closes_socket_after_ack() -> None:
    ch = _mk()
    ws = _FakeSocket()
    await ch._handle_socket_frame(ws, '{"envelope_id":"env-2","type":"disconnect"}')

    assert ws.sent == ['{"envelope_id": "env-2"}']
    assert ws.closed is True


async def test_send_auto_targets_reply_conversation() -> None:
    ch = _mk()  # slack_channel_id empty on purpose
    fake = _FakeClient()
    ch._get_client = lambda: fake  # type: ignore[method-assign]
    await ch.send(OutgoingMessage(content="hello", reply_to="D999"))
    post = next(c for c in fake.calls if c[0] == "/chat.postMessage")
    assert post[1] is not None
    assert post[1]["channel"] == "D999"
    # A conversation id must NOT be misused as a thread anchor.
    assert "thread_ts" not in post[1]


async def test_send_threads_only_on_message_ts() -> None:
    ch = _mk(slack_channel_id="C1")
    fake = _FakeClient()
    ch._get_client = lambda: fake  # type: ignore[method-assign]
    await ch.send(OutgoingMessage(content="hi", reply_to="1700000000.000200"))
    post = next(c for c in fake.calls if c[0] == "/chat.postMessage")
    assert post[1] is not None
    assert post[1]["channel"] == "C1"
    assert post[1]["thread_ts"] == "1700000000.000200"


async def test_send_thread_timestamp_uses_metadata_channel_without_default() -> None:
    ch = _mk()
    fake = _FakeClient()
    ch._get_client = lambda: fake  # type: ignore[method-assign]
    await ch.send(
        OutgoingMessage(
            content="hi",
            reply_to="1700000000.000200",
            metadata={"channel": "C42"},
        )
    )
    post = next(c for c in fake.calls if c[0] == "/chat.postMessage")
    assert post[1] is not None
    assert post[1]["channel"] == "C42"
    assert post[1]["thread_ts"] == "1700000000.000200"


def test_reply_helpers_target_inbound_conversation() -> None:
    ch = _mk()
    inbound = IncomingMessage(sender_id="U", channel_id="D42", content="hi")
    assert ch.build_reply_message("r", inbound).reply_to == "D42"
    assert ch.build_reply_message("r", inbound).metadata == {"channel": "D42"}
    assert ch.streaming_reply_kwargs(inbound) == {"channel": "D42"}


def test_reply_helpers_preserve_thread_target_when_enabled() -> None:
    ch = _mk(reply_in_thread=True)
    inbound = IncomingMessage(
        sender_id="U",
        channel_id="C42",
        content="hi",
        metadata={"ts": "1700000000.000200", "thread_ts": "1700000000.000100"},
    )

    reply = ch.build_reply_message("r", inbound)

    assert reply.reply_to == "C42"
    assert reply.metadata == {
        "channel": "C42",
        "thread_ts": "1700000000.000100",
    }
    assert ch.streaming_reply_kwargs(inbound) == {
        "channel": "C42",
        "thread_ts": "1700000000.000100",
    }


def test_reply_helpers_thread_root_when_enabled() -> None:
    ch = _mk(reply_in_thread=True)
    inbound = IncomingMessage(
        sender_id="U",
        channel_id="C42",
        content="hi",
        metadata={"ts": "1700000000.000200"},
    )

    assert ch.build_reply_message("r", inbound).metadata == {
        "channel": "C42",
        "thread_ts": "1700000000.000200"
    }


def test_channel_manager_skips_webhook_route_for_slack_socket_mode() -> None:
    manager = ChannelManager(
        _channels={
            "webhook": _mk(connection_mode="webhook"),
            "socket": _mk(connection_mode="socket", app_token="xapp-valid"),
        },
        _turn_runner=None,
        _session_manager=None,
    )

    routes = manager.collect_webhook_routes()

    assert [route.path for route in routes] == ["/slack/events"]
