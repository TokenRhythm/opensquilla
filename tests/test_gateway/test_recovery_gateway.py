"""New recovery methods preserve lease, dirty and delivery ownership."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState

from opensquilla.gateway import session_services
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.recovery_scheduler import get_recovery_scheduler
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.transport_flow import get_transport_budget
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
        assert not any(frame.get("id") == "read-a" for frame in connection.ws.frames)
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


async def test_resume_does_not_clear_barrier_without_control_proof_capacity(connection):
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
        assert not result.ok and result.error.code == "SNAPSHOT_BUSY"
        assert key in connection._flow.dirty
        assert key not in connection._resume_proofs
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
