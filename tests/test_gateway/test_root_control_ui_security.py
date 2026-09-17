from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import AuthConfig, ControlUiConfig, GatewayConfig


def _config(tmp_path: Path) -> GatewayConfig:
    config = GatewayConfig(
        auth=AuthConfig(mode="token", token="secret"),
        control_ui=ControlUiConfig(base_path="/"),
    )
    config.attachments.media_root = str(tmp_path / "media")
    return config


def test_root_mounted_ui_keeps_registered_api_and_preview_controls_protected(
    tmp_path: Path,
) -> None:
    app = create_gateway_app(_config(tmp_path))

    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        ui = client.get("/")
        api_without_token = client.get("/api/config")
        api_with_token = client.get(
            "/api/config",
            headers={"Authorization": "Bearer secret"},
        )
        lease_without_token = client.post(
            "/api/v1/artifacts/missing/preview-leases",
            json={"version": 1, "mode": "offline", "client": "web"},
        )
        lease_with_token = client.post(
            "/api/v1/artifacts/missing/preview-leases",
            json={"version": 1, "mode": "offline", "client": "web"},
            headers={
                "Authorization": "Bearer secret",
                "Origin": "http://127.0.0.1:18791",
                "x-opensquilla-session-key": "agent:main:webchat:missing",
            },
        )
        health = client.get("/health")

    assert ui.status_code == 200
    assert ui.headers["content-type"].startswith("text/html")
    assert "content-security-policy" in ui.headers

    assert api_without_token.status_code == 401
    assert api_with_token.status_code != 401
    assert api_with_token.headers["content-type"].startswith("application/json")
    assert "content-security-policy" not in api_with_token.headers

    assert lease_without_token.status_code == 401
    assert lease_with_token.status_code == 404
    assert lease_with_token.json()["code"] == "NOT_FOUND"
    assert "content-security-policy" not in lease_with_token.headers

    assert health.status_code == 200
    assert "content-security-policy" not in health.headers
