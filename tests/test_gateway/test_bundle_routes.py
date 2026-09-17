"""POST /api/v1/diagnostics/bundle: owner-gated zip download.

TestClient's default synthetic peer ("testclient") is not a loopback
address, so owner tests connect from an explicit loopback peer, mirroring
tests/test_gateway/test_shutdown_endpoint.py. An autouse fixture pins
config resolution to a synthetic TOML so no test reads or rewrites the
developer's real config (the doctor collector's migration path could).
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig

# A loopback peer on a loopback-bound, no-auth gateway is the proven owner;
# a non-loopback peer is not (see gateway.auth.OpenScopeResolver).
_OWNER_PEER = ("127.0.0.1", 51000)
_REMOTE_PEER = ("203.0.113.7", 51000)


@pytest.fixture(autouse=True)
def _hermetic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "synthetic-config.toml"
    config_path.write_text("# synthetic bundle-route-test config\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_path))


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    peer: tuple[str, int] = _OWNER_PEER,
) -> TestClient:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "debug.log").write_text("2026-07-07 [INFO] opensquilla: ok\n")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(home / "logs"))
    return TestClient(
        create_gateway_app(GatewayConfig()),
        base_url="http://127.0.0.1:18791",
        client=peer,
    )


def _track_mkdtemp(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record the bundle temp dirs the route creates so cleanup can be asserted."""
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def _tracking(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]
        if Path(path).name.startswith("opensquilla-bundle-"):
            created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", _tracking)
    return created


def test_bundle_route_returns_zip_for_loopback_owner(monkeypatch, tmp_path) -> None:
    with _client(monkeypatch, tmp_path) as client:
        response = client.post("/api/v1/diagnostics/bundle", json={})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers.get("content-disposition", "")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "manifest.json" in archive.namelist()


def test_bundle_route_honors_include_content(monkeypatch, tmp_path) -> None:
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/v1/diagnostics/bundle", json={"include_content": True}
        )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["content_tier"] is True


def test_bundle_route_include_content_requires_json_true(monkeypatch, tmp_path) -> None:
    # A JSON string like "false" is truthy in Python; it must NOT enable the
    # conversation-content tier. Only JSON `true` may.
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/v1/diagnostics/bundle", json={"include_content": "false"}
        )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["content_tier"] is False


def test_bundle_route_defaults_to_metadata_tier(monkeypatch, tmp_path) -> None:
    # include_content must come from an explicit JSON body key, never default on.
    with _client(monkeypatch, tmp_path) as client:
        response = client.post("/api/v1/diagnostics/bundle")

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["content_tier"] is False


def test_bundle_route_clamps_days(monkeypatch, tmp_path) -> None:
    with _client(monkeypatch, tmp_path) as client:
        high = client.post("/api/v1/diagnostics/bundle", json={"days": 999})
        low = client.post("/api/v1/diagnostics/bundle", json={"days": 0})
        garbage = client.post("/api/v1/diagnostics/bundle", json={"days": "soon"})

    for response, expected in ((high, 30), (low, 1), (garbage, 3)):
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        assert manifest["days"] == expected


def test_bundle_route_non_finite_days_uses_default(monkeypatch, tmp_path) -> None:
    # Strict JSON has no NaN literal, so TestClient's json= kwarg can't send
    # it; post the raw body Python's lenient json.loads still accepts.
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/v1/diagnostics/bundle",
            content='{"days": NaN}',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["days"] == 3


def test_bundle_route_rejects_cross_origin_browser_request(monkeypatch, tmp_path) -> None:
    # Drive-by exfiltration guard: a hostile page's fetch always carries its
    # Origin. Even though the victim's browser connects from loopback (and so
    # resolves as owner in open mode), the request must be refused before the
    # bundle is ever generated.
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/v1/diagnostics/bundle",
            json={"include_content": True},
            headers={"Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    payload = response.json()
    assert payload["code"] == "FORBIDDEN_ORIGIN"
    assert payload["error"]


def test_bundle_route_rejects_same_host_different_port_origin(monkeypatch, tmp_path) -> None:
    # Same hostname on another port is a different web origin.
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/v1/diagnostics/bundle",
            json={},
            headers={"Origin": "http://127.0.0.1:8080"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_ORIGIN"


def test_bundle_route_allows_same_origin_request(monkeypatch, tmp_path) -> None:
    # The Web UI is served by the gateway itself, so its Origin matches the
    # request's own host and must keep working.
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/v1/diagnostics/bundle",
            json={},
            headers={"Origin": "http://127.0.0.1:18791"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"


def test_bundle_route_rejects_non_owner(monkeypatch, tmp_path) -> None:
    import opensquilla.gateway.bundle_routes as bundle_routes

    monkeypatch.setattr(
        bundle_routes, "_request_principal_is_owner", lambda config, request: False
    )
    with _client(monkeypatch, tmp_path) as client:
        response = client.post("/api/v1/diagnostics/bundle", json={})

    assert response.status_code == 403
    payload = response.json()
    assert payload["code"]
    assert payload["error"]


def test_bundle_route_rejects_remote_peer(monkeypatch, tmp_path) -> None:
    # End-to-end: open mode grants owner only to loopback peers.
    with _client(monkeypatch, tmp_path, peer=_REMOTE_PEER) as client:
        response = client.post("/api/v1/diagnostics/bundle", json={})

    assert response.status_code == 403


def test_bundle_route_cleans_temp_after_serve(monkeypatch, tmp_path) -> None:
    created = _track_mkdtemp(monkeypatch)
    with _client(monkeypatch, tmp_path) as client:
        response = client.post("/api/v1/diagnostics/bundle", json={})

    assert response.status_code == 200
    assert created, "route did not create a temp dir"
    # TestClient runs the BackgroundTask before returning the response.
    for path in created:
        assert not path.exists()


def test_bundle_route_generation_failure_returns_500_and_cleans_temp(
    monkeypatch, tmp_path
) -> None:
    import opensquilla.observability.bundle as bundle_mod

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret /srv/fake-opensquilla/state detail")

    monkeypatch.setattr(bundle_mod, "collect_bundle", _boom)
    created = _track_mkdtemp(monkeypatch)
    with _client(monkeypatch, tmp_path) as client:
        response = client.post("/api/v1/diagnostics/bundle", json={})

    assert response.status_code == 500
    payload = response.json()
    assert payload["code"] == "INTERNAL_ERROR"
    # Never leak exception details or a traceback into the response body.
    assert "secret" not in response.text
    assert "Traceback" not in response.text
    assert created, "route did not create a temp dir"
    for path in created:
        assert not path.exists()


def test_bundle_route_rejects_get(monkeypatch, tmp_path) -> None:
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/v1/diagnostics/bundle")

    assert response.status_code == 405
