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
from opensquilla.gateway.token_store import TokenStore, run_auth_database_operation
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
        database = (
            store._path if store._initialize else f"{store._path.resolve().as_uri()}?mode=rw"
        )
        connection = sqlite3.connect(
            database, uri=not store._initialize, timeout=10, factory=TrackedConnection,
        )
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
async def test_preparation_and_named_verification_keep_sqlite_off_loop(
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
    await run_auth_database_operation(lambda: TokenStore(tmp_path / "sessions.db"))

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
async def test_usage_write_failure_preserves_named_identity_without_legacy_promotion(
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
    assert principal.auth_state == "authenticated"
    assert principal.token_public_id == issued.record.public_id
    assert principal.capabilities == issued.record.capabilities
    assert not principal.is_owner
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        assert connection.execute("SELECT last_used_at FROM sandbox_tokens").fetchone() == (None,)


async def test_repeated_cancel_drains_usage_commit_and_connection_cleanup(
    tmp_path, monkeypatch,
):
    issued = _issue(tmp_path)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    original = TokenStore.verify
    connections = _trace_connections(monkeypatch)

    def blocked_operation(store, *args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return original(store, *args, **kwargs)

    monkeypatch.setattr(TokenStore, "verify", blocked_operation)
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
    assert cancelled.value.__cause__ is None
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


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_cancelled_verify_waits_for_usage_transaction_and_connection_close(
    tmp_path, monkeypatch, commit_fails,
):
    issued = _issue(tmp_path)
    store = TokenStore(tmp_path / "sessions.db", initialize=False)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    connections = []

    class BlockingCommitConnection(sqlite3.Connection):
        closed = False

        def __exit__(self, exc_type, exc_value, traceback):
            assert threading.get_ident() != loop_thread
            assert self.in_transaction
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "Test did not release the usage transaction"
            if commit_fails:
                self.rollback()
                raise sqlite3.OperationalError("synthetic usage commit failure")
            return super().__exit__(exc_type, exc_value, traceback)

        def close(self):
            assert threading.get_ident() != loop_thread
            super().close()
            self.closed = True

    def connect():
        connection = sqlite3.connect(store._path, factory=BlockingCommitConnection)
        connection.row_factory = sqlite3.Row
        connections.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", connect)
    task = asyncio.create_task(store.verify_async(issued.token, peer_ip="127.0.0.2"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert len(connections) == 1 and not connections[0].closed
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connections[0].closed
    with sqlite3.connect(store._path) as connection:
        row = connection.execute(
            "SELECT last_used_at, last_peer FROM sandbox_tokens",
        ).fetchone()
    if commit_fails:
        assert row == (None, None)
    else:
        assert row[0] is not None and row[1] == "127.0.0.2"


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
async def test_turn_authority_read_is_off_loop_and_preserves_decision(
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
        with sqlite3.connect(tmp_path / "sessions.db") as connection:
            connection.execute("DROP TABLE sandbox_tokens")
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


@pytest.mark.parametrize("schema", ["missing_file", "missing_table", "old_revision"])
async def test_unprepared_auth_fails_closed_without_preparing_or_creating_database(
    tmp_path, schema,
):
    state_dir = tmp_path / "unprepared"
    database = state_dir / "sessions.db"
    token = "osq_missing_abcdefghijklmnop"
    if schema != "missing_file":
        token = _issue(state_dir).token
        with sqlite3.connect(database) as connection:
            if schema == "missing_table":
                connection.execute("DROP TABLE sandbox_tokens")
            else:
                connection.execute("ALTER TABLE sandbox_tokens DROP COLUMN authorization_revision")
        before = database.read_bytes()
    principal = await auth.resolve_auth_async(
        _config(state_dir), {"token": token}, "operator", peer_ip="127.0.0.1",
    )
    assert principal.auth_state == "invalid" and not principal.is_owner
    if schema == "missing_file":
        assert not state_dir.exists()
    else:
        assert database.read_bytes() == before
    if schema == "old_revision":
        await run_auth_database_operation(lambda: TokenStore(database))
        restored = await auth.resolve_auth_async(
            _config(state_dir), {"token": token}, "operator", peer_ip="127.0.0.1",
        )
        assert restored.auth_state == "authenticated" and not restored.is_owner


async def test_repeated_auth_and_turn_authority_do_not_reprepare_schema(tmp_path, monkeypatch):
    issued = _issue(tmp_path)

    def unexpected(_store):
        raise AssertionError("Request-time auth must not prepare the database")

    monkeypatch.setattr(TokenStore, "_ensure_schema", unexpected)
    config = _config(tmp_path)
    admission = object.__new__(_GatewayAdmissionPrimitives)
    admission._runtime_config = config
    for _ in range(3):
        principal = await auth.resolve_auth_async(
            config, {"token": issued.token}, "operator", peer_ip="127.0.0.1",
        )
        assert principal.auth_state == "authenticated"
        admission._principal = principal
        result = await admission.resolve_activation_authority(session_id="sid", session_epoch=0)
        assert result["authorization_revision"] == 1
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute("UPDATE sandbox_tokens SET revoked_at = 1")
    principal = await auth.resolve_auth_async(
        config, {"token": issued.token}, "operator", peer_ip="127.0.0.1",
    )
    assert principal.auth_state == "invalid"
    with pytest.raises(RpcHandlerError) as caught:
        await admission.resolve_activation_authority(session_id="sid", session_epoch=0)
    assert caught.value.code == "UNAUTHORIZED"


@pytest.mark.parametrize("state", ["wrong_secret", "revoked", "read_error"])
async def test_usage_failure_policy_does_not_allow_invalid_authority(tmp_path, state):
    issued = _issue(tmp_path)
    token = issued.token
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute(
            "CREATE TRIGGER reject_usage BEFORE UPDATE OF last_used_at ON sandbox_tokens "
            "BEGIN SELECT RAISE(ABORT, 'synthetic usage failure'); END",
        )
        if state == "revoked":
            connection.execute("UPDATE sandbox_tokens SET revoked_at = 1")
        elif state == "read_error":
            connection.execute("DROP TABLE sandbox_tokens")
        else:
            token = token[:-1] + ("A" if token[-1] != "A" else "B")
    principal = await auth.resolve_auth_async(
        _config(tmp_path), {"token": token}, "operator", peer_ip="127.0.0.1",
    )
    assert principal.auth_state == "invalid" and not principal.is_owner


async def test_named_looking_legacy_credential_keeps_explicit_configured_fallback(tmp_path):
    token = "osq_legacy_abcdefghijklmnop"
    principal = await auth.resolve_auth_async(
        _config(tmp_path, token), {"token": token}, "operator", peer_ip="127.0.0.1",
    )
    assert principal.auth_state == "authenticated" and principal.is_owner
    assert principal.token_public_id == "legacy"
    assert not (tmp_path / "sessions.db").exists()


async def test_metadata_failure_keeps_http_named_authority_and_recovers_usage(tmp_path):
    issued = _issue(tmp_path)
    config = _config(tmp_path, issued.token)

    async def whoami(request):
        principal = request.state.principal
        return JSONResponse({"public_id": principal.token_public_id, "owner": principal.is_owner})

    app = Starlette(routes=[Route("/api/whoami", whoami)])
    app.add_middleware(AuthMiddleware, config=config)
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute(
            "CREATE TRIGGER reject_usage BEFORE UPDATE OF last_used_at ON sandbox_tokens "
            "BEGIN SELECT RAISE(ABORT, 'synthetic usage failure'); END",
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
    ) as client:
        headers = {"Authorization": f"Bearer {issued.token}"}
        response = await client.get("/api/whoami", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"public_id": issued.record.public_id, "owner": False}
        with sqlite3.connect(tmp_path / "sessions.db") as connection:
            row = connection.execute("SELECT last_used_at FROM sandbox_tokens").fetchone()
            assert row == (None,)
            connection.execute("DROP TRIGGER reject_usage")
        assert (await client.get("/api/whoami", headers=headers)).status_code == 200
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        row = connection.execute("SELECT last_used_at FROM sandbox_tokens").fetchone()
        assert row[0] is not None
