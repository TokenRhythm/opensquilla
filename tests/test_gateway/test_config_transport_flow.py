"""Tests for the default-on WebSocket transport flow switch."""

from __future__ import annotations

import logging

import pytest

from opensquilla.gateway.config import GatewayConfig


def test_transport_flow_defaults_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_WS_TRANSPORT_FLOW_ENABLED", raising=False)
    assert GatewayConfig().ws_transport_flow_enabled is True


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "Yes"])
def test_transport_flow_env_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_WS_TRANSPORT_FLOW_ENABLED", value)
    assert GatewayConfig().ws_transport_flow_enabled is True


@pytest.mark.parametrize("value", ["false", "0", "no", "FALSE", "No"])
def test_transport_flow_env_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_WS_TRANSPORT_FLOW_ENABLED", value)
    assert GatewayConfig().ws_transport_flow_enabled is False


def test_transport_flow_env_invalid_keeps_enabled_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_WS_TRANSPORT_FLOW_ENABLED", "maybe")
    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.ws_transport_flow_enabled is True
    assert any(
        "OPENSQUILLA_GATEWAY_WS_TRANSPORT_FLOW_ENABLED" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )
