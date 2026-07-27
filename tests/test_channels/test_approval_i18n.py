"""Localized system messages for shared channel approval flows."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opensquilla.channels.approval_prompt import (
    ApprovalPromptRequest,
    bind_short_code,
    render_approval_prompt,
    reset_short_codes,
)
from opensquilla.channels.contract import ChannelCapabilityProfile
from opensquilla.channels.system_messages import _MESSAGES, render_channel_message
from opensquilla.channels.types import IncomingMessage
from opensquilla.gateway.approval_notify import _deliver_channel_prompt
from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.gateway.channel_dispatch import _maybe_resolve_channel_approval


def _config(locale: str) -> SimpleNamespace:
    return SimpleNamespace(control_ui=SimpleNamespace(default_locale=locale))


def test_all_supported_locales_have_a_complete_formatable_catalog() -> None:
    english_keys = set(_MESSAGES["en"])
    values = {
        "pairing_code": "PAIR1234",
        "label": "Command",
        "command": "echo ok",
        "code": "AB12",
        "question": "Run it?",
        "code_label": "Code",
        "bundle_id": "python-pypi",
        "name": "compact",
        "reason": ": provider down",
        "mode": "Full Host Access",
        "method": "sessions.reset",
        "detail": ": missing operator.write",
        "missing": "operator.write",
    }

    for locale, messages in _MESSAGES.items():
        assert set(messages) == english_keys, locale
        for key in english_keys:
            assert render_channel_message(key, config=_config(locale), **values)


@pytest.fixture(autouse=True)
def _reset_approval_state() -> None:
    reset_approval_queue()
    reset_short_codes()
    yield
    reset_approval_queue()
    reset_short_codes()


def test_zh_hans_card_localizes_text_without_changing_approval_protocol() -> None:
    request = ApprovalPromptRequest(
        approval_id="exec-1",
        namespace="exec",
        session_key="agent:main:chat",
        command_or_tool="rm target.txt",
        agent="main",
        short_code="AB12",
        offer_always=True,
        origin_channel_id="chat-1",
        origin_is_group=False,
        origin_chat_type="direct",
        origin_thread_id="thread-1",
    )

    rendered = render_approval_prompt(
        ChannelCapabilityProfile(channel_type="feishu", interactive_cards=True),
        request,
        config=_config("zh-Hans"),
    )

    assert "需要批准才能运行特权命令" in rendered["text"]
    assert "/approve AB12 always" in rendered["text"]
    assert "/deny AB12" in rendered["text"]
    card = rendered["card"]
    assert card["header"]["title"]["content"] == "需要批准"
    assert card["elements"][0]["text"]["content"] == (
        "要运行特权命令吗？\n**命令：** `rm target.txt`\n**代码：** `AB12`"
    )
    assert [action["text"]["content"] for action in card["elements"][1]["actions"]] == [
        "批准",
        "始终允许",
        "拒绝",
    ]
    assert [action["value"] for action in card["elements"][1]["actions"]] == [
        {
            "opensquilla_action": "approval_resolve",
            "code": "AB12",
            "decision": "approve",
            "channel_id": "chat-1",
            "is_group": False,
            "chat_type": "direct",
            "thread_id": "thread-1",
        },
        {
            "opensquilla_action": "approval_resolve",
            "code": "AB12",
            "decision": "always",
            "channel_id": "chat-1",
            "is_group": False,
            "chat_type": "direct",
            "thread_id": "thread-1",
        },
        {
            "opensquilla_action": "approval_resolve",
            "code": "AB12",
            "decision": "deny",
            "channel_id": "chat-1",
            "is_group": False,
            "chat_type": "direct",
            "thread_id": "thread-1",
        },
    ]


class _Node:
    last_channel = "feishu"
    last_to = "chat-1"
    last_thread_id = None


class _SessionManager:
    async def get_session(self, session_key: str) -> _Node:
        return _Node()


class _Adapter:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(channel_type="feishu", interactive_cards=True)

    async def send(self, message: object) -> None:
        self.sent.append(message)


class _ChannelManager:
    def __init__(self, adapter: _Adapter) -> None:
        self._adapter = adapter

    def get(self, name: str) -> _Adapter:
        return self._adapter


def test_notifier_uses_configured_locale_for_summary_labels() -> None:
    adapter = _Adapter()
    params = {
        "approvalKind": "sandbox_network",
        "host": "pypi.org",
        "sessionKey": "agent:main:chat",
        "senderId": "owner-1",
    }
    approval_id = get_approval_queue().request(namespace="exec", params=params)

    asyncio.run(
        _deliver_channel_prompt(
            {"id": approval_id, "namespace": "exec", "params": params},
            session_manager=_SessionManager(),
            channel_manager=_ChannelManager(adapter),
            config=_config("zh-Hans"),
        )
    )

    assert len(adapter.sent) == 1
    message = adapter.sent[0]
    assert "网络主机：pypi.org" in message.content
    assert message.metadata["card"]["header"]["title"]["content"] == "需要批准"


def test_dispatch_localizes_result_without_changing_denial_decision() -> None:
    approval_id = get_approval_queue().request(
        namespace="exec",
        params={
            "toolName": "exec_command",
            "command": "rm target.txt",
            "sessionKey": "agent:main:chat",
            "senderId": "owner-1",
        },
    )
    code = bind_short_code(
        approval_id,
        namespace="exec",
        session_key="agent:main:chat",
        owner_sender_id="owner-1",
    )

    reply = asyncio.run(
        _maybe_resolve_channel_approval(
            msg=IncomingMessage(sender_id="owner-1", channel_id="chat-1", content=f"/deny {code}"),
            session_key="agent:main:chat",
            config=_config("zh-Hans"),
        )
    )

    assert reply is not None
    assert reply.content == f"已拒绝 {code}。"
    entry = get_approval_queue().get(approval_id)
    assert entry.resolved is True
    assert entry.approved is False
