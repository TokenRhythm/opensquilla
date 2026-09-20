from __future__ import annotations

import logging

import pytest

from opensquilla.gateway import config as config_module
from opensquilla.gateway.config import CompactionLlmConfig, GatewayConfig


def test_compaction_explicit_deployment_preserves_provider_model_pair() -> None:
    config = CompactionLlmConfig(provider=" anthropic ", model=" claude-test ")

    assert config.provider == "anthropic"
    assert config.model == "claude-test"


def test_compaction_model_only_remains_backwards_compatible() -> None:
    config = CompactionLlmConfig(model="session-provider-model")

    assert config.provider is None
    assert config.model == "session-provider-model"


def test_compaction_provider_only_falls_back_without_blocking_boot(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        config = CompactionLlmConfig(provider="openai")

    assert config.provider is None
    assert config.model is None
    assert "compaction.model is not set" in caplog.text


def test_legacy_context_budget_default_is_quiet_and_schema_is_deprecated(
    monkeypatch, caplog,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONTEXT_BUDGET_TOKENS", raising=False)
    monkeypatch.setattr(config_module, "_CONTEXT_BUDGET_WARNING_EMITTED", False)
    with caplog.at_level(logging.WARNING):
        config = GatewayConfig()
    assert config.context_budget_tokens == 100_000
    assert "context_budget_tokens" not in caplog.text
    assert GatewayConfig.model_json_schema()["properties"]["context_budget_tokens"]["deprecated"]


@pytest.mark.parametrize("source", ["constructor", "toml", "environment"])
def test_explicit_legacy_context_budget_is_readable_and_warns_once(
    monkeypatch, caplog, tmp_path, source,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONTEXT_BUDGET_TOKENS", raising=False)
    monkeypatch.setattr(config_module, "_CONTEXT_BUDGET_WARNING_EMITTED", False)
    if source == "environment":
        monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONTEXT_BUDGET_TOKENS", "777")
        load = GatewayConfig
    elif source == "toml":
        path = tmp_path / "config.toml"
        path.write_text("context_budget_tokens = 777\n", encoding="utf-8")
        def load():
            return GatewayConfig.load(path, read_only=True)
    else:
        def load():
            return GatewayConfig(context_budget_tokens=777)
    with caplog.at_level(logging.WARNING):
        first, second = load(), load()
    assert first.context_budget_tokens == second.context_budget_tokens == 777
    assert first.to_toml_dict()["context_budget_tokens"] == 777
    assert sum("context_budget_tokens is deprecated" in row.message for row in caplog.records) == 1
