"""Authenticated channel admission and pre-dispatch ordering contracts."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opensquilla.channels._util import ChannelAccessPolicy
from opensquilla.channels.admission import decide_channel_admission
from opensquilla.channels.types import (
    AuthenticatedPrincipal,
    IncomingMessage,
    IngressProvenance,
    IngressVerification,
)
from opensquilla.gateway.channel_dispatch import run_channel_dispatch


def test_ingress_provenance_defaults_legacy_and_is_immutable() -> None:
    message = IncomingMessage(sender_id="u1", channel_id="c1", content="hello")

    assert message.provenance == IngressProvenance()
    assert message.provenance.authenticated is False
    with pytest.raises(FrozenInstanceError):
        message.provenance.provider = "discord"  # type: ignore[misc]


def test_ingress_provenance_parses_authenticated_principal() -> None:
    message = IncomingMessage(
        sender_id="u1",
        channel_id="c1",
        content="hello",
        provenance={
            "provider": "discord",
            "account_id": "primary",
            "transport": "gateway",
            "verification": "sdk_session",
            "event_id": "evt-1",
            "principal": {"subject_id": "u1", "tenant_id": "guild-1"},
        },
    )

    assert message.provenance.authenticated is True
    assert message.provenance.verification is IngressVerification.SDK_SESSION
    assert message.provenance.principal == AuthenticatedPrincipal(
        subject_id="u1",
        tenant_id="guild-1",
    )


def test_legacy_direct_message_is_admitted_without_mention_hook() -> None:
    message = IncomingMessage(sender_id="u1", channel_id="dm1", content="hello")

    decision = decide_channel_admission(
        SimpleNamespace(),
        message,
        "agent:main:legacy:direct:u1",
    )

    assert decision.admit is True
    assert decision.reason == "dm_admitted"
    assert decision.is_group is False


def test_authenticated_sender_mismatch_is_denied() -> None:
    message = IncomingMessage(
        sender_id="payload-user",
        channel_id="dm1",
        content="hello",
        provenance=IngressProvenance(
            provider="discord",
            transport="gateway",
            verification=IngressVerification.SDK_SESSION,
            principal=AuthenticatedPrincipal(subject_id="authenticated-user"),
        ),
    )

    decision = decide_channel_admission(
        SimpleNamespace(),
        message,
        "agent:main:discord:direct:payload-user",
    )

    assert decision.admit is False
    assert decision.reason == "principal_mismatch"
    assert decision.sender_id == "authenticated-user"


@pytest.mark.parametrize(
    "metadata",
    [
        {
            "is_group": True,
            "conversation_kind": "group",
            "interaction_type": "slash_command",
        },
        {
            "is_group": True,
            "conversation_kind": "interaction",
            "approval_action": {
                "opensquilla_action": "approval_resolve",
                "code": "AB12",
            },
        },
    ],
)
def test_provider_interactions_are_addressed_but_still_apply_allowlist(
    metadata: dict[str, object],
) -> None:
    policy = ChannelAccessPolicy(
        group_allowed=True,
        mention_required_in_group=True,
        allowlist=frozenset({"operator"}),
    )
    channel = SimpleNamespace(
        policy=policy,
        is_group_mentioned=MagicMock(return_value=False),
    )
    admitted = IncomingMessage(
        sender_id="operator",
        channel_id="group-1",
        content="/help",
        metadata=metadata,
    )
    denied = admitted.model_copy(update={"sender_id": "outsider"})

    admitted_decision = decide_channel_admission(
        channel,
        admitted,
        "agent:main:provider:group:group-1",
    )
    denied_decision = decide_channel_admission(
        channel,
        denied,
        "agent:main:provider:group:group-1",
    )

    assert admitted_decision.admit is True
    assert admitted_decision.mentioned is True
    assert denied_decision.admit is False
    assert denied_decision.reason == "not_in_allowlist"
    channel.is_group_mentioned.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("deny_mode", ["closed_group", "unmentioned"])
async def test_denied_group_event_has_zero_pre_admission_side_effects(
    deny_mode: str,
) -> None:
    message = IncomingMessage(
        sender_id="u1",
        channel_id="group-1",
        content="/approve AB12",
        metadata={"is_group": True, "conversation_kind": "group"},
    )
    channel = SimpleNamespace(
        policy=ChannelAccessPolicy(
            group_allowed=deny_mode != "closed_group",
            mention_required_in_group=True,
        ),
        is_group_mentioned=MagicMock(return_value=False),
        supports_slash_commands=True,
        send=AsyncMock(),
    )
    receive_count = 0

    async def receive() -> IncomingMessage:
        nonlocal receive_count
        receive_count += 1
        if receive_count == 1:
            return message
        raise asyncio.CancelledError

    channel.receive = receive
    session_manager = SimpleNamespace(
        get_or_create=AsyncMock(),
        update=AsyncMock(),
        append_message=AsyncMock(),
    )
    debounce = SimpleNamespace(schedule=AsyncMock())

    with (
        patch(
            "opensquilla.gateway.routing.build_channel_route_envelope"
        ) as build_route,
        patch(
            "opensquilla.gateway.channel_dispatch._maybe_resolve_channel_approval"
        ) as approval_intercept,
        patch(
            "opensquilla.gateway.channel_dispatch._dispatch_channel_slash_command",
            new=AsyncMock(),
        ) as slash_intercept,
        patch(
            "opensquilla.gateway.channel_dispatch._record_delivery_context",
            new=AsyncMock(),
        ) as record_context,
        patch(
            "opensquilla.gateway.channel_dispatch._ingest_channel_message_attachments",
            new=AsyncMock(),
        ) as ingest_attachments,
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_channel_dispatch(
                channel=channel,
                turn_runner=SimpleNamespace(),
                session_manager=session_manager,
                session_key_builder=lambda _msg: "agent:main:discord:group:group-1",
                session_prefix="discord",
                task_runtime=SimpleNamespace(),
                rpc_dispatcher=SimpleNamespace(),
                channel_rpc_context_factory=lambda _envelope: SimpleNamespace(),
                debounce_coordinator=debounce,
                debounce_window_s=0.5,
            )

    build_route.assert_not_called()
    approval_intercept.assert_not_called()
    slash_intercept.assert_not_awaited()
    debounce.schedule.assert_not_awaited()
    record_context.assert_not_awaited()
    ingest_attachments.assert_not_awaited()
    session_manager.get_or_create.assert_not_awaited()
    session_manager.update.assert_not_awaited()
    session_manager.append_message.assert_not_awaited()
    channel.send.assert_not_awaited()
