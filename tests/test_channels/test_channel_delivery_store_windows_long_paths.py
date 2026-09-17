from __future__ import annotations

import os
from pathlib import Path

import pytest

from opensquilla.channels.contract import ChannelCapabilities, ChannelSendResult
from opensquilla.channels.delivery_store import ChannelDeliveryStore
from opensquilla.channels.types import (
    AuthenticatedPrincipal,
    IncomingMessage,
    IngressProvenance,
    IngressVerification,
    OutgoingMessage,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows long-path regression")


def _long_db_path(tmp_path: Path) -> Path:
    segment = "channel-delivery-" + ("x" * 32)
    path = tmp_path.joinpath(segment, segment, segment, segment, "channel_delivery.sqlite")
    assert len(os.fspath(path)) > 260
    return path


def _incoming_message() -> IncomingMessage:
    return IncomingMessage(
        sender_id="user-1",
        channel_id="chat-1",
        content="hello",
        metadata={"is_group": False, "native_message_id": "event-long-path"},
        provenance=IngressProvenance(
            provider="slack",
            account_id="team-1",
            transport="webhook",
            verification=IngressVerification.WEBHOOK_SIGNATURE,
            event_id="event-long-path",
            principal=AuthenticatedPrincipal(subject_id="user-1"),
        ),
    )


def test_long_path_journal_commits_ingress_and_outbox_roundtrip(tmp_path: Path) -> None:
    db_path = _long_db_path(tmp_path)
    store = ChannelDeliveryStore(db_path)
    assert store.path == db_path
    assert not os.fspath(store.path).startswith("\\\\?\\")

    message = _incoming_message()
    assert store.accept_inbound("slack-main", message) is True
    claim = store.claim_inbound("slack-main", message)
    assert claim is not None
    store.complete_inbound(claim, "turn_dispatched")

    outgoing = OutgoingMessage(content="reply", reply_to="chat-1")
    send_id = store.begin_send("slack-main", outgoing)
    store.complete_send(
        send_id,
        ChannelSendResult.sent(
            capability=ChannelCapabilities.GROUP_CHAT,
            target_id="chat-1",
            provider_message_id="provider-message-1",
        ),
    )
    store.close()

    restarted = ChannelDeliveryStore(db_path)
    try:
        diagnostics = restarted.diagnostics("slack-main")
        assert diagnostics["ingress"]["completed"]["count"] == 1
        assert diagnostics["outbox"]["sent"]["count"] == 1
        assert restarted.recover_inbound("slack-main") == []
    finally:
        restarted.close()
