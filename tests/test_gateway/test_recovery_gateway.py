"""New recovery methods preserve lease, dirty and delivery ownership."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.websockets import WebSocketState

from opensquilla.gateway import session_services
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.recovery_scheduler import get_recovery_scheduler
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.session_streams import get_session_streams
from opensquilla.gateway.transport_flow import SESSION_FLOW_V2_CAPABILITY, get_transport_budget
from opensquilla.gateway.websocket import (
    SubscriptionManager,
    WsConnection,
    _admit_session_mutation,
    _message_loop,
    get_registry,
)


class Socket:
    client_state = WebSocketState.CONNECTED
    application_state = WebSocketState.CONNECTED

    def __init__(self):
        self.frames = []
        self.closed = []
        self.incoming = asyncio.Queue()

    async def send_text(self, text):
        self.frames.append(json.loads(text))

    async def close(self, **kwargs):
        self.closed.append(kwargs)

    async def receive_text(self):
        return await self.incoming.get()


@pytest.fixture
async def connection():
    before = get_transport_budget().used
    conn = WsConnection("synthetic-recovery", Socket())
    conn._recovery_enabled = True
    conn._subscriptions = SubscriptionManager()
    conn._enable_flow()
    conn._start_writer(maxsize=512, enabled=True)
    get_registry().register(conn)
    yield conn
    await conn._stop_writer()
    conn._cleanup_transport()
    get_registry().unregister(conn.conn_id)
    assert conn._transport_bytes == 0
    assert get_transport_budget().used == before


@pytest.fixture
async def v2_connection():
    before = get_transport_budget().used
    conn = WsConnection(
        "synthetic-recovery-v2", Socket(),
        client_caps=frozenset({SESSION_FLOW_V2_CAPABILITY}),
    )
    conn._recovery_enabled = True
    conn._subscriptions = SubscriptionManager()
    conn._enable_flow()
    conn._start_writer(maxsize=512, enabled=True)
    get_registry().register(conn)
    yield conn
    await conn._stop_writer()
    conn._cleanup_transport()
    get_registry().unregister(conn.conn_id)
    assert conn._transport_bytes == 0
    assert get_transport_budget().used == before


def subscribe(conn, key):
    intent, _ = conn._subscriptions.admit_message_subscription(conn.conn_id, key)
    assert conn._subscriptions.activate_message_subscription(conn.conn_id, key, intent.token)
    return intent.token


async def request(conn, method, params):
    return await get_dispatcher().dispatch(
        "request", method, params,
        RpcContext(conn_id=conn.conn_id, principal=conn.principal,
                   subscription_manager=conn._subscriptions),
    )


async def snapshot(conn, key, revision="one"):
    response = await request(conn, "sessions.messages.snapshot.read", {
        "key": key, "sync_revision": revision,
    })
    assert response.ok, response.error
    await conn.send_res(response)
    await asyncio.sleep(0)
    return response.payload


async def stage(conn, payload):
    response = await request(conn, "transport.flow.update", {
        "delivery_epoch": conn._flow.epoch, "ack_delivery_id": 0,
        "staged_delivery_ids": [payload["delivery"]["delivery_id"]],
    })
    assert response.ok, response.error


def receipt(payload):
    return {name: payload[name] for name in ("key", "snapshot_id", "sync_revision",
                                            "stream_generation")} | {
        "stream_seq": payload["current_stream_seq"],
    }


async def test_two_sessions_stage_independently_and_install_exact_proofs(connection):
    for key in ("agent:main:a", "agent:main:b"):
        subscribe(connection, key)
    first = await snapshot(connection, "agent:main:a")
    second = await snapshot(connection, "agent:main:b")
    assert len(connection._flow.deliveries) == 2
    await stage(connection, second)
    installed = await request(connection, "sessions.messages.resume", receipt(second))
    assert installed.ok, installed.error
    assert installed.payload["snapshot_id"] == second["snapshot_id"]
    assert installed.payload["replay_to_seq"] == second["current_stream_seq"]
    assert first["delivery"]["delivery_id"] in connection._flow.deliveries
    repeated = await request(connection, "sessions.messages.resume", receipt(second))
    assert repeated.ok and repeated.payload == installed.payload
    await stage(connection, first)


async def test_snapshot_recovery_converges_when_nine_events_arrive_before_install(connection):
    key = "agent:main:batch-nine"
    subscribe(connection, key)
    streams = get_session_streams()
    payload = await snapshot(connection, key)
    assert payload["current_stream_seq"] == 0
    for index in range(9):
        streams.record(key, "session.event.text_delta", {"text": str(index)})
    await stage(connection, payload)
    installed = await request(connection, "sessions.messages.resume", receipt(payload))

    assert installed.ok, installed.error
    assert installed.payload["replay_to_seq"] == 9
    await connection.send_res(installed)
    for _ in range(20):
        if len(connection.ws.frames) >= 10:
            break
        await asyncio.sleep(0.01)
    assert len(connection.ws.frames) >= 10


@pytest.mark.parametrize("event_count, expected_batches", [(8, 1), (9, 2), (17, 3), (33, 5)])
@pytest.mark.parametrize("first_delivery_id", [1, 1_000_000_000])
async def test_replay_logical_batches_pipeline_before_cumulative_ack(
    connection, event_count, expected_batches, first_delivery_id,
):
    """Logical batches share the physical window instead of waiting on each ACK."""
    connection._flow.next_id = first_delivery_id
    connection._flow.ack_id = first_delivery_id - 1
    key = f"agent:main:batch-ack-{event_count}"
    subscribe(connection, key)
    streams = get_session_streams()
    payload = await snapshot(connection, key)
    for index in range(event_count):
        streams.record(key, "session.event.text_delta", {"text": str(index)})
    await stage(connection, payload)
    installed = await request(connection, "sessions.messages.resume", receipt(payload))
    assert installed.ok, installed.error
    await connection.send_res(installed)
    await asyncio.sleep(0.05)

    pending = connection._pending_replays[key]
    assert len(pending.batches) == expected_batches
    assert len(pending.pending_delivery_ids) == event_count
    observed = 1
    while key in connection._pending_replays:
        pending = connection._pending_replays[key]
        ids = sorted(pending.pending_delivery_ids)
        assert ids, "a non-final batch must have admitted delivery receipts"
        # Writer admission is synchronous; wait only for physical send before
        # exercising the same cumulative ACK accepted by the WebUI client.
        await asyncio.sleep(0.05)
        end = max(ids)
        update = await request(connection, "transport.flow.update", {
            "delivery_epoch": connection._flow.epoch,
            "ack_delivery_id": end,
        })
        assert update.ok, update.error
        if key in connection._pending_replays:
            observed += 1
            assert observed <= expected_batches
    assert observed == 1
    assert key not in connection._flow.dirty
    assert connection.installed_snapshot_proof(receipt(payload)) is not None


async def test_v2_replay_batches_converge_from_lane_delivery_ack(v2_connection):
    """A v2 lane ACK must advance a 9-event replay to its final CAS."""
    key = "agent:main:v2-batch-nine"
    subscribe(v2_connection, key)
    streams = get_session_streams()
    payload = await snapshot(v2_connection, key)
    for index in range(9):
        streams.record(key, "session.event.text_delta", {"text": str(index)})
    await stage(v2_connection, payload)
    installed = await request(v2_connection, "sessions.messages.resume", receipt(payload))
    assert installed.ok, installed.error
    await v2_connection.send_res(installed)
    await asyncio.sleep(0.05)
    assert key in v2_connection._pending_replays
    while key in v2_connection._pending_replays:
        pending = v2_connection._pending_replays[key]
        ids = sorted(pending.pending_delivery_ids)
        assert ids
        await asyncio.sleep(0.05)
        first = v2_connection._flow.deliveries[ids[0]]
        assert first.lane_epoch is not None
        end = ids[-1]
        update = await request(v2_connection, "transport.sessionFlow.update.v2", {
            "connection_epoch": v2_connection._flow.epoch,
            "consumed": [{
                "subscription_epoch": first.lane_epoch,
                "through_delivery_id": end,
            }],
            "staged_recovery": [],
            "discarded_lanes": [],
        })
        assert update.ok, update.error
    assert key not in v2_connection._flow.dirty
    assert v2_connection.installed_snapshot_proof(receipt(payload)) is not None


@pytest.fixture(params=["connection", "v2_connection"])
def resumable_connection(request):
    return request.getfixturevalue(request.param)


async def test_pending_resume_retry_preserves_replay_owner_and_batch_progress(
    resumable_connection,
):
    conn = resumable_connection
    key = f"agent:main:resume-retry-{conn.conn_id}"
    subscribe(conn, key)
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    for index in range(17):
        get_session_streams().record(key, "session.event.text_delta", {"text": str(index)})
    first = await request(conn, "sessions.messages.resume", receipt(payload))
    assert first.ok, first.error
    pending = conn._pending_replays[key]
    batch_count = 0
    while key in conn._pending_replays:
        await asyncio.sleep(0.01)
        ids = set(pending.pending_delivery_ids)
        batch_index = pending.batch_index
        # Simulate an observer retry after a missing/late control reply, both
        # before the first ACK and after later batches have started.
        repeated = await request(conn, "sessions.messages.resume", receipt(payload))
        assert repeated.ok, repeated.error
        assert repeated.payload == first.payload
        assert conn._pending_replays[key] is pending
        assert pending.pending_delivery_ids == ids
        assert pending.batch_index == batch_index
        end = max(ids)
        if conn._session_flow_v2_enabled:
            lane_epoch = conn._flow.deliveries[end].lane_epoch
            update = await request(conn, "transport.sessionFlow.update.v2", {
                "connection_epoch": conn._flow.epoch,
                "consumed": [{"subscription_epoch": lane_epoch, "through_delivery_id": end}],
                "staged_recovery": [], "discarded_lanes": [],
            })
        else:
            update = await request(conn, "transport.flow.update", {
                "delivery_epoch": conn._flow.epoch, "ack_delivery_id": end,
            })
        assert update.ok, update.error
        batch_count += 1
        assert batch_count <= 3
    assert batch_count == 1
    assert conn.installed_snapshot_proof(receipt(payload)) == first.payload
    replayed = [frame for frame in conn.ws.frames if frame.get("meta", {}).get("replayed")]
    assert len(replayed) == 17


async def ack_replay(conn, ids):
    await asyncio.sleep(0.01)
    end = max(ids)
    if conn._session_flow_v2_enabled:
        lane_epoch = conn._flow.deliveries[end].lane_epoch
        update = await request(conn, "transport.sessionFlow.update.v2", {
            "connection_epoch": conn._flow.epoch,
            "consumed": [{"subscription_epoch": lane_epoch, "through_delivery_id": end}],
            "staged_recovery": [], "discarded_lanes": [],
        })
    else:
        update = await request(conn, "transport.flow.update", {
            "delivery_epoch": conn._flow.epoch, "ack_delivery_id": end,
        })
    assert update.ok, update.error


async def test_replay_waits_for_another_lanes_credit_without_restarting_snapshot(v2_connection):
    from opensquilla.gateway.transport_flow import FLOW_WINDOW_FRAMES

    conn = v2_connection
    active, recovering = "agent:main:active-window", "agent:main:waiting-replay"
    subscribe(conn, active)
    subscribe(conn, recovering)
    payload = await snapshot(conn, recovering)
    await stage(conn, payload)
    streams = get_session_streams()
    for index in range(FLOW_WINDOW_FRAMES):
        event = streams.record(active, "session.event.text_delta", {"text": str(index)})
        await conn.send_event("session.event.text_delta", event)
    streams.record(recovering, "session.event.text_delta", {"text": "replay tail"})
    installed = await request(conn, "sessions.messages.resume", receipt(payload))
    assert installed.ok, installed.error
    assert installed.payload["replay_to_seq"] == 1
    pending = conn._pending_replays[recovering]
    assert not pending.pending_delivery_ids
    assert not conn._flow.dirty
    assert conn.installed_snapshot_proof(receipt(payload)) is None

    # A successful resume identifies the accepted replay target, even if the
    # first delivery is still waiting for another lane's shared credit.
    await conn.send_res(installed, transport_control=True)
    await ack_replay(conn, set(conn._flow.deliveries))
    assert conn._pending_replays[recovering] is pending
    assert len(pending.pending_delivery_ids) == 1
    await ack_replay(conn, pending.pending_delivery_ids)
    assert conn.installed_snapshot_proof(receipt(payload)) == installed.payload
    replayed = [frame for frame in conn.ws.frames if frame.get("meta", {}).get("replayed")]
    assert [frame["payload"]["text"] for frame in replayed] == ["replay tail"]
    assert not conn._flow.dirty


async def test_v2_replay_splits_large_frames_without_changing_logical_batch(v2_connection):
    conn = v2_connection
    key = "agent:main:byte-segments"
    subscribe(conn, key)
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    for text in ("a", "b"):
        get_session_streams().record(key, "session.event.text_delta", {"text": text * 2_200_000})
    revision = conn._flow.dirty_revision(key)
    first = await request(conn, "sessions.messages.resume", receipt(payload))
    assert first.ok, first.error
    pending = conn._pending_replays[key]
    assert (pending.batch_index, pending.batch_offset) == (0, 1)
    assert len(pending.pending_delivery_ids) == 1
    assert conn._flow.dirty_revision(key) == revision
    first_ids = set(pending.pending_delivery_ids)
    repeated = await request(conn, "sessions.messages.resume", receipt(payload))
    assert repeated.ok and repeated.payload == first.payload
    assert pending.pending_delivery_ids == first_ids
    assert conn.installed_snapshot_proof(receipt(payload)) is None
    await ack_replay(conn, first_ids)
    assert conn._pending_replays[key] is pending
    assert pending.pending_delivery_ids.isdisjoint(first_ids)
    assert (pending.batch_index, pending.batch_offset) == (1, 0)
    assert conn.installed_snapshot_proof(receipt(payload)) is None
    await ack_replay(conn, pending.pending_delivery_ids)
    assert key not in conn._pending_replays
    assert conn.installed_snapshot_proof(receipt(payload)) == first.payload
    replayed = [frame for frame in conn.ws.frames if frame.get("meta", {}).get("replayed")]
    assert [frame["payload"]["text"][0] for frame in replayed] == ["a", "b"]
    assert [frame["meta"]["replay_batch"] for frame in replayed] == [
        {"index": 0, "count": 1}, {"index": 0, "count": 1},
    ]


async def test_replay_queue_capacity_segments_before_publication(resumable_connection):
    conn = resumable_connection
    key = f"agent:main:queue-segments-{conn.conn_id}"
    subscribe(conn, key)
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    for text in ("first", "second", "third"):
        get_session_streams().record(key, "session.event.text_delta", {"text": text})
    # One physical event and one reserved resume/control response slot.
    conn._outbox._maxsize = 2
    response = await request(conn, "sessions.messages.resume", receipt(payload))
    assert response.ok, response.error
    pending = conn._pending_replays[key]
    assert len(pending.pending_delivery_ids) == 1
    assert (pending.batch_index, pending.batch_offset) == (0, 1)
    # Dequeue restores physical queue capacity even before an ACK. It must
    # publish the remaining finite proof under the available flow credit.
    await asyncio.sleep(0.05)
    assert len(pending.pending_delivery_ids) == 3
    assert set(conn._flow.deliveries) == pending.pending_delivery_ids
    assert conn.installed_snapshot_proof(receipt(payload)) is None
    await ack_replay(conn, set(pending.pending_delivery_ids))
    assert key not in conn._pending_replays
    assert conn.installed_snapshot_proof(receipt(payload)) == response.payload
    replayed = [frame for frame in conn.ws.frames if frame.get("meta", {}).get("replayed")]
    assert [frame["payload"]["text"] for frame in replayed] == ["first", "second", "third"]


@pytest.mark.parametrize("recover_credit", [True, False])
async def test_replay_owns_prefix_when_global_reservation_refuses_suffix(
    resumable_connection, monkeypatch, recover_credit,
):
    conn = resumable_connection
    key = f"agent:main:global-prefix-{conn.conn_id}-{recover_credit}"
    subscribe(conn, key)
    conn._mark_flow_dirty({"session_key": key})

    def dirty_notices():
        return [
            frame for frame in conn.ws.frames
            if frame.get("event") == "transport.flow.dirty"
        ]

    async def wait_for_dirty_notices(count):
        while len(dirty_notices()) < count:
            await asyncio.sleep(0)

    # Consume the original notice before starting recovery, so an old notice
    # cannot satisfy the assertion about a later terminal replay failure.
    await asyncio.wait_for(wait_for_dirty_notices(1), 1)
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    for text in ("first", "second", "third"):
        get_session_streams().record(key, "session.event.text_delta", {"text": text})
    budget = get_transport_budget()
    reserve = budget.reserve
    bulk_attempts = 0

    def reserve_first_only(size, *, kind=None):
        nonlocal bulk_attempts
        if kind == "bulk":
            bulk_attempts += 1
            if bulk_attempts > 1:
                return False
        return reserve(size, kind=kind)

    monkeypatch.setattr(budget, "reserve", reserve_first_only)
    revision = conn._flow.dirty_revision(key)
    response = await request(conn, "sessions.messages.resume", receipt(payload))
    assert response.ok, response.error
    pending = conn._pending_replays[key]
    assert len(pending.pending_delivery_ids) == 1
    assert (pending.batch_index, pending.batch_offset) == (0, 1)
    assert conn._flow.dirty_revision(key) == revision
    assert set(conn._flow.deliveries) == pending.pending_delivery_ids
    original_notice_count = len(dirty_notices())
    if recover_credit:
        monkeypatch.setattr(budget, "reserve", reserve)
    await ack_replay(conn, pending.pending_delivery_ids)
    if recover_credit:
        assert conn._pending_replays[key] is pending
        assert len(pending.pending_delivery_ids) == 2
        await ack_replay(conn, pending.pending_delivery_ids)
        assert conn.installed_snapshot_proof(receipt(payload)) == response.payload
        replayed = [frame for frame in conn.ws.frames if (frame.get("meta") or {}).get("replayed")]
        assert [frame["payload"]["text"] for frame in replayed] == ["first", "second", "third"]
        assert len(dirty_notices()) == original_notice_count
    else:
        assert pending.transfer.closed
        assert key in conn._flow.dirty
        assert conn.installed_snapshot_proof(receipt(payload)) is None
        await asyncio.wait_for(wait_for_dirty_notices(original_notice_count + 1), 1)
        assert len(dirty_notices()) == original_notice_count + 1
        assert key in dirty_notices()[-1]["payload"]["dirty_keys"]
    assert key not in conn._pending_replays
    assert not conn._flow.deliveries


async def test_replay_initial_zero_credit_is_busy_and_retries_same_snapshot(
    resumable_connection, monkeypatch,
):
    conn = resumable_connection
    key = f"agent:main:initial-busy-{conn.conn_id}"
    subscribe(conn, key)
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    get_session_streams().record(key, "session.event.text_delta", {"text": "tail"})
    budget = get_transport_budget()
    reserve = budget.reserve
    monkeypatch.setattr(
        budget, "reserve",
        lambda size, *, kind=None: False if kind == "bulk" else reserve(size, kind=kind),
    )
    next_id = conn._flow.next_id
    revision = conn._flow.dirty_revision(key)
    response = await request(conn, "sessions.messages.resume", receipt(payload))
    assert not response.ok and response.error.code == "SNAPSHOT_BUSY"
    assert key not in conn._pending_replays and not conn._flow.deliveries
    assert conn._flow.next_id == next_id
    assert conn._flow.dirty_revision(key) == revision
    transfer = conn.snapshot_registry().get(key, payload["sync_revision"], payload["snapshot_id"])
    assert transfer is not None and not transfer.closed
    monkeypatch.setattr(budget, "reserve", reserve)
    response = await request(conn, "sessions.messages.resume", receipt(payload))
    assert response.ok, response.error
    await ack_replay(conn, conn._pending_replays[key].pending_delivery_ids)
    assert conn.installed_snapshot_proof(receipt(payload)) == response.payload


async def test_replay_prevalidates_suffix_before_first_reservation(
    resumable_connection, monkeypatch,
):
    from opensquilla.gateway import websocket

    conn = resumable_connection
    key = f"agent:main:encode-suffix-{conn.conn_id}"
    subscribe(conn, key)
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    for text in ("first", "invalid"):
        get_session_streams().record(key, "session.event.text_delta", {"text": text})
    original = websocket.encode_payload_for_protocol

    def encode(value, *, protocol):
        if isinstance(value, dict) and value.get("text") == "invalid":
            raise ValueError("synthetic invalid second frame")
        return original(value, protocol=protocol)

    monkeypatch.setattr(websocket, "encode_payload_for_protocol", encode)
    reserve = Mock(wraps=conn._flow._reserve)
    monkeypatch.setattr(conn._flow, "_reserve", reserve)
    next_id = conn._flow.next_id
    reserved = conn._transport_bytes
    response = await request(conn, "sessions.messages.resume", receipt(payload))
    assert not response.ok
    assert key not in conn._pending_replays and not conn._flow.deliveries
    assert conn._flow.next_id == next_id
    reserve.assert_not_called()
    assert conn._transport_bytes <= reserved
    await asyncio.sleep(0)
    assert not any(frame.get("meta", {}).get("replayed") for frame in conn.ws.frames)


async def test_failed_first_replay_publication_fences_replaced_queued_output(
    resumable_connection, monkeypatch,
):
    from opensquilla.gateway.snapshot_transfer import SnapshotTransferError

    conn = resumable_connection
    key = "agent:main:failed-replay-ownership-transfer"
    subscribe(conn, key)
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    # Hold ordinary drain as if the writer window were occupied. The queued
    # frame is the only charged owner until snapshot installation replaces it.
    monkeypatch.setattr(conn, "_drain_pending_flow", Mock())
    live = get_session_streams().record(key, "session.event.text_delta", {"text": "queued"})
    await conn.send_event("session.event.text_delta", live)
    assert conn._pending_flow and not conn._flow.dirty
    monkeypatch.setattr(conn, "_prepare_replay_suffix", Mock(
        side_effect=SnapshotTransferError("SNAPSHOT_BUSY"),
    ))
    result = await request(conn, "sessions.messages.resume", receipt(payload))
    assert not result.ok and result.error.code == "SNAPSHOT_BUSY"
    assert not conn._pending_flow and not conn._pending_replays
    assert not conn._flow.deliveries and key in conn._flow.dirty
    live = get_session_streams().record(key, "session.event.text_delta", {"text": "after-failure"})
    await conn.send_event("session.event.text_delta", live)
    await asyncio.sleep(0.01)
    assert not any(frame.get("event") == "session.event.text_delta" for frame in conn.ws.frames)
    assert conn._transport_bytes == 0


@pytest.mark.parametrize("size, accepted", [(300_000, True), (1_048_576, True), (5_000_000, True)])
async def test_v2_replay_singleton_uses_connection_window_with_large_frame_exception(
    v2_connection, size, accepted,
):
    conn = v2_connection
    key = f"agent:main:singleton-{size}"
    subscribe(conn, key)
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    get_session_streams().record(key, "session.event.text_delta", {"text": "x" * size})
    response = await request(conn, "sessions.messages.resume", receipt(payload))
    if accepted:
        assert response.ok, response.error
        await ack_replay(conn, conn._pending_replays[key].pending_delivery_ids)
        assert conn.installed_snapshot_proof(receipt(payload)) == response.payload
    else:
        assert not response.ok and response.error.code == "SNAPSHOT_STALE"
        assert key not in conn._pending_replays and not conn._flow.deliveries


async def test_pending_resume_retry_does_not_bypass_new_dirty_barrier(resumable_connection):
    conn = resumable_connection
    key = f"agent:main:resume-retry-dirty-{conn.conn_id}"
    subscribe(conn, key)
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    get_session_streams().record(key, "session.event.text_delta", {"text": "tail"})
    first = await request(conn, "sessions.messages.resume", receipt(payload))
    assert first.ok, first.error
    pending = conn._pending_replays[key]
    conn._mark_flow_dirty({"session_key": key})
    repeated = await request(conn, "sessions.messages.resume", receipt(payload))
    assert not repeated.ok and repeated.error.code == "SNAPSHOT_STALE"
    assert conn._pending_replays[key] is pending
    assert conn.installed_snapshot_proof(receipt(payload)) is None


@pytest.mark.parametrize("suffix_count", [1, 8, 17])
@pytest.mark.parametrize("initially_dirty", [False, True])
async def test_replay_hands_off_owned_fifo_while_output_continues(
    resumable_connection, monkeypatch, suffix_count, initially_dirty,
):
    from opensquilla.gateway import session_streams

    connection = resumable_connection
    streams = session_streams.SessionStreamRegistry(stream_generation="synthetic-live-cas")
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    key = "agent:main:batch-live-cas"
    subscribe(connection, key)
    if initially_dirty:
        connection._mark_flow_dirty({"session_key": key})
    payload = await snapshot(connection, key)
    for index in range(9):
        streams.record(key, "session.event.text_delta", {"text": str(index)})
    await stage(connection, payload)
    installed = await request(connection, "sessions.messages.resume", receipt(payload))
    assert installed.ok, installed.error
    frozen_proof = dict(installed.payload)
    for index in range(suffix_count):
        live = streams.record(key, "session.event.text_delta", {"text": f"suffix-{index}"})
        await connection.send_event("session.event.text_delta", live)
    # There is new output before EVERY ACK, including the ACK that completes
    # recovery. Waiting for a quiet ACK window would never finish this test.
    for index in range(12):
        pending = connection._pending_replays.get(key)
        if pending is None:
            break
        live = streams.record(key, "session.event.text_delta", {"text": f"continuing-{index}"})
        await connection.send_event("session.event.text_delta", live)
        retry = await request(connection, "sessions.messages.resume", receipt(payload))
        assert retry.ok and retry.payload == frozen_proof
        await ack_replay(connection, set(pending.pending_delivery_ids))
        assert len(connection._flow.deliveries) <= 128
    assert key not in connection._pending_replays
    assert key not in connection._flow.dirty
    assert connection.installed_snapshot_proof(receipt(payload)) == frozen_proof
    assert frozen_proof["replay_to_seq"] == 9
    # Handoff retains ordinary credit for the not-yet-consumed suffix, and
    # leaves room for a real live delta before its first ACK.
    assert connection._flow.deliveries
    live = streams.record(key, "session.event.text_delta", {"text": "live-after-handoff"})
    await connection.send_event("session.event.text_delta", live)
    assert key not in connection._flow.dirty
    await ack_replay(connection, set(connection._flow.deliveries))
    delivered = [
        frame["payload"]["stream_seq"] for frame in connection.ws.frames
        if frame.get("event") == "session.event.text_delta"
    ]
    assert delivered == list(range(1, streams.current_seq(key) + 1))
    assert not connection._flow.deliveries
    repeated = await request(connection, "sessions.messages.resume", receipt(payload))
    assert repeated.ok and repeated.payload == frozen_proof


async def test_replay_refills_partial_credit_and_owns_tail_past_raw_cache_eviction(
    resumable_connection, monkeypatch,
):
    from opensquilla.gateway import session_streams
    from opensquilla.gateway.transport_flow import FLOW_WINDOW_FRAMES

    conn = resumable_connection
    streams = session_streams.SessionStreamRegistry(stream_generation="owned-long-tail")
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    key = "agent:main:owned-long-tail"
    subscribe(conn, key)
    conn._mark_flow_dirty({"session_key": key})
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    target = FLOW_WINDOW_FRAMES * 2 + 17
    for index in range(target):
        streams.record(key, "session.event.text_delta", {"text": f"proof-{index}"})
    result = await request(conn, "sessions.messages.resume", receipt(payload))
    assert result.ok, result.error
    assert result.payload["replay_to_seq"] == target
    pending = conn._pending_replays[key]
    original_ids = sorted(pending.pending_delivery_ids)
    assert len(original_ids) == FLOW_WINDOW_FRAMES

    # Returning half the real window must immediately refill it, even though
    # the other half of the original replay remains unconsumed.
    await ack_replay(conn, set(original_ids[:FLOW_WINDOW_FRAMES // 2]))
    assert len(pending.pending_delivery_ids) == FLOW_WINDOW_FRAMES
    assert set(original_ids[FLOW_WINDOW_FRAMES // 2:]) <= pending.pending_delivery_ids
    assert max(pending.pending_delivery_ids) > original_ids[-1]

    async def emit(text):
        event = streams.record(key, "session.event.text_delta", {"text": text})
        await conn.send_event("session.event.text_delta", event)

    # The default raw replay cache only retains 500 lossy events. None of
    # these later frames may be forgotten while the finite proof is consumed.
    for index in range(601):
        await emit(f"owned-{index}")
    assert sum(len(queue) for queue in conn._pending_flow.values()) == 601
    assert conn._transport_bytes > sum(item.size for item in conn._flow.deliveries.values())
    replay = Mock(side_effect=AssertionError("handoff must not chase mutable raw history"))
    monkeypatch.setattr(streams, "replay", replay)
    await asyncio.sleep(0.01)
    assert all(
        frame["payload"]["stream_seq"] <= target for frame in conn.ws.frames
        if frame.get("event") == "session.event.text_delta"
    )
    rounds = 0
    while key in conn._pending_replays:
        rounds += 1
        assert rounds <= 2
        await emit(f"continuous-proof-{rounds}")
        await ack_replay(conn, set(pending.pending_delivery_ids))
    assert conn.installed_snapshot_proof(receipt(payload)) == result.payload
    assert key not in conn._flow.dirty
    assert conn._pending_flow and conn._flow.deliveries
    await emit("after-handoff")
    for _ in range(8):
        if not conn._flow.deliveries:
            break
        await ack_replay(conn, set(conn._flow.deliveries))
    assert not conn._pending_flow and not conn._flow.deliveries
    assert conn._transport_bytes == 0
    delivered = [
        frame["payload"]["stream_seq"] for frame in conn.ws.frames
        if frame.get("event") == "session.event.text_delta"
    ]
    assert delivered == list(range(1, streams.current_seq(key) + 1))
    replay.assert_not_called()


async def test_replay_barrier_preserves_full_outbox_tail_and_other_lane_progress(v2_connection):
    conn = v2_connection
    key, other = "agent:main:barrier", "agent:main:other-live"
    subscribe(conn, key)
    subscribe(conn, other)
    streams = get_session_streams()
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    for index in range(9):
        streams.record(key, "session.event.text_delta", {"text": str(index)})
    result = await request(conn, "sessions.messages.resume", receipt(payload))
    assert result.ok
    pending = conn._pending_replays[key]
    # All nine proof frames are synchronously enqueued. A full physical
    # outbox must not drop the next lossy frame before the charged FIFO owns it.
    original_maxsize = conn._outbox._maxsize
    conn._outbox._maxsize = conn._outbox.qsize()
    assert conn._outbox.full()
    try:
        live = streams.record(key, "session.event.text_delta", {"text": "held"})
        await conn.send_event("session.event.text_delta", live)
        assert sum(len(queue) for queue in conn._pending_flow.values()) == 1
    finally:
        conn._outbox._maxsize = original_maxsize
    live = streams.record(other, "session.event.text_delta", {"text": "other-progress"})
    await conn.send_event("session.event.text_delta", live)
    await asyncio.sleep(0.01)
    assert any(frame.get("payload", {}).get("text") == "other-progress" for frame in conn.ws.frames)
    assert not any(frame.get("payload", {}).get("text") == "held" for frame in conn.ws.frames)
    await ack_replay(conn, set(pending.pending_delivery_ids))
    await asyncio.sleep(0.01)
    assert any(frame.get("payload", {}).get("text") == "held" for frame in conn.ws.frames)
    for lane_key in (key, other):
        ids = {delivery_id for delivery_id, item in conn._flow.deliveries.items()
               if item.lane == lane_key}
        await ack_replay(conn, ids)
    assert not conn._flow.deliveries and not conn._pending_flow
    assert conn._transport_bytes == 0


@pytest.mark.parametrize("retire", ["release", "expired_release", "replacement"])
async def test_explicit_replay_retirement_discards_tail_without_false_failure(
    resumable_connection, monkeypatch, retire,
):
    from opensquilla.gateway import websocket

    conn = resumable_connection
    logger = Mock()
    monkeypatch.setattr(websocket, "log", logger)
    key = f"agent:main:retire-{retire}"
    subscribe(conn, key)
    streams = get_session_streams()
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    streams.record(key, "session.event.text_delta", {"text": "proof"})
    result = await request(conn, "sessions.messages.resume", receipt(payload))
    assert result.ok
    pending = conn._pending_replays[key]
    release_params = {name: payload[name] for name in ("key", "sync_revision", "snapshot_id")}
    live = streams.record(key, "session.event.text_delta", {"text": "owned-tail"})
    await conn.send_event("session.event.text_delta", live)
    assert conn._pending_flow
    bytes_before = conn._transport_bytes
    replacement = None
    if retire == "replacement":
        replacement = await snapshot(conn, key, "successor")
        await stage(conn, replacement)
    else:
        if retire == "expired_release":
            pending.transfer.deadline = 0
        released = await request(conn, "sessions.messages.snapshot.release", release_params)
        assert released.ok, released.error
    assert key not in conn._pending_replays
    assert not conn._pending_flow
    if retire != "replacement":
        assert conn._transport_bytes < bytes_before
    # Already published receipts remain owned until a real ACK; closing a
    # snapshot must not counterfeit consumption or resurrect it on that ACK.
    assert pending.pending_delivery_ids <= conn._flow.deliveries.keys()
    await ack_replay(conn, set(pending.pending_delivery_ids))
    assert key in conn._flow.dirty
    assert conn.installed_snapshot_proof(receipt(payload)) is None
    phases = [
        call.kwargs["phase"] for call in logger.info.call_args_list
        if call.args == ("gateway.session_recovery",)
    ]
    assert phases == ["started", "cancelled"]

    replacement = replacement or await snapshot(conn, key, "successor")
    if retire != "replacement":
        await stage(conn, replacement)
    streams.record(key, "session.event.text_delta", {"text": "new-proof"})
    next_result = await request(conn, "sessions.messages.resume", receipt(replacement))
    assert next_result.ok
    successor = conn._pending_replays[key]
    released = await request(conn, "sessions.messages.snapshot.release", release_params)
    assert released.ok and conn._pending_replays[key] is successor
    await ack_replay(conn, set(successor.pending_delivery_ids))
    assert key not in conn._flow.dirty
    assert conn.installed_snapshot_proof(receipt(replacement)) == next_result.payload


async def test_busy_replacement_retires_expired_pending_without_false_replay_failure(
    resumable_connection, monkeypatch,
):
    from opensquilla.gateway import websocket

    conn = resumable_connection
    logger = Mock()
    monkeypatch.setattr(websocket, "log", logger)
    key = "agent:main:busy-replacement"
    subscribe(conn, key)
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    streams = get_session_streams()
    streams.record(key, "session.event.text_delta", {"text": "proof"})
    result = await request(conn, "sessions.messages.resume", receipt(payload))
    assert result.ok
    pending = conn._pending_replays[key]
    live = streams.record(key, "session.event.text_delta", {"text": "owned-tail"})
    await conn.send_event("session.event.text_delta", live)
    assert conn._pending_flow
    # Expiry frees a retained slot, which other reads may occupy before this
    # pending installation tries to replace its snapshot. No limits change.
    registry = conn.snapshot_registry()
    pending.transfer.deadline = 0
    retained = [
        registry.admit(f"agent:main:retained-{index}", "retained", object())
        for index in range(registry._max_retained)
    ]
    failed = await request(conn, "sessions.messages.snapshot.read", {
        "key": key, "sync_revision": "replacement",
    })
    assert not failed.ok and failed.error.code == "SNAPSHOT_BUSY"
    assert key not in conn._pending_replays and not conn._pending_flow
    assert key in conn._flow.dirty
    assert all(not transfer.closed for transfer in retained)
    assert pending.pending_delivery_ids <= conn._flow.deliveries.keys()
    await ack_replay(conn, set(pending.pending_delivery_ids))
    phases = [
        call.kwargs["phase"] for call in logger.info.call_args_list
        if call.args == ("gateway.session_recovery",)
    ]
    assert phases == ["started", "cancelled"]
    assert not conn._flow.deliveries and conn._transport_bytes == 0


async def test_failed_snapshot_admission_preserves_still_current_replay(resumable_connection):
    from opensquilla.gateway.snapshot_transfer import SnapshotTransferError

    conn = resumable_connection
    key = "agent:main:rejected-replacement"
    subscribe(conn, key)
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    get_session_streams().record(key, "session.event.text_delta", {"text": "proof"})
    result = await request(conn, "sessions.messages.resume", receipt(payload))
    assert result.ok
    pending = conn._pending_replays[key]
    # A stale caller rejected before retirement has no authority over the
    # still-live owner, even though it names the same session and revision.
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_STALE"):
        conn.admit_snapshot(key, payload["sync_revision"], object(), is_current=lambda: True)
    assert conn._pending_replays[key] is pending and not pending.transfer.closed
    assert key not in conn._flow.dirty
    await ack_replay(conn, set(pending.pending_delivery_ids))
    assert conn.installed_snapshot_proof(receipt(payload)) == result.payload


@pytest.mark.parametrize("failure", ["dirty", "generation", "subscription", "deadline"])
async def test_replay_catchup_preserves_recovery_fences(
    resumable_connection, monkeypatch, failure,
):
    from opensquilla.gateway import session_streams

    conn = resumable_connection
    streams = session_streams.SessionStreamRegistry(stream_generation="synthetic-fenced-catchup")
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    key = "agent:main:fenced-catchup"
    subscribe(conn, key)
    conn._mark_flow_dirty({"session_key": key})
    payload = await snapshot(conn, key)
    streams.record(key, "session.event.text_delta", {"text": "first"})
    await stage(conn, payload)
    result = await request(conn, "sessions.messages.resume", receipt(payload))
    assert result.ok
    pending = conn._pending_replays[key]
    if failure == "dirty":
        conn._mark_flow_dirty({"session_key": key})
    elif failure == "generation":
        streams._stream_generation = "replacement-generation"
    elif failure == "subscription":
        conn._subscriptions.unsubscribe_messages(conn.conn_id, key)
        subscribe(conn, key)
        await asyncio.sleep(0.01)
        # Retirement owns disposal of the old delivery and dirty barrier.
        # It must not contaminate the replacement subscription.
        assert key not in conn._pending_replays
        assert key not in conn._flow.dirty
        assert conn.installed_snapshot_proof(receipt(payload)) is None
        return
    elif failure == "deadline":
        pending.transfer.deadline = 0
    await ack_replay(conn, set(pending.pending_delivery_ids))
    assert key in conn._flow.dirty
    assert key not in conn._pending_replays
    assert conn.installed_snapshot_proof(receipt(payload)) is None


async def test_catchup_uses_raw_cursor_past_capability_filtered_events(
    resumable_connection, monkeypatch,
):
    from opensquilla.gateway import session_streams, websocket

    conn = resumable_connection
    streams = session_streams.SessionStreamRegistry(stream_generation="synthetic-filtered-catchup")
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    project = websocket.project_session_event_for_client

    def filtered(name, payload, **kwargs):
        return None if payload.get("text") == "filtered" else project(name, payload, **kwargs)

    monkeypatch.setattr(websocket, "project_session_event_for_client", filtered)
    key = "agent:main:filtered-catchup"
    subscribe(conn, key)
    conn._mark_flow_dirty({"session_key": key})
    payload = await snapshot(conn, key)
    streams.record(key, "session.event.text_delta", {"text": "first"})
    streams.record(key, "session.event.text_delta", {"text": "filtered"})
    await stage(conn, payload)
    result = await request(conn, "sessions.messages.resume", receipt(payload))
    assert result.ok and result.payload["replay_to_seq"] == 1
    live = streams.record(key, "session.event.text_delta", {"text": "third"})
    await conn.send_event("session.event.text_delta", live)
    await ack_replay(conn, set(conn._pending_replays[key].pending_delivery_ids))
    assert key not in conn._flow.dirty
    assert conn.installed_snapshot_proof(receipt(payload)) == result.payload
    await ack_replay(conn, set(conn._flow.deliveries))
    delivered = [
        frame["payload"]["stream_seq"] for frame in conn.ws.frames
        if frame.get("event") == "session.event.text_delta"
    ]
    assert delivered == [1, 3]


async def test_recovery_logs_first_admission_reason_once_without_content(
    v2_connection, monkeypatch,
):
    import hashlib

    from opensquilla.gateway import session_streams, websocket

    conn = v2_connection
    streams = session_streams.SessionStreamRegistry(stream_generation="PRIVATE_GENERATION")
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    logger = Mock()
    monkeypatch.setattr(websocket, "log", logger)
    key = "agent:main:PRIVATE_SESSION_KEY"
    subscribe(conn, key)
    reserve = conn.reserve_transport_bytes

    def fail_bulk_after_eight(size, *, kind=None):
        if kind is None and len(conn._flow.deliveries) >= 8:
            return False
        return reserve(size, kind=kind)

    monkeypatch.setattr(conn, "reserve_transport_bytes", fail_bulk_after_eight)
    for _ in range(100):
        event = streams.record(key, "session.event.text_delta", {"text": "PRIVATE_OUTPUT"})
        await conn.send_event("session.event.text_delta", event)
    required = [
        call.kwargs for call in logger.info.call_args_list
        if call.args == ("gateway.session_recovery",) and call.kwargs["phase"] == "required"
    ]
    assert len(required) == 1
    assert required[0]["reason_code"] == "transport_reservation_rejected"
    assert required[0]["flow_unacked_frames"] == 8
    assert required[0]["stream_seq"] == 9
    assert required[0]["session_key_sha256"] == hashlib.sha256(key.encode()).hexdigest()[:16]
    assert "PRIVATE" not in json.dumps(required)
    monkeypatch.setattr(conn, "reserve_transport_bytes", reserve)
    # A later admission success cannot reuse the earlier refusal diagnostic.
    await ack_replay(conn, set(conn._flow.deliveries))
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    result = await request(conn, "sessions.messages.resume", receipt(payload))
    assert result.ok and key not in conn._flow.dirty
    assert conn._flow.last_rejection_reason is None
    phases = [
        call.kwargs["phase"] for call in logger.info.call_args_list
        if call.args == ("gateway.session_recovery",)
    ]
    assert phases == ["required", "started", "installed"]


async def test_recovery_logging_failure_does_not_change_dirty_ownership(
    v2_connection, monkeypatch,
):
    from opensquilla.gateway import websocket

    conn = v2_connection
    key = "agent:main:diagnostic-failure"
    subscribe(conn, key)
    logger = Mock()
    logger.info.side_effect = RuntimeError("logging unavailable")
    monkeypatch.setattr(websocket, "log", logger)
    conn._mark_flow_dirty({"session_key": key}, reason_code="client_reported_dirty")
    assert key in conn._flow.dirty
    payload = await snapshot(conn, key)
    await stage(conn, payload)
    result = await request(conn, "sessions.messages.resume", receipt(payload))
    assert result.ok and key not in conn._flow.dirty
    assert conn.installed_snapshot_proof(receipt(payload)) == result.payload


async def test_slow_snapshot_cannot_publish_across_subscription_replacement(
    connection, monkeypatch,
):
    key = "agent:main:lease"
    old_token = subscribe(connection, key)
    started, release = asyncio.Event(), asyncio.Event()

    async def identity(*_):
        started.set()
        await release.wait()
        return "same-session", 1

    monkeypatch.setattr(session_services, "read_session_identity", identity)
    pending = asyncio.create_task(request(connection, "sessions.messages.snapshot.read", {
        "key": key, "sync_revision": "old",
    }))
    await started.wait()
    connection._subscriptions.unsubscribe_messages(connection.conn_id, key)
    assert subscribe(connection, key) != old_token
    release.set()
    response = await pending
    assert not response.ok and response.error.code == "SNAPSHOT_STALE"
    assert not connection._flow.deliveries


async def test_resume_rechecks_dirty_barrier_after_identity_prepare(connection, monkeypatch):
    key = "agent:main:dirty"
    subscribe(connection, key)
    payload = await snapshot(connection, key)
    await stage(connection, payload)
    started, release = asyncio.Event(), asyncio.Event()

    async def identity(*_):
        started.set()
        await release.wait()
        return None, None

    monkeypatch.setattr(session_services, "read_session_identity", identity)
    pending = asyncio.create_task(request(connection, "sessions.messages.resume", receipt(payload)))
    await started.wait()
    connection._mark_flow_dirty({"session_key": key})
    release.set()
    failed = await pending
    assert not failed.ok and failed.error.code == "SNAPSHOT_STALE"
    assert key in connection._flow.dirty
    retried = await request(connection, "sessions.messages.resume", receipt(payload))
    assert retried.ok, retried.error
    assert key not in connection._flow.dirty


async def test_release_without_snapshot_id_does_not_unsubscribe_or_destroy_successor(connection):
    key = "agent:main:release"
    token = subscribe(connection, key)
    payload = await snapshot(connection, key)
    await stage(connection, payload)
    released = await request(connection, "sessions.messages.snapshot.release", {
        "key": key, "sync_revision": "one",
    })
    assert released.ok and released.payload["retired"]
    successor = await snapshot(connection, key, "two")
    await request(connection, "sessions.messages.snapshot.release", {
        "key": key, "sync_revision": "one",
    })
    assert connection.snapshot_registry().get(key, "two", successor["snapshot_id"]) is not None
    assert connection._subscriptions.get_message_subscription_token(
        connection.conn_id, key,
    ) == token
    await stage(connection, successor)


async def test_cancelled_reserved_piece_uses_one_neutral_tombstone_and_staged_credit(connection):
    key = "agent:main:tombstone"
    subscribe(connection, key)
    ordinary = connection._flow.admit(10)
    connection._flow.mark_sent(ordinary)
    response = await request(connection, "sessions.messages.snapshot.read", {
        "key": key, "sync_revision": "one",
    })
    assert response.ok
    delivery = response.payload["delivery"]
    connection.cancel_snapshot_delivery(delivery)
    connection.cancel_snapshot_delivery(delivery)
    await connection.send_res(response)
    await asyncio.sleep(0)
    frames = [frame for frame in connection.ws.frames
              if frame.get("meta", {}).get("flow") == delivery]
    assert len(frames) == 1
    assert frames[0]["payload"]["global_dirty"] is False
    assert not any(
        isinstance(frame.get("error"), dict)
        and frame["error"].get("code") == "SNAPSHOT_STALE"
        for frame in connection.ws.frames
    )
    await stage(connection, response.payload)
    assert list(connection._flow.deliveries) == [ordinary]


async def test_permanently_missing_piece_closes_only_its_epoch(connection):
    key = "agent:main:lost"
    subscribe(connection, key)
    payload = await snapshot(connection, key)
    connection._force_close = AsyncMock()
    connection._expire_snapshot_credit(payload["delivery"]["delivery_id"])
    await asyncio.sleep(0)
    connection._force_close.assert_awaited_once()
    assert connection._closing
    assert payload["delivery"]["delivery_id"] in connection._flow.deliveries


async def test_reader_cancel_and_other_session_progress_while_old_read_retires(
    connection, monkeypatch,
):
    started, retired, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def identity(_, key):
        if key == "agent:main:a":
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                retired.set()
                await release.wait()
        return None, None

    monkeypatch.setattr(session_services, "read_session_identity", identity)
    loop = asyncio.create_task(_message_loop(
        connection, GatewayConfig(), get_dispatcher(), None,
        subscription_manager=connection._subscriptions,
    ))

    def send(request_id, method, params):
        connection.ws.incoming.put_nowait(json.dumps({
            "type": "req", "id": request_id, "method": method, "params": params,
        }))

    async def response(request_id):
        async def wait():
            while True:
                for frame in connection.ws.frames:
                    if frame.get("id") == request_id:
                        return frame
                await asyncio.sleep(0)
        return await asyncio.wait_for(wait(), 1)

    try:
        send("subscribe-a", "sessions.messages.subscribe", {
            "key": "agent:main:a", "fast_ack": True,
        })
        assert (await response("subscribe-a"))["ok"]
        send("read-a", "sessions.messages.snapshot.read", {
            "key": "agent:main:a", "sync_revision": "one",
        })
        await asyncio.wait_for(started.wait(), 1)
        connection.ws.incoming.put_nowait(json.dumps({"type": "cancel", "id": "read-a"}))
        await asyncio.wait_for(retired.wait(), 1)
        send("subscribe-b", "sessions.messages.subscribe", {
            "key": "agent:main:b", "fast_ack": True,
        })
        assert (await response("subscribe-b"))["ok"]
        send("read-b", "sessions.messages.snapshot.read", {
            "key": "agent:main:b", "sync_revision": "one",
        })
        assert (await response("read-b"))["ok"]
        assert "read-a" in connection._recovery_operations
        cancelled = await response("read-a")
        assert not cancelled["ok"]
        assert cancelled["error"]["code"] == "SNAPSHOT_STALE"
    finally:
        release.set()
        await asyncio.sleep(0)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


@pytest.mark.parametrize("method", [
    "sessions.messages.resume", "sessions.messages.snapshot.release",
])
async def test_guest_cannot_resume_or_release_another_owners_key(connection, method):
    connection.principal = Principal(
        role="operator", scopes=frozenset({"operator.read"}), authenticated=False,
        is_owner=False, guest_owner_id="a" * 64,
    )
    params = {"key": "agent:main:private", "sync_revision": "one"}
    if method.endswith("resume"):
        params.update(snapshot_id="snapshot", stream_generation="generation", stream_seq=0)
    denied = await request(connection, method, params)
    assert not denied.ok and denied.error.code == "UNAUTHORIZED"


async def test_cancel_ignoring_writer_remains_charged_until_send_unwinds(connection, monkeypatch):
    import opensquilla.gateway.websocket as websocket

    started, release = asyncio.Event(), asyncio.Event()

    async def stuck_send(_):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    monkeypatch.setattr(connection.ws, "send_text", stuck_send)
    monkeypatch.setattr(websocket, "_WRITER_STOP_SECONDS", 0.02)
    await connection.send_event("session.event.text_delta", {"session_key": "a", "text": "x"})
    await started.wait()
    writer = connection._writer_task
    held = connection._transport_bytes
    try:
        await asyncio.wait_for(connection._force_close(reason="synthetic-gate"), 0.5)
        assert writer in websocket._DRAINING_WRITER_TASKS
        connection._cleanup_transport()
        assert connection._transport_bytes == held > 0
    finally:
        release.set()
        await asyncio.wait_for(writer, 1)
    assert connection._transport_bytes == 0


async def test_batch_mutation_from_legacy_connection_fences_each_modern_owner(connection):
    runtime = object()
    connection._recovery_runtime = runtime
    transfers = []
    for key in ("agent:main:a", "agent:main:b"):
        token = subscribe(connection, key)
        transfers.append(connection.snapshot_registry().admit(key, "one", token))
    context = RpcContext(conn_id="legacy", session_manager=runtime)
    _, completed = _admit_session_mutation(
        "sessions.delete", {"keys": ["agent:main:a", "agent:main:b"]}, context,
    )
    assert completed is not None
    assert all(transfer.closed for transfer in transfers)
    scheduler = get_recovery_scheduler()
    assert scheduler.mutation_tail(runtime, "agent:main:a") is not None
    assert scheduler.mutation_tail(runtime, "agent:main:b") is not None
    completed.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert scheduler.mutation_tail(runtime, "agent:main:a") is None
    assert scheduler.mutation_tail(runtime, "agent:main:b") is None


@pytest.mark.parametrize("method", ["sessions.reset", "sessions.delete", "sessions.truncate"])
async def test_identity_mutations_still_retire_snapshot_and_fence_live_delivery(connection, method):
    runtime = object()
    key = "agent:main:webchat:identity-fence"
    connection._recovery_runtime = runtime
    token = subscribe(connection, key)
    transfer = connection.snapshot_registry().admit(key, "before", token)
    _, completed = _admit_session_mutation(
        method, {"key": key}, RpcContext(conn_id="mutation-owner", session_manager=runtime),
    )
    try:
        assert transfer.closed
        assert key in connection._flow.dirty
        assert get_recovery_scheduler().mutation_tail(runtime, key) is not None
    finally:
        completed.set_result(None)
        await asyncio.sleep(0)


async def test_manual_compaction_keeps_live_retry_progress_while_ordered_mutation_is_pending(
    connection,
):
    runtime = object()
    key = "agent:main:webchat:manual-live-progress"
    connection._recovery_runtime = runtime
    token = subscribe(connection, key)
    transfer = connection.snapshot_registry().admit(key, "before", token)
    _, completed = _admit_session_mutation(
        "sessions.contextCompact", {"key": key},
        RpcContext(conn_id="mutation-owner", session_manager=runtime),
    )
    try:
        assert not transfer.closed
        assert key not in connection._flow.dirty
        assert get_recovery_scheduler().mutation_tail(runtime, key) is not None
        payload = get_session_streams().record(key, "session.event.compaction", {
            "source": "manual", "status": "observed", "epoch": 0,
            "compaction_id": "cmp-live-review", "phase": "summarizing",
            "recovery": {"phase": "retry_wait", "reason": "rate_limited",
                         "retry_after_ms": 35000, "http_status": 429},
        })
        await connection.send_event("session.event.compaction", payload)
        await asyncio.sleep(0)
        frames = [frame for frame in connection.ws.frames
                  if frame.get("event") == "session.event.compaction"]
        assert len(frames) == 1
        assert frames[0]["payload"]["recovery"]["http_status"] == 429
        assert frames[0]["payload"]["recovery"]["phase"] == "retry_wait"
        assert not completed.done(), "Progress must arrive before the operation finishes"
    finally:
        completed.set_result(None)
        await asyncio.sleep(0)


async def test_dirty_notice_successor_survives_old_send_finally(connection, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    original_send = connection.ws.send_text
    first = True

    async def send(text):
        nonlocal first
        if first:
            first = False
            started.set()
            await release.wait()
        await original_send(text)

    monkeypatch.setattr(connection.ws, "send_text", send)
    connection._mark_flow_dirty({"session_key": "agent:main:a"})
    await started.wait()
    connection._mark_flow_dirty({"session_key": "agent:main:b"})
    successor = connection._flow_dirty_notice_pending
    assert successor is not None
    release.set()
    await asyncio.sleep(0)
    notices = [frame for frame in connection.ws.frames
               if frame.get("event") == "transport.flow.dirty"]
    assert len(notices) == 2
    assert "agent:main:b" in notices[-1]["payload"]["dirty_keys"]


async def test_filtered_tail_proof_uses_last_visible_sequence(connection, monkeypatch):
    from opensquilla.gateway import session_streams

    streams = session_streams.SessionStreamRegistry(stream_generation="synthetic-generation")
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    key = "agent:main:visible"
    subscribe(connection, key)
    payload = await snapshot(connection, key)
    await stage(connection, payload)
    streams.record(key, "session.event.text_delta", {"text": "visible"})
    # This typed receipt is deliberately hidden for a peer without its cap.
    from opensquilla.contracts.gateway_transport import TURN_COMMITTED_EVENT
    streams.record(key, TURN_COMMITTED_EVENT, {})
    result = await request(connection, "sessions.messages.resume", receipt(payload))
    assert result.ok, result.error
    assert result.payload["replay_to_seq"] == payload["current_stream_seq"] + 1


async def test_legacy_batch_mutation_blocks_only_affected_modern_reads(connection, monkeypatch):
    runtime = object()
    started, release = asyncio.Event(), asyncio.Event()
    identity_calls = []

    async def mutation(*_):
        started.set()
        await release.wait()
        return {}

    async def identity(_, key):
        identity_calls.append(key)
        return None, None

    dispatcher = get_dispatcher()
    monkeypatch.setattr(dispatcher.get_entry("sessions.delete"), "handler", mutation)
    monkeypatch.setattr(session_services, "read_session_identity", identity)
    legacy = WsConnection("synthetic-legacy", Socket())
    legacy._start_writer(maxsize=512, enabled=True)
    get_registry().register(legacy)
    for key in ("agent:main:a", "agent:main:c"):
        subscribe(connection, key)
    loops = [asyncio.create_task(_message_loop(
        conn, GatewayConfig(), dispatcher, runtime,
        subscription_manager=conn._subscriptions,
    )) for conn in (legacy, connection)]

    def send(conn, request_id, method, params):
        conn.ws.incoming.put_nowait(json.dumps({
            "type": "req", "id": request_id, "method": method, "params": params,
        }))

    async def response(request_id):
        async def wait():
            while True:
                for frame in connection.ws.frames:
                    if frame.get("id") == request_id:
                        return frame
                await asyncio.sleep(0)
        return await asyncio.wait_for(wait(), 1)

    try:
        send(legacy, "delete", "sessions.delete", {"keys": ["agent:main:a", "agent:main:b"]})
        await asyncio.wait_for(started.wait(), 1)
        send(connection, "affected", "sessions.messages.snapshot.read", {
            "key": "agent:main:a", "sync_revision": "after-delete",
        })
        send(connection, "healthy", "sessions.messages.snapshot.read", {
            "key": "agent:main:c", "sync_revision": "one",
        })
        assert (await response("healthy"))["ok"]
        assert "agent:main:a" not in identity_calls
        release.set()
        assert (await response("affected"))["ok"]
    finally:
        release.set()
        for task in loops:
            task.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        await legacy._stop_writer()
        legacy._cleanup_transport()
        get_registry().unregister(legacy.conn_id)


async def test_resume_control_proof_can_borrow_unused_shared_capacity(connection):
    from opensquilla.gateway.transport_flow import CONTROL_BUFFER_BYTES

    key = "agent:main:capacity"
    subscribe(connection, key)
    payload = await snapshot(connection, key)
    await stage(connection, payload)
    connection._mark_flow_dirty({"session_key": key})
    await asyncio.sleep(0)
    reserved = CONTROL_BUFFER_BYTES - 1
    assert connection.reserve_transport_bytes(reserved, kind="control")
    connection._flow_control_bytes = reserved
    try:
        result = await request(connection, "sessions.messages.resume", receipt(payload))
        assert result.ok, result.error
        assert key not in connection._flow.dirty
        assert key in connection._resume_proofs
    finally:
        connection._flow_control_bytes = 0
        connection.release_transport_bytes(reserved, kind="control")


@pytest.mark.parametrize("failure", ["transport_budget", "scheduler"])
async def test_rejected_recovery_admission_releases_new_transfer(connection, monkeypatch, failure):
    import opensquilla.gateway.websocket as websocket

    key = f"agent:main:rejected-{failure}"
    subscribe(connection, key)

    if failure == "transport_budget":
        monkeypatch.setattr(
            connection, "reserve_transport_bytes", lambda *_args, **_kwargs: False,
        )
    else:
        class RejectingScheduler:
            def mutation_tail(self, *_args):
                return None

            def submit(self, *_args, **_kwargs):
                return False

        monkeypatch.setattr(websocket, "get_recovery_scheduler", lambda: RejectingScheduler())

    context = RpcContext(
        conn_id=connection.conn_id,
        principal=connection.principal,
        subscription_manager=connection._subscriptions,
    )
    accepted = connection._try_recovery_request(
        get_dispatcher(), f"rejected-{failure}", "sessions.messages.snapshot.read",
        {"key": key, "sync_revision": "one"}, context, 128,
    )

    assert not accepted
    assert connection.snapshot_registry().get(key, "one") is None


async def test_large_modern_read_response_uses_bulk_without_closing(connection):
    from opensquilla.gateway.rpc import RpcDispatcher
    from opensquilla.gateway.websocket import _dispatch_request

    dispatcher = RpcDispatcher()
    content = "x" * (2 * 1024 * 1024)

    async def history(_params, _ctx):
        return {"messages": [{"role": "user", "content": content}]}

    dispatcher.register("chat.history", history, scope="operator.read")
    ctx = RpcContext(conn_id=connection.conn_id, principal=connection.principal)
    await _dispatch_request(connection, dispatcher, "large-history", "chat.history", {}, ctx)
    assert not connection._closing
    assert connection._flow_control_bytes == 0
    assert connection._transport_kinds["bulk"] > len(content)
    await asyncio.sleep(0)
    assert connection.ws.frames[-1]["payload"]["messages"][0]["content"] == content
    assert connection._transport_bytes == 0


@pytest.mark.parametrize("flow_enabled", [False, True])
async def test_idle_writer_releases_completed_payload(flow_enabled):
    import gc
    import weakref

    from opensquilla.gateway.protocol import make_ok_res

    class Payload(dict):
        pass

    class DiscardingSocket(Socket):
        async def send_text(self, _text):
            pass

    conn = WsConnection("synthetic-idle-writer", DiscardingSocket())
    conn._recovery_enabled = flow_enabled
    if flow_enabled:
        conn._enable_flow()
    conn._start_writer(maxsize=8, enabled=True)
    payload = Payload(content="x" * (2 * 1024 * 1024))
    reference = weakref.ref(payload)
    try:
        await conn.send_res(make_ok_res("history", payload))
        del payload
        await asyncio.sleep(0)
        assert conn._outbox.empty() and conn._transport_bytes == 0
        assert not conn._writer_task.done()
        gc.collect()
        assert reference() is None
    finally:
        await conn._stop_writer()
        conn._cleanup_transport()


async def test_credit_response_uses_control_reserve_when_bulk_is_full(connection):
    from opensquilla.gateway.transport_flow import (
        CONNECTION_BUFFER_BYTES,
        CONTROL_BUFFER_BYTES,
        RECOVERY_WINDOW_BYTES,
    )
    from opensquilla.gateway.websocket import _dispatch_request

    reserved = CONNECTION_BUFFER_BYTES - CONTROL_BUFFER_BYTES - RECOVERY_WINDOW_BYTES
    assert connection.reserve_transport_bytes(reserved)
    try:
        await _dispatch_request(connection, get_dispatcher(), "credit", "transport.flow.update", {
            "delivery_epoch": connection._flow.epoch, "ack_delivery_id": 0,
        }, RpcContext(conn_id=connection.conn_id, principal=connection.principal))
        assert not connection._closing
        assert connection._flow_control_bytes > 0
        await asyncio.sleep(0)
        assert connection.ws.frames[-1]["id"] == "credit"
        assert connection.ws.frames[-1]["ok"]
    finally:
        connection.release_transport_bytes(reserved)


async def test_resume_proof_uses_control_reserve_after_bulk_body_reclamation(connection):
    from opensquilla.gateway.transport_flow import (
        CONNECTION_BUFFER_BYTES,
        CONTROL_BUFFER_BYTES,
        RECOVERY_WINDOW_BYTES,
    )
    from opensquilla.gateway.websocket import _dispatch_request

    key = "agent:main:proof-headroom"
    subscribe(connection, key)
    payload = await snapshot(connection, key)
    await stage(connection, payload)
    reserved = (CONNECTION_BUFFER_BYTES - CONTROL_BUFFER_BYTES - RECOVERY_WINDOW_BYTES
                - connection._transport_kinds.get("bulk", 0))
    assert connection.reserve_transport_bytes(reserved)
    try:
        await _dispatch_request(
            connection, get_dispatcher(), "proof", "sessions.messages.resume", receipt(payload),
            RpcContext(conn_id=connection.conn_id, principal=connection.principal),
        )
        assert not connection._closing
        assert connection._flow_control_bytes > 0
        assert connection._transport_kinds["bulk"] == reserved
        await asyncio.sleep(0)
        assert connection.ws.frames[-1]["id"] == "proof"
        assert connection.ws.frames[-1]["ok"]
        assert connection.ws.frames[-1]["payload"]["snapshot_id"] == payload["snapshot_id"]
    finally:
        connection.release_transport_bytes(reserved)


async def test_subscription_rollback_returns_its_error_without_cancelling_itself(
    connection, monkeypatch,
):
    from opensquilla.gateway import rpc_sessions

    monkeypatch.setattr(rpc_sessions, "_build_sessions_messages_subscription_payload",
                        AsyncMock(side_effect=ValueError("synthetic rejected payload")))
    key = "agent:main:rollback"
    context = RpcContext(conn_id=connection.conn_id, principal=connection.principal,
                         subscription_manager=connection._subscriptions)
    assert connection._try_recovery_request(
        get_dispatcher(), "rollback", "sessions.messages.subscribe",
        {"key": key, "fast_ack": True}, context, 128,
    )

    async def response():
        while not connection.ws.frames:
            await asyncio.sleep(0)
        return connection.ws.frames[0]

    result = await asyncio.wait_for(response(), 1)
    assert result["id"] == "rollback" and not result["ok"]
    assert connection._subscriptions.get_message_subscription_token(connection.conn_id, key) is None


@pytest.mark.parametrize("keys", [1, "bad-string", None, ["agent:main:a", 1]])
async def test_malformed_delete_keeps_reader_available(connection, keys):
    loop = asyncio.create_task(_message_loop(
        connection, GatewayConfig(), get_dispatcher(), object(),
        subscription_manager=connection._subscriptions,
    ))
    try:
        connection.ws.incoming.put_nowait(json.dumps({
            "type": "req", "id": "bad-delete", "method": "sessions.delete",
            "params": {"keys": keys},
        }))
        connection.ws.incoming.put_nowait(json.dumps({"type": "ping"}))

        async def pong():
            while not any(frame.get("type") == "pong" for frame in connection.ws.frames):
                await asyncio.sleep(0)

        await asyncio.wait_for(pong(), 1)
        assert not loop.done()
        assert not connection._closing
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        await connection._stop_ordinary_requests()


async def test_live_tokens_under_existing_dirty_barrier_do_not_starve_resume(
    connection, monkeypatch,
):
    from opensquilla.gateway import session_streams

    streams = session_streams.SessionStreamRegistry(stream_generation="synthetic-live")
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    key = "agent:main:streaming"
    subscribe(connection, key)
    connection._mark_flow_dirty({"session_key": key})
    payload = await snapshot(connection, key)
    await stage(connection, payload)
    started, release = asyncio.Event(), asyncio.Event()

    async def identity(*_):
        started.set()
        await release.wait()
        return None, None

    monkeypatch.setattr(session_services, "read_session_identity", identity)
    pending = asyncio.create_task(request(connection, "sessions.messages.resume", receipt(payload)))
    await started.wait()
    barrier = connection._flow.dirty_revision(key)
    for index in range(5):
        event = streams.record(key, "session.event.text_delta", {"text": str(index)})
        await connection.send_event("session.event.text_delta", event)
    assert connection._flow.dirty_revision(key) == barrier
    release.set()
    result = await pending
    assert result.ok, result.error
    assert result.payload["replay_to_seq"] == payload["current_stream_seq"] + 5
