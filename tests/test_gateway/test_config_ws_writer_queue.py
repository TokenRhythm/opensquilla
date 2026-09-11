"""Tests for OPENSQUILLA_WS_WRITER_QUEUE_ENABLED and
OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE env overrides.

Mirrors the pattern in test_config_concurrency.py.
"""
from __future__ import annotations

import logging

import pytest

from opensquilla.gateway.config import GatewayConfig

# The transport-flow switch shares the same explicit runtime environment
# override path as the writer queue. Keep these default/kill-switch checks in
# this historically weighted configuration file so CI shard assignment remains
# balanced when the default changes.


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

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """ws_writer_queue_enabled defaults to True; maxsize defaults to 512.

    Robust to external env state (the test suite may be invoked with the
    kill switch flipped, e.g. for the parity sweep — that is not the
    default-state test).
    """
    monkeypatch.delenv("OPENSQUILLA_WS_WRITER_QUEUE_ENABLED", raising=False)
    monkeypatch.delenv("OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE", raising=False)
    config = GatewayConfig()
    assert config.ws_writer_queue_enabled is True
    assert config.ws_writer_queue_maxsize == 512


# ---------------------------------------------------------------------------
# Boolean env override
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "Yes", "YES"])
def test_enabled_env_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("OPENSQUILLA_WS_WRITER_QUEUE_ENABLED", value)
    config = GatewayConfig()
    assert config.ws_writer_queue_enabled is True


@pytest.mark.parametrize("value", ["false", "0", "no", "FALSE", "No", "NO"])
def test_enabled_env_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("OPENSQUILLA_WS_WRITER_QUEUE_ENABLED", value)
    config = GatewayConfig()
    assert config.ws_writer_queue_enabled is False


def test_enabled_env_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Invalid bool string keeps default (True) and emits warning."""
    monkeypatch.setenv("OPENSQUILLA_WS_WRITER_QUEUE_ENABLED", "maybe")
    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.ws_writer_queue_enabled is True  # default preserved
    assert any(
        "OPENSQUILLA_WS_WRITER_QUEUE_ENABLED" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


# ---------------------------------------------------------------------------
# Integer env override
# ---------------------------------------------------------------------------


def test_maxsize_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE", "1024")
    config = GatewayConfig()
    assert config.ws_writer_queue_maxsize == 1024


def test_maxsize_env_invalid_int_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-integer maxsize keeps default 512 with warning."""
    monkeypatch.setenv("OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE", "abc")
    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.ws_writer_queue_maxsize == 512
    assert any(
        "OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_maxsize_env_below_minimum_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """maxsize below 16 falls back to default 512 with warning."""
    monkeypatch.setenv("OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE", "8")
    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.ws_writer_queue_maxsize == 512
    assert any(
        "OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_maxsize_env_zero_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE", "0")
    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.ws_writer_queue_maxsize == 512


def test_maxsize_env_negative_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE", "-5")
    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.ws_writer_queue_maxsize == 512


def test_maxsize_env_at_minimum_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """maxsize=16 is exactly the minimum and must be accepted."""
    monkeypatch.setenv("OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE", "16")
    config = GatewayConfig()
    assert config.ws_writer_queue_maxsize == 16
