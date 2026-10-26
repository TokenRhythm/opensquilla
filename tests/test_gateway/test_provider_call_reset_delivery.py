"""Rejected call resets must never diverge between live delivery and replay."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.event_bridge import EventBridge
from opensquilla.gateway.session_streams import SessionStreamRegistry
from opensquilla.gateway.websocket import SubscriptionManager

_KEY = "agent:main:reset-delivery"
_RESET = "session.event.answer_generation_reset"
_IDENTITY = {"task_id": "task-reset", "assistant_message_id": "assistant-reset", "epoch": 0}


@pytest.fixture
def delivery(monkeypatch):
    registry = SessionStreamRegistry()
    subscriptions = SubscriptionManager()
    subscriptions.subscribe_messages("viewer", _KEY)
    connection = SimpleNamespace(send_event=AsyncMock())
    connections = SimpleNamespace(get=lambda key: connection if key == "viewer" else None)
    for module in ("event_bridge", "session_event_publisher", "rpc_sessions"):
        monkeypatch.setattr(
            f"opensquilla.gateway.{module}.get_session_streams", lambda: registry,
        )
    monkeypatch.setattr("opensquilla.gateway.websocket.get_registry", lambda: connections)
    for call, text in (("1.0", "committed prefix"), ("2.0", "failed draft")):
        registry.record(_KEY, "session.event.text_delta", {
            **_IDENTITY, "model_call_id": call, "text": text, "generation_epoch": 1,
        })
    return SimpleNamespace(
        registry=registry, connection=connection,
        bridge=EventBridge(subscriptions, connections),
        context=SimpleNamespace(subscription_manager=subscriptions),
    )


async def _emit(delivery, path: str, payload: dict) -> None:
    if path == "pre_recorded":
        recorded = delivery.registry.record(_KEY, _RESET, payload)
        await delivery.bridge.emit(_KEY, _RESET, recorded, replay_recorded=True)
    elif path == "bridge":
        await delivery.bridge.emit(_KEY, _RESET, payload)
    elif path == "publisher":
        from opensquilla.gateway.session_event_publisher import emit_session_event

        await emit_session_event(delivery.context, _KEY, _RESET, payload)
    else:
        from opensquilla.gateway.rpc_sessions import _emit_to_subscribers

        await _emit_to_subscribers(delivery.context, _KEY, _RESET, payload)


def _reset_payload(call: str = "2.0", epoch: int = 1) -> dict:
    return {
        **_IDENTITY, "reset_scope": "provider_call", "rollback_model_call_id": call,
        "old_generation_epoch": epoch, "new_generation_epoch": epoch + 1,
        "authoritative_text_snapshot": "committed prefix", "terminal": False,
    }


@pytest.mark.parametrize("path", ["bridge", "pre_recorded", "publisher", "rpc"])
@pytest.mark.parametrize("call, epoch", [("2.0", 0), ("unknown", 1), ("1.0", 1)])
async def test_rejected_reset_is_not_published_or_replayed(delivery, path, call, epoch):
    before = delivery.registry.live_snapshot(_KEY).events

    await _emit(delivery, path, _reset_payload(call, epoch))

    delivery.connection.send_event.assert_not_awaited()
    assert delivery.registry.live_snapshot(_KEY).events == before
    assert delivery.registry.replay(_KEY, 0).events == before


@pytest.mark.parametrize("path", ["bridge", "pre_recorded", "publisher", "rpc"])
async def test_accepted_reset_has_identical_live_and_replayed_payload(delivery, path):
    await _emit(delivery, path, _reset_payload())

    delivery.connection.send_event.assert_awaited_once()
    event_name, payload = delivery.connection.send_event.call_args.args
    assert event_name == _RESET
    assert payload["stream_seq"] == 3
    delivery.registry.record(_KEY, "session.event.text_delta", {
        **_IDENTITY, "model_call_id": "2.1", "text": "recovered suffix", "generation_epoch": 2,
    })
    replay = delivery.registry.replay(_KEY, 0)
    snapshot = delivery.registry.live_snapshot(_KEY)
    assert replay.replay_complete
    assert snapshot.events == replay.events
    assert [event.payload for event in replay.events if event.event_name == _RESET] == [payload]
    assert [event.payload["text"] for event in replay.events if "text" in event.payload] == [
        "committed prefix", "recovered suffix",
    ]


def test_record_returns_no_publishable_payload_for_rejected_reset(delivery):
    assert delivery.registry.record(_KEY, _RESET, _reset_payload(epoch=0)) is None
