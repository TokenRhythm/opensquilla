"""Production flow ownership survives churn, retries, and partial bad batches."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketState

from opensquilla.contracts.generated.v4.transport_flow_dirty import TransportFlowDirtyPayload
from opensquilla.contracts.generated.v4.transport_session_flow_v2 import SessionFlowUpdateV2Result
from opensquilla.gateway import websocket
from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.gateway.transport_flow import (
    FLOW_V2_MAX_LANE_EPOCHS,
    SESSION_FLOW_V2_CAPABILITY,
    FlowWindow,
    TransportBudget,
    get_transport_budget,
)


class Socket:
    client_state = WebSocketState.CONNECTED

    def __init__(self):
        self.frames = []

    async def send_text(self, text):
        self.frames.append(json.loads(text))


@pytest.fixture
def connection(monkeypatch):
    before = get_transport_budget().used
    registry = websocket.ConnectionRegistry()
    monkeypatch.setattr(websocket, "_registry", registry)
    conn = websocket.WsConnection(
        "flow-lifecycle", Socket(), client_caps=frozenset({SESSION_FLOW_V2_CAPABILITY}),
    )
    conn._subscriptions = websocket.SubscriptionManager()
    registry.register(conn)
    conn._enable_flow()
    yield conn
    conn._cleanup_transport()
    registry.unregister(conn.conn_id)
    assert get_transport_budget().used == before


def subscribe(conn, key):
    conn._subscriptions.subscribe_messages(conn.conn_id, key)
    assert conn._register_flow_subscription_epoch(key)
    return conn._subscriptions.get_message_subscription_epoch(conn.conn_id, key)


def retire(conn, key):
    receipt = conn._subscriptions.unsubscribe_messages(conn.conn_id, key)
    assert receipt is not None
    return {name: receipt[name] for name in (
        "subscription_epoch", "retire_token", "final_published_id",
    )}


def update(conn, **params):
    return conn.apply_session_flow_update_v2({"connection_epoch": conn._flow.epoch, **params})


@pytest.mark.asyncio
@pytest.mark.parametrize("churn", [0, 20])
async def test_writer_dirty_notice_matches_closed_wire_contract(connection, churn):
    conn = connection
    for index in range(churn):
        key = f"session-{index}"
        subscribe(conn, key)
        update(conn, discarded_lanes=[retire(conn, key)])
    conn._start_writer(maxsize=512, enabled=True)
    try:
        conn._mark_flow_dirty({"session_key": "current", "stream_seq": 1})
        await asyncio.sleep(0)
        notice = conn.ws.frames[0]["payload"]
        assert set(notice) == {"delivery_epoch", "dirty_keys", "global_dirty"}
        parsed = TransportFlowDirtyPayload.model_validate(notice)
        assert parsed.model_dump()["dirty_keys"] == ["current"]
        assert parsed.global_dirty is False
    finally:
        await conn._stop_writer()


@pytest.mark.asyncio
async def test_rejected_subscription_rolls_back_without_allocating_seventeenth_epoch(connection):
    from opensquilla.gateway.rpc_sessions import _handle_sessions_messages_subscribe

    conn = connection
    key = "agent:main:webchat:churn"
    receipts = []
    for _ in range(FLOW_V2_MAX_LANE_EPOCHS):
        subscribe(conn, key)
        receipts.append(retire(conn, key))
    ctx = SimpleNamespace(conn_id=conn.conn_id, subscription_manager=conn._subscriptions)
    for _ in range(3):
        with pytest.raises(RpcHandlerError, match="Too many unretired"):
            await _handle_sessions_messages_subscribe({"key": key, "fast_ack": True}, ctx)
        assert len(conn._flow._lane_epoch_states) == FLOW_V2_MAX_LANE_EPOCHS
        assert conn._subscriptions.get_message_subscription_epoch(conn.conn_id, key) is None
    result = update(conn, discarded_lanes=[receipts[0]])
    SessionFlowUpdateV2Result.model_validate(result)
    assert "lane_count" not in result
    assert len(conn._flow._lane_epoch_states) == FLOW_V2_MAX_LANE_EPOCHS - 1
    subscribe(conn, key)
    assert len(conn._flow._lane_epoch_states) == FLOW_V2_MAX_LANE_EPOCHS


def test_settled_key_history_is_pruned_and_precise_retire_retry_cache_is_bounded(connection):
    conn = connection
    receipts = []
    for index in range(1000):
        key = f"session-{index}"
        subscribe(conn, key)
        receipt = retire(conn, key)
        receipts.append(receipt)
        result = update(conn, discarded_lanes=[receipt])
        assert set(result) == {"connection_epoch", "consumed", "staged_recovery", "discarded_lanes"}
    for attribute in (
        "_lane_seen", "_lane_states", "_lane_epochs", "_lane_retire_tokens", "_lane_final_ids",
        "_lane_next_ids", "lane_ack_ids", "lane_delivery_ack_ids", "_lane_epoch_states",
        "_lane_epoch_retire_tokens", "_lane_epoch_final_ids", "_lane_epoch_delivery_ack_ids",
    ):
        assert not getattr(conn._flow, attribute), attribute
    assert not conn._flow_lane_epochs
    assert len(conn._confirmed_flow_retires) == FLOW_V2_MAX_LANE_EPOCHS
    assert update(conn, discarded_lanes=[receipts[-1]]) == result
    with pytest.raises(ValueError, match="not current"):
        update(conn, discarded_lanes=[{**receipts[-1], "retire_token": "wrong-token"}])
    with pytest.raises(ValueError, match="not current"):
        update(conn, discarded_lanes=[receipts[0]])


@pytest.mark.parametrize("sending", [False, True])
def test_pruning_waits_for_queued_or_sending_delivery_to_release(sending):
    budget = TransportBudget()
    flow = FlowWindow(budget.reserve, budget.release, lane_mode=True)
    delivery = flow.admit(100, lane="session", lane_epoch="old")
    if sending:
        flow.mark_sending(delivery)
    token = flow.retire_lane(flow.epoch, "session", lane_epoch="old")
    flow.confirm_lane_retire(flow.epoch, "session", token, delivery, lane_epoch="old")
    flow.prune_settled_lanes()
    assert flow._lane_states == {"session": "CLOSED"}
    assert budget.used == 100
    if sending:
        flow.mark_send_finished(delivery)
    else:
        assert flow.discard_retired_queued(delivery)
    flow.prune_settled_lanes()
    assert not flow._lane_states
    assert not flow._lane_seen
    assert budget.used == 0


@pytest.mark.asyncio
async def test_late_publication_after_pruning_cannot_readmit_retired_session(connection):
    conn = connection
    key = "session"
    old_epoch = subscribe(conn, key)
    receipt = retire(conn, key)
    update(conn, discarded_lanes=[receipt])
    assert not conn._flow._lane_states
    conn._start_writer(maxsize=512, enabled=True)
    try:
        await conn.send_event("session.event.text_delta", {"session_key": key, "text": "late"})
        await asyncio.sleep(0)
        assert not conn.ws.frames
        assert not conn._flow.deliveries
        assert not conn._flow.dirty
        new_epoch = subscribe(conn, key)
        assert new_epoch != old_epoch
        await conn.send_event("session.event.text_delta", {"session_key": key, "text": "current"})
        await asyncio.sleep(0)
        assert [frame["payload"]["text"] for frame in conn.ws.frames] == ["current"]
        delivery = next(iter(conn._flow.deliveries))
        reserved = conn._transport_bytes
        update(conn, discarded_lanes=[receipt])
        with pytest.raises(ValueError, match="not current"):
            update(conn, consumed=[{
                "subscription_epoch": old_epoch, "through_delivery_id": delivery,
            }])
        assert conn._transport_bytes == reserved
        assert conn._flow._lane_epoch_states[(key, new_epoch)] == "ACTIVE"
    finally:
        await conn._stop_writer()


@pytest.mark.parametrize("bad_part", ["consumed", "discarded"])
def test_invalid_batch_preserves_unrelated_consumption_and_recovery_credit(connection, bad_part):
    conn = connection
    epoch = subscribe(conn, "healthy")
    ordinary = conn._flow.admit(100, lane="healthy", lane_epoch=epoch)
    recovery = conn._flow.admit(200, recovery=True, key="snapshot")
    conn._flow.mark_sent(ordinary)
    conn._flow.mark_sent(recovery)
    consumed = [{"subscription_epoch": epoch, "through_delivery_id": ordinary}]
    discarded = []
    if bad_part == "consumed":
        consumed.append({"subscription_epoch": "unknown", "through_delivery_id": ordinary})
    else:
        discarded.append({
            "subscription_epoch": "unknown", "retire_token": "unknown", "final_published_id": 0,
        })
    before = conn._transport_bytes
    with pytest.raises(ValueError, match="not current"):
        update(conn, consumed=consumed, staged_recovery=[{
            "delivery_epoch": conn._flow.epoch, "delivery_id": recovery,
        }], discarded_lanes=discarded)
    assert set(conn._flow.deliveries) == {ordinary, recovery}
    assert conn._transport_bytes == before
    assert not conn._flow._lane_epoch_delivery_ack_ids[("healthy", epoch)]


@pytest.mark.asyncio
async def test_subscription_publication_fence_preserves_unkeyed_legacy_frames(connection):
    conn = connection
    conn._start_writer(maxsize=512, enabled=True)
    try:
        await conn.send_event("session.event.text_delta", {"text": "legacy"})
        await asyncio.sleep(0)
        assert len(conn.ws.frames) == 1
        assert conn.ws.frames[0]["payload"] == {"text": "legacy"}
        assert not conn._flow._lane_states
    finally:
        await conn._stop_writer()


def test_duplicate_retire_in_one_batch_applies_once(connection):
    conn = connection
    subscribe(conn, "session")
    receipt = retire(conn, "session")
    result = update(conn, discarded_lanes=[receipt, receipt])
    assert result["discarded_lanes"] == [receipt, receipt]
    assert not conn._flow._lane_epoch_states
    assert len(conn._confirmed_flow_retires) == 1


def test_old_retire_and_retry_do_not_prune_or_release_replacement(connection):
    conn = connection
    old_epoch = subscribe(conn, "session")
    old_delivery = conn._flow.admit(100, lane="session", lane_epoch=old_epoch)
    conn._flow.mark_sent(old_delivery)
    receipt = retire(conn, "session")
    new_epoch = subscribe(conn, "session")
    new_delivery = conn._flow.admit(200, lane="session", lane_epoch=new_epoch)
    conn._flow.mark_sent(new_delivery)
    update(conn, discarded_lanes=[receipt])
    update(conn, discarded_lanes=[receipt])
    assert set(conn._flow.deliveries) == {new_delivery}
    assert conn._transport_bytes == 200
    assert conn._flow._lane_epochs == {"session": new_epoch}
    assert conn._flow._lane_states == {"session": "ACTIVE"}
    with pytest.raises(ValueError, match="not current"):
        update(conn, consumed=[{
            "subscription_epoch": old_epoch, "through_delivery_id": old_delivery,
        }])
    update(conn, consumed=[{
        "subscription_epoch": new_epoch, "through_delivery_id": new_delivery,
    }])
    assert conn._transport_bytes == 0
