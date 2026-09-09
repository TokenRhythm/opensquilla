"""Exercise the generated recovery contract through the production dispatcher."""

import base64
import json
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway import session_services
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.guest_rpc_policy import guest_owned_session_key
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.session_streams import get_session_streams
from opensquilla.gateway.transport_flow import get_transport_budget
from opensquilla.gateway.websocket import WsConnection, get_registry


@pytest.fixture
def connection():
    conn = WsConnection(conn_id="snapshot-transfer-rpc", ws=AsyncMock())
    get_registry().register(conn)
    initial_budget = get_transport_budget().used
    yield conn
    conn._cleanup_transport()
    get_registry().unregister(conn.conn_id)
    get_session_streams().evict("agent:main:snapshot-transfer")
    assert get_transport_budget().used == initial_budget


async def request(conn, params, *, method="sessions.messages.snapshot.read"):
    return await get_dispatcher().dispatch(
        "snapshot-read", method, params, RpcContext(conn_id=conn.conn_id, principal=conn.principal)
    )


async def test_segmented_rpc_roundtrip_matches_existing_snapshot(connection):
    key = "agent:main:snapshot-transfer"
    streams = get_session_streams()
    streams.record(key, "session.event.text_delta", {"task_id": "task", "text": "中" * 100_000})
    old = await request(connection, {"key": key}, method="sessions.messages.snapshot")
    assert old.ok
    first = await request(connection, {"key": key, "sync_revision": "one"})
    assert first.ok, first.error
    assert first.payload["segment_count"] > 1
    parts = [base64.b64decode(first.payload["data"])]
    for index in range(1, first.payload["segment_count"]):
        part = await request(
            connection,
            {
                "key": key,
                "sync_revision": "one",
                "snapshot_id": first.payload["snapshot_id"],
                "segment_index": index,
            },
        )
        assert part.ok, part.error
        parts.append(base64.b64decode(part.payload["data"]))
    assert json.loads(b"".join(parts)) == old.payload
    assert "delivery" not in first.payload  # No unnegotiated ACK requirement.


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"key": "x"},
        {
            "key": "x",
            "sync_revision": "revision",
            "segment_index": True,
        },
        {"key": "x", "sync_revision": "revision", "extra": "invalid"},
    ],
)
async def test_invalid_request_remains_local(connection, params):
    response = await request(connection, params)
    assert not response.ok and response.error.code == "INVALID_REQUEST"
    assert not connection._closing


async def test_reset_during_encoding_rejects_old_identity(connection, monkeypatch):
    identities = AsyncMock(side_effect=[("session", 1), ("session", 2)])
    monkeypatch.setattr(session_services, "read_session_identity", identities)
    response = await request(
        connection,
        {
            "key": "agent:main:snapshot-transfer",
            "sync_revision": "one",
        },
    )
    assert not response.ok and response.error.code == "SNAPSHOT_STALE"
    assert connection._transport_bytes == 0


async def test_snapshot_id_cannot_be_read_by_different_connection(connection):
    first = await request(
        connection, {"key": "agent:main:snapshot-transfer", "sync_revision": "one"}
    )
    assert first.ok
    other = WsConnection(conn_id="snapshot-other", ws=AsyncMock())
    get_registry().register(other)
    try:
        response = await request(
            other,
            {
                "key": "agent:main:snapshot-transfer",
                "sync_revision": "one",
                "snapshot_id": first.payload["snapshot_id"],
                "segment_index": 0,
            },
        )
        assert not response.ok and response.error.code == "SNAPSHOT_EXPIRED"
    finally:
        other._cleanup_transport()
        get_registry().unregister(other.conn_id)


async def test_guest_snapshot_and_control_do_not_grant_session_access(connection):
    connection.principal = Principal(
        role="operator",
        scopes=frozenset({"operator.read"}),
        authenticated=False,
        is_owner=False,
        guest_owner_id="a" * 64,
    )
    forbidden = await request(connection, {"key": "agent:main:private", "sync_revision": "one"})
    assert not forbidden.ok and forbidden.error.code == "UNAUTHORIZED"
    key = guest_owned_session_key("a" * 64, "allowed")
    allowed = await request(connection, {"key": key, "sync_revision": "one"})
    assert allowed.ok, allowed.error
    control = await request(
        connection,
        {
            "delivery_epoch": "one",
            "ack_delivery_id": 0,
        },
        method="transport.flow.update",
    )
    assert not control.ok and control.error.code == "FLOW_DISABLED"


@pytest.mark.parametrize("invalid", ["old_epoch", "future_ack", "future_staged"])
async def test_invalid_flow_update_is_local_and_cannot_release_valid_credit(connection, invalid):
    connection._enable_flow()
    delivery_id = connection._flow.admit(100)
    connection._flow.mark_sent(delivery_id)
    params = {"delivery_epoch": connection._flow.epoch, "ack_delivery_id": delivery_id}
    if invalid == "old_epoch":
        params["delivery_epoch"] = "not-current"
    elif invalid == "future_ack":
        params["ack_delivery_id"] = delivery_id + 1
    else:
        params["staged_delivery_ids"] = [delivery_id + 1]
    response = await request(connection, params, method="transport.flow.update")
    assert not response.ok
    assert response.error.code == "INVALID_REQUEST"
    assert response.error.accepted is False
    assert connection._flow.ack_id == 0
    assert connection._transport_bytes == 100
    assert not connection._closing
    followup = await request(
        connection,
        {
            "delivery_epoch": connection._flow.epoch,
            "ack_delivery_id": delivery_id,
        },
        method="transport.flow.update",
    )
    assert followup.ok
    assert connection._transport_bytes == 0


async def test_reset_after_snapshot_send_rejects_old_install_even_when_cursor_is_zero(
    connection,
    monkeypatch,
):
    from opensquilla.gateway.websocket import SubscriptionManager

    key = "agent:main:snapshot-transfer"
    connection._subscriptions = SubscriptionManager()
    connection._subscriptions.subscribe_messages(connection.conn_id, key)
    connection._enable_flow()
    monkeypatch.setattr(
        session_services, "read_session_identity", AsyncMock(return_value=("old", 1))
    )
    first = await request(connection, {"key": key, "sync_revision": "old-owner"})
    assert first.ok
    assert first.payload["current_stream_seq"] == 0
    connection._flow.mark_dirty(key)
    monkeypatch.setattr(
        session_services, "read_session_identity", AsyncMock(return_value=("new", 2))
    )
    response = await request(
        connection,
        {
            "delivery_epoch": connection._flow.epoch,
            "ack_delivery_id": 0,
            "resume": [
                {
                    "key": key,
                    "snapshot_id": first.payload["snapshot_id"],
                    "sync_revision": "old-owner",
                    "stream_generation": first.payload["stream_generation"],
                    "stream_seq": 0,
                }
            ],
        },
        method="transport.flow.update",
    )
    assert not response.ok
    assert response.error.accepted is False
    assert key in connection._flow.dirty
    assert not connection._closing


async def test_installed_retry_keeps_its_own_identity_after_another_key_snapshot(
    connection, monkeypatch
):
    from opensquilla.gateway.websocket import SubscriptionManager

    connection._subscriptions = SubscriptionManager()
    connection._enable_flow()
    keys = ["agent:main:snapshot-transfer", "agent:main:second-snapshot"]
    owners = {key: (f"owner-{index}", 1) for index, key in enumerate(keys)}

    async def current_identity(ctx, key):
        return owners[key]

    monkeypatch.setattr(session_services, "read_session_identity", current_identity)
    receipts = []
    for key in keys:
        connection._subscriptions.subscribe_messages(connection.conn_id, key)
        first = await request(connection, {"key": key, "sync_revision": "one"})
        assert first.ok
        delivery = first.payload["delivery"]["delivery_id"]
        connection._flow.mark_sent(delivery)
        receipt = {
            "key": key,
            "snapshot_id": first.payload["snapshot_id"],
            "sync_revision": "one",
            "stream_generation": first.payload["stream_generation"],
            "stream_seq": 0,
        }
        receipts.append(receipt)
        installed = await request(
            connection,
            {
                "delivery_epoch": connection._flow.epoch,
                "ack_delivery_id": delivery,
                "resume": [receipt],
            },
            method="transport.flow.update",
        )
        assert installed.ok, installed.error
    # The live transfer last belonged to B, but retrying A still proves A's
    # original owner. Do not accidentally compare A with B's cached identity.
    params = {
        "delivery_epoch": connection._flow.epoch,
        "ack_delivery_id": connection._flow.ack_id,
        "resume": [receipts[0]],
    }
    repeated = await request(connection, params, method="transport.flow.update")
    assert repeated.ok, repeated.error
    owners[keys[0]] = ("recreated-owner", 2)
    stale = await request(connection, params, method="transport.flow.update")
    assert not stale.ok and stale.error.code == "INVALID_REQUEST"
    assert stale.error.accepted is False
    assert keys[0] in connection._flow.dirty
    assert not connection._closing
