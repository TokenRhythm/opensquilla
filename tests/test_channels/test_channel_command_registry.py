from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.channels.command_registry import (
    DEFAULT_COMMAND_REGISTRY,
    build_channel_rpc_context,
)
from opensquilla.channels.system_messages import _MESSAGES, render_channel_message
from opensquilla.channels.types import (
    AuthenticatedPrincipal,
    IncomingMessage,
    IngressProvenance,
    IngressVerification,
)
from opensquilla.engine.commands import DEFAULT_REGISTRY, Surface
from opensquilla.gateway.channel_dispatch import (
    _dispatch_channel_slash_command,
    _stamp_channel_admin_principal,
)
from opensquilla.gateway.protocol import make_error_res, make_ok_res
from opensquilla.gateway.routing import build_channel_route_envelope


def test_channel_command_names_include_usage_and_registry_words() -> None:
    expected = {
        word.lstrip("/").lower()
        for cmd in DEFAULT_REGISTRY.for_surface(Surface.CHANNEL)
        for word in cmd.words()
    }

    assert "usage" in DEFAULT_COMMAND_REGISTRY.command_names
    assert "sandbox" in DEFAULT_COMMAND_REGISTRY.command_names
    assert expected <= DEFAULT_COMMAND_REGISTRY.command_names


@pytest.mark.asyncio
async def test_channel_sandbox_command_sets_run_mode_from_argument() -> None:
    msg = IncomingMessage(sender_id="admin-1", channel_id="c1", content="/sandbox full")
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:admin-1",
        session_prefix="feishu",
        agent_id="main",
    )
    captured: dict[str, object] = {}

    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            captured["method"] = method
            captured["params"] = params
            return make_ok_res(req_id, {"runMode": "full"})

    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=envelope,
        message_content="/sandbox full",
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
    )

    assert captured == {
        "method": "sandbox.run_context.set",
        "params": {
            "sessionKey": "agent:main:feishu:admin-1",
            "runMode": "full",
        },
    }
    assert reply is not None
    assert reply.content == "Sandbox mode set to Full Host Access."
    assert reply.metadata["command"] == "sandbox"


@pytest.mark.asyncio
async def test_channel_sandbox_command_canonicalizes_legacy_safe_alias() -> None:
    msg = IncomingMessage(sender_id="admin-1", channel_id="c1", content="/sandbox trusted")
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:admin-1",
        session_prefix="feishu",
        agent_id="main",
    )
    captured: dict[str, object] = {}

    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            captured["params"] = params
            return make_ok_res(req_id, {"runMode": "safe"})

    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=envelope,
        message_content=msg.content,
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
    )

    assert captured["params"] == {
        "sessionKey": "agent:main:feishu:admin-1",
        "runMode": "safe",
    }
    assert reply is not None
    assert reply.content == "Sandbox mode set to Safe mode."


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", sorted(_MESSAGES))
async def test_channel_command_replies_follow_gateway_locale(locale: str) -> None:
    config = SimpleNamespace(control_ui=SimpleNamespace(default_locale=locale))
    msg = IncomingMessage(sender_id="u1", channel_id="c1", content="/sandbox full")
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )

    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            return make_ok_res(req_id, {"runMode": "full"})

    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=envelope,
        message_content=msg.content,
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
        config=config,
    )

    assert reply is not None
    assert reply.content == render_channel_message(
        "command_sandbox_updated",
        config=config,
        mode=render_channel_message("command_sandbox_full", config=config),
    )

    unsupported_msg = msg.model_copy(update={"content": "/unavailable"})
    unsupported_reply = await _dispatch_channel_slash_command(
        route_envelope=envelope,
        msg=unsupported_msg,
        session_manager=None,
        session_key=envelope.session_key,
        session_prefix="feishu",
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
        config=config,
    )

    assert unsupported_reply is not None
    assert unsupported_reply.content == render_channel_message(
        "command_unsupported", config=config, command="/unavailable"
    )


def test_channel_admin_rpc_context_is_owner_for_sandbox_full_switch() -> None:
    msg = IncomingMessage(
        sender_id="admin-1",
        channel_id="c1",
        content="/sandbox full",
        provenance=IngressProvenance(
            provider="feishu",
            verification=IngressVerification.SDK_SESSION,
            principal=AuthenticatedPrincipal(subject_id="admin-1"),
        ),
    )
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:admin-1",
        session_prefix="feishu",
        agent_id="main",
    )
    config = SimpleNamespace(channel_admin_senders={"feishu": ["admin-1"]})
    assert _stamp_channel_admin_principal(config, envelope, msg) is True

    admin_ctx = build_channel_rpc_context(envelope, gateway_config=config)

    assert admin_ctx.principal.role == "operator"
    assert "operator.write" in admin_ctx.principal.scopes
    assert admin_ctx.principal.is_owner is True


def test_channel_command_context_rejects_raw_admin_marker_spoof() -> None:
    msg = IncomingMessage(
        sender_id="admin-1",
        channel_id="c1",
        content="/sandbox full",
        metadata={"principal_is_owner": True, "channel_admin_verified": True},
    )
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:admin-1",
        session_prefix="feishu",
        agent_id="main",
    )
    config = SimpleNamespace(channel_admin_senders={"feishu": ["admin-1"]})

    assert "principal_is_owner" not in envelope.metadata
    assert "channel_admin_verified" not in envelope.metadata
    assert _stamp_channel_admin_principal(config, envelope, msg) is False

    ctx = build_channel_rpc_context(envelope, gateway_config=config)

    assert ctx.principal.role == "viewer"
    assert ctx.principal.is_owner is False


def test_channel_non_admin_rpc_context_is_not_owner_for_sandbox_full_switch() -> None:
    msg = IncomingMessage(sender_id="user-1", channel_id="c1", content="/sandbox full")
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:user-1",
        session_prefix="feishu",
        agent_id="main",
    )
    config = SimpleNamespace(channel_admin_senders={"feishu": ["admin-1"]})

    user_ctx = build_channel_rpc_context(envelope, gateway_config=config)

    assert user_ctx.principal.role == "viewer"
    assert user_ctx.principal.scopes == frozenset()
    assert user_ctx.principal.is_owner is False


@pytest.mark.asyncio
async def test_channel_compact_command_uses_short_context_budget_wording() -> None:
    msg = IncomingMessage(sender_id="u1", channel_id="c1", content="/compact")
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )

    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            return make_ok_res(
                req_id,
                {
                    "key": "agent:main:feishu:u1",
                    "compacted": False,
                    "status": "skipped",
                },
            )

    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=envelope,
        message_content="/compact",
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
    )

    assert reply is not None
    assert reply.content == "Already within context budget; no compact was applied."
    assert reply.metadata["command"] == "compact"


@pytest.mark.asyncio
async def test_channel_compact_command_reports_failure_shortly() -> None:
    msg = IncomingMessage(sender_id="u1", channel_id="c1", content="/compact")
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )

    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            return make_error_res(req_id, "INTERNAL_ERROR", "provider down")

    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=envelope,
        message_content="/compact",
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
    )

    assert reply is not None
    assert reply.content == "Compact failed: provider down"
    assert reply.metadata["command"] == "compact"


@pytest.mark.asyncio
async def test_channel_meta_command_renders_skill_names() -> None:
    msg = IncomingMessage(sender_id="u1", channel_id="c1", content="/meta")
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )

    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            assert method == "meta.list"
            return make_ok_res(
                req_id,
                {
                    "skills": [
                        {"name": "researcher", "description": "Deep research"},
                        {"name": "planner", "description": "Plan work"},
                    ]
                },
            )

    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=envelope,
        message_content="/meta",
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
    )

    assert reply is not None
    assert reply.content.startswith("Available meta-skills:")
    assert "- researcher — Deep research" in reply.content
    assert "- planner — Plan work" in reply.content
    assert reply.metadata["command"] == "meta"
    assert reply.metadata["method"] == "meta.list"


@pytest.mark.asyncio
async def test_channel_meta_command_handles_empty_or_disabled() -> None:
    msg = IncomingMessage(sender_id="u1", channel_id="c1", content="/meta")
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )

    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            return make_ok_res(req_id, {"skills": [], "disabled": True})

    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=envelope,
        message_content="/meta",
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
    )

    assert reply is not None
    assert reply.content == "No meta-skills available."
    assert reply.metadata["command"] == "meta"


def test_channel_admin_matcher_is_shared_across_rpc_and_dispatch():
    # One matcher decides channel-admin standing everywhere: the command
    # registry (operator Principal for channel RPC) and gateway dispatch
    # (who may resolve sandbox approvals from chat) must never diverge on
    # str vs list vs mixed-type configured entries.
    from opensquilla.channels._util import sender_is_channel_admin
    from opensquilla.gateway.channel_dispatch import _sender_is_channel_admin

    cases = [
        ("u-1", "u-1", True),
        ("u-1", "u-2", False),
        ("u-1", ["u-1", "u-2"], True),
        ("u-1", ["u-2"], False),
        ("42", [42, "u-2"], True),
        ("u-1", ("u-1",), True),
        ("u-1", {"u-1"}, True),
        ("u-1", None, False),
        ("u-1", 42, False),
        ("", ["u-1"], False),
    ]
    for sender, configured, expected in cases:
        assert sender_is_channel_admin(sender, configured=configured) is expected, (
            sender,
            configured,
        )
        config = SimpleNamespace(channel_admin_senders={"work": configured})
        assert _sender_is_channel_admin(config, "work", sender) is expected, (
            sender,
            configured,
        )
