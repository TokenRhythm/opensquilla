"""Artifact prompt annotations preserve the configured Ensemble execution."""

import pytest

from opensquilla.engine.runtime import _artifact_ensemble_bypass_reason
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.ensemble import build_ensemble_provider_from_config
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import ModelCapabilities


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


def test_artifact_ensemble_allows_unverified_tool_capability_for_aggregator() -> None:
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

    provider = build_ensemble_provider_from_config(
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

    assert provider.artifact_tool_executor_capabilities is not None
    assert provider.artifact_tool_executor_capabilities.supports_tools is True
    assert provider.artifact_tools_capability_verified is False
    assert provider.proposer_tools is False


def test_artifact_ensemble_honors_explicit_openrouter_tool_denial() -> None:
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "api_key": "synthetic-test-key",
            "base_url": "https://openrouter.ai/api/v1",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": [
                {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-pro",
                    "role": "primary",
                },
                {
                    "provider": "openrouter",
                    "model": "qwen/qwen3.6-flash",
                    "role": "contrast",
                },
                {
                    "provider": "openrouter",
                    "model": "z-ai/glm-5.2",
                    "role": "aggregator",
                },
            ],
        },
    )
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {"openrouter/z-ai/glm-5.2": {"supports_tools": False}}
    )

    with pytest.raises(
        ValueError,
        match="artifact_ensemble_unavailable:aggregator_tools_unsupported",
    ):
        build_ensemble_provider_from_config(
            config=config,
            inherited_provider_config=ProviderConfig(
                provider="openrouter",
                model="z-ai/glm-5.2",
                api_key="synthetic-test-key",
                base_url="https://openrouter.ai/api/v1",
            ),
            fallback_provider=None,
            _model_catalog=catalog,
            _artifact_mutation=True,
        )


class _ExplicitlyUnsupportedToolCatalog:
    def resolve_deployment_capabilities(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> ModelCapabilities:
        return ModelCapabilities(supports_tools=False)

    def deployment_tool_capability_is_verified(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> bool:
        return True


def test_artifact_ensemble_rejects_explicitly_unsupported_aggregator() -> None:
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
                    "model": "no-tool-fuser",
                    "role": "aggregator",
                },
            ],
        },
    )

    with pytest.raises(
        ValueError,
        match="artifact_ensemble_unavailable:aggregator_tools_unsupported",
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
            _model_catalog=_ExplicitlyUnsupportedToolCatalog(),
            _artifact_mutation=True,
        )
