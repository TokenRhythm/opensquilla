"""Named auth owns the complete SQLite chain off-loop, including cancellation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.websockets import WebSocketState

from opensquilla.gateway import auth, desktop_ownership
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.middleware import AuthMiddleware
from opensquilla.gateway.origin_guard import request_principal_is_owner_async
from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.gateway.rpc_sessions import _GatewayAdmissionPrimitives
from opensquilla.gateway.token_store import TokenStore
from opensquilla.gateway.websocket import handle_ws_connection


def _config(tmp_path, token="legacy"):
    return GatewayConfig(
        state_dir=str(tmp_path), auth=AuthConfig(mode="token", token=token),
    )


def _issue(tmp_path):
    return TokenStore(tmp_path / "sessions.db").create(
        name="Worker boundary", roles={"operator"}, scopes={"operator.read"},
        capabilities={"task.read"},
    )


def _trace_connections(monkeypatch):
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            # Check closure on the creating thread, retaining SQLite's thread guard.
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                self.execute("SELECT 1")
            self.closed = True

    def connect(store):
        connection = sqlite3.connect(store._path, timeout=10, factory=TrackedConnection)
        connection.row_factory = sqlite3.Row
        connections.append((threading.get_ident(), connection))
        return connection

    monkeypatch.setattr(TokenStore, "_connect", connect)
    return connections


def _assert_closed_off_loop(connections, loop_thread):
    assert connections
    for thread, connection in connections:
        assert thread != loop_thread
        assert connection.closed


@pytest.mark.parametrize("schema", ["cold", "warm", "missing_index", "old_revision"])
async def test_named_resolver_includes_schema_read_and_committed_usage_off_loop(
    tmp_path, monkeypatch, schema,
):
    token = "osq_missing_abcdefghijklmnop"
    if schema != "cold":
        token = _issue(tmp_path).token
        with sqlite3.connect(tmp_path / "sessions.db") as connection:
            if schema == "missing_index":
                connection.execute("DROP INDEX idx_sandbox_tokens_active")
            if schema == "old_revision":
                connection.execute("ALTER TABLE sandbox_tokens DROP COLUMN authorization_revision")
    connections = _trace_connections(monkeypatch)
    loop_thread = threading.get_ident()

    principal = await auth.resolve_auth_async(
        _config(tmp_path), {"token": token}, "operator", peer_ip="192.168.1.7",
    )

    assert principal.auth_state == ("invalid" if schema == "cold" else "authenticated")
    assert not principal.is_owner
    _assert_closed_off_loop(connections, loop_thread)
    if schema != "cold":
        with sqlite3.connect(tmp_path / "sessions.db") as connection:
            row = connection.execute(
                "SELECT last_used_at, last_peer, authorization_revision FROM sandbox_tokens",
            ).fetchone()
        assert row[0] is not None and row[1:] == ("192.168.1.7", 1)


@pytest.mark.parametrize("mode,token", [("none", ""), ("token", ""), ("token", "legacy")])
async def test_no_auth_missing_and_ordinary_legacy_keep_immediate_path(
    tmp_path, monkeypatch, mode, token,
):
    def unexpected(_operation):
        raise AssertionError("No named-token candidate should enter an auth worker")

    monkeypatch.setattr(auth, "run_auth_database_operation", unexpected)
    config = _config(tmp_path)
    config.auth.mode = mode
    principal = await auth.resolve_auth_async(
        config, {"token": token}, "operator", peer_ip="127.0.0.1",
    )
    assert principal is not None
    assert principal.is_owner is (mode == "none" or bool(token))


async def test_owner_guard_rechecks_desktop_liveness_instead_of_cached_principal(
    tmp_path, monkeypatch,
):
    owner = SimpleNamespace(instance_nonce="a" * 64)
    monkeypatch.setattr(desktop_ownership, "_ACTIVE_DESKTOP_GATEWAY_OWNERSHIP", owner)
    token = desktop_ownership.desktop_gateway_auth_token(owner.instance_nonce)
    config = _config(tmp_path)
    principal = await auth.resolve_auth_async(
        config, {"token": token}, "operator", peer_ip="127.0.0.1",
    )
    assert principal.is_owner and principal.token_public_id == "desktop"
    request = Request({
        "type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 12345), "query_string": b"",
    })
    request.state.principal = principal
    assert await request_principal_is_owner_async(config, request)

    monkeypatch.setattr(desktop_ownership, "_ACTIVE_DESKTOP_GATEWAY_OWNERSHIP", None)
    assert not await request_principal_is_owner_async(config, request)


async def test_revoked_named_token_remains_invalid_and_never_promotes_owner(tmp_path):
    issued = _issue(tmp_path)
    TokenStore(tmp_path / "sessions.db").revoke(issued.record.public_id)
    principal = await auth.resolve_auth_async(
        _config(tmp_path), {"token": issued.token}, "operator", peer_ip="127.0.0.1",
    )
    assert principal.auth_state == "invalid" and not principal.is_owner


@pytest.mark.parametrize("configured_match", [False, True])
async def test_usage_write_failure_preserves_existing_legacy_fallback(
    tmp_path, configured_match,
):
    issued = _issue(tmp_path)
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute(
            "CREATE TRIGGER reject_usage BEFORE UPDATE OF last_used_at ON sandbox_tokens "
            "BEGIN SELECT RAISE(ABORT, 'synthetic usage failure'); END",
        )
    principal = await auth.resolve_auth_async(
        _config(tmp_path, issued.token if configured_match else "legacy"),
        {"token": issued.token}, "operator", peer_ip="127.0.0.1",
    )
    assert principal.auth_state == ("authenticated" if configured_match else "invalid")
    assert principal.is_owner is configured_match
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        assert connection.execute("SELECT last_used_at FROM sandbox_tokens").fetchone() == (None,)


@pytest.mark.parametrize("blocked_stage", ["_ensure_schema", "verify"])
async def test_repeated_cancel_drains_usage_commit_and_connection_cleanup(
    tmp_path, monkeypatch, blocked_stage,
):
    issued = _issue(tmp_path)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    original = getattr(TokenStore, blocked_stage)
    connections = _trace_connections(monkeypatch)

    def blocked_operation(store, *args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return original(store, *args, **kwargs)

    monkeypatch.setattr(TokenStore, blocked_stage, blocked_operation)
    task = asyncio.create_task(auth.resolve_auth_async(
        _config(tmp_path), {"token": issued.token}, "operator", peer_ip="192.168.1.8",
    ))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_closed_off_loop(connections, threading.get_ident())
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        assert connection.execute("SELECT last_peer FROM sandbox_tokens").fetchone() == (
            "192.168.1.8",
        )


async def test_cancelled_direct_verify_drains_rollback_and_closes_connections(
    tmp_path, monkeypatch,
):
    issued = _issue(tmp_path)
    store = TokenStore(tmp_path / "sessions.db")
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute(
            "CREATE TRIGGER reject_usage BEFORE UPDATE OF last_used_at ON sandbox_tokens "
            "BEGIN SELECT RAISE(ABORT, 'synthetic usage failure'); END",
        )
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    original = TokenStore.verify
    connections = _trace_connections(monkeypatch)

    def blocked_verify(store, token, *, peer_ip=None):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return original(store, token, peer_ip=peer_ip)

    monkeypatch.setattr(TokenStore, "verify", blocked_verify)
    task = asyncio.create_task(store.verify_async(issued.token))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert isinstance(cancelled.value.__cause__, sqlite3.IntegrityError)
    _assert_closed_off_loop(connections, threading.get_ident())
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        assert connection.execute("SELECT last_used_at FROM sandbox_tokens").fetchone() == (None,)


@pytest.mark.parametrize("role,peer", [("node", "127.0.0.1"), ("operator", "8.8.8.8")])
async def test_async_named_resolver_preserves_role_and_peer_rejection(tmp_path, role, peer):
    issued = _issue(tmp_path)
    principal = await auth.resolve_auth_async(
        _config(tmp_path), {"token": issued.token}, role, peer_ip=peer,
    )
    if role == "node":
        assert principal.auth_state == "invalid" and not principal.is_owner
    else:
        assert principal is None


@pytest.mark.parametrize("boundary", ["http", "websocket"])
async def test_actual_transport_auth_entry_uses_complete_worker(tmp_path, monkeypatch, boundary):
    issued = _issue(tmp_path)
    connections = _trace_connections(monkeypatch)
    config = _config(tmp_path)
    if boundary == "http":
        async def whoami(request):
            return JSONResponse({"public_id": request.state.principal.token_public_id})

        app = Starlette(routes=[Route("/api/whoami", whoami)])
        app.add_middleware(AuthMiddleware, config=config)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
        ) as client:
            response = await client.get(
                "/api/whoami", headers={"Authorization": f"Bearer {issued.token}"},
            )
        assert response.json() == {"public_id": issued.record.public_id}
    else:
        class WebSocket:
            client_state = WebSocketState.CONNECTED
            client = SimpleNamespace(host="127.0.0.1", port=12345)

            def __init__(self):
                self.frames = []

            async def accept(self):
                pass

            async def send_text(self, text):
                self.frames.append(json.loads(text))

            async def receive_text(self):
                # Stop after auth by rejecting protocol negotiation.
                return json.dumps({
                    "type": "req", "id": "h", "method": "connect", "params": {
                        "auth": {"token": issued.token}, "minProtocol": 1, "maxProtocol": 0,
                    },
                })

            async def close(self, code=1000):
                self.client_state = WebSocketState.DISCONNECTED

        websocket = WebSocket()
        await handle_ws_connection(websocket, config, dispatcher=object())
        assert websocket.frames[-1]["error"]["message"] == "Unsupported protocol version range"
    _assert_closed_off_loop(connections, threading.get_ident())


@pytest.mark.parametrize("state", ["active", "revoked", "unavailable"])
async def test_turn_authority_constructor_is_off_loop_and_preserves_decision(
    tmp_path, monkeypatch, state,
):
    issued = _issue(tmp_path)
    principal = auth.resolve_auth(
        _config(tmp_path), {"token": issued.token}, "operator", peer_ip="192.168.1.7",
    )
    if state == "revoked":
        TokenStore(tmp_path / "sessions.db").revoke(issued.record.public_id)
    admission = object.__new__(_GatewayAdmissionPrimitives)
    admission._principal = principal
    admission._runtime_config = _config(tmp_path)
    connections = _trace_connections(monkeypatch)
    if state == "unavailable":
        original = TokenStore._ensure_schema

        def broken_schema(store):
            assert threading.get_ident() != loop_thread
            original(store)
            raise sqlite3.OperationalError("synthetic schema failure")

        monkeypatch.setattr(TokenStore, "_ensure_schema", broken_schema)
    loop_thread = threading.get_ident()
    if state == "active":
        result = await admission.resolve_activation_authority(session_id="sid", session_epoch=0)
        assert result["authorization_revision"] == 1
        assert result["token_public_id"] == issued.record.public_id
    else:
        with pytest.raises(RpcHandlerError) as caught:
            await admission.resolve_activation_authority(session_id="sid", session_epoch=0)
        expected_code = "UNAUTHORIZED" if state == "revoked" else "AUTHORITY_UNAVAILABLE"
        assert caught.value.code == expected_code
    _assert_closed_off_loop(connections, loop_thread)
