from __future__ import annotations

import logging

from opensquilla.gateway.config import CompactionLlmConfig


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
