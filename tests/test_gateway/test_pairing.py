"""Pairing-token lifecycle tests for the web remote-control feature.

Covers: create -> WebSocket handshake auth with the correct scopes, one-shot
claim (a second handshake with the same token is rejected), TTL expiry,
revocation, and owner-only RPC enforcement.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import opensquilla.gateway.rpc_pairing as rpc_pairing
from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import AuthConfig, GatewayConfig

_OWNER_PEER = ("127.0.0.1", 51000)
_REMOTE_PEER = ("192.168.50.77", 51000)


@pytest.fixture(autouse=True)
def _hermetic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "workspace").mkdir(parents=True)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(home / "logs"))
    monkeypatch.setenv("OPENSQUILLA_WORKSPACE_DIR", str(home / "workspace"))
    config_path = tmp_path / "synthetic-config.toml"
    config_path.write_text("# synthetic pairing-test config\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_path))
    # The module-level pairing service caches the first state dir; each test
    # needs a fresh service bound to its own temp dir.
    rpc_pairing._PAIRING_SERVICE = None
    rpc_pairing.set_pairing_base_url_provider(lambda _ctx: "https://pair.test/control")
    yield
    rpc_pairing._PAIRING_SERVICE = None
    rpc_pairing.set_pairing_base_url_provider(None)


def _app(config: GatewayConfig | None = None):
    return create_gateway_app(config or GatewayConfig())


def _create_pairing(
    client: TestClient,
    params: dict | None = None,
) -> dict[str, object]:
    params = params or {}
    response = client.post(
        "/api/v2/rpc",
        json={"method": "gateway.pairing.create", "params": params},
    )
    # Fall back to the WS RPC surface if the HTTP alias is absent.
    if response.status_code == 404:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({
                "type": "req", "id": "1", "method": "connect",
                "params": {
                    "minProtocol": 3, "maxProtocol": 4,
                    "client": {"name": "test", "version": "1"},
                },
            })
            ws.receive_json()
            ws.send_json({
                "type": "req", "id": "2",
                "method": "gateway.pairing.create",
                "params": params,
            })
            frame = ws.receive_json()
            assert frame.get("type") == "res" and frame.get("ok") is True
            return frame["payload"]
    assert response.status_code == 200, response.text
    return response.json()["payload"]


def _ws_handshake(
    client: TestClient,
    token: str | None = None,
    peer: tuple[str, int] | None = None,
    headers: dict[str, str] | None = None,
):
    # The phone connects from a non-loopback address. Build a fresh
    # TestClient over the same app so the transport peer reflects that.
    target = TestClient(
        client.app,
        base_url="http://127.0.0.1:18791",
        client=peer or _REMOTE_PEER,
    )
    ws = target.websocket_connect("/ws", headers=headers or {})
    ws.__enter__()
    ws.receive_json()  # connect.challenge
    auth: dict[str, str] = {}
    if token:
        auth["token"] = token
    ws.send_json({
        "type": "req", "id": "1", "method": "connect",
        "params": {
            "minProtocol": 3, "maxProtocol": 4,
            "client": {"name": "test", "version": "1"},
            "auth": auth,
        },
    })
    frame = ws.receive_json()
    return ws, frame


def _close_ws(ws) -> None:
    ws.__exit__(None, None, None)


def test_pairing_create_returns_url_qr_and_metadata() -> None:
    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(client, {"expiresInSeconds": 300})

    assert "osq_" in payload["pairingUrl"]
    # The secret must ride in the fragment, which browsers never transmit, so
    # it cannot reach server or proxy access logs on the initial navigation.
    assert payload["pairingUrl"].startswith("https://pair.test/control/#token=")
    assert "?token=" not in payload["pairingUrl"]
    assert "&token=" not in payload["pairingUrl"]
    assert payload["qrCodeData"].startswith("<svg")
    # Without a viewBox, CSS-resized SVGs crop instead of scaling.
    assert 'viewBox="0 0 ' in payload["qrCodeData"]
    assert payload["expiresAt"] > int(time.time())
    assert payload["publicId"]
    assert payload["allowHostExecute"] is False
    assert "osq_" not in str(payload["qrCodeData"])


def test_pairing_url_keeps_secret_out_of_the_request_target() -> None:
    """A query-string secret is already sent before any client scrubbing runs.

    urlsplit models what the browser puts on the wire: everything up to the
    fragment. The token must be absent from that part for every shape of
    pairing URL, including the session-bound one.
    """

    from urllib.parse import urlsplit

    from opensquilla.gateway.rpc_pairing import _pairing_url

    for session_key in (None, "agent:main:webchat:guest:abcd:remote"):
        url = _pairing_url("https://pair.test/control", "osq_secret_value", session_key)
        parts = urlsplit(url)
        assert "osq_secret_value" not in parts.path
        assert "osq_secret_value" not in parts.query
        assert "osq_secret_value" in parts.fragment
        # The session hint is not a credential, so it stays queryable.
        if session_key:
            assert "session=" in parts.query


def test_pairing_create_resolves_base_url_without_stubbed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real (unstubbed) base-URL path must await the tunnel coroutine.

    Regression: _base_url read .base_url off the un-awaited _ensure_tunnel
    coroutine, so every create raised AttributeError and the desktop toggle
    snapped back off with no QR code.
    """

    from opensquilla.gateway.tunnel import TunnelInfo

    class _FakeTunnelManager:
        def ensure_tunnel(self) -> TunnelInfo:
            return TunnelInfo(
                mode="cloudflare",
                base_url="https://remote-control.test/control",
                origin="https://remote-control.test",
            )

    # Exercise the production path instead of the injected test provider.
    rpc_pairing.set_pairing_base_url_provider(None)
    monkeypatch.setattr(rpc_pairing, "_TUNNEL_MANAGER", _FakeTunnelManager())

    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(client, {"expiresInSeconds": 300})

    assert payload["pairingUrl"].startswith("https://remote-control.test/control/#token=")
    assert payload["qrCodeData"].startswith("<svg")


def test_pairing_create_reports_expiry_in_milliseconds_for_js_clients() -> None:
    """expiresAtMs must be directly comparable to the browser's Date.now().

    Regression: the UI compared epoch-seconds expiresAt against Date.now()
    milliseconds, so the QR was treated as already expired and never rendered.
    """

    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(client, {"expiresInSeconds": 300})

    now_ms = int(time.time() * 1000)
    assert payload["expiresAtMs"] == payload["expiresAt"] * 1000
    assert payload["expiresAtMs"] > now_ms


def test_pairing_create_revokes_token_when_base_url_resolution_fails() -> None:
    """A failed URL build must not leave an unclaimable pending device behind."""

    def _boom(_ctx: object) -> str:
        raise RuntimeError("no reachable address")

    rpc_pairing.set_pairing_base_url_provider(_boom)

    with TestClient(_app(), client=_OWNER_PEER) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({
                "type": "req", "id": "1", "method": "connect",
                "params": {
                    "minProtocol": 3, "maxProtocol": 4,
                    "client": {"name": "test", "version": "1"},
                },
            })
            ws.receive_json()

            ws.send_json({
                "type": "req", "id": "2",
                "method": "gateway.pairing.create",
                "params": {},
            })
            create_frame = ws.receive_json()
            assert create_frame.get("ok") is not True, create_frame

            ws.send_json({
                "type": "req", "id": "3",
                "method": "gateway.pairing.list",
                "params": {},
            })
            list_frame = ws.receive_json()
            assert list_frame.get("ok") is True, list_frame
            pairings = list_frame["payload"]["pairings"]

    assert pairings == [], f"orphaned pairing left behind: {pairings}"


def _revoke_pairing(client: TestClient, public_id: str) -> None:
    response = client.post(
        "/api/v2/rpc",
        json={"method": "gateway.pairing.revoke", "params": {"publicId": public_id}},
    )
    if response.status_code != 404:
        assert response.status_code == 200, response.text
        return
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({
            "type": "req", "id": "1", "method": "connect",
            "params": {
                "minProtocol": 3, "maxProtocol": 4,
                "client": {"name": "test", "version": "1"},
            },
        })
        ws.receive_json()
        ws.send_json({
            "type": "req", "id": "2",
            "method": "gateway.pairing.revoke",
            "params": {"publicId": public_id},
        })
        frame = ws.receive_json()
        assert frame.get("ok") is True, frame


def test_pairing_claim_issues_device_credential_for_reconnect() -> None:
    """短配对 + 长会话：认领后下发的 deviceToken 必须比配对 token 活得久。"""

    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(client, {"expiresInSeconds": 1})
        token = payload["pairingUrl"].split("#token=", 1)[1]
        ws, frame = _ws_handshake(client, token)
        _close_ws(ws)
        device_token = frame["auth"].get("deviceToken")
        assert isinstance(device_token, str) and device_token.startswith("osq_")

        # The one-shot pairing token expires; the device credential must keep
        # authenticating the phone on its own.
        time.sleep(1.2)
        ws2, frame2 = _ws_handshake(client, device_token)
        _close_ws(ws2)

    assert frame2.get("type") == "hello-ok", frame2
    principal = frame2["auth"]["principal"]
    assert principal["authenticated"] is True
    assert "operator.approvals" in principal["scopes"]


def test_tunnel_relay_connection_without_token_is_not_owner() -> None:
    """A loopback peer carrying X-Forwarded-For is a remote phone, not the owner.

    The tunnel relay dials from 127.0.0.1; without the forwarded-for guard the
    loopback-proximity check would hand every scanned phone full owner rights.
    """

    with TestClient(_app(), client=_OWNER_PEER) as client:
        ws, frame = _ws_handshake(
            client,
            peer=_OWNER_PEER,
            headers={"X-Forwarded-For": "240e:469:ea14:55c2::1"},
        )
        _close_ws(ws)

    assert frame.get("type") == "hello-ok", frame
    principal = frame["auth"]["principal"]
    assert principal["isOwner"] is False
    assert principal["authenticated"] is False


def test_pairing_token_claim_works_through_tunnel_relay() -> None:
    """End-to-end phone scenario: scanned token claims via the tunnel relay."""

    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(client, {"expiresInSeconds": 300})
        token = payload["pairingUrl"].split("#token=", 1)[1]
        ws, frame = _ws_handshake(
            client,
            token=token,
            peer=_OWNER_PEER,
            headers={"X-Forwarded-For": "240e:469:ea14:55c2::1"},
        )
        _close_ws(ws)

    assert frame.get("type") == "hello-ok", frame
    principal = frame["auth"]["principal"]
    assert principal["authenticated"] is True
    assert principal["isOwner"] is False
    assert set(principal["scopes"]) >= {"operator.read", "operator.write"}
    assert isinstance(frame["auth"].get("deviceToken"), str)


def test_pairing_revoke_cuts_off_device_credential() -> None:
    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(client)
        token = payload["pairingUrl"].split("#token=", 1)[1]
        ws, frame = _ws_handshake(client, token)
        _close_ws(ws)
        device_token = frame["auth"]["deviceToken"]

        _revoke_pairing(client, payload["publicId"])
        ws2, frame2 = _ws_handshake(client, device_token)
        _close_ws(ws2)

    assert frame2.get("type") == "hello-ok", frame2
    assert frame2["auth"]["principal"]["authenticated"] is False


def test_pairing_handshake_authenticates_with_operator_scopes() -> None:
    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(client)
        token = payload["pairingUrl"].split("#token=", 1)[1]
        ws, frame = _ws_handshake(client, token)
        _close_ws(ws)

    assert frame.get("type") == "hello-ok", frame
    principal = frame["auth"]["principal"]
    assert principal["isOwner"] is False
    assert principal["authenticated"] is True
    assert set(principal["scopes"]) >= {"operator.read", "operator.write", "operator.approvals"}
    assert "host.execute" not in principal["capabilities"]


def test_pairing_token_is_one_shot() -> None:
    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(client)
        token = payload["pairingUrl"].split("#token=", 1)[1]
        ws1, frame1 = _ws_handshake(client, token)
        _close_ws(ws1)
        ws2, frame2 = _ws_handshake(client, token)
        _close_ws(ws2)

    assert frame1.get("type") == "hello-ok", frame1
    # The claimed token can no longer authenticate as an operator: the
    # gateway degrades it to an anonymous guest (authState=invalid) instead
    # of granting operator scopes again.
    assert frame2.get("type") == "hello-ok", frame2
    principal = frame2["auth"]["principal"]
    assert principal["authState"] == "guest"
    assert principal["authenticated"] is False
    assert "operator.approvals" not in principal["scopes"]


def test_pairing_token_claim_is_race_safe() -> None:
    import os
    import tempfile

    from opensquilla.gateway.pairing import PairingService
    from opensquilla.gateway.token_store import TokenStore

    db = os.path.join(tempfile.mkdtemp(), "s.db")
    svc = PairingService(TokenStore(db))
    token, info = svc.create()
    public_id = info.public_id
    assert svc.try_claim(public_id) is True
    assert svc.try_claim(public_id) is False
    assert token.startswith("osq_")


def test_pairing_token_expires() -> None:
    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(client, {"expiresInSeconds": 1})
        token = payload["pairingUrl"].split("#token=", 1)[1]
        time.sleep(1.2)
        ws, frame = _ws_handshake(client, token)
        _close_ws(ws)

    assert frame.get("type") == "hello-ok", frame
    principal = frame["auth"]["principal"]
    assert principal["authState"] == "guest"
    assert principal["authenticated"] is False


def test_pairing_revoke_rejects_subsequent_handshake() -> None:
    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(client)
        token = payload["pairingUrl"].split("#token=", 1)[1]
        public_id = payload["publicId"]
        revoked = client.post(
            "/api/v2/rpc",
            json={"method": "gateway.pairing.revoke", "params": {"publicId": public_id}},
        )
        if revoked.status_code == 404:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                ws.send_json({
                    "type": "req", "id": "1", "method": "connect",
                    "params": {
                        "minProtocol": 3, "maxProtocol": 4,
                        "client": {"name": "test", "version": "1"},
                    },
                })
                ws.receive_json()
                ws.send_json({
                    "type": "req", "id": "2",
                    "method": "gateway.pairing.revoke",
                    "params": {"publicId": public_id},
                })
                frame = ws.receive_json()
                assert frame["ok"] is True
        else:
            assert revoked.status_code == 200, revoked.text
        ws, frame = _ws_handshake(client, token)
        _close_ws(ws)

    assert frame.get("type") == "hello-ok", frame
    principal = frame["auth"]["principal"]
    assert principal["authState"] == "guest"
    assert principal["authenticated"] is False


def test_pairing_rpc_requires_owner() -> None:
    with TestClient(_app(), client=_REMOTE_PEER) as client:
        response = client.post(
            "/api/v2/rpc",
            json={"method": "gateway.pairing.create", "params": {}},
        )
    # A remote non-owner peer must not be able to mint a pairing token.
    assert response.status_code in (403, 404)


def test_pairing_list_returns_metadata_without_secrets() -> None:
    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(client)
        public_id = payload["publicId"]
        listing = client.post(
            "/api/v2/rpc",
            json={"method": "gateway.pairing.list", "params": {}},
        )
        if listing.status_code == 404:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                ws.send_json({
                    "type": "req", "id": "1", "method": "connect",
                    "params": {
                        "minProtocol": 3, "maxProtocol": 4,
                        "client": {"name": "test", "version": "1"},
                    },
                })
                ws.receive_json()
                ws.send_json({
                    "type": "req", "id": "2",
                    "method": "gateway.pairing.list",
                    "params": {},
                })
                frame = ws.receive_json()
                data = frame["payload"]
        else:
            assert listing.status_code == 200, listing.text
            data = listing.json()["payload"]

    pairings = data["pairings"]
    assert any(p["publicId"] == public_id for p in pairings)
    for p in pairings:
        assert "token" not in str(p)
        assert "secret" not in str(p)


def test_pairing_create_binds_session_key_when_requested() -> None:
    with TestClient(_app(), client=_OWNER_PEER) as client:
        payload = _create_pairing(
            client,
            {"sessionKey": "agent:main:webchat:guest:abcd:one"},
        )
    assert "session=agent%3Amain%3Awebchat%3Aguest%3Aabcd%3Aone" in payload["pairingUrl"]
    assert payload["sessionKey"] == "agent:main:webchat:guest:abcd:one"


def test_pairing_host_execute_is_opt_in() -> None:
    with TestClient(_app(), client=_OWNER_PEER) as client:
        safe = _create_pairing(client)
        elevated = _create_pairing(client, {"allowHostExecute": True})

    assert safe["allowHostExecute"] is False
    assert elevated["allowHostExecute"] is True

    token = elevated["pairingUrl"].split("#token=", 1)[1]
    with TestClient(_app(), client=_OWNER_PEER) as client:
        ws, frame = _ws_handshake(client, token)
        _close_ws(ws)
    principal = frame["auth"]["principal"]
    assert "host.execute" in principal["capabilities"]


def test_pairing_list_hides_expired_tokens() -> None:
    def _list_pairings(client: TestClient) -> dict[str, object]:
        listing = client.post(
            "/api/v2/rpc",
            json={"method": "gateway.pairing.list", "params": {}},
        )
        if listing.status_code == 404:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                ws.send_json({
                    "type": "req", "id": "1", "method": "connect",
                    "params": {
                        "minProtocol": 3, "maxProtocol": 4,
                        "client": {"name": "test", "version": "1"},
                    },
                })
                ws.receive_json()
                ws.send_json({
                    "type": "req", "id": "2",
                    "method": "gateway.pairing.list",
                    "params": {},
                })
                frame = ws.receive_json()
                assert frame.get("type") == "res" and frame.get("ok") is True
                return frame["payload"]
        assert listing.status_code == 200, listing.text
        return listing.json()["payload"]

    with TestClient(_app(), client=_OWNER_PEER) as client:
        expired = _create_pairing(client, {"expiresInSeconds": 1})
        active = _create_pairing(client)
        time.sleep(1.1)
        data = _list_pairings(client)

    ids = {p["publicId"] for p in data["pairings"]}
    assert expired["publicId"] not in ids
    assert active["publicId"] in ids


def test_http_owner_gate_denies_tunnel_relayed_loopback_peer() -> None:
    """HTTP must agree with WS: a loopback peer carrying X-Forwarded-For is remote.

    The tunnel relay dials from 127.0.0.1; without the forwarded-for guard on
    the HTTP surface, owner-only routes would treat every relayed phone as the
    local owner while the WS surface correctly denies it.
    """

    with TestClient(_app(), client=_OWNER_PEER) as client:
        response = client.post(
            "/api/v1/artifacts/missing/open",
            headers={"X-Forwarded-For": "240e:469:ea14:55c2::1"},
        )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "OWNER_REQUIRED"


def test_token_mode_shared_token_not_owner_through_tunnel() -> None:
    """mode=token: a shared token over the tunnel relay must not gain owner."""

    config = GatewayConfig(auth=AuthConfig(mode="token", token="legacy-secret"))
    with TestClient(_app(config), client=_OWNER_PEER) as client:
        relayed = client.post(
            "/api/v1/artifacts/missing/open",
            headers={
                "Authorization": "Bearer legacy-secret",
                "X-Forwarded-For": "203.0.113.9",
            },
        )
        direct = client.post(
            "/api/v1/artifacts/missing/open",
            headers={"Authorization": "Bearer legacy-secret"},
        )

    assert relayed.status_code == 403, relayed.text
    # True loopback proximity still upgrades the shared token to owner; the
    # owner gate passes and the missing artifact surfaces as NOT_FOUND.
    assert direct.status_code == 404, direct.text
