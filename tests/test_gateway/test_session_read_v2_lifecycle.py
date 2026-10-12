"""Connection teardown owns v2 read leases even when client close is lost."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opensquilla.gateway import rpc_sessions, websocket
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError


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
