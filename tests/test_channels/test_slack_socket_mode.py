"""Slack Socket Mode transport, auto-target replies, and self-echo filtering."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
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


class _FakeClient:
    session = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None, Any]] = []
        self.socket_open_payload: dict[str, Any] = {
            "ok": True,
            "url": "wss://socket.slack.test/session",
        }

    async def api_call(self, method: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((f"/{method}", json, None))
        if method == "auth.test":
            return {"ok": True, "user_id": "UBOT"}
        return {"ok": True, "ts": "1700000000.000100"}

    async def apps_connections_open(self, *, app_token: str) -> dict[str, Any]:
        self.calls.append(
            ("/apps.connections.open", None, {"Authorization": f"Bearer {app_token}"})
        )
        return self.socket_open_payload


class _FakeSocketClient:
    def __init__(self) -> None:
        self.wss_uri: str | None = None
        self.connected_urls: list[str | None] = []
        self.closed = False
        self.stale = False
        self.current_session = SimpleNamespace(closed=True)
        self.sent: list[dict[str, Any]] = []

    async def connect(self) -> None:
        self.connected_urls.append(self.wss_uri)
        self.current_session.closed = False

    async def close(self) -> None:
        self.closed = True
        self.current_session.closed = True

    async def is_connected(self) -> bool:
        return not self.closed and not self.stale and not self.current_session.closed

    async def send_socket_mode_response(self, response: Any) -> None:
        self.sent.append(response.to_dict())


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
    socket = _FakeSocketClient()
    ch._create_socket_client = lambda: socket  # type: ignore[method-assign]
    with pytest.raises(SlackAuthError, match="invalid_auth"):
        await ch.start()
    assert ch.is_connected() is False
    assert socket.closed is True
    assert ch._socket_client is None


async def test_socket_mode_start_delegates_lifecycle_to_sdk() -> None:
    ch = _mk(connection_mode="socket", app_token="xapp-valid")
    fake = _FakeClient()
    socket = _FakeSocketClient()
    ch._get_client = lambda: fake  # type: ignore[method-assign]
    ch._create_socket_client = lambda: socket  # type: ignore[method-assign]

    await ch.start()
    assert ch.is_connected() is True
    assert (await ch.health_check()).connected is True
    await ch.stop()

    assert socket.connected_urls == ["wss://socket.slack.test/session"]
    assert socket.closed is True
    assert ch.is_connected() is False
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


async def test_socket_request_acks_and_dispatches_verified_events() -> None:
    ch = _mk()
    socket = _FakeSocketClient()
    request = SimpleNamespace(
        envelope_id="env-1",
        type="events_api",
        payload={
            "type": "event_callback",
            "event_id": "Ev1",
            "event": {
                "type": "message", "user": "UUSER", "channel": "D123", "text": "hi", "ts": "1.1"
            },
        },
    )
    await ch._handle_socket_request(socket, request)

    assert socket.sent == [{"envelope_id": "env-1"}]
    assert ch._queue.qsize() == 1
    incoming = ch._queue.get_nowait()
    assert incoming.provenance is not None
    assert incoming.provenance.authenticated is True


async def test_socket_request_acks_non_event_without_ingesting() -> None:
    ch = _mk()
    socket = _FakeSocketClient()
    await ch._handle_socket_request(
        socket,
        SimpleNamespace(envelope_id="env-2", type="interactive", payload={}),
    )

    assert socket.sent == [{"envelope_id": "env-2"}]
    assert ch._queue.empty()


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


async def test_socket_start_failure_closes_sdk_resources() -> None:
    ch = _mk(connection_mode="socket", app_token="xapp-valid")
    fake = _FakeClient()
    socket = _FakeSocketClient()

    async def fail_connect() -> None:
        raise OSError("synthetic connection failure")

    socket.connect = fail_connect  # type: ignore[method-assign]
    ch._get_client = lambda: fake  # type: ignore[method-assign]
    ch._create_socket_client = lambda: socket  # type: ignore[method-assign]
    with pytest.raises(OSError, match="synthetic"):
        await ch.start()
    assert socket.closed is True
    assert ch._socket_client is None
    assert ch.is_connected() is False


async def test_socket_start_cancellation_closes_sdk_resources() -> None:
    ch = _mk(connection_mode="socket", app_token="xapp-valid")
    fake = _FakeClient()
    socket = _FakeSocketClient()
    connecting = asyncio.Event()

    async def pending_connect() -> None:
        connecting.set()
        await asyncio.Event().wait()

    socket.connect = pending_connect  # type: ignore[method-assign]
    ch._get_client = lambda: fake  # type: ignore[method-assign]
    ch._create_socket_client = lambda: socket  # type: ignore[method-assign]
    task = asyncio.create_task(ch.start())
    await connecting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed is True
    assert ch.is_connected() is False


async def test_sdk_client_retains_proxy_policy_and_disables_implicit_mutation_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from slack_sdk.web.async_client import AsyncWebClient

    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-proxy.example:8080")
    monkeypatch.setattr("opensquilla.channels.slack._trust_env", lambda: False)
    ch = _mk()
    client = ch._get_client()
    assert isinstance(client, AsyncWebClient)
    assert client.proxy is None
    assert client.trust_env_in_session is False
    assert client.retry_handlers == []
    await ch.close()


@pytest.mark.parametrize("operation", ["message", "file", "probe"])
@pytest.mark.parametrize(
    ("status", "code", "headers", "classification", "retry_after"),
    [
        (429, "ratelimited", {"Retry-After": "17"}, "rate_limited", 17),
        (200, "invalid_auth", {}, "auth_invalid", None),
        (403, "unrecognized-provider-detail", {}, "auth_invalid", None),
    ],
)
async def test_sdk_errors_preserve_safe_classification_without_mutation_retry(
    operation: str, status: int, code: str, headers: dict[str, str],
    classification: str, retry_after: int | None,
) -> None:
    from unittest.mock import AsyncMock

    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    from opensquilla.channels.contract import classify_channel_send_error
    from opensquilla.channels.slack import SlackAPIError

    response = AsyncSlackResponse(
        client=None, http_verb="POST", api_url="https://slack.com/api/test",
        req_args={"headers": {"Authorization": "synthetic-sensitive-value"}},
        data={"ok": False, "error": code, "request_secret": "synthetic-sensitive-value"},
        headers=headers, status_code=status,
    )
    rejected = AsyncMock(side_effect=SlackApiError("synthetic-sensitive-value", response))
    ch = _mk()
    ch._client = SimpleNamespace(api_call=rejected, files_upload_v2=rejected)
    with pytest.raises((SlackAPIError, SlackAuthError)) as caught:
        if operation == "file":
            await ch.send_file("C-target", "report.pdf")
        elif operation == "probe":
            await ch.probe_connection()
        else:
            await ch.send(OutgoingMessage(content="hello", metadata={"channel": "C-target"}))

    error = caught.value
    assert classify_channel_send_error(error) == classification
    assert rejected.await_count == 1
    assert "synthetic-sensitive-value" not in str(error)
    assert "unrecognized-provider-detail" not in str(error)
    assert not hasattr(error, "response")
    if isinstance(error, SlackAPIError):
        assert error.status_code == status
        assert error.retry_after == retry_after


async def test_artifact_upload_pins_original_thread_and_channel(tmp_path: Path) -> None:
    from opensquilla.channels.types import ChannelArtifactDeliveryRequest

    ch = _mk(slack_channel_id="C-default", reply_in_thread=True)
    path = tmp_path / "report.txt"
    path.write_text("synthetic report", encoding="utf-8")
    uploads: list[dict[str, Any]] = []

    class Client:
        async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
            uploads.append(kwargs)
            return {"ok": True, "file": {"id": "F1"}}

    ch._client = Client()
    ch._last_thread_ts = "unrelated-thread"
    result = await ch.deliver_artifact(
        ChannelArtifactDeliveryRequest(
            inbound=IncomingMessage(
                sender_id="U-user",
                channel_id="C-origin",
                content="make a report",
                metadata={"thread_ts": "123.456", "ts": "123.789"},
            ),
            artifact_id="artifact-1",
            file_path=str(path),
            name=path.name,
            mime_type="text/plain",
            size=path.stat().st_size,
        )
    )

    assert uploads[0]["channel"] == "C-origin"
    assert uploads[0]["thread_ts"] == "123.456"
    assert result.provider_file_id == "F1"


async def test_sdk_reactions_disable_missing_scope_without_failing_turn() -> None:
    ch = _mk(status_reactions_enabled=True)

    class Client:
        async def api_call(self, method: str, **_kwargs: object) -> dict[str, Any]:
            assert method == "reactions.add"
            return {"ok": False, "error": "missing_scope"}

    ch._client = Client()
    await ch.status_reactor.received(
        IncomingMessage(
            sender_id="U-user", channel_id="C-origin", content="hi", metadata={"ts": "1"}
        )
    )
    assert ch.status_reactor._disabled is True


async def test_sdk_socket_client_owns_reconnect_and_is_closed_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from slack_sdk.socket_mode.aiohttp import SocketModeClient

    monkeypatch.setattr("opensquilla.channels.slack._trust_env", lambda: False)
    ch = _mk(connection_mode="socket", app_token="xapp-synthetic")
    client = ch._create_socket_client()
    ch._socket_client = client
    try:
        assert isinstance(client, SocketModeClient)
        assert client.auto_reconnect_enabled is True
        assert client.proxy is None
        assert ch._handle_socket_request in client.socket_mode_request_listeners
    finally:
        await ch.stop()
    assert client.closed is True
    assert client.aiohttp_client_session.closed is True


@pytest.mark.parametrize("body,timestamp", [(b"\xff", "1"), (b"{}", "1.5")])
def test_webhook_signature_rejects_malformed_input(body: bytes, timestamp: str) -> None:
    ch = _mk(signing_secret="synthetic-secret")
    assert ch._verify_signature(body, timestamp, "v0=invalid") is False
