"""Runtime dynamic-origin allowlist for the remote-control tunnel feature.

The pairing flow registers the exact browser origin of a freshly established
tunnel (e.g. ``https://<random>.trycloudflare.com``) while it is active.
These tests prove:

1. A registered origin passes HTTP mutation guards and WS upgrades even when
   the gateway is loopback-bound (the desktop default).
2. Unregistered foreign origins are still rejected - registration of one
   origin never widens trust to any other name, port, or scheme.
3. DNS-rebinding semantics do not degrade: a hostile Host header cannot ride
   a registered origin, and wildcard bind behavior stays intact.
4. Registration input is strictly parsed (no wildcards/paths/credentials).
5. Revocation restores the closed posture immediately.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import (
    dynamic_origins_snapshot,
    register_dynamic_origin,
    revoke_dynamic_origin,
)

_OWNER_PEER = ("127.0.0.1", 51000)
_REMOTE_PEER = ("192.168.50.77", 51000)
_TUNNEL_ORIGIN = "https://random-words-here.trycloudflare.com"


@pytest.fixture(autouse=True)
def _hermetic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "synthetic-config.toml"
    config_path.write_text("# synthetic dynamic-origin test config\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_path))
    home = tmp_path / "state"
    (home / "logs").mkdir(parents=True)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(home / "logs"))


@pytest.fixture(autouse=True)
def _clean_dynamic_origins() -> Iterator[None]:
    for origin in dynamic_origins_snapshot():
        revoke_dynamic_origin(origin)
    yield
    for origin in dynamic_origins_snapshot():
        revoke_dynamic_origin(origin)


def _mutation_app(config: GatewayConfig | None = None):
    async def mutate(_request):
        return JSONResponse({"ok": True})

    return create_gateway_app(
        config or GatewayConfig(),
        extra_routes=[Route("/dynamic-mutation", mutate, methods=["POST"])],
    )


def test_registered_tunnel_origin_passes_http_guard_on_loopback_bind() -> None:
    assert register_dynamic_origin(_TUNNEL_ORIGIN) is True
    with TestClient(
        _mutation_app(),
        # Real tunnel path: the phone page at the tunnel origin talks to the
        # same tunnel authority, so request Host/authority == Origin.
        base_url=_TUNNEL_ORIGIN,
        client=_OWNER_PEER,
    ) as client:
        response = client.post(
            "/dynamic-mutation",
            headers={"Origin": _TUNNEL_ORIGIN},
        )
    assert response.status_code == 200


def test_registered_origin_cannot_authorize_loopback_authority() -> None:
    # Dynamic origins mirror the static cors.allowed_origins contract: the
    # pairing lifecycle is the trust anchor. A tunnel page may reach the
    # gateway through its public hostname (cloudflared forwards to a
    # loopback-bound gateway) or through any other authority the operator
    # exposes; the exact origin match is what carries trust.
    assert register_dynamic_origin(_TUNNEL_ORIGIN) is True
    with TestClient(
        _mutation_app(),
        base_url="http://127.0.0.1:18791",
        client=_OWNER_PEER,
    ) as client:
        response = client.post(
            "/dynamic-mutation",
            headers={"Origin": _TUNNEL_ORIGIN},
        )
    assert response.status_code == 200

def test_unregistered_foreign_origin_still_rejected() -> None:
    assert register_dynamic_origin(_TUNNEL_ORIGIN) is True
    with TestClient(
        _mutation_app(),
        base_url="http://127.0.0.1:18791",
        client=_OWNER_PEER,
    ) as client:
        response = client.post(
            "/dynamic-mutation",
            headers={"Origin": "https://other.example"},
        )
    assert response.status_code == 403


def test_registered_origin_does_not_cover_different_port_or_scheme() -> None:
    assert register_dynamic_origin(_TUNNEL_ORIGIN) is True
    with TestClient(
        _mutation_app(),
        base_url="http://127.0.0.1:18791",
        client=_OWNER_PEER,
    ) as client:
        http_variant = client.post(
            "/dynamic-mutation",
            headers={"Origin": "http://random-words-here.trycloudflare.com"},
        )
        port_variant = client.post(
            "/dynamic-mutation",
            headers={"Origin": "https://random-words-here.trycloudflare.com:8443"},
        )
    assert http_variant.status_code == 403
    assert port_variant.status_code == 403


def test_hostile_host_header_cannot_ride_registered_origin() -> None:
    # Host-header forgery cannot smuggle an UNREGISTERED origin through: only
    # exact dynamic entries are honored, and this request presents none.
    assert register_dynamic_origin(_TUNNEL_ORIGIN) is True
    with TestClient(
        _mutation_app(),
        base_url="http://127.0.0.1:18791",
        client=_OWNER_PEER,
    ) as client:
        response = client.post(
            "/dynamic-mutation",
            headers={
                "Host": "evil.example:443",
                "Origin": "https://evil.example",
            },
        )
    assert response.status_code == 403


def test_wildcard_bind_dns_rebinding_protection_unchanged() -> None:
    # On a wildcard bind, a hostile hostname that resolves to the gateway
    # still cannot pass without being explicitly registered.
    config = GatewayConfig()
    config.host = "0.0.0.0"
    with TestClient(
        _mutation_app(config),
        base_url="http://192.0.2.10:18791",
        client=("192.0.2.20", 51000),
    ) as client:
        response = client.post(
            "/dynamic-mutation",
            headers={
                "Host": _TUNNEL_ORIGIN.removeprefix("https://") + ":18791",
                "Origin": _TUNNEL_ORIGIN,
            },
        )
    assert response.status_code == 403


def test_wildcard_bind_registered_origin_passes_despite_authority_mismatch() -> None:
    # The flip side: once the tunnel origin IS registered, it passes even on
    # a wildcard bind with a differing request authority - same contract as
    # static allowed_origins entries.
    config = GatewayConfig()
    config.host = "0.0.0.0"
    assert register_dynamic_origin(_TUNNEL_ORIGIN) is True
    with TestClient(
        _mutation_app(config),
        base_url="http://192.0.2.10:18791",
        client=("192.0.2.20", 51000),
    ) as client:
        response = client.post(
            "/dynamic-mutation",
            headers={"Origin": _TUNNEL_ORIGIN},
        )
    assert response.status_code == 200


def test_wildcard_bind_registered_origin_matching_authority_passes() -> None:
    config = GatewayConfig()
    config.host = "0.0.0.0"
    lan_origin = "http://192.0.2.10:18791"
    assert register_dynamic_origin(lan_origin) is True
    with TestClient(
        _mutation_app(config),
        base_url="http://192.0.2.10:18791",
        client=_REMOTE_PEER,
    ) as client:
        response = client.post(
            "/dynamic-mutation",
            headers={"Origin": lan_origin},
        )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "bad_origin",
    [
        "https://*.trycloudflare.com",
        "https://evil.example/path",
        "https://evil.example?x=1",
        "https://evil.example#frag",
        "https://user@evil.example",
        "ftp://evil.example",
        "null",
        "",
        "not an origin",
    ],
)
def test_registration_rejects_non_exact_browser_origins(bad_origin: str) -> None:
    assert register_dynamic_origin(bad_origin) is False
    assert bad_origin not in dynamic_origins_snapshot()


def test_revocation_restores_closed_posture() -> None:
    assert register_dynamic_origin(_TUNNEL_ORIGIN) is True
    assert revoke_dynamic_origin(_TUNNEL_ORIGIN) is True
    assert revoke_dynamic_origin(_TUNNEL_ORIGIN) is False
    with TestClient(
        _mutation_app(),
        base_url="http://127.0.0.1:18791",
        client=_OWNER_PEER,
    ) as client:
        response = client.post(
            "/dynamic-mutation",
            headers={"Origin": _TUNNEL_ORIGIN},
        )
    assert response.status_code == 403


def test_snapshot_is_sorted_copy() -> None:
    register_dynamic_origin("https://b.example")
    register_dynamic_origin("https://a.example")
    snap = dynamic_origins_snapshot()
    assert snap == ("https://a.example", "https://b.example")
    # mutating the snapshot does not affect the live set
    assert dynamic_origins_snapshot() == snap


def test_ws_upgrade_with_registered_origin_on_loopback_bind() -> None:
    assert register_dynamic_origin(_TUNNEL_ORIGIN) is True
    app = create_gateway_app(GatewayConfig())
    with TestClient(
        app,
        base_url=_TUNNEL_ORIGIN.replace("https", "wss"),
        client=_OWNER_PEER,
    ) as client:
        with client.websocket_connect(
            "/ws", headers={"Origin": _TUNNEL_ORIGIN},
        ) as ws:
            frame = ws.receive_json()
    assert frame.get("event") == "connect.challenge"


def test_ws_upgrade_unregistered_origin_still_rejected() -> None:
    app = create_gateway_app(GatewayConfig())
    foreign_origin = "https://unregistered.example"
    with pytest.raises(Exception):
        with TestClient(
            app,
            base_url=foreign_origin.replace("https", "wss"),
            client=_OWNER_PEER,
        ) as client:
            with client.websocket_connect(
                "/ws", headers={"Origin": foreign_origin},
            ) as ws:
                ws.receive_json()
