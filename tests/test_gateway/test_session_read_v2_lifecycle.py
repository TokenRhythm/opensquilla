"""Connection teardown owns v2 read leases even when client close is lost."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opensquilla.gateway import rpc_sessions, websocket
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.session_streams import SessionStreamRegistry


@pytest.fixture
def connections(monkeypatch):
    registry = websocket.ConnectionRegistry()
    monkeypatch.setattr(websocket, "_registry", registry)
    monkeypatch.setattr(rpc_sessions, "_V2_READ_LEASES", {})
    monkeypatch.setattr(rpc_sessions, "_V2_READ_OPEN_BY_CONNECTION", {})
    monkeypatch.setattr(rpc_sessions, "_V2_READ_CONNECTION_OWNERS", {})

    async def identity(ctx, key):
        return f"id:{key}", 1

    monkeypatch.setattr(rpc_sessions, "_snapshot_session_identity", identity)
    created = []

    def create(conn_id):
        connection = websocket.WsConnection(conn_id, SimpleNamespace())
        registry.register(connection)
        created.append(connection)
        return connection, RpcContext(conn_id=conn_id, principal=connection.principal)

    yield create
    for connection in created:
        connection._cleanup_transport()


async def test_disconnect_clears_only_its_lease_and_index_entries(connections):
    first, ctx = connections("first")
    second, other = connections("second")
    for key in ("alpha", "beta"):
        await rpc_sessions._handle_sessions_read_open_v2({"key": key}, ctx)
    retained = await rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, other)
    first._closing = True
    websocket.get_registry().unregister(first.conn_id)
    first._cleanup_transport()
    first._cleanup_transport()  # cleanup is idempotent
    assert list(rpc_sessions._V2_READ_LEASES) == [(second.conn_id, retained["lease_id"])]
    assert rpc_sessions._V2_READ_OPEN_BY_CONNECTION == {
        (second.conn_id, "alpha"): retained["lease_id"],
    }
    assert list(rpc_sessions._V2_READ_CONNECTION_OWNERS) == [second.conn_id]


async def test_explicit_close_reopen_registers_one_cleanup_for_the_socket(connections):
    connection, ctx = connections("reopen")
    first = await rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, ctx)
    same = await rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, ctx)
    assert first["lease_id"] == same["lease_id"]
    await rpc_sessions._handle_sessions_read_close_v2(
        {"key": "alpha", "lease_id": first["lease_id"]}, ctx,
    )
    second = await rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, ctx)
    assert first["lease_id"] != second["lease_id"]
    assert len(connection._transport_cleanup) == 1
    connection._cleanup_transport()
    assert not rpc_sessions._V2_READ_LEASES
    assert not rpc_sessions._V2_READ_OPEN_BY_CONNECTION
    assert not rpc_sessions._V2_READ_CONNECTION_OWNERS


async def test_reconnect_and_stale_cleanup_do_not_clear_replacement_owner(connections):
    old, old_ctx = connections("reused-connection-id")
    first = await rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, old_ctx)
    replacement, ctx = connections(old.conn_id)
    second = await rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, ctx)
    assert first["lease_id"] != second["lease_id"]
    old._cleanup_transport()
    assert list(rpc_sessions._V2_READ_LEASES) == [(replacement.conn_id, second["lease_id"])]
    assert rpc_sessions._V2_READ_CONNECTION_OWNERS[replacement.conn_id] is replacement
    replacement._cleanup_transport()
    assert not rpc_sessions._V2_READ_LEASES


async def test_repeated_abrupt_disconnects_leave_no_read_state(connections):
    for index in range(12):
        connection, ctx = connections(f"reconnect-{index}")
        await rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, ctx)
        websocket.get_registry().unregister(connection.conn_id)
        connection._closing = True
        connection._cleanup_transport()
        assert not rpc_sessions._V2_READ_LEASES
        assert not rpc_sessions._V2_READ_OPEN_BY_CONNECTION
        assert not rpc_sessions._V2_READ_CONNECTION_OWNERS


async def test_disconnect_during_identity_read_cannot_retain_a_new_lease(
    connections, monkeypatch,
):
    connection, ctx = connections("read-race")
    started, release = asyncio.Event(), asyncio.Event()

    async def identity(ctx, key):
        started.set()
        await release.wait()
        return "id:alpha", 1

    monkeypatch.setattr(rpc_sessions, "_snapshot_session_identity", identity)
    request = asyncio.create_task(rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, ctx))
    await started.wait()
    connection._closing = True
    websocket.get_registry().unregister(connection.conn_id)
    connection._cleanup_transport()
    release.set()
    with pytest.raises(RpcHandlerError, match="connection is no longer current"):
        await request
    assert not rpc_sessions._V2_READ_LEASES
    assert not rpc_sessions._V2_READ_OPEN_BY_CONNECTION
    assert not rpc_sessions._V2_READ_CONNECTION_OWNERS


@pytest.fixture
def streams(monkeypatch):
    registry = SessionStreamRegistry(stream_generation="generation-one")
    monkeypatch.setattr(rpc_sessions, "get_session_streams", lambda: registry)
    monkeypatch.setattr(rpc_sessions, "get_session_storage", lambda _: None)
    return registry


async def _installed_read(connections, streams):
    _, ctx = connections("installed-read")
    opened = await rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, ctx)
    streams.record("alpha", "session.event.text_delta", {"text": "hello"})
    params = {
        "key": "alpha", "lease_id": opened["lease_id"],
        "recovery_id": opened["recovery_id"],
        "consumed_through_seq": 1, "ack_through_seq": 1,
    }
    installed = await rpc_sessions._handle_sessions_read_install_v2(params, ctx)
    assert installed["status"] == "installed"
    return ctx, params, installed


def _advance_beyond_replay_window(streams):
    for index in range(600):
        streams.record("alpha", "session.event.tool_input_preview", {
            "preview_id": "write-html", "input": f"chunk:{index}",
        })


async def test_installed_confirmation_is_idempotent_after_live_replay_eviction(
    connections, streams,
):
    ctx, params, installed = await _installed_read(connections, streams)
    state = rpc_sessions._V2_READ_LEASES[(ctx.conn_id, params["lease_id"])]
    proof = dict(state["replay_proof"])
    _advance_beyond_replay_window(streams)
    # A long tool-input stream evicts the recovery's old deltas even while the
    # consumer remains healthy. The completed receipt is not a live cursor.
    with pytest.raises(RpcHandlerError, match="no longer contiguous"):
        rpc_sessions._v2_replay_snapshot(
            key="alpha", stream_generation="generation-one", base_seq=0,
        )
    # The client calls state before installConsumed and ACKs that receipt's
    # target, even when its separately confirmed snapshot covers newer events.
    observed = await rpc_sessions._handle_sessions_read_state_v2(params, ctx)
    assert observed["status"] == "installed"
    assert observed["progress"] == installed["progress"]
    confirmed = await rpc_sessions._handle_sessions_read_install_v2({
        **params, "consumed_through_seq": observed["progress"]["target_seq"],
        "ack_through_seq": observed["progress"]["target_seq"],
    }, ctx)
    assert confirmed == installed
    # Even a repeated base application must not reopen the completed proof.
    assert await rpc_sessions._handle_sessions_read_install_v2(
        {**params, "base_applied": True}, ctx,
    ) == installed
    assert state["replay_proof"] == proof
    assert state["base_seq"] == 0
    assert state["target_seq"] == state["consumed_through_seq"] == 1


async def test_uninstalled_recovery_still_rejects_an_evicted_replay_base(connections, streams):
    _, ctx = connections("uninstalled-read")
    opened = await rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, ctx)
    _advance_beyond_replay_window(streams)
    with pytest.raises(RpcHandlerError, match="no longer contiguous") as error:
        await rpc_sessions._handle_sessions_read_install_v2({
            "key": "alpha", "lease_id": opened["lease_id"],
            "base_applied": True, "consumed_through_seq": 600, "ack_through_seq": 600,
        }, ctx)
    assert error.value.code == "REBASE_REQUIRED"
    state = rpc_sessions._V2_READ_LEASES[(ctx.conn_id, opened["lease_id"])]
    assert state["status"] != "installed"
    assert state["consumed_through_seq"] == 0


@pytest.mark.parametrize("field", ["consumed_through_seq", "ack_through_seq"])
async def test_installed_confirmation_cannot_extend_its_proof_with_client_ack(
    connections, streams, field,
):
    ctx, params, installed = await _installed_read(connections, streams)
    _advance_beyond_replay_window(streams)
    with pytest.raises(RpcHandlerError, match="exceeds the server replay target") as error:
        await rpc_sessions._handle_sessions_read_install_v2({**params, field: 601}, ctx)
    assert error.value.code == "INVALID_REQUEST"
    assert await rpc_sessions._handle_sessions_read_install_v2(params, ctx) == installed


@pytest.mark.parametrize("changed", ["generation", "session-id", "epoch"])
async def test_installed_confirmation_keeps_generation_and_identity_fences(
    connections, streams, monkeypatch, changed,
):
    ctx, params, _ = await _installed_read(connections, streams)
    if changed == "generation":
        replacement = SessionStreamRegistry(stream_generation="generation-two")
        monkeypatch.setattr(rpc_sessions, "get_session_streams", lambda: replacement)
    else:
        async def identity(ctx, key):
            return ("replacement" if changed == "session-id" else f"id:{key}"), (
                2 if changed == "epoch" else 1
            )

        monkeypatch.setattr(rpc_sessions, "_snapshot_session_identity", identity)
    with pytest.raises(RpcHandlerError) as error:
        await rpc_sessions._handle_sessions_read_install_v2(params, ctx)
    assert error.value.code == "REBASE_REQUIRED"
    state = rpc_sessions._V2_READ_LEASES[(ctx.conn_id, params["lease_id"])]
    assert state["status"] == "rebase_required"


@pytest.mark.parametrize("first_write_fails", [False, True])
async def test_concurrent_confirmations_wait_for_the_durable_receipt(
    connections, streams, monkeypatch, first_write_fails,
):
    _, ctx = connections("durable-read")
    opened = await rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, ctx)
    params = {"key": "alpha", "lease_id": opened["lease_id"]}
    started, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def persist(receipt):
        writes.append(receipt)
        if len(writes) == 1:
            started.set()
            await release.wait()
            if first_write_fails:
                raise OSError("receipt write failed")

    storage = SimpleNamespace(upsert_session_read_recovery_receipt=persist)
    monkeypatch.setattr(rpc_sessions, "get_session_storage", lambda _: storage)
    first = asyncio.create_task(rpc_sessions._handle_sessions_read_install_v2(params, ctx))
    await started.wait()
    second = asyncio.create_task(rpc_sessions._handle_sessions_read_install_v2(params, ctx))
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    assert len(writes) == 1
    if not first_write_fails:
        streams.record("alpha", "session.event.text_delta", {"text": "new live tail"})
    observed = await rpc_sessions._handle_sessions_read_state_v2(params, ctx)
    assert observed["status"] != "installed"
    assert observed["progress"]["target_seq"] == writes[0]["target_seq"] == 0
    release.set()
    if first_write_fails:
        with pytest.raises(RpcHandlerError) as error:
            await first
        assert error.value.code == "READ_UNAVAILABLE"
    else:
        assert (await first)["status"] == "installed"
    installed = await second
    assert installed["status"] == "installed"
    assert len(writes) == (2 if first_write_fails else 1)
    assert all(receipt["status"] == "installed" for receipt in writes)
    state = rpc_sessions._V2_READ_LEASES[(ctx.conn_id, params["lease_id"])]
    assert state["receipt_durable"] is True
    assert state["status"] == "installed"
    assert state["target_seq"] == writes[-1]["target_seq"] == 0
    assert await rpc_sessions._handle_sessions_read_install_v2(params, ctx) == installed


@pytest.mark.parametrize("changed", ["generation", "epoch", "disconnect"])
async def test_identity_fences_still_apply_after_a_slow_receipt_write(
    connections, streams, monkeypatch, changed,
):
    connection, ctx = connections("reset-during-write")
    opened = await rpc_sessions._handle_sessions_read_open_v2({"key": "alpha"}, ctx)
    params = {"key": "alpha", "lease_id": opened["lease_id"]}
    started, release = asyncio.Event(), asyncio.Event()

    async def persist(receipt):
        started.set()
        await release.wait()

    storage = SimpleNamespace(upsert_session_read_recovery_receipt=persist)
    monkeypatch.setattr(rpc_sessions, "get_session_storage", lambda _: storage)
    request = asyncio.create_task(rpc_sessions._handle_sessions_read_install_v2(params, ctx))
    await started.wait()
    if changed == "generation":
        replacement = SessionStreamRegistry(stream_generation="generation-two")
        monkeypatch.setattr(rpc_sessions, "get_session_streams", lambda: replacement)
    elif changed == "epoch":
        async def identity(ctx, key):
            return f"id:{key}", 2

        monkeypatch.setattr(rpc_sessions, "_snapshot_session_identity", identity)
        observed = await rpc_sessions._handle_sessions_read_state_v2(params, ctx)
        assert observed["status"] == "rebase_required"
    else:
        connection._cleanup_transport()
    release.set()
    with pytest.raises(RpcHandlerError) as error:
        await request
    assert error.value.code == ("READ_STALE" if changed == "disconnect" else "REBASE_REQUIRED")
    state = rpc_sessions._V2_READ_LEASES.get((ctx.conn_id, params["lease_id"]))
    assert state is None or state["status"] == "rebase_required"
