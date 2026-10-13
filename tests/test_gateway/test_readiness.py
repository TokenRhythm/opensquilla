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


def test_core_ready_endpoint_is_separate_from_optional_services() -> None:
    app = create_gateway_app(GatewayConfig())
    app.state.core_ready = False
    app.state.optional_services = {
        "channels": {"status": "starting", "generation": 1},
        "mcp": {"status": "starting", "generation": 1},
    }

    with TestClient(app) as client:
        starting = client.get("/readyz/core")
        assert starting.status_code == 503
        assert starting.json()["ready"] is False
        assert starting.json()["services"]["mcp"]["status"] == "starting"

        app.state.core_ready = True
        app.state.optional_services["mcp"] = {
            "status": "degraded",
            "generation": 1,
        }
        ready = client.get("/readyz/core")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True
        assert ready.json()["services"]["mcp"]["status"] == "degraded"


def test_core_ready_payload_is_bounded_when_optional_services_are_large() -> None:
    app = create_gateway_app(GatewayConfig())
    app.state.core_ready = True
    app.state.optional_services = {
        f"service-{index}-" + ("x" * 80): {
            "status": "degraded",
            "generation": index,
            "diagnostics": "y" * 100_000,
        }
        for index in range(200)
    }

    with TestClient(app) as client:
        response = client.get("/readyz/core")

    assert response.status_code == 200
    assert len(response.content) <= 2048
    assert response.json()["ready"] is True
    assert response.json()["services"]["truncated"] is True


def test_legacy_ready_waits_for_aggregate_services_after_core_ready() -> None:
    app = create_gateway_app(GatewayConfig())
    app.state.gateway_ready = True
    app.state.core_ready = True
    app.state.legacy_ready = False

    with TestClient(app) as client:
        pending = client.get("/readyz")
        assert pending.status_code == 503
        assert pending.json()["ready"] is False

        app.state.legacy_ready = True
        ready = client.get("/readyz")
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
