"""The channel-approval notifier pushes a prompt to the originating channel."""

from __future__ import annotations

import asyncio

import pytest

from opensquilla.channels.approval_prompt import reset_short_codes, resolve_short_code
from opensquilla.channels.contract import ChannelCapabilityProfile
from opensquilla.gateway.approval_notify import register_approval_channel_notifier
from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue


class _FakeNode:
    def __init__(self) -> None:
        self.last_channel = "feishu"
        self.last_to = "chat-1"
        self.last_thread_id = None


class _FakeSessionManager:
    async def get_session(self, session_key: str):
        return _FakeNode()


class _FakeAdapter:
    def __init__(self, interactive_cards: bool) -> None:
        self._interactive_cards = interactive_cards
        self.sent: list = []

    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="feishu", interactive_cards=self._interactive_cards
        )

    async def send(self, message) -> None:
        self.sent.append(message)


class _FakeChannelManager:
    def __init__(self, adapter: _FakeAdapter) -> None:
        self._adapter = adapter

    def get(self, name: str):
        return self._adapter


@pytest.fixture(autouse=True)
def _reset_state():
    reset_approval_queue()
    reset_short_codes()
    yield
    reset_approval_queue()
    reset_short_codes()


def _run_notifier(adapter: _FakeAdapter, *, sender_id: str) -> str:
    async def _run() -> str:
        loop = asyncio.get_running_loop()
        scheduled: list = []

        def _schedule(coro):
            scheduled.append(loop.create_task(coro))

        remove = register_approval_channel_notifier(
            get_approval_queue(),
            session_manager=_FakeSessionManager(),
            channel_manager_ref=lambda: _FakeChannelManager(adapter),
            schedule=_schedule,
        )
        try:
            approval_id = get_approval_queue().request(
                namespace="exec",
                params={
                    "toolName": "exec_command",
                    "command": "rm target.txt",
                    "sessionKey": "agent:main:chat",
                    "senderId": sender_id,
                    "channelKind": "feishu",
                },
            )
            if scheduled:
                await asyncio.gather(*scheduled)
            return approval_id
        finally:
            remove()

    return asyncio.run(_run())


def test_notifier_sends_interactive_card_to_origin_channel() -> None:
    adapter = _FakeAdapter(interactive_cards=True)
    approval_id = _run_notifier(adapter, sender_id="owner-1")

    assert len(adapter.sent) == 1
    message = adapter.sent[0]
    assert message.reply_to == "chat-1"
    assert "card" in message.metadata
    # A short code bound to this approval + owner is now resolvable.
    code = None
    for candidate_card_value in message.metadata["card"]["elements"][1]["actions"]:
        code = candidate_card_value["value"]["code"]
        break
    assert code is not None
    binding = resolve_short_code(code)
    assert binding is not None
    assert binding.approval_id == approval_id
    assert binding.owner_sender_id == "owner-1"


def test_notifier_falls_back_to_text_without_cards() -> None:
    adapter = _FakeAdapter(interactive_cards=False)
    _run_notifier(adapter, sender_id="owner-1")

    assert len(adapter.sent) == 1
    message = adapter.sent[0]
    assert "card" not in message.metadata
    assert "/approve" in message.content


def test_notifier_ignores_non_channel_requests() -> None:
    adapter = _FakeAdapter(interactive_cards=True)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        scheduled: list = []
        remove = register_approval_channel_notifier(
            get_approval_queue(),
            session_manager=_FakeSessionManager(),
            channel_manager_ref=lambda: _FakeChannelManager(adapter),
            schedule=lambda coro: scheduled.append(loop.create_task(coro)),
        )
        try:
            # No senderId -> not a channel-originated approval; nothing scheduled.
            get_approval_queue().request(
                namespace="exec",
                params={"toolName": "exec_command", "command": "rm x", "sessionKey": "s"},
            )
            if scheduled:
                await asyncio.gather(*scheduled)
        finally:
            remove()

    asyncio.run(_run())
    assert adapter.sent == []


class _FailingAdapter(_FakeAdapter):
    async def send(self, message) -> None:
        raise RuntimeError("socket torn down")


def test_send_failure_denies_the_approval_fail_closed() -> None:
    adapter = _FailingAdapter(interactive_cards=False)
    approval_id = _run_notifier(adapter, sender_id="owner-1")

    entry = get_approval_queue().get(approval_id)
    # The prompt's addressee is the only expected resolver; an undeliverable
    # prompt is denied immediately instead of hanging until deadline expiry.
    assert entry.resolved is True
    assert entry.approved is False


def test_prompt_offers_always_only_when_same_type_choice_exists() -> None:
    adapter = _FakeAdapter(interactive_cards=True)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        scheduled: list = []
        remove = register_approval_channel_notifier(
            get_approval_queue(),
            session_manager=_FakeSessionManager(),
            channel_manager_ref=lambda: _FakeChannelManager(adapter),
            schedule=lambda coro: scheduled.append(loop.create_task(coro)),
        )
        try:
            get_approval_queue().request(
                namespace="exec",
                params={
                    "approvalKind": "sandbox_network",
                    "host": "pypi.org",
                    "sessionKey": "agent:main:chat",
                    "senderId": "owner-1",
                    "choices": [
                        {"id": "allow_once", "label": "Allow once", "approved": True},
                        {"id": "allow_same_type", "label": "Allow same type", "approved": True},
                        {"id": "deny", "label": "Deny", "approved": False},
                    ],
                },
            )
            await asyncio.gather(*scheduled)
        finally:
            remove()

    asyncio.run(_run())

    assert len(adapter.sent) == 1
    message = adapter.sent[0]
    assert "always" in message.content
    actions = message.metadata["card"]["elements"][1]["actions"]
    decisions = [action["value"]["decision"] for action in actions]
    assert decisions == ["approve", "always", "deny"]


class _NoneChannelManager:
    def get(self, name: str):
        return None


class _MissingSessionManager:
    async def get_session(self, session_key: str):
        return None


def _run_notifier_with(
    *,
    session_manager,
    channel_manager,
    params: dict | None = None,
) -> str:
    async def _run() -> str:
        loop = asyncio.get_running_loop()
        scheduled: list = []
        remove = register_approval_channel_notifier(
            get_approval_queue(),
            session_manager=session_manager,
            channel_manager_ref=lambda: channel_manager,
            schedule=lambda coro: scheduled.append(loop.create_task(coro)),
        )
        try:
            approval_id = get_approval_queue().request(
                namespace="exec",
                params=params
                or {
                    "toolName": "exec_command",
                    "command": "rm target.txt",
                    "sessionKey": "agent:main:chat",
                    "senderId": "owner-1",
                    "channelKind": "feishu",
                },
            )
            if scheduled:
                await asyncio.gather(*scheduled)
            return approval_id
        finally:
            remove()

    return asyncio.run(_run())


def test_adapter_missing_denies_the_approval_fail_closed() -> None:
    # The channel was removed/disabled mid-flight: the prompt's only expected
    # resolver can never see it, so the ask is denied instead of hanging for
    # the full queue deadline.
    approval_id = _run_notifier_with(
        session_manager=_FakeSessionManager(),
        channel_manager=_NoneChannelManager(),
    )
    entry = get_approval_queue().get(approval_id)
    assert entry.resolved is True
    assert entry.approved is False


def test_session_missing_denies_the_approval_fail_closed() -> None:
    approval_id = _run_notifier_with(
        session_manager=_MissingSessionManager(),
        channel_manager=_FakeChannelManager(_FakeAdapter(interactive_cards=False)),
    )
    entry = get_approval_queue().get(approval_id)
    assert entry.resolved is True
    assert entry.approved is False


def test_missing_channel_manager_stays_additive() -> None:
    # Transient boot/reload state: nothing is denied, the approval stays
    # resolvable from other surfaces.
    approval_id = _run_notifier_with(
        session_manager=_FakeSessionManager(),
        channel_manager=None,
    )
    entry = get_approval_queue().get(approval_id)
    assert entry.resolved is False


def _sandbox_choices() -> list[dict]:
    return [
        {"id": "allow_once", "label": "Allow once", "approved": True},
        {"id": "allow_same_type", "label": "Allow same type", "approved": True},
        {"id": "deny", "label": "Deny", "approved": False},
    ]


def test_sandbox_network_prompt_names_the_host() -> None:
    adapter = _FakeAdapter(interactive_cards=True)
    _run_notifier_with(
        session_manager=_FakeSessionManager(),
        channel_manager=_FakeChannelManager(adapter),
        params={
            "approvalKind": "sandbox_network",
            "host": "pypi.org",
            "sessionKey": "agent:main:chat",
            "senderId": "owner-1",
            "choices": _sandbox_choices(),
        },
    )
    assert len(adapter.sent) == 1
    message = adapter.sent[0]
    assert "Network host: pypi.org" in message.content
    assert "(unknown command)" not in message.content
    card_body = message.metadata["card"]["elements"][0]["text"]["content"]
    assert "pypi.org" in card_body


def test_sandbox_bundle_prompt_names_the_bundle() -> None:
    adapter = _FakeAdapter(interactive_cards=False)
    _run_notifier_with(
        session_manager=_FakeSessionManager(),
        channel_manager=_FakeChannelManager(adapter),
        params={
            "approvalKind": "sandbox_network",
            "bundle_id": "python-pypi",
            "sessionKey": "agent:main:chat",
            "senderId": "owner-1",
            "choices": _sandbox_choices(),
        },
    )
    assert len(adapter.sent) == 1
    assert "packages: python-pypi" in adapter.sent[0].content


def test_sandbox_path_prompt_names_the_path() -> None:
    adapter = _FakeAdapter(interactive_cards=False)
    _run_notifier_with(
        session_manager=_FakeSessionManager(),
        channel_manager=_FakeChannelManager(adapter),
        params={
            "approvalKind": "sandbox_path",
            "path": "/srv/data",
            "access": "rw",
            "sessionKey": "agent:main:chat",
            "senderId": "owner-1",
            "choices": _sandbox_choices(),
        },
    )
    assert len(adapter.sent) == 1
    message = adapter.sent[0]
    assert "Path: /srv/data (rw)" in message.content
    assert "(unknown command)" not in message.content
