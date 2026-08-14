"""Typed router-tier contract: TierConfig, misroute detection, override helper."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.engine.capacity_admission import (
    LargeContextCapacityError,
    model_has_request_capacity,
)
from opensquilla.engine.selector_override import apply_model_override
from opensquilla.engine.steps.squilla_router import _flag_tier_provider_mismatch
from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.mutations import _cross_provider_tier_warnings, upsert_router
from opensquilla.provider.model_catalog import DeploymentModelLimits, ModelCatalog
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.router_tiers import TierConfig

# ---------------------------------------------------------------------------
# TierConfig
# ---------------------------------------------------------------------------


def test_tier_config_from_dict() -> None:
    tier = TierConfig.from_value(
        {
            "provider": " openrouter ",
            "model": "deepseek/deepseek-v4-flash",
            "thinking_level": "low",
            "supports_image": True,
        }
    )
    assert tier.provider == "openrouter"
    assert tier.model == "deepseek/deepseek-v4-flash"
    assert tier.thinking_level == "low"
    assert tier.supports_image is True
    assert tier.image_only is False


def test_tier_config_from_object_and_none() -> None:
    obj = SimpleNamespace(provider="openai", model="gpt-5.4-nano", image_only=True)
    tier = TierConfig.from_value(obj)
    assert tier.provider == "openai"
    assert tier.image_only is True
    assert TierConfig.from_value(None) == TierConfig()
    assert TierConfig.from_value({}) == TierConfig()


# ---------------------------------------------------------------------------
# Tier provider mismatch detection
# ---------------------------------------------------------------------------


def _ctx(active_provider: str) -> SimpleNamespace:
    return SimpleNamespace(
        metadata={},
        config=SimpleNamespace(llm=SimpleNamespace(provider=active_provider)),
        session_key="s1",
    )


def test_mismatched_tier_provider_is_flagged() -> None:
    ctx = _ctx("openrouter")
    tiers = {"c2": {"provider": "openai", "model": "gpt-5.5"}}
    _flag_tier_provider_mismatch(ctx, tiers, "c2", routing_applied=True)
    assert ctx.metadata["router_tier_provider_mismatch"] == "openai"


def test_matching_tier_provider_is_not_flagged() -> None:
    ctx = _ctx("openrouter")
    tiers = {"c2": {"provider": "openrouter", "model": "z-ai/glm-5.1"}}
    _flag_tier_provider_mismatch(ctx, tiers, "c2", routing_applied=True)
    assert "router_tier_provider_mismatch" not in ctx.metadata


def test_observe_phase_does_not_flag() -> None:
    ctx = _ctx("openrouter")
    tiers = {"c2": {"provider": "openai", "model": "gpt-5.5"}}
    _flag_tier_provider_mismatch(ctx, tiers, "c2", routing_applied=False)
    assert "router_tier_provider_mismatch" not in ctx.metadata


# ---------------------------------------------------------------------------
# Shared selector-override helper
# ---------------------------------------------------------------------------


class _StubSelector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def override_model(self, model: str) -> None:
        self.calls.append(("override_model", model))

    def override_model_with_fallback_chain(self, model: str, chain: list) -> None:
        self.calls.append(("override_with_chain", (model, chain)))

    def override_model_with_bounded_fallback_chain(
        self,
        model: str,
        chain: list,
        approved_configured_fallbacks: list | None = None,
    ) -> None:
        self.calls.append(
            (
                "override_with_bounded_chain",
                (model, chain, approved_configured_fallbacks),
            )
        )

    def resolve(self) -> object:
        return "provider-sentinel"


def test_override_uses_fallback_chain_when_routing_applied() -> None:
    selector = _StubSelector()
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [{"tier": "c0", "model": "cheap"}],
        "routed_model": "routed",
    }
    provider = apply_model_override(
        selector, "routed", turn_metadata=metadata, realign_routed_model=False
    )
    assert provider == "provider-sentinel"
    assert selector.calls[0][0] == "override_with_chain"
    assert metadata["routed_model"] == "routed"


def test_override_without_routing_uses_plain_override() -> None:
    selector = _StubSelector()
    metadata = {"routing_applied": False, "routed_model": "would-be-routed"}
    apply_model_override(
        selector, "baseline", turn_metadata=metadata, realign_routed_model=False
    )
    assert selector.calls[0][0] == "override_model"
    # Observe phase: routed_model intentionally keeps the would-be choice.
    assert metadata["routed_model"] == "would-be-routed"


def test_large_context_floor_uses_capacity_bounded_selector_chain() -> None:
    selector = _StubSelector()
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [{"tier": "c2", "model": "capacity-safe"}],
        "large_context_floor_min_tier": "c2",
        "routed_model": "routed",
    }

    apply_model_override(
        selector,
        "routed",
        turn_metadata=metadata,
        realign_routed_model=False,
    )

    assert selector.calls[0][0] == "override_with_bounded_chain"


def test_large_context_capacity_block_stops_selector_execution() -> None:
    selector = _StubSelector()
    metadata = {
        "large_context_capacity_blocked": True,
        "large_context_capacity_block_reason": "No proven large-context route.",
    }

    with pytest.raises(LargeContextCapacityError, match="No proven"):
        apply_model_override(
            selector,
            "baseline-small",
            turn_metadata=metadata,
            realign_routed_model=False,
        )

    assert selector.calls == []


def test_large_context_floor_keeps_only_definitely_capable_configured_fallbacks(
    monkeypatch,
) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openai/configured-lower": {"context_window": 20_000},
            "openai/configured-same": {"context_window": 50_000},
            "openai/configured-higher": {"context_window": 128_000},
            "openai/routed-at-floor": {"context_window": 200_000},
        }
    )
    monkeypatch.setattr(
        "opensquilla.provider.model_catalog._shared_catalog",
        catalog,
    )
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openai",
                "configured-lower",
                api_key="test-key",
            ),
            fallbacks=[
                ProviderConfig("openai", "configured-same", api_key="test-key"),
                ProviderConfig("openai", "configured-higher", api_key="test-key"),
                ProviderConfig("openai", "configured-unknown", api_key="test-key"),
            ],
        )
    )
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [],
        "large_context_floor_min_tier": "c2",
        "large_context_material_tokens": 50_000,
        "large_context_thinking_budget_tokens": 0,
        "routed_model": "routed-at-floor",
    }

    apply_model_override(
        selector,
        "routed-at-floor",
        turn_metadata=metadata,
        realign_routed_model=False,
    )

    assert [config.model for config in selector.remaining_chain()] == [
        "routed-at-floor",
        "configured-higher",
    ]


def test_large_context_floor_retains_safe_original_primary_as_fallback(
    monkeypatch,
) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openai/original-safe": {"context_window": 128_000},
            "openai/routed-at-floor": {"context_window": 200_000},
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openai", "original-safe", api_key="test-key")
        )
    )
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [],
        "large_context_floor_min_tier": "c2",
        "large_context_material_tokens": 50_000,
        "large_context_thinking_budget_tokens": 0,
        "routed_model": "routed-at-floor",
    }

    apply_model_override(
        selector,
        "routed-at-floor",
        turn_metadata=metadata,
        realign_routed_model=False,
    )

    assert [config.model for config in selector.remaining_chain()] == [
        "routed-at-floor",
        "original-safe",
    ]


def test_large_context_floor_validates_router_fallback_capacity(monkeypatch) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openai/router-small": {"context_window": 32_000},
            "openai/router-safe": {"context_window": 128_000},
            "openai/routed-at-floor": {"context_window": 200_000},
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openai", "configured-primary", api_key="test-key")
        )
    )
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [
            {"tier": "c2", "model": "router-small"},
            {"tier": "c2", "model": "router-unknown"},
            {"tier": "c2", "model": "router-safe"},
        ],
        "large_context_floor_min_tier": "c2",
        "large_context_material_tokens": 50_000,
        "large_context_thinking_budget_tokens": 0,
        "routed_model": "routed-at-floor",
    }

    apply_model_override(
        selector,
        "routed-at-floor",
        turn_metadata=metadata,
        realign_routed_model=False,
    )

    assert [config.model for config in selector.remaining_chain()] == [
        "routed-at-floor",
        "router-safe",
    ]


def test_capacity_admission_reserves_actual_high_thinking_budget(monkeypatch) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openai/reasoning-model": {
                "context_window": 128_000,
                "max_output_tokens": 10_000,
            }
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)

    assert model_has_request_capacity(
        provider="openai",
        model="reasoning-model",
        material_tokens=60_000,
        thinking_budget_tokens=4_096,
    )
    assert not model_has_request_capacity(
        provider="openai",
        model="reasoning-model",
        material_tokens=60_000,
        thinking_budget_tokens=20_000,
    )


def test_capacity_admission_honors_global_context_and_output_overrides(
    monkeypatch,
) -> None:
    catalog = ModelCatalog()
    catalog.set_live_provider_entries(
        "openai",
        {
            "override-model": {
                "context_window": 200_000,
                "max_output_tokens": 4_000,
            }
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)

    assert model_has_request_capacity(
        provider="openai",
        model="override-model",
        material_tokens=50_000,
        thinking_budget_tokens=0,
    )
    assert not model_has_request_capacity(
        provider="openai",
        model="override-model",
        material_tokens=50_000,
        thinking_budget_tokens=0,
        context_window_override_tokens=80_000,
        max_output_override_tokens=10_000,
    )


def test_capacity_admission_honors_endpoint_and_explicit_proof_caps(
    monkeypatch,
) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openai/endpoint-model": {
                "context_window": 200_000,
                "max_output_tokens": 10_000,
            }
        }
    )
    monkeypatch.setattr(
        catalog,
        "resolve_deployment_limits",
        lambda *_args, **kwargs: DeploymentModelLimits(
            context_window=(80_000 if kwargs.get("base_url") else 200_000),
            max_output_tokens=10_000,
            max_output_tokens_known=True,
        ),
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)

    assert not model_has_request_capacity(
        provider="openai",
        model="endpoint-model",
        material_tokens=50_000,
        thinking_budget_tokens=0,
        base_url="https://deployment.example/v1",
    )
    assert not model_has_request_capacity(
        provider="openai",
        model="endpoint-model",
        material_tokens=50_000,
        thinking_budget_tokens=0,
        provider_request_proof_max_chars=160_000,
    )


def test_large_context_fallback_rejects_model_at_high_thinking_budget(
    monkeypatch,
) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openai/router-borderline": {
                "context_window": 128_000,
                "max_output_tokens": 10_000,
            },
            "openai/routed-at-floor": {
                "context_window": 200_000,
                "max_output_tokens": 10_000,
            },
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openai", "configured-primary", api_key="test-key")
        )
    )
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [
            {"tier": "c3", "model": "router-borderline"},
        ],
        "large_context_floor_min_tier": "c3",
        "large_context_material_tokens": 60_000,
        "large_context_thinking_budget_tokens": 20_000,
        "routed_model": "routed-at-floor",
    }

    apply_model_override(
        selector,
        "routed-at-floor",
        turn_metadata=metadata,
        realign_routed_model=False,
    )

    assert [config.model for config in selector.remaining_chain()] == [
        "routed-at-floor"
    ]


def test_large_context_final_head_honors_global_context_override(monkeypatch) -> None:
    catalog = ModelCatalog()
    catalog.set_live_provider_entries(
        "openai",
        {
            "catalog-large": {
                "context_window": 200_000,
                "max_output_tokens": 4_000,
            },
            "routed-at-floor": {
                "context_window": 200_000,
                "max_output_tokens": 4_000,
            },
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openai", "configured-primary", api_key="test-key")
        )
    )
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [{"tier": "c3", "model": "catalog-large"}],
        "large_context_floor_min_tier": "c3",
        "large_context_material_tokens": 50_000,
        "large_context_thinking_budget_tokens": 0,
        "large_context_context_window_override_tokens": 80_000,
        "large_context_max_output_override_tokens": 10_000,
        "routed_model": "routed-at-floor",
    }

    with pytest.raises(LargeContextCapacityError, match="final model deployment"):
        apply_model_override(
            selector,
            "routed-at-floor",
            turn_metadata=metadata,
            realign_routed_model=False,
        )

    assert metadata["large_context_capacity_blocked"] is True


def test_configured_fallback_uses_its_exact_endpoint_limits(monkeypatch) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openai/endpoint-fallback": {
                "context_window": 200_000,
                "max_output_tokens": 10_000,
            },
            "openai/routed-at-floor": {
                "context_window": 200_000,
                "max_output_tokens": 10_000,
            },
        }
    )
    monkeypatch.setattr(
        catalog,
        "resolve_deployment_limits",
        lambda *_args, **kwargs: DeploymentModelLimits(
            context_window=(
                80_000 if kwargs.get("base_url") == "https://small.example/v1" else 200_000
            ),
            max_output_tokens=10_000,
            max_output_tokens_known=True,
        ),
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openai", "configured-primary", api_key="test-key"),
            fallbacks=[
                ProviderConfig(
                    "openai",
                    "endpoint-fallback",
                    api_key="fallback-key",
                    base_url="https://small.example/v1",
                )
            ],
        )
    )
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [],
        "large_context_floor_min_tier": "c3",
        "large_context_material_tokens": 50_000,
        "large_context_thinking_budget_tokens": 0,
        "routed_model": "routed-at-floor",
    }

    apply_model_override(
        selector,
        "routed-at-floor",
        turn_metadata=metadata,
        realign_routed_model=False,
    )

    assert [config.model for config in selector.remaining_chain()] == [
        "routed-at-floor"
    ]


def test_cross_provider_large_context_floor_filters_original_tail(monkeypatch) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openrouter/original-small": {"context_window": 32_000},
            "openai/routed-large": {"context_window": 200_000},
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openrouter",
                "original-small",
                api_key="original-key",
            ),
            fallbacks=[
                ProviderConfig("openrouter", "unknown-tail", api_key="fallback-key")
            ],
        )
    )
    metadata = {
        "routing_applied": True,
        "large_context_floor_min_tier": "c2",
        "large_context_material_tokens": 50_000,
        "routed_model": "routed-large",
    }

    apply_model_override(
        selector,
        "routed-large",
        turn_metadata=metadata,
        realign_routed_model=False,
        tier_provider_config=ProviderConfig(
            "openai",
            "routed-large",
            api_key="routed-key",
        ),
    )

    assert [
        (config.provider, config.model) for config in selector.remaining_chain()
    ] == [("openai", "routed-large")]


def test_cross_provider_large_context_floor_retains_safe_original_primary(
    monkeypatch,
) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openrouter/original-safe": {"context_window": 128_000},
            "openai/routed-large": {"context_window": 200_000},
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openrouter",
                "original-safe",
                api_key="original-key",
            )
        )
    )
    metadata = {
        "routing_applied": True,
        "large_context_floor_min_tier": "c2",
        "large_context_material_tokens": 50_000,
        "large_context_thinking_budget_tokens": 0,
        "routed_model": "routed-large",
    }

    apply_model_override(
        selector,
        "routed-large",
        turn_metadata=metadata,
        realign_routed_model=False,
        tier_provider_config=ProviderConfig(
            "openai",
            "routed-large",
            api_key="routed-key",
        ),
    )

    assert [
        (config.provider, config.model) for config in selector.remaining_chain()
    ] == [
        ("openai", "routed-large"),
        ("openrouter", "original-safe"),
    ]


def test_cross_provider_large_context_head_uses_exact_endpoint_limits(
    monkeypatch,
) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "tokenrhythm/routed-large": {
                "context_window": 200_000,
                "max_output_tokens": 10_000,
            }
        }
    )
    monkeypatch.setattr(
        catalog,
        "resolve_deployment_limits",
        lambda *_args, **kwargs: DeploymentModelLimits(
            context_window=(
                80_000
                if kwargs.get("base_url") == "https://small.example/v1"
                else 200_000
            ),
            max_output_tokens=10_000,
            max_output_tokens_known=True,
        ),
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openai", "configured-primary", api_key="test-key")
        )
    )
    metadata = {
        "routing_applied": True,
        "large_context_floor_min_tier": "c2",
        "large_context_material_tokens": 50_000,
        "large_context_thinking_budget_tokens": 0,
        "routed_model": "routed-large",
    }

    with pytest.raises(LargeContextCapacityError, match="cross-provider"):
        apply_model_override(
            selector,
            "routed-large",
            turn_metadata=metadata,
            realign_routed_model=False,
            tier_provider_config=ProviderConfig(
                "tokenrhythm",
                "routed-large",
                api_key="routed-key",
                base_url="https://small.example/v1",
            ),
        )

    assert selector.current_config.provider == "openai"
    assert selector.current_config.model == "configured-primary"


def test_large_context_explicit_override_requires_final_head_capacity(
    monkeypatch,
) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openai/routed-large": {"context_window": 200_000},
            "openai/explicit-small": {"context_window": 80_000},
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openai", "configured-primary", api_key="test-key")
        )
    )
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [],
        "large_context_floor_min_tier": "c2",
        "large_context_material_tokens": 50_000,
        "large_context_thinking_budget_tokens": 0,
        "routed_model": "routed-large",
    }

    with pytest.raises(LargeContextCapacityError, match="final model deployment"):
        apply_model_override(
            selector,
            "explicit-small",
            turn_metadata=metadata,
            realign_routed_model=True,
        )

    assert metadata["large_context_capacity_blocked"] is True


def test_cross_provider_large_context_explicit_restore_revalidates_primary(
    monkeypatch,
) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "tokenrhythm/routed-large": {"context_window": 200_000},
            "openai/explicit-small": {"context_window": 80_000},
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openai", "configured-primary", api_key="test-key")
        )
    )
    metadata = {
        "routing_applied": True,
        "large_context_floor_min_tier": "c2",
        "large_context_material_tokens": 50_000,
        "large_context_thinking_budget_tokens": 0,
        "routed_model": "routed-large",
    }
    apply_model_override(
        selector,
        "routed-large",
        turn_metadata=metadata,
        realign_routed_model=False,
        tier_provider_config=ProviderConfig(
            "tokenrhythm",
            "routed-large",
            api_key="routed-key",
        ),
    )

    with pytest.raises(LargeContextCapacityError, match="explicit model override"):
        apply_model_override(
            selector,
            "explicit-small",
            turn_metadata=metadata,
            realign_routed_model=True,
        )

    assert selector.current_config.provider == "openai"
    assert selector.current_config.model == "explicit-small"


def test_large_context_blocked_route_rejects_unsafe_primary_rebound(
    monkeypatch,
) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {"openai/primary-small": {"context_window": 80_000}}
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openai", "primary-small", api_key="test-key")
        )
    )
    metadata = {
        "routing_applied": True,
        "large_context_floor_min_tier": "c2",
        "large_context_material_tokens": 50_000,
        "large_context_thinking_budget_tokens": 0,
        "routed_provider": "tokenrhythm",
        "routed_provider_blocked": "missing_credential",
        "routed_model": "routed-large",
    }

    with pytest.raises(LargeContextCapacityError, match="configured primary"):
        apply_model_override(
            selector,
            "routed-large",
            turn_metadata=metadata,
            realign_routed_model=False,
        )

    assert selector.current_config.model == "primary-small"


def test_router_fallback_uses_exact_configured_endpoint_capacity(monkeypatch) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openai/routed-large": {"context_window": 200_000},
            "tokenrhythm/fallback-large": {"context_window": 200_000},
        }
    )
    monkeypatch.setattr(
        catalog,
        "resolve_deployment_limits",
        lambda *_args, **kwargs: DeploymentModelLimits(
            context_window=(
                80_000
                if kwargs.get("base_url") == "https://small.example/v1"
                else 200_000
            ),
            max_output_tokens=10_000,
            max_output_tokens_known=True,
        ),
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openai", "configured-primary", api_key="test-key"),
            fallbacks=[
                ProviderConfig(
                    "tokenrhythm",
                    "fallback-large",
                    api_key="fallback-key",
                    base_url="https://small.example/v1",
                )
            ],
        )
    )
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [
            {
                "tier": "c2",
                "provider": "tokenrhythm",
                "model": "fallback-large",
            }
        ],
        "large_context_floor_min_tier": "c2",
        "large_context_material_tokens": 50_000,
        "large_context_thinking_budget_tokens": 0,
        "routed_model": "routed-large",
    }

    apply_model_override(
        selector,
        "routed-large",
        turn_metadata=metadata,
        realign_routed_model=False,
    )

    assert [config.model for config in selector.remaining_chain()] == [
        "routed-large"
    ]


def test_explicit_override_realigns_routed_model_and_drops_savings() -> None:
    selector = _StubSelector()
    metadata = {
        "routing_applied": True,
        "router_fallback_chain": [],
        "routed_model": "routed",
        "savings_pct": 10.0,
        "savings_max_price_per_m": 3.0,
    }
    apply_model_override(
        selector, "explicit", turn_metadata=metadata, realign_routed_model=True
    )
    assert metadata["routed_model"] == "explicit"
    assert metadata["savings_pct"] == 0.0
    assert metadata["savings_max_price_per_m"] == 0.0


# ---------------------------------------------------------------------------
# Onboarding warnings for cross-provider tiers
# ---------------------------------------------------------------------------


def test_cross_provider_tier_warning_text() -> None:
    warnings = _cross_provider_tier_warnings(
        {"c2": {"provider": "openai", "model": "gpt-5.5"}},
        "openrouter",
        tier_provider_mismatch="veto",
    )
    assert len(warnings) == 1
    assert "'c2'" in warnings[0]
    assert "'openai'" in warnings[0]
    assert "not enabled" in warnings[0]
    assert "choice is vetoed" in warnings[0]
    assert "current 'openrouter' deployment" in warnings[0]
    assert "model will be requested" not in warnings[0]


def test_cross_provider_tier_warning_preserves_legacy_route_semantics() -> None:
    warnings = _cross_provider_tier_warnings(
        {"c2": {"provider": "openai", "model": "gpt-5.5"}},
        "openrouter",
        tier_provider_mismatch="route",
    )
    assert len(warnings) == 1
    assert "model will be requested from 'openrouter'" in warnings[0]
    assert "choice is vetoed" not in warnings[0]


def test_cross_provider_warning_flips_to_credential_check_when_enabled(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tiers = {"c2": {"provider": "openai", "model": "gpt-5.5"}}
    warnings = _cross_provider_tier_warnings(
        tiers, "openrouter", cross_provider_enabled=True, llm_profiles=None
    )
    assert len(warnings) == 1
    assert "no credentials resolve" in warnings[0]
    assert "llm_profiles.openai" in warnings[0]

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert (
        _cross_provider_tier_warnings(
            tiers, "openrouter", cross_provider_enabled=True, llm_profiles=None
        )
        == []
    )


def test_cross_provider_warning_matches_runtime_for_operator_owned_endpoints(
    monkeypatch,
) -> None:
    """Save-time warnings mirror the runtime deployment resolver: an azure-style
    tier with the env key set but no profile base_url is vetoed at turn time,
    so the save must warn (previously the plain env lookup stayed silent)."""
    from opensquilla.gateway.config import LlmProviderProfile

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-azure-env")
    tiers = {"c2": {"provider": "azure", "model": "gpt-tier"}}

    warnings = _cross_provider_tier_warnings(
        tiers, "openrouter", cross_provider_enabled=True, llm_profiles=None
    )
    assert len(warnings) == 1
    assert "no endpoint resolves" in warnings[0]
    assert "llm_profiles.azure" in warnings[0]

    # With the operator-supplied endpoint stored, the registry env key
    # resolves at runtime — and the save-time check must agree.
    assert (
        _cross_provider_tier_warnings(
            tiers,
            "openrouter",
            cross_provider_enabled=True,
            llm_profiles={
                "azure": LlmProviderProfile(
                    base_url="https://acct.azure-endpoint.example/v1"
                )
            },
        )
        == []
    )


def test_cross_provider_warning_accepts_case_variant_profile_keys(monkeypatch) -> None:
    from opensquilla.gateway.config import LlmProviderProfile

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    tiers = {"c2": {"provider": "deepseek", "model": "deepseek-tier"}}

    warnings = _cross_provider_tier_warnings(
        tiers,
        "openrouter",
        cross_provider_enabled=True,
        # Hand-authored configs predate key normalization; the runtime
        # resolver accepts the case variant, so the save-time check must too.
        llm_profiles={"DeepSeek": LlmProviderProfile(api_key="sk-profile")},
    )
    assert warnings == []


def test_upsert_router_surfaces_cross_provider_warning() -> None:
    cfg = GatewayConfig()  # defaults: openrouter provider + openrouter tiers
    res = upsert_router(
        cfg,
        mode="recommended",
        tiers={"c2": {"provider": "openai", "model": "gpt-5.5"}},
    )
    assert any("cross-provider" in w.lower() for w in res.warnings)


def test_upsert_router_no_warning_for_matching_tiers() -> None:
    cfg = GatewayConfig()
    res = upsert_router(cfg, mode="recommended")
    assert res.warnings == []


def test_upsert_router_redacts_secret_like_tier_fields() -> None:
    # Tiers are untyped dicts: a hand-written api_key must not be echoed
    # back through the router-configure RPC response.
    cfg = GatewayConfig()
    res = upsert_router(
        cfg,
        mode="recommended",
        tiers={"c2": {"provider": "openrouter", "model": "z-ai/glm-5.1", "api_key": "sk-leak"}},
    )
    echoed = res.public_payload["tiers"]["c2"]
    assert echoed["api_key"] == "***"
    assert echoed["model"] == "z-ai/glm-5.1"
    # The stored config keeps the real value; only the echo is redacted.
    assert res.config.squilla_router.tiers["c2"]["api_key"] == "sk-leak"


def test_upsert_router_redacts_camel_and_kebab_tier_secrets() -> None:
    # Only three known display aliases are canonicalized on write, so an
    # apiKey/accessToken passes into the stored tier verbatim — the echo
    # redaction must match secret-shaped keys in any spelling.
    cfg = GatewayConfig()
    res = upsert_router(
        cfg,
        mode="recommended",
        tiers={
            "c2": {
                "provider": "openrouter",
                "model": "z-ai/glm-5.1",
                "apiKey": "sk-camel",
                "accessToken": "tok-camel",
                "api-key": "sk-kebab",
                "clientSecret": "sec-camel",
            }
        },
    )
    echoed = res.public_payload["tiers"]["c2"]
    assert echoed["apiKey"] == "***"
    assert echoed["accessToken"] == "***"
    assert echoed["api-key"] == "***"
    assert echoed["clientSecret"] == "***"
    assert echoed["model"] == "z-ai/glm-5.1"
    assert echoed["provider"] == "openrouter"


def test_upsert_router_redacts_acronym_style_tier_secrets() -> None:
    # Acronym runs have no lowercase->uppercase boundary (APIKey, APIKEY):
    # the acronym rule and the separator-free fallback must still match.
    cfg = GatewayConfig()
    res = upsert_router(
        cfg,
        mode="recommended",
        tiers={
            "c2": {
                "provider": "openrouter",
                "model": "z-ai/glm-5.1",
                "APIKey": "sk-acronym",
                "APIKEY": "sk-caps",
                "API_KEY": "sk-shout",
                "AccessTOKEN": "tok-caps",
                "key": "sk-bare",
                "KEY": "sk-bare-caps",
                # Ordinary words must NOT be redacted by the fallback.
                "monkey": "keep-me",
            }
        },
    )
    echoed = res.public_payload["tiers"]["c2"]
    assert echoed["APIKey"] == "***"
    assert echoed["APIKEY"] == "***"
    assert echoed["API_KEY"] == "***"
    assert echoed["AccessTOKEN"] == "***"
    assert echoed["key"] == "***"
    assert echoed["KEY"] == "***"
    assert echoed["monkey"] == "keep-me"
