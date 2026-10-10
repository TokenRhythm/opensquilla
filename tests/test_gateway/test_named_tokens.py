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


@pytest.mark.parametrize("failure_stage", ["open", "execute", "commit"])
def test_usage_failure_rolls_back_without_invalidating_verified_authority(
    tmp_path, monkeypatch, failure_stage,
) -> None:
    store = TokenStore(tmp_path / "sessions.db")
    issued = store.create(
        name="Usage failure", roles={"operator"}, scopes={"operator.read"},
        capabilities={"task.read"},
    )
    original_connect = store._connect
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

    calls = 0

    def connect():
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_connect()
        if failure_stage == "open":
            raise OSError("synthetic usage connection failure")
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
    assert all(connection.closed for connection in connections)
    with sqlite3.connect(store._path) as connection:
        row = connection.execute("SELECT last_used_at, last_peer FROM sandbox_tokens").fetchone()
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
