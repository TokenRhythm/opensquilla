"""Same-origin guard on state-changing routes + CORS-off-by-default posture.

A hostile page can make a loopback victim's browser fire simple POSTs at the
gateway; those execute server-side even when the browser withholds the
response. Every state-changing HTTP route must therefore reject requests
whose ``Origin`` differs from the gateway itself (403 FORBIDDEN_ORIGIN),
while same-origin Web UI requests and Origin-less non-browser clients (curl,
the desktop app's Node fetch) keep working. CORS response headers are off by
default and only appear for origins the operator explicitly configured.

TestClient's default synthetic peer is not loopback, so owner-gated routes
are exercised from an explicit loopback peer, mirroring
tests/test_gateway/test_shutdown_endpoint.py. An autouse fixture pins config
resolution to a synthetic TOML so no test reads the developer's real config.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog.testing
from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig

# A loopback peer on a loopback-bound, no-auth gateway is the proven owner
# (see gateway.auth.OpenScopeResolver).
_OWNER_PEER = ("127.0.0.1", 51000)

_FOREIGN_ORIGIN = "https://evil.example"
# TestClient requests carry base URL http://testserver, so this is same-origin.
_SAME_ORIGIN = "http://testserver"

# Every state-changing HTTP route behind the shared guard. Bodies are the
# minimal synthetic payloads that get each handler past input validation far
# enough to prove the request was NOT rejected for its Origin. The expected
# same-origin statuses come from running against a bare app (no session
# manager, no channels, audio disabled) — none of them is 403.
_PROTECTED_ENDPOINTS = [
    pytest.param("/api/chat", {"sessionKey": "s", "message": "hi"}, id="chat"),
    pytest.param("/api/system/shutdown", None, id="shutdown"),
    pytest.param("/api/channels/logout", {"channel": "nope"}, id="channels-logout"),
    pytest.param(
        "/api/channels/pairings/approve",
        {"channelName": "nope", "pairingId": "missing"},
        id="channel-pairing-approve",
    ),
    pytest.param(
        "/api/channels/pairings/revoke",
        {"channelName": "nope", "pairingId": "missing"},
        id="channel-pairing-revoke",
    ),
    pytest.param("/api/approvals/settings", {"mode": "prompt"}, id="approvals-settings"),
    pytest.param(
        "/api/approvals/resolve", {"id": "missing", "approved": False}, id="approvals-resolve"
    ),
    pytest.param(
        "/api/elevated-mode", {"sessionKey": "agent:main:test", "mode": "off"}, id="elevated-mode"
    ),
    pytest.param("/api/v1/files/upload", None, id="upload"),
    pytest.param("/api/audio/transcribe", None, id="audio-transcribe"),
    pytest.param("/api/v1/artifacts/a-missing/open", {}, id="artifact-open"),
    pytest.param("/api/v1/diagnostics/bundle", {}, id="diagnostics-bundle"),
]


@pytest.fixture(autouse=True)
def _hermetic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "synthetic-config.toml"
    config_path.write_text("# synthetic origin-guard-test config\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_path))
    home = tmp_path / "state"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "debug.log").write_text("2026-07-08 [INFO] opensquilla: ok\n")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(home / "logs"))


def _client(config: GatewayConfig | None = None) -> TestClient:
    return TestClient(create_gateway_app(config or GatewayConfig()), client=_OWNER_PEER)


def _post(client: TestClient, path: str, body: dict | None, origin: str | None):
    headers = {"Origin": origin} if origin is not None else {}
    if body is None:
        return client.post(path, headers=headers)
    return client.post(path, json=body, headers=headers)


# ── Per-endpoint guard behavior ─────────────────────────────────────────────


@pytest.mark.parametrize(("path", "body"), _PROTECTED_ENDPOINTS)
def test_foreign_origin_is_rejected(path: str, body: dict | None) -> None:
    with _client() as client:
        response = _post(client, path, body, _FOREIGN_ORIGIN)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "FORBIDDEN_ORIGIN"


@pytest.mark.parametrize(("path", "body"), _PROTECTED_ENDPOINTS)
def test_same_origin_passes_the_guard(path: str, body: dict | None) -> None:
    # The gateway-served Web UI always sends its own origin; it must never
    # trip the guard. Handlers may still fail later for unrelated reasons
    # (missing session manager, audio disabled, unknown ids) but never with
    # the guard's marker.
    with _client() as client:
        response = _post(client, path, body, _SAME_ORIGIN)

    payload = response.json() if response.headers.get("content-type", "").startswith(
        "application/json"
    ) else {}
    assert payload.get("code") != "FORBIDDEN_ORIGIN", response.text


@pytest.mark.parametrize(("path", "body"), _PROTECTED_ENDPOINTS)
def test_absent_origin_passes_the_guard(path: str, body: dict | None) -> None:
    # curl and the desktop client's Node fetch send no Origin header; they are
    # not browser-mediated and must keep working.
    with _client() as client:
        response = _post(client, path, body, None)

    payload = response.json() if response.headers.get("content-type", "").startswith(
        "application/json"
    ) else {}
    assert payload.get("code") != "FORBIDDEN_ORIGIN", response.text


def test_foreign_origin_never_reaches_the_shutdown_trigger() -> None:
    # End-to-end CSRF proof on the most destructive route: the drain trigger
    # must not fire for a cross-origin request, and must fire for the
    # same-origin Web UI and for an Origin-less client.
    calls: list[str] = []
    app = create_gateway_app(GatewayConfig())
    app.state.request_shutdown = calls.append

    with TestClient(app, client=_OWNER_PEER) as client:
        forbidden = client.post("/api/system/shutdown", headers={"Origin": _FOREIGN_ORIGIN})
        assert forbidden.status_code == 403
        assert calls == []

        same = client.post("/api/system/shutdown", headers={"Origin": _SAME_ORIGIN})
        assert same.status_code == 202
        no_origin = client.post("/api/system/shutdown")
        assert no_origin.status_code == 202
    assert calls == ["api_shutdown", "api_shutdown"]


def test_configured_extra_origin_passes_the_guard() -> None:
    # An operator hosting a separate frontend lists its origin explicitly;
    # the guard honors that deliberate choice.
    config = GatewayConfig()
    config.cors.allowed_origins = ["https://frontend.example"]
    with _client(config) as client:
        response = client.post(
            "/api/approvals/settings",
            json={"mode": "prompt"},
            headers={"Origin": "https://frontend.example"},
        )

    assert response.status_code == 200, response.text


def test_wildcard_origin_does_not_bypass_the_guard() -> None:
    # "*" widens CORS response headers only; it must never turn off the
    # same-origin check, or the drive-by exposure returns.
    config = GatewayConfig()
    config.cors.allowed_origins = ["*"]
    with _client(config) as client:
        response = client.post(
            "/api/approvals/settings",
            json={"mode": "prompt"},
            headers={"Origin": _FOREIGN_ORIGIN},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_ORIGIN"


# ── CORS headers off by default, opt-in per origin ──────────────────────────


def test_default_config_emits_no_cors_headers() -> None:
    with _client() as client:
        response = client.get("/api/config", headers={"Origin": _FOREIGN_ORIGIN})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_explicitly_configured_origin_still_gets_cors_headers() -> None:
    # Upgrade compatibility: users who pinned origins in their TOML keep a
    # working cross-origin deployment.
    config = GatewayConfig()
    config.cors.allowed_origins = ["https://frontend.example"]
    with _client(config) as client:
        allowed = client.get("/api/config", headers={"Origin": "https://frontend.example"})
        foreign = client.get("/api/config", headers={"Origin": _FOREIGN_ORIGIN})

    assert allowed.headers.get("access-control-allow-origin") == "https://frontend.example"
    assert "access-control-allow-origin" not in foreign.headers


# ── Boot-time wildcard warning ───────────────────────────────────────────────


def test_wildcard_with_credentials_warns_once_at_boot() -> None:
    config = GatewayConfig()
    config.cors.allowed_origins = ["*"]
    config.cors.allow_credentials = True

    with structlog.testing.capture_logs() as captured:
        create_gateway_app(config)

    events = [e for e in captured if e["event"] == "gateway.cors_wildcard_with_credentials"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"


def test_no_wildcard_warning_for_default_or_explicit_origins() -> None:
    explicit = GatewayConfig()
    explicit.cors.allowed_origins = ["https://frontend.example"]
    wildcard_without_credentials = GatewayConfig()
    wildcard_without_credentials.cors.allowed_origins = ["*"]
    wildcard_without_credentials.cors.allow_credentials = False

    with structlog.testing.capture_logs() as captured:
        create_gateway_app(GatewayConfig())
        create_gateway_app(explicit)
        create_gateway_app(wildcard_without_credentials)

    assert not [e for e in captured if e["event"] == "gateway.cors_wildcard_with_credentials"]
