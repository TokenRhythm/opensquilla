"""Tests for the OPENSQUILLA_TASK_MAX_CONCURRENCY and
OPENSQUILLA_CHANNEL_INFLIGHT_CAP env overrides.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from opensquilla.gateway.config import GatewayConfig


def test_fresh_install_task_concurrency_defaults_to_eight() -> None:
    assert GatewayConfig().task_runtime.max_concurrency == 8


def test_generated_config_example_uses_desktop_default_eight() -> None:
    example = Path(__file__).resolve().parents[2] / "opensquilla.toml.example"
    payload = tomllib.loads(example.read_text(encoding="utf-8"))
    assert payload["task_runtime"]["max_concurrency"] == 8


def test_task_max_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """OPENSQUILLA_TASK_MAX_CONCURRENCY=16 sets task_runtime.max_concurrency to 16."""
    monkeypatch.setenv("OPENSQUILLA_TASK_MAX_CONCURRENCY", "16")
    config = GatewayConfig()
    assert config.task_runtime.max_concurrency == 16


def test_invalid_env_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Invalid task concurrency falls back to the fresh-install default 8."""
    monkeypatch.setenv("OPENSQUILLA_TASK_MAX_CONCURRENCY", "abc")
    import logging

    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.task_runtime.max_concurrency == 8
    assert any(
        "OPENSQUILLA_TASK_MAX_CONCURRENCY" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_channel_inflight_cap_default() -> None:
    """channel_inflight_cap defaults to 8 when env is not set."""
    config = GatewayConfig()
    assert config.task_runtime.channel_inflight_cap == 8


def test_channel_inflight_cap_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """OPENSQUILLA_CHANNEL_INFLIGHT_CAP=12 sets task_runtime.channel_inflight_cap to 12."""
    monkeypatch.setenv("OPENSQUILLA_CHANNEL_INFLIGHT_CAP", "12")
    config = GatewayConfig()
    assert config.task_runtime.channel_inflight_cap == 12


def test_channel_inflight_cap_invalid_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-integer OPENSQUILLA_CHANNEL_INFLIGHT_CAP falls back to default 8 with a warning."""
    monkeypatch.setenv("OPENSQUILLA_CHANNEL_INFLIGHT_CAP", "bad")
    import logging

    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.task_runtime.channel_inflight_cap == 8
    assert any(
        "OPENSQUILLA_CHANNEL_INFLIGHT_CAP" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_zero_env_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OPENSQUILLA_TASK_MAX_CONCURRENCY=0 falls back to default 8 with a warning."""
    monkeypatch.setenv("OPENSQUILLA_TASK_MAX_CONCURRENCY", "0")
    import logging

    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.task_runtime.max_concurrency == 8
    assert any(
        "OPENSQUILLA_TASK_MAX_CONCURRENCY" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_negative_env_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OPENSQUILLA_TASK_MAX_CONCURRENCY=-5 falls back to default 8 with a warning."""
    monkeypatch.setenv("OPENSQUILLA_TASK_MAX_CONCURRENCY", "-5")
    import logging

    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.task_runtime.max_concurrency == 8
    assert any(
        "OPENSQUILLA_TASK_MAX_CONCURRENCY" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


def test_explicit_legacy_task_concurrency_is_preserved() -> None:
    """Existing users who explicitly pinned four slots are not migrated."""

    config = GatewayConfig(task_runtime={"max_concurrency": 4})
    assert config.task_runtime.max_concurrency == 4


def test_explicit_legacy_task_concurrency_env_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy four-slot environment override remains authoritative."""

    monkeypatch.setenv("OPENSQUILLA_TASK_MAX_CONCURRENCY", "4")
    assert GatewayConfig().task_runtime.max_concurrency == 4


def test_channel_zero_env_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-M5: OPENSQUILLA_CHANNEL_INFLIGHT_CAP=0 falls back to default 8 with a warning."""
    monkeypatch.setenv("OPENSQUILLA_CHANNEL_INFLIGHT_CAP", "0")
    import logging

    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()

    assert config.task_runtime.channel_inflight_cap == 8
    assert any(
        "OPENSQUILLA_CHANNEL_INFLIGHT_CAP" in record.message
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )
