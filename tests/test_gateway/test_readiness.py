from __future__ import annotations

from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.diagnostics import DiagnosticsState


def test_ready_endpoint_reports_starting_until_gateway_marks_ready() -> None:
    app = create_gateway_app(GatewayConfig())
    app.state.gateway_ready = False

    with TestClient(app) as client:
        starting = client.get("/ready")
        assert starting.status_code == 503
        assert starting.json()["ready"] is False

        app.state.gateway_ready = True
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True


def test_create_gateway_app_creates_default_diagnostics_state() -> None:
    app = create_gateway_app(GatewayConfig(diagnostics_enabled=True))

    assert isinstance(app.state.diagnostics_state, DiagnosticsState)
    assert app.state.diagnostics_state.snapshot().effective_enabled is True


def test_system_status_exposes_sandbox_upgrade_report() -> None:
    report = {
        "ok": False,
        "status": "partial_commit",
        "committedStores": ["config.toml"],
    }
    app = create_gateway_app(GatewayConfig(), sandbox_upgrade_report=report)

    with TestClient(app) as client:
        response = client.get("/api/system/status")

    assert response.status_code == 200
    assert response.json()["sandboxUpgrade"] == report
