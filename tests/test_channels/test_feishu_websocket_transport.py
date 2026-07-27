from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.protocol import State as WebSocketState

from opensquilla.channels.approval_prompt import ApprovalPromptRequest, render_approval_prompt
from opensquilla.channels.contract import ChannelCapabilities
from opensquilla.channels.feishu import (
    FeishuChannel,
    FeishuChannelConfig,
    FeishuWebSocketTransport,
    _feishu_sdk_websocket_is_open,
    _TokenState,
)
from opensquilla.channels.transports import InboundEventEnvelope


class _Builder:
    instances: list[_Builder] = []

    def __init__(self) -> None:
        self.registered: dict[str, Callable[[Any], None]] = {}
        _Builder.instances.append(self)

    def register_p2_im_message_receive_v1(self, callback: Callable[[Any], None]) -> _Builder:
        self.message_callback = callback
        self.registered["im.message.receive_v1"] = callback
        return self

    def register_p2_im_message_message_read_v1(self, callback: Callable[[Any], None]) -> _Builder:
        self.read_callback = callback
        self.registered["im.message.message_read_v1"] = callback
        return self

    def register_p2_im_chat_member_bot_added_v1(self, callback: Callable[[Any], None]) -> _Builder:
        self.registered["im.chat.member.bot.added_v1"] = callback
        return self

    def register_p2_im_chat_member_bot_deleted_v1(
        self, callback: Callable[[Any], None]
    ) -> _Builder:
        self.registered["im.chat.member.bot.deleted_v1"] = callback
        return self

    def register_p2_im_message_reaction_created_v1(
        self, callback: Callable[[Any], None]
    ) -> _Builder:
        self.registered["im.message.reaction.created_v1"] = callback
        return self

    def register_p2_im_message_reaction_deleted_v1(
        self, callback: Callable[[Any], None]
    ) -> _Builder:
        self.registered["im.message.reaction.deleted_v1"] = callback
        return self

    def register_p2_card_action_trigger(self, callback: Callable[[Any], None]) -> _Builder:
        self.registered["card.action.trigger"] = callback
        return self

    def build(self) -> object:
        return object()


def _install_fake_lark_module(monkeypatch: pytest.MonkeyPatch) -> tuple[types.ModuleType, type]:
    sdk_module = types.ModuleType("_fake_lark_ws_client")
    sdk_module.loop = None
    sys.modules[sdk_module.__name__] = sdk_module
    _Builder.instances.clear()

    async def _select_forever() -> None:
        while True:
            await asyncio.sleep(3600)

    class FakeClient:
        instances: list[FakeClient] = []

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs
            self.disconnect_called = False
            self.started = False
            self.start_loop: asyncio.AbstractEventLoop | None = None
            self._conn: Any | None = None
            FakeClient.instances.append(self)

        def start(self) -> None:
            loop = sdk_module.loop
            assert isinstance(loop, asyncio.AbstractEventLoop)
            self.start_loop = loop
            self.started = True
            self._conn = SimpleNamespace(state=WebSocketState.OPEN)
            loop.run_until_complete(_select_forever())

        async def _disconnect(self) -> None:
            self.disconnect_called = True

    FakeClient.__module__ = sdk_module.__name__
    sdk_module.Client = FakeClient

    fake_lark = types.SimpleNamespace(
        EventDispatcherHandler=types.SimpleNamespace(builder=lambda *_args: _Builder()),
        FEISHU_DOMAIN="https://open.feishu.cn",
        LARK_DOMAIN="https://open.larksuite.com",
        LogLevel=types.SimpleNamespace(INFO="info"),
        ws=types.SimpleNamespace(Client=FakeClient),
    )
    monkeypatch.setattr("opensquilla.channels.feishu._import_lark_oapi", lambda: fake_lark)
    return sdk_module, FakeClient


async def _noop_handler(_event: Any) -> None:
    return None


async def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


class _AliveThread:
    @staticmethod
    def is_alive() -> bool:
        return True


def _health_transport() -> FeishuWebSocketTransport:
    transport = FeishuWebSocketTransport(
        FeishuChannelConfig(
            app_id="cli_test_health_only",
            app_secret="test-secret-health-only",
            connection_mode="websocket",
        )
    )
    transport._thread = _AliveThread()  # type: ignore[assignment]
    return transport


@pytest.mark.parametrize(
    "client",
    (
        object(),
        SimpleNamespace(_conn=None),
        SimpleNamespace(_conn=SimpleNamespace(state="OPEN")),
    ),
)
def test_feishu_websocket_connection_helper_fails_closed_for_unknown_sdk_state(
    client: object,
) -> None:
    assert _feishu_sdk_websocket_is_open(client) is False


@pytest.mark.asyncio
async def test_feishu_websocket_worker_without_connection_is_connecting() -> None:
    transport = _health_transport()
    transport._ws_client = SimpleNamespace(_conn=None)

    health = await transport.health_check()

    assert health.connected is False
    assert health.extra == {
        "transport": "websocket",
        "connection_phase": "connecting",
    }


@pytest.mark.asyncio
async def test_feishu_websocket_requires_open_state_and_reports_reconnecting_after_close() -> None:
    transport = _health_transport()
    connection = SimpleNamespace(state=WebSocketState.OPEN)
    transport._ws_client = SimpleNamespace(_conn=connection)

    open_health = await transport.health_check()

    assert open_health.connected is True
    assert open_health.extra["connection_phase"] == "open"

    connection.state = WebSocketState.CLOSED
    closed_health = await transport.health_check()

    assert closed_health.connected is False
    assert closed_health.extra["connection_phase"] == "reconnecting"


@pytest.mark.asyncio
async def test_feishu_websocket_closed_sdk_connection_is_reconnecting() -> None:
    transport = _health_transport()
    transport._ws_client = SimpleNamespace(
        _conn=SimpleNamespace(state=WebSocketState.CLOSED)
    )

    health = await transport.health_check()

    assert health.connected is False
    assert health.extra["connection_phase"] == "reconnecting"


@pytest.mark.asyncio
async def test_feishu_websocket_stop_stops_sdk_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_module, fake_client = _install_fake_lark_module(monkeypatch)
    transport = FeishuWebSocketTransport(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )

    await transport.start(_noop_handler)
    await _wait_until(lambda: bool(fake_client.instances and fake_client.instances[-1].started))

    client = fake_client.instances[-1]
    assert client.start_loop is sdk_module.loop
    assert client.args[:2] == ("app", "secret")

    open_health = await transport.health_check()
    assert open_health.connected is True
    assert open_health.extra["connection_phase"] == "open"

    await transport.stop()

    assert client.disconnect_called is True
    assert transport._thread is None
    stopped_health = await transport.health_check()
    assert stopped_health.connected is False
    assert stopped_health.extra["connection_phase"] == "stopped"


@pytest.mark.asyncio
async def test_feishu_websocket_worker_error_is_structured_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sdk_module, fake_client = _install_fake_lark_module(monkeypatch)
    app_id = "cli_test_redaction_identifier"
    app_secret = "test-secret-redaction-value"

    def fail_with_credentials(_self: Any) -> None:
        raise RuntimeError(f"failed for app_id={app_id} app_secret={app_secret}")

    monkeypatch.setattr(fake_client, "start", fail_with_credentials)
    transport = FeishuWebSocketTransport(
        FeishuChannelConfig(
            app_id=app_id,
            app_secret=app_secret,
            connection_mode="websocket",
        )
    )

    with pytest.raises(RuntimeError) as caught:
        await transport.start(_noop_handler)

    health = await transport.health_check()
    diagnostic = health.extra["last_error"]
    combined = f"{caught.value!s} {diagnostic!r}"
    assert health.connected is False
    assert health.extra["connection_phase"] == "stopped"
    assert diagnostic["error_class"] == "transport_transient"
    assert diagnostic["retryable"] is True
    assert "***" in diagnostic["message"]
    assert app_id not in combined
    assert app_secret not in combined


@pytest.mark.asyncio
async def test_feishu_websocket_client_init_error_is_structured_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sdk_module, fake_client = _install_fake_lark_module(monkeypatch)
    app_id = "cli_test_init_redaction_identifier"
    app_secret = "test-secret-init-redaction-value"

    def fail_with_credentials(_self: Any, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(f"failed for app_id={app_id} app_secret={app_secret}")

    monkeypatch.setattr(fake_client, "__init__", fail_with_credentials)
    transport = FeishuWebSocketTransport(
        FeishuChannelConfig(
            app_id=app_id,
            app_secret=app_secret,
            connection_mode="websocket",
        )
    )

    with pytest.raises(RuntimeError) as caught:
        await transport.start(_noop_handler)

    health = await transport.health_check()
    diagnostic = health.extra["last_error"]
    combined = f"{caught.value!s} {caught.value!r} {diagnostic!r}"
    assert health.connected is False
    assert health.extra["connection_phase"] == "stopped"
    assert diagnostic["error_class"] == "transport_transient"
    assert diagnostic["retryable"] is True
    assert "***" in diagnostic["message"]
    assert app_id not in combined
    assert app_secret not in combined
    assert transport._handler is None
    assert transport._loop is None
    assert transport._lark is None
    assert transport._ws_client is None
    assert transport._active_registration is False


@pytest.mark.asyncio
async def test_feishu_websocket_auth_failure_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sdk_module, fake_client = _install_fake_lark_module(monkeypatch)
    client_exception = type(
        "ClientException",
        (RuntimeError,),
        {"__module__": "lark_oapi.ws.exception"},
    )

    def reject_credentials(_self: Any) -> None:
        raise client_exception("invalid app credentials")

    monkeypatch.setattr(fake_client, "start", reject_credentials)
    transport = FeishuWebSocketTransport(
        FeishuChannelConfig(
            app_id="cli_test_auth_invalid",
            app_secret="test-secret-auth-invalid",
            connection_mode="websocket",
        )
    )

    with pytest.raises(RuntimeError):
        await transport.start(_noop_handler)

    health = await transport.health_check()
    diagnostic = health.extra["last_error"]
    assert diagnostic["error_class"] == "auth_invalid"
    assert diagnostic["retryable"] is False


@pytest.mark.asyncio
async def test_feishu_websocket_delayed_auth_failure_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sdk_module, fake_client = _install_fake_lark_module(monkeypatch)
    client_exception = type(
        "ClientException",
        (RuntimeError,),
        {"__module__": "lark_oapi.ws.exception"},
    )

    def reject_credentials_after_connect_attempt(_self: Any) -> None:
        # This exceeds the former 50 ms grace window. Startup must wait for
        # the transport to open or fail, rather than registering a dead client.
        time.sleep(0.1)
        raise client_exception("invalid app credentials")

    monkeypatch.setattr(fake_client, "start", reject_credentials_after_connect_attempt)
    transport = FeishuWebSocketTransport(
        FeishuChannelConfig(
            app_id="cli_test_delayed_auth_invalid",
            app_secret="test-secret-delayed-auth-invalid",
            connection_mode="websocket",
        )
    )

    with pytest.raises(RuntimeError):
        await transport.start(_noop_handler)

    health = await transport.health_check()
    diagnostic = health.extra["last_error"]
    assert diagnostic["error_class"] == "auth_invalid"
    assert diagnostic["retryable"] is False
    assert health.extra["connection_phase"] == "stopped"


def test_feishu_websocket_approval_prompt_falls_back_to_text() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )
    request = ApprovalPromptRequest(
        approval_id="approval-1",
        namespace="exec",
        session_key="agent:main:chat",
        command_or_tool="rm target.txt",
        agent="main",
        short_code="AB12",
    )

    prompt = render_approval_prompt(channel.capability_profile, request)

    assert channel.capability_profile.supports(ChannelCapabilities.CARDS)
    assert not channel.capability_profile.supports(ChannelCapabilities.INTERACTIVE_CARDS)
    assert "card" not in prompt
    assert "/approve AB12" in prompt["text"]
    assert "/deny AB12" in prompt["text"]


@pytest.mark.asyncio
async def test_feishu_channel_propagates_transport_phase_and_structured_error() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(
            app_id="cli_test_channel_health",
            app_secret="test-secret-channel-health",
            connection_mode="websocket",
        )
    )
    transport = channel._transport
    assert isinstance(transport, FeishuWebSocketTransport)
    transport._thread = _AliveThread()  # type: ignore[assignment]
    transport._ws_client = SimpleNamespace(
        _conn=SimpleNamespace(state=WebSocketState.CLOSED)
    )
    transport._last_error = {
        "error_class": "transport_transient",
        "message": "Synthetic websocket failure",
        "retryable": True,
    }
    channel._connected = True

    health = await channel.health_check()

    assert channel.is_connected() is False
    assert health.connected is False
    assert health.extra == {
        "transport": "websocket",
        "transport_connected": False,
        "connection_phase": "reconnecting",
        "last_error": {
            "error_class": "transport_transient",
            "message": "Synthetic websocket failure",
            "retryable": True,
        },
    }


@pytest.mark.asyncio
async def test_feishu_websocket_registers_supported_non_message_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_lark_module(monkeypatch)
    transport = FeishuWebSocketTransport(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )

    await transport.start(_noop_handler)
    await transport.stop()

    assert _Builder.instances
    assert set(_Builder.instances[-1].registered) == {
        "im.message.receive_v1",
        "im.message.message_read_v1",
        "im.chat.member.bot.added_v1",
        "im.chat.member.bot.deleted_v1",
        "im.message.reaction.created_v1",
        "im.message.reaction.deleted_v1",
        "card.action.trigger",
    }


@pytest.mark.asyncio
async def test_feishu_websocket_rejects_second_concurrent_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_lark_module(monkeypatch)
    first = FeishuWebSocketTransport(
        FeishuChannelConfig(app_id="app-1", app_secret="secret", connection_mode="websocket")
    )
    second = FeishuWebSocketTransport(
        FeishuChannelConfig(app_id="app-2", app_secret="secret", connection_mode="websocket")
    )

    await first.start(_noop_handler)
    try:
        with pytest.raises(RuntimeError, match="only one Feishu websocket"):
            await second.start(_noop_handler)
    finally:
        await first.stop()

    await second.start(_noop_handler)
    await second.stop()


@pytest.mark.asyncio
async def test_feishu_websocket_stop_releases_singleton_after_worker_thread_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_lark_module(monkeypatch)
    first = FeishuWebSocketTransport(
        FeishuChannelConfig(app_id="app-1", app_secret="secret", connection_mode="websocket")
    )
    second = FeishuWebSocketTransport(
        FeishuChannelConfig(app_id="app-2", app_secret="secret", connection_mode="websocket")
    )

    first._register_active_client()
    dead_thread = threading.Thread(target=lambda: None)
    dead_thread.start()
    dead_thread.join()
    first._thread = dead_thread
    await first.stop()

    await second.start(_noop_handler)
    await second.stop()


class _FakeTransport:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self, _handler: Callable[[Any], Awaitable[None]]) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def health_check(self) -> object:
        return object()


@pytest.mark.asyncio
async def test_feishu_websocket_start_does_not_block_on_bot_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )
    transport = _FakeTransport()
    channel._transport = transport  # type: ignore[assignment]

    async def slow_token() -> str:
        await asyncio.sleep(3600)
        return "tenant-token"

    monkeypatch.setattr(channel, "_get_token", slow_token)

    await asyncio.wait_for(channel.start(), timeout=0.1)
    await channel.stop()

    assert transport.started is True
    assert transport.stopped is True


@pytest.mark.asyncio
async def test_feishu_websocket_bot_identity_error_log_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_id = "cli_test_identity_redaction_identifier"
    app_secret = "test-secret-identity-redaction-value"
    tenant_token = "t-synthetic-tenant-token"
    channel = FeishuChannel(
        FeishuChannelConfig(
            app_id=app_id,
            app_secret=app_secret,
            connection_mode="websocket",
        )
    )
    channel._token_state = _TokenState(token=tenant_token, expires_at=float("inf"))

    async def fail_with_credentials() -> None:
        raise RuntimeError(
            f"failed for app_id={app_id} app_secret={app_secret} token={tenant_token}"
        )

    captured: dict[str, Any] = {}

    def capture_warning(event: str, **kwargs: Any) -> None:
        captured.update(event=event, **kwargs)

    monkeypatch.setattr(channel, "_refresh_bot_identity", fail_with_credentials)
    monkeypatch.setattr(
        "opensquilla.channels.feishu.log",
        SimpleNamespace(warning=capture_warning),
    )

    await channel._refresh_bot_identity_best_effort()

    combined = repr(captured)
    assert captured["event"] == "feishu.bot_identity_lookup_failed"
    assert "***" in captured["error"]
    assert app_id not in combined
    assert app_secret not in combined
    assert tenant_token not in combined


@pytest.mark.asyncio
async def test_feishu_websocket_dedupes_replayed_message_event() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )
    raw_event = {
        "header": {
            "event_id": "evt-duplicate",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_chat",
                "chat_type": "p2p",
                "message_type": "text",
                "content": '{"text":"draw an image"}',
            },
        },
    }
    envelope = InboundEventEnvelope(
        source="feishu:websocket",
        event_id="evt-duplicate",
        event_type="im.message.receive_v1",
        raw=raw_event,
        received_at=datetime.now(UTC),
    )

    await channel._handle_inbound_event(envelope)
    await channel._handle_inbound_event(envelope)

    assert channel._queue.qsize() == 1
    assert (await channel.receive()).content == "draw an image"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type,raw_event",
    [
        (
            "im.chat.member.bot.added_v1",
            {"event": {"chat_id": "oc_chat", "operator_id": {"open_id": "ou_user"}}},
        ),
        (
            "im.chat.member.bot.deleted_v1",
            {"event": {"chat_id": "oc_chat", "operator_id": {"open_id": "ou_user"}}},
        ),
        (
            "im.message.reaction.created_v1",
            {
                "event": {
                    "message_id": "om_1",
                    "operator_type": "user",
                    "user_id": {"open_id": "ou_user"},
                    "reaction_type": {"emoji_type": "OK"},
                }
            },
        ),
        (
            "im.message.reaction.deleted_v1",
            {
                "event": {
                    "message_id": "om_1",
                    "operator_type": "user",
                    "user_id": {"open_id": "ou_user"},
                    "reaction_type": {"emoji_type": "OK"},
                }
            },
        ),
        (
            "card.action.trigger",
            {"event": {"open_id": "ou_user", "action": {"value": {"action": "noop"}}}},
        ),
    ],
)
async def test_feishu_non_message_events_do_not_start_agent_turns(
    event_type: str,
    raw_event: dict[str, Any],
) -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )
    envelope = InboundEventEnvelope(
        source="feishu:websocket",
        event_id=f"evt-{event_type}",
        event_type=event_type,
        raw={"header": {"event_id": f"evt-{event_type}", "event_type": event_type}, **raw_event},
        received_at=datetime.now(UTC),
    )

    await channel._handle_inbound_event(envelope)

    assert channel._queue.qsize() == 0


@pytest.mark.asyncio
async def test_feishu_clarify_card_action_enqueues_form_submission() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )
    envelope = InboundEventEnvelope(
        source="feishu:websocket",
        event_id="evt-clarify-card-action",
        event_type="card.action.trigger",
        raw={
            "header": {
                "event_id": "evt-clarify-card-action",
                "event_type": "card.action.trigger",
            },
            "event": {
                "open_id": "ou_user",
                "operator": {"open_id": "ou_operator"},
                "action": {
                    "value": {
                        "opensquilla_action": "clarify_submit",
                        "channel_id": "oc_chat",
                        "run_id": "run-1",
                        "step": "clarify",
                    },
                    "form_value": {
                        "destination": "Tokyo",
                        "days": 5,
                        "include_food": True,
                    },
                },
            },
        },
        received_at=datetime.now(UTC),
    )

    await channel._handle_inbound_event(envelope)

    msg = await channel.receive()
    assert msg.sender_id == "ou_operator"
    assert msg.channel_id == "oc_chat"
    assert "destination: Tokyo" in msg.content
    assert "days: 5" in msg.content
    assert "include_food: true" in msg.content
    assert msg.metadata["conversation_kind"] == "interaction"
    assert msg.metadata["is_group"] is True
    assert msg.metadata["input_provenance"] == "clarify_form"
    assert msg.metadata["clarify_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_feishu_clarify_card_action_preserves_direct_session_type() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )
    envelope = InboundEventEnvelope(
        source="feishu:websocket",
        event_id="evt-clarify-card-action-dm",
        event_type="card.action.trigger",
        raw={
            "header": {
                "event_id": "evt-clarify-card-action-dm",
                "event_type": "card.action.trigger",
            },
            "event": {
                "open_id": "ou_user",
                "operator": {"open_id": "ou_operator"},
                "action": {
                    "value": {
                        "opensquilla_action": "clarify_submit",
                        "channel_id": "oc_dm",
                        "chat_type": "p2p",
                        "is_group": False,
                        "run_id": "run-1",
                    },
                    "form_value": {"destination": "Tokyo"},
                },
            },
        },
        received_at=datetime.now(UTC),
    )

    await channel._handle_inbound_event(envelope)

    msg = await channel.receive()
    assert msg.sender_id == "ou_operator"
    assert msg.channel_id == "oc_dm"
    assert msg.metadata["is_group"] is False
    assert msg.metadata["chat_type"] == "p2p"
    assert msg.metadata["native_chat_id"] == "oc_dm"
