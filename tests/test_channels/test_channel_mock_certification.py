"""Deterministic certification contracts for public channel adapters.

These tests deliberately stop at the provider boundary.  They prove local
admission, durability, routing, lifecycle, and receipt behavior without
requiring vendor credentials or making external requests.  Passing them is a
prerequisite for live certification, not a substitute for it.
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.channels.contract import (
    ChannelCapabilities,
    ChannelSendResult,
    channel_capability_evidence,
)
from opensquilla.channels.delivery_store import (
    ChannelDeliveryStore,
    deliver_with_outbox,
    durable_enqueue,
    install_outbox,
)
from opensquilla.channels.dingtalk import DingTalkChannel, DingTalkChannelConfig
from opensquilla.channels.discord import DiscordChannel, DiscordChannelConfig
from opensquilla.channels.feishu import FeishuChannel, FeishuChannelConfig
from opensquilla.channels.matrix import MatrixChannel, MatrixChannelConfig
from opensquilla.channels.qq import QQChannel, QQChannelConfig
from opensquilla.channels.slack import SlackChannel
from opensquilla.channels.telegram import TelegramChannel, TelegramChannelConfig
from opensquilla.channels.types import (
    AuthenticatedPrincipal,
    IncomingMessage,
    IngressProvenance,
    IngressVerification,
    OutgoingMessage,
)
from opensquilla.channels.wecom import WeComChannel, WeComChannelConfig


def _inbound(provider: str, event_id: str = "event-1") -> IncomingMessage:
    return IncomingMessage(
        sender_id="user-origin",
        channel_id="chat-origin",
        content="hello",
        metadata={
            "is_group": True,
            "native_message_id": event_id,
            "event_id": event_id,
            "message_id": event_id,
            "msg_id": event_id,
            "thread_ts": "thread-origin",
            "native_thread_id": "thread-origin",
            "thread_id": "thread-origin",
            "chat_type": "group",
            "group_openid": "chat-origin",
        },
        provenance=IngressProvenance(
            provider=provider,
            account_id="account-1",
            transport="mock",
            verification=IngressVerification.SDK_SESSION,
            event_id=event_id,
            principal=AuthenticatedPrincipal(subject_id="user-origin"),
        ),
    )


def _public_channels(tmp_path: Path) -> dict[str, Any]:
    return {
        "slack": SlackChannel(
            token="test-token",
            slack_channel_id="C-default",
            signing_secret="test-secret",
        ),
        "discord": DiscordChannel(DiscordChannelConfig(token="test-token")),
        "feishu": FeishuChannel(
            FeishuChannelConfig(
                app_id="test-app",
                app_secret="test-secret",
                connection_mode="websocket",
            )
        ),
        "dingtalk": DingTalkChannel(
            DingTalkChannelConfig(client_id="test-app", client_secret="test-secret")
        ),
        "wecom": WeComChannel(
            WeComChannelConfig(
                connection_mode="websocket",
                bot_id="test-bot",
                bot_secret="test-secret",
            )
        ),
        "qq": QQChannel(QQChannelConfig(app_id="test-app", app_secret="test-secret")),
        "matrix": MatrixChannel(
            MatrixChannelConfig(
                homeserver_url="https://matrix.example.test",
                user_id="@bot:example.test",
                access_token="test-token",
                device_id="TEST",
                workspace_dir=str(tmp_path),
            )
        ),
        "telegram": TelegramChannel(
            TelegramChannelConfig(token="test-token", transport_name="polling")
        ),
    }


@pytest.mark.parametrize(
    ("provider", "entrypoints"),
    [
        ("slack", (SlackChannel.enqueue,)),
        ("discord", (DiscordChannel.enqueue,)),
        ("feishu", (FeishuChannel.enqueue,)),
        ("dingtalk", (DingTalkChannel.enqueue,)),
        ("wecom", (WeComChannel.enqueue,)),
        ("qq", (QQChannel._enqueue_message,)),
        (
            "matrix",
            (MatrixChannel._on_room_message_text, MatrixChannel._on_room_message_media),
        ),
        ("telegram", (TelegramChannel.enqueue,)),
    ],
)
def test_every_public_ingress_path_uses_durable_enqueue(
    provider: str,
    entrypoints: tuple[Callable[..., Any], ...],
) -> None:
    for entrypoint in entrypoints:
        assert "durable_enqueue" in inspect.getsource(entrypoint), (
            f"{provider}.{entrypoint.__name__} bypasses the durable ingress journal"
        )


def test_public_capability_evidence_never_claims_missing_methods_effective(
    tmp_path: Path,
) -> None:
    method_backed = {
        ChannelCapabilities.TYPING_INDICATOR,
        ChannelCapabilities.NATIVE_FILE_UPLOAD,
        ChannelCapabilities.REPLY,
        ChannelCapabilities.THREAD_REPLY,
        ChannelCapabilities.EDIT,
        ChannelCapabilities.DELETE,
        ChannelCapabilities.REACTIONS,
        ChannelCapabilities.CARDS,
        ChannelCapabilities.INTERACTIVE_CARDS,
    }

    for provider, channel in _public_channels(tmp_path).items():
        evidence = channel_capability_evidence(channel)
        assert evidence, f"{provider} did not expose capability evidence"
        for capability, row in evidence.items():
            assert row["proof_status"] == "unverified"
            if capability in method_backed:
                assert row["effective"] is bool(row["methods"]), (
                    f"{provider}.{capability} reports effective support without a method"
                )


@pytest.mark.asyncio
async def test_all_public_channels_are_disconnected_before_start(tmp_path: Path) -> None:
    for provider, channel in _public_channels(tmp_path).items():
        health = await channel.health_check()
        assert health.connected is False, f"{provider} was healthy before authentication"


def test_reply_builders_pin_the_triggering_conversation(tmp_path: Path) -> None:
    channels = _public_channels(tmp_path)

    slack = channels["slack"]
    slack.reply_in_thread = True
    slack_reply = slack.build_reply_message("answer", _inbound("slack"))
    assert slack_reply.reply_to == "chat-origin"
    assert slack_reply.metadata["thread_ts"] == "thread-origin"

    discord_reply = channels["discord"].build_reply_message(
        "answer", _inbound("discord")
    )
    assert discord_reply.reply_to == "chat-origin"
    assert discord_reply.metadata["reply_to_message_id"] == "event-1"

    feishu_reply = channels["feishu"].build_reply_message("answer", _inbound("feishu"))
    assert feishu_reply.reply_to == "chat-origin"
    assert feishu_reply.metadata["native_thread_id"] == "thread-origin"

    dingtalk_reply = channels["dingtalk"].build_reply_message(
        "answer", _inbound("dingtalk")
    )
    assert dingtalk_reply.reply_to == "chat-origin"
    assert dingtalk_reply.metadata["dingtalk_reply_msg_id"] == "event-1"

    wecom_reply = channels["wecom"].build_reply_message("answer", _inbound("wecom"))
    assert wecom_reply.reply_to == "chat-origin"

    qq_reply = channels["qq"].build_reply_message("answer", _inbound("qq"))
    assert qq_reply.metadata["group_openid"] == "chat-origin"
    assert qq_reply.metadata["msg_id"] == "event-1"

    matrix_reply = channels["matrix"].build_reply_message("answer", _inbound("matrix"))
    assert matrix_reply.reply_to == "chat-origin"
    assert matrix_reply.metadata["reply_event_id"] == "event-1"

    telegram_reply = channels["telegram"].build_reply_message(
        "answer", _inbound("telegram")
    )
    assert telegram_reply.reply_to == "chat-origin"
    assert telegram_reply.metadata["thread_id"] == "thread-origin"


def test_ingress_event_namespace_isolated_by_provider_and_account(tmp_path: Path) -> None:
    store = ChannelDeliveryStore(tmp_path / "channel_delivery.sqlite")
    try:
        slack = _inbound("slack", "same-native-id")
        discord = _inbound("discord", "same-native-id")
        second_account = slack.model_copy(
            update={
                "provenance": replace(slack.provenance, account_id="account-2")
            }
        )

        assert store.accept_inbound("main", slack) is True
        assert store.accept_inbound("main", discord) is True
        assert store.accept_inbound("main", second_account) is True
        assert store.diagnostics("main")["ingress"]["accepted"]["count"] == 3
    finally:
        store.close()


def test_duplicate_accepted_event_is_not_enqueued_twice_and_recovers_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "channel_delivery.sqlite"
    message = _inbound("slack")
    first = ChannelDeliveryStore(path)
    queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
    channel = SimpleNamespace(
        _delivery_store=first,
        _delivery_channel_name="slack-main",
    )
    try:
        assert durable_enqueue(channel, message, queue) is True
        assert durable_enqueue(channel, message, queue) is False
        assert queue.qsize() == 1
    finally:
        first.close()

    restarted = ChannelDeliveryStore(path)
    recovered_queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
    restarted_channel = SimpleNamespace(
        _delivery_store=restarted,
        _delivery_channel_name="slack-main",
    )
    try:
        recovered = restarted.recover_inbound("slack-main")
        assert len(recovered) == 1
        assert durable_enqueue(restarted_channel, recovered[0], recovered_queue) is True
        assert durable_enqueue(restarted_channel, message, recovered_queue) is False
        assert recovered_queue.qsize() == 1
    finally:
        restarted.close()


@pytest.mark.parametrize(
    ("result", "expected_state"),
    [
        (
            ChannelSendResult.sent(
                capability=ChannelCapabilities.GROUP_CHAT,
                provider_message_id="provider-message",
            ),
            "sent",
        ),
        (None, "sent_unconfirmed"),
        (
            ChannelSendResult.failed(
                capability=ChannelCapabilities.GROUP_CHAT,
                reason="rate limited",
                retryable=True,
            ),
            "failed",
        ),
        (
            ChannelSendResult.unsupported(
                capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
                reason="not implemented",
            ),
            "unsupported",
        ),
    ],
)
@pytest.mark.asyncio
async def test_outbox_records_explicit_provider_outcomes_without_retry(
    tmp_path: Path,
    result: ChannelSendResult | None,
    expected_state: str,
) -> None:
    store = ChannelDeliveryStore(tmp_path / "channel_delivery.sqlite")
    calls = 0

    class Channel:
        _delivery_store = store
        _delivery_channel_name = "mock-main"

        async def send(self, _message: OutgoingMessage) -> ChannelSendResult | None:
            nonlocal calls
            calls += 1
            return result

    channel = Channel()
    channel._delivery_raw_send = channel.send
    try:
        assert (
            await deliver_with_outbox(
                channel,
                OutgoingMessage(content="hello", reply_to="chat-origin"),
            )
            is result
        )
        assert calls == 1
        assert store.diagnostics("mock-main")["outbox"][expected_state]["count"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_install_outbox_is_idempotent(tmp_path: Path) -> None:
    store = ChannelDeliveryStore(tmp_path / "channel_delivery.sqlite")
    calls = 0

    class Channel:
        _delivery_store = store
        _delivery_channel_name = "mock-main"

        async def send(self, _message: OutgoingMessage) -> None:
            nonlocal calls
            calls += 1

    channel = Channel()
    try:
        install_outbox(channel)
        wrapped = channel.send
        install_outbox(channel)
        assert channel.send is wrapped

        await channel.send(OutgoingMessage(content="hello", reply_to="chat-origin"))
        assert calls == 1
        assert store.diagnostics("mock-main")["outbox"]["sent_unconfirmed"]["count"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_safe_probes_do_not_start_ingress_or_mutate_provider_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, list[str]] = {
        "slack": [],
        "discord": [],
        "feishu": [],
        "telegram": [],
    }

    class SlackResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"ok": True, "user_id": "B1", "team_id": "T1"}

    class SlackClient:
        async def post(self, path: str) -> SlackResponse:
            calls["slack"].append(path)
            return SlackResponse()

    slack = SlackChannel("token", "C1", signing_secret="secret")
    monkeypatch.setattr(slack, "_get_client", lambda: SlackClient())
    assert (await slack.probe_connection())["authenticated"] is True
    assert calls["slack"] == ["/auth.test"]
    assert slack.is_connected() is False

    discord = DiscordChannel(DiscordChannelConfig(token="token"))

    async def fetch_gateway() -> str:
        calls["discord"].append("gateway/bot")
        return "wss://gateway.example.test?v=10"

    monkeypatch.setattr(discord, "_fetch_gateway_url", fetch_gateway)
    assert (await discord.probe_connection())["authenticated"] is True
    assert calls["discord"] == ["gateway/bot"]
    assert discord._ws is None

    feishu = FeishuChannel(
        FeishuChannelConfig(
            app_id="app",
            app_secret="secret",
            connection_mode="websocket",
        )
    )

    async def refresh_feishu_identity() -> None:
        calls["feishu"].append("bot/v3/info")
        feishu.bot_open_id = "ou_bot"

    monkeypatch.setattr(feishu, "_refresh_bot_identity", refresh_feishu_identity)
    assert (await feishu.probe_connection())["authenticated"] is True
    assert calls["feishu"] == ["bot/v3/info"]
    assert feishu.is_connected() is False

    telegram = TelegramChannel(TelegramChannelConfig(token="token"))

    async def telegram_api(method: str, _payload: Any = None) -> dict[str, Any]:
        calls["telegram"].append(method)
        return {"id": 1, "username": "bot"}

    monkeypatch.setattr(telegram, "_api", telegram_api)
    assert (await telegram.probe_connection())["authenticated"] is True
    assert calls["telegram"] == ["getMe"]
    assert telegram._poll_task is None

    wecom = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot",
            bot_secret="secret",
        )
    )
    assert (await wecom.probe_connection()) == {
        "authenticated": False,
        "supported": False,
        "reason": (
            "WeCom AI Bot credentials can only be validated by subscribing; "
            "a probe could disconnect the active single-owner connection."
        ),
    }
    assert (await wecom.health_check()).connected is False


@pytest.mark.asyncio
async def test_missing_credentials_fail_before_transport_or_http_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discord = DiscordChannel(DiscordChannelConfig(token=""))
    discord_transport_calls = 0

    async def discord_connect(_url: str) -> None:
        nonlocal discord_transport_calls
        discord_transport_calls += 1

    monkeypatch.setattr(discord, "_connect_ws", discord_connect)
    with pytest.raises(ValueError, match="bot token is required"):
        await discord.start()
    assert discord_transport_calls == 0

    wecom = WeComChannel(WeComChannelConfig(connection_mode="webhook"))
    wecom_client_calls = 0

    def wecom_client() -> None:
        nonlocal wecom_client_calls
        wecom_client_calls += 1

    monkeypatch.setattr(wecom, "_get_client", wecom_client)
    with pytest.raises(ValueError, match="corp_id and corp_secret"):
        await wecom.start()
    assert wecom_client_calls == 0


@pytest.mark.asyncio
async def test_empty_targets_fail_before_provider_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    discord = DiscordChannel(DiscordChannelConfig(token="token"))
    discord_client_calls = 0

    def discord_client() -> None:
        nonlocal discord_client_calls
        discord_client_calls += 1

    monkeypatch.setattr(discord, "_get_client", discord_client)
    with pytest.raises(ValueError, match="channel target is required"):
        await discord.send(OutgoingMessage(content="hello"))
    with pytest.raises(ValueError, match="channel target is required"):
        await discord.send_file("", str(tmp_path / "never-opened"))
    with pytest.raises(ValueError, match="channel target is required"):
        await discord.edit("unknown-message", "edit")
    with pytest.raises(ValueError, match="channel target is required"):
        await discord.delete("unknown-message")
    assert discord_client_calls == 0

    feishu = FeishuChannel(
        FeishuChannelConfig(
            app_id="app",
            app_secret="secret",
            connection_mode="websocket",
        )
    )
    feishu_client_calls = 0

    def feishu_client() -> None:
        nonlocal feishu_client_calls
        feishu_client_calls += 1

    monkeypatch.setattr(feishu, "_get_client", feishu_client)
    with pytest.raises(ValueError, match="chat target is required"):
        await feishu.send(OutgoingMessage(content="hello"))
    with pytest.raises(ValueError, match="chat target is required"):
        await feishu.send_text("", "hello")
    with pytest.raises(ValueError, match="chat target is required"):
        await feishu.send_file("", str(tmp_path / "never-opened"))
    assert feishu_client_calls == 0


@pytest.mark.asyncio
async def test_dead_dispatch_and_poll_workers_make_health_unhealthy() -> None:
    discord = DiscordChannel(DiscordChannelConfig(token="token"))
    discord._connected = True
    discord._heartbeat_task = asyncio.create_task(asyncio.sleep(60))
    discord._dispatch_task = asyncio.create_task(asyncio.sleep(0))
    await discord._dispatch_task
    try:
        assert (await discord.health_check()).connected is False
    finally:
        discord._heartbeat_task.cancel()
        await asyncio.gather(discord._heartbeat_task, return_exceptions=True)

    telegram = TelegramChannel(
        TelegramChannelConfig(token="token", transport_name="polling")
    )
    telegram._connected = True
    telegram._poll_task = asyncio.create_task(asyncio.sleep(0))
    await telegram._poll_task
    assert (await telegram.health_check()).connected is False

    webhook = TelegramChannel(
        TelegramChannelConfig(
            token="token",
            transport_name="webhook",
            webhook_url="https://example.test/telegram",
            webhook_secret_token="secret",
        )
    )
    webhook._connected = True
    assert (await webhook.health_check()).connected is True


def test_retryable_outbox_failure_is_recorded_without_implicit_retry(tmp_path: Path) -> None:
    """The retry bit stays diagnostic; the outbox does not duplicate a side effect."""
    store = ChannelDeliveryStore(tmp_path / "channel_delivery.sqlite")
    try:
        send_id = store.begin_send(
            "mock-main",
            OutgoingMessage(content="hello", reply_to="chat-origin"),
        )
        store.complete_send(
            send_id,
            ChannelSendResult.failed(
                capability=ChannelCapabilities.GROUP_CHAT,
                reason="provider rate limit",
                retryable=True,
            ),
        )
        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                "SELECT state, retryable, error_message FROM channel_outbox "
                "WHERE send_id = ?",
                (send_id,),
            ).fetchone()
        assert row == ("failed", 1, "provider rate limit")
    finally:
        store.close()
