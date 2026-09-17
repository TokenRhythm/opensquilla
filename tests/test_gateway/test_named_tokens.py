from __future__ import annotations

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
