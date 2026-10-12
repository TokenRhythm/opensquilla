from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from opensquilla.gateway.auth import resolve_auth
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.middleware import AuthMiddleware
from opensquilla.gateway.token_store import TokenStore


def test_named_token_is_returned_once_and_verified_by_digest(tmp_path) -> None:
    store = TokenStore(tmp_path / "sessions.db")

    issued = store.create(
        name="Laptop",
        roles={"operator"},
        scopes={"operator.read", "operator.write"},
        capabilities={"host.execute", "task.submit", "task.read"},
    )

    assert issued.token.startswith(f"osq_{issued.record.public_id}_")
    assert issued.record.name == "Laptop"
    assert issued.record.secret_digest
    assert store.verify(issued.token) == issued.record
    assert issued.token.encode("utf-8") not in (tmp_path / "sessions.db").read_bytes()


def test_named_token_revoke_prevents_future_verification(tmp_path) -> None:
    store = TokenStore(tmp_path / "sessions.db")
    issued = store.create(
        name="Temporary",
        roles={"operator"},
        scopes={"operator.read"},
        capabilities={"task.read"},
    )

    assert store.revoke(issued.record.public_id) is True
    assert store.verify(issued.token) is None


def test_active_token_authorization_uses_current_record_without_loading_secret(tmp_path) -> None:
    store = TokenStore(tmp_path / "sessions.db")
    issued = store.create(
        name="Synthetic Goal operator", roles={"operator"},
        scopes={"operator.read", "operator.write"}, capabilities={"task.submit"},
    )
    assert store.get_active_authorization(issued.record.public_id) == (
        frozenset({"operator"}), frozenset({"operator.read", "operator.write"}),
        frozenset({"task.submit"}),
    )
    assert store.get_active_authorization("unknown-public-id") is None
    store.revoke(issued.record.public_id)
    assert store.get_active_authorization(issued.record.public_id) is None


@pytest.mark.parametrize("state", ["active", "missing", "revoked", "query_error"])
def test_active_token_authorization_closes_connection(tmp_path, monkeypatch, state) -> None:
    store = TokenStore(tmp_path / "sessions.db")
    issued = store.create(
        name="Synthetic authorization lookup", roles={"operator"},
        scopes={"operator.write"}, capabilities={"task.submit"},
    )
    if state == "revoked":
        assert store.revoke(issued.record.public_id)
    connection = store._connect()
    if state == "query_error":
        connection.execute("DROP TABLE sandbox_tokens")
    monkeypatch.setattr(store, "_connect", lambda: connection)
    public_id = "missing-public-id" if state == "missing" else issued.record.public_id
    try:
        if state == "query_error":
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                store.get_active_authorization(public_id)
        else:
            result = store.get_active_authorization(public_id)
            assert (result is not None) is (state == "active")
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")
    finally:
        connection.close()


def test_named_token_list_never_returns_secret_material(tmp_path) -> None:
    store = TokenStore(tmp_path / "sessions.db")
    issued = store.create(
        name="Desktop",
        roles={"operator"},
        scopes={"operator.read"},
        capabilities={"task.read", "host.execute"},
    )

    records = store.list_active()

    assert records == (issued.record,)
    assert issued.token not in repr(records)
    assert store.revoke(issued.record.public_id) is True
    assert store.list_active() == ()


def test_wrong_secret_and_unknown_public_id_are_indistinguishable(tmp_path) -> None:
    store = TokenStore(tmp_path / "sessions.db")
    issued = store.create(
        name="Phone",
        roles={"operator"},
        scopes={"operator.read"},
        capabilities={"task.read"},
    )
    secret = issued.token.rsplit("_", 1)[-1]

    assert store.verify(f"osq_{issued.record.public_id}_{secret}x") is None
    assert store.verify(f"osq_unknown_{secret}") is None


def test_named_token_resolves_capabilities_without_owner_promotion(tmp_path) -> None:
    issued = TokenStore(tmp_path / "sessions.db").create(
        name="LAN laptop",
        roles={"operator"},
        scopes={"operator.read", "operator.write"},
        capabilities={"host.execute", "task.read", "task.submit"},
    )
    config = GatewayConfig(
        host="0.0.0.0",
        state_dir=str(tmp_path),
        auth=AuthConfig(mode="token", token="legacy"),
    )

    principal = resolve_auth(
        config,
        auth_params={"token": issued.token},
        role_claim="operator",
        peer_ip="192.168.1.7",
    )

    assert principal is not None
    assert principal.auth_state == "authenticated"
    assert principal.is_owner is False
    assert principal.token_public_id == issued.record.public_id
    assert principal.capabilities == frozenset(
        {"host.execute", "task.read", "task.submit"}
    )


def test_named_token_authenticates_http_bearer_boundary(tmp_path) -> None:
    issued = TokenStore(tmp_path / "sessions.db").create(
        name="HTTP client",
        roles={"operator"},
        scopes={"operator.read"},
        capabilities={"task.read"},
    )
    config = GatewayConfig(
        host="0.0.0.0",
        state_dir=str(tmp_path),
        auth=AuthConfig(mode="token", token="legacy"),
    )

    async def whoami(request):
        principal = request.state.principal
        return JSONResponse(
            {
                "publicId": principal.token_public_id,
                "capabilities": sorted(principal.capabilities),
            }
        )

    app = Starlette(routes=[Route("/api/whoami", whoami)])
    app.add_middleware(AuthMiddleware, config=config)

    with TestClient(app, client=("192.168.1.7", 50000)) as client:
        response = client.get(
            "/api/whoami",
            headers={"Authorization": f"Bearer {issued.token}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "publicId": issued.record.public_id,
        "capabilities": ["task.read"],
    }


@pytest.mark.parametrize("state", ["active", "wrong_secret", "unknown", "revoked"])
def test_verification_reuses_one_connection_without_retaining_a_read_transaction(
    tmp_path, monkeypatch, state,
) -> None:
    store = TokenStore(tmp_path / "sessions.db")
    issued = store.create(
        name="One connection", roles={"operator"}, scopes={"operator.read"},
        capabilities={"task.read"},
    )
    token = issued.token
    if state == "revoked":
        store.revoke(issued.record.public_id)
    elif state == "wrong_secret":
        token = token[:-1] + ("A" if token[-1] != "A" else "B")
    elif state == "unknown":
        token = "osq_unknown_abcdefghijklmnop"
    connections = []
    events = []

    class ReadCursor(sqlite3.Cursor):
        def close(self):
            super().close()
            with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
                self.fetchone()
            events.append("read_closed")

    class TrackedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT"):
                return self.cursor(factory=ReadCursor).execute(sql, parameters)
            if sql.startswith("UPDATE"):
                assert events == ["read_closed"]
                assert not self.in_transaction
                events.append("usage_update")
            return super().execute(sql, parameters)

        def close(self):
            super().close()
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                self.execute("SELECT 1")
            events.append("connection_closed")

    def connect():
        connection = sqlite3.connect(store._path, factory=TrackedConnection)
        connection.row_factory = sqlite3.Row
        connections.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", connect)
    result = store.verify(token, peer_ip="127.0.0.2")
    assert result == (issued.record if state == "active" else None)
    assert len(connections) == 1
    assert events == (
        ["read_closed", "usage_update", "connection_closed"]
        if state == "active" else ["read_closed", "connection_closed"]
    )
    with sqlite3.connect(store._path) as connection:
        used_at, peer = connection.execute(
            "SELECT last_used_at, last_peer FROM sandbox_tokens",
        ).fetchone()
    assert (used_at is not None) is (state == "active")
    assert peer == ("127.0.0.2" if state == "active" else None)


@pytest.mark.parametrize("failure_stage", ["open", "select", "decode", "read_close"])
def test_verification_read_failures_are_not_treated_as_usage_failures(
    tmp_path, monkeypatch, failure_stage,
) -> None:
    store = TokenStore(tmp_path / "sessions.db")
    issued = store.create(
        name="Read failure", roles={"operator"}, scopes={"operator.read"},
        capabilities={"task.read"},
    )
    connections = []

    class FailingCursor(sqlite3.Cursor):
        def close(self):
            super().close()
            if failure_stage == "read_close":
                raise sqlite3.OperationalError("synthetic read cleanup failure")

    class FailingConnection(sqlite3.Connection):
        closed = False

        def execute(self, sql, parameters=()):
            assert not sql.startswith("UPDATE"), "Unverified authority must not record usage"
            if failure_stage == "select":
                raise sqlite3.OperationalError("synthetic read failure")
            return self.cursor(factory=FailingCursor).execute(sql, parameters)

        def close(self):
            super().close()
            self.closed = True

    def connect():
        if failure_stage == "open":
            raise OSError("synthetic read connection failure")
        connection = sqlite3.connect(store._path, factory=FailingConnection)
        connection.row_factory = sqlite3.Row
        connections.append(connection)
        return connection

    def decode(_row):
        raise sqlite3.DatabaseError("synthetic authority decode failure")

    monkeypatch.setattr(store, "_connect", connect)
    if failure_stage == "decode":
        monkeypatch.setattr(store, "_record_from_row", decode)
    with pytest.raises((OSError, sqlite3.Error), match="synthetic"):
        store.verify(issued.token)
    assert all(connection.closed for connection in connections)
    with sqlite3.connect(store._path) as connection:
        assert connection.execute("SELECT last_used_at FROM sandbox_tokens").fetchone() == (None,)


def test_revoke_after_authority_read_is_not_blocked_by_reused_connection(
    tmp_path, monkeypatch,
) -> None:
    store = TokenStore(tmp_path / "sessions.db")
    issued = store.create(
        name="Concurrent revocation", roles={"operator"}, scopes={"operator.read"},
        capabilities={"task.read"},
    )
    decode = store._record_from_row

    def revoke_then_decode(row):
        other = TokenStore(store._path, initialize=False)
        assert other.revoke(issued.record.public_id)
        return decode(row)

    monkeypatch.setattr(store, "_record_from_row", revoke_then_decode)
    # Preserve the existing read-point authority semantics; usage must not
    # overwrite a concurrent revocation, and the next check must reject it.
    assert store.verify(issued.token, peer_ip="127.0.0.1") == issued.record
    assert store.verify(issued.token) is None
    with sqlite3.connect(store._path) as connection:
        revoked_at, used_at, peer = connection.execute(
            "SELECT revoked_at, last_used_at, last_peer FROM sandbox_tokens",
        ).fetchone()
    assert revoked_at is not None
    assert used_at is None and peer is None


@pytest.mark.parametrize("failure_stage", ["execute", "commit", "close"])
def test_usage_failure_preserves_verified_authority_and_closes_connection(
    tmp_path, monkeypatch, failure_stage,
) -> None:
    store = TokenStore(tmp_path / "sessions.db")
    issued = store.create(
        name="Usage failure", roles={"operator"}, scopes={"operator.read"},
        capabilities={"task.read"},
    )
    connections = []

    class FailingConnection(sqlite3.Connection):
        closed = False

        def __exit__(self, exc_type, exc_value, traceback):
            if failure_stage == "commit" and exc_type is None:
                self.rollback()
                raise sqlite3.OperationalError("synthetic commit failure")
            return super().__exit__(exc_type, exc_value, traceback)

        def close(self):
            super().close()
            self.closed = True
            if failure_stage == "close":
                raise sqlite3.OperationalError("synthetic usage cleanup failure")

    def connect():
        connection = sqlite3.connect(store._path, factory=FailingConnection)
        connection.row_factory = sqlite3.Row
        connections.append(connection)
        if failure_stage == "execute":
            connection.set_authorizer(
                lambda action, *_: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_UPDATE else sqlite3.SQLITE_OK,
            )
        return connection

    monkeypatch.setattr(store, "_connect", connect)
    assert store.verify(issued.token, peer_ip="127.0.0.1") == issued.record
    assert len(connections) == 1
    assert all(connection.closed for connection in connections)
    with sqlite3.connect(store._path) as connection:
        row = connection.execute("SELECT last_used_at, last_peer FROM sandbox_tokens").fetchone()
        if failure_stage == "close":
            assert row[0] is not None and row[1] == "127.0.0.1"
        else:
            assert row == (None, None)


@pytest.mark.parametrize("use_tilde", [False, True])
def test_prepared_store_uri_keeps_unicode_and_reserved_path_characters(
    tmp_path, monkeypatch, use_tilde,
) -> None:
    state_dir = tmp_path / "Token 数据 # 100%"
    database = state_dir / "sessions.db"
    if use_tilde:
        expanded_database = database
        original_expanduser = Path.expanduser
        monkeypatch.setattr(
            Path, "expanduser",
            lambda path: expanded_database if path.parts[0] == "~" else original_expanduser(path),
        )
        database = Path("~/token-uri.db")
    store = TokenStore(database)
    issued = store.create(
        name="URI", roles={"operator"}, scopes={"operator.read"}, capabilities={"task.read"},
    )
    reader = TokenStore(database, initialize=False)
    assert reader.verify(issued.token) == issued.record
