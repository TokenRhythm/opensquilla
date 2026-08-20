"""Artifact prompt annotations preserve the configured Ensemble execution."""

import pytest

from opensquilla.engine.runtime import _artifact_ensemble_bypass_reason
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.ensemble import build_ensemble_provider_from_config
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.selector import ProviderConfig


def test_source_backed_artifact_mutations_keep_ensemble() -> None:
    for operation in (
        "selection_edit",
        "structural_edit",
        "conflict_recovery",
    ):
        assert _artifact_ensemble_bypass_reason({"artifact_operation_class": operation}) is None


def test_browser_use_still_bypasses_ensemble_until_multimodal_support_exists() -> None:
    assert (
        _artifact_ensemble_bypass_reason({"artifact_operation_class": "browser_use"})
        == "artifact_browser_use"
    )


def test_open_and_unbound_turns_keep_existing_ensemble_behavior() -> None:
    assert _artifact_ensemble_bypass_reason({"artifact_operation_class": "open"}) is None
    assert _artifact_ensemble_bypass_reason({}) is None
    assert _artifact_ensemble_bypass_reason(None) is None


def test_artifact_ensemble_forces_aggregator_only_failure_policy() -> None:
    config = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "deepseek-v4-pro",
            "api_key": "synthetic-test-key",
            "base_url": "https://tokenrhythm.studio/v1",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_tokenrhythm_b5",
            "all_failed_policy": "fallback_single",
            "proposer_tools": True,
        },
    )
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=ProviderConfig(
            provider="tokenrhythm",
            model="deepseek-v4-pro",
            api_key="synthetic-test-key",
            base_url="https://tokenrhythm.studio/v1",
        ),
        fallback_provider=object(),  # type: ignore[arg-type]
        _model_catalog=ModelCatalog(),
        _artifact_mutation=True,
    )

    assert provider.proposer_tools is False
    assert provider.all_failed_policy == "error"
    assert provider.fallback_provider is None
    assert provider.selection_plan["artifact_execution_policy"] == "aggregator_only"


def test_artifact_ensemble_rejects_unverified_aggregator_before_provider() -> None:
    config = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "deepseek-v4-pro",
            "api_key": "synthetic-test-key",
            "base_url": "https://tokenrhythm.studio/v1",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": [
                {
                    "provider": "tokenrhythm",
                    "model": "deepseek-v4-pro",
                    "role": "primary",
                },
                {
                    "provider": "tokenrhythm",
                    "model": "glm-5.2",
                    "role": "contrast",
                },
                {
                    "provider": "tokenrhythm",
                    "model": "unverified-fuser",
                    "role": "aggregator",
                },
            ],
        },
    )

    with pytest.raises(
        ValueError,
        match="artifact_ensemble_unavailable:aggregator_tools_unverified",
    ):
        build_ensemble_provider_from_config(
            config=config,
            inherited_provider_config=ProviderConfig(
                provider="tokenrhythm",
                model="deepseek-v4-pro",
                api_key="synthetic-test-key",
                base_url="https://tokenrhythm.studio/v1",
            ),
            fallback_provider=None,
            _model_catalog=ModelCatalog(),
            _artifact_mutation=True,
        )
