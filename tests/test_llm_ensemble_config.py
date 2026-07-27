from __future__ import annotations

import pytest

from opensquilla.gateway.config import GatewayConfig, LlmProviderProfile
from opensquilla.provider.compat_policy import compat_policy_for_kind
from opensquilla.provider.ensemble import build_ensemble_provider_from_config
from opensquilla.provider.openai import _build_openai_wire_messages
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import ChatConfig, Message, ModelCapabilities


def test_llm_ensemble_defaults_to_disabled_for_model_router_first_install() -> None:
    cfg = GatewayConfig()

    ensemble = cfg.llm_ensemble
    assert cfg.squilla_router.enabled is True
    assert ensemble.enabled is False
    assert ensemble.mode == "b5_fusion"
    assert ensemble.selection_mode == "static_openrouter_b5"
    assert ensemble.proposer_tools is False
    assert ensemble.min_successful_proposers == 1
    assert ensemble.model_options == []
    assert ensemble.candidates == []
    assert ensemble.candidate_max_chars == 24_000
    assert ensemble.proposer_timeout_seconds == 3600.0
    assert ensemble.aggregator_timeout_seconds == 3600.0
    assert ensemble.shuffle_candidates is True
    assert ensemble.record_candidates is False

    enabled_cfg = cfg.model_copy(deep=True)
    enabled_cfg.llm_ensemble.enabled = True
    provider = build_ensemble_provider_from_config(
        config=enabled_cfg,
        inherited_provider_config=ProviderConfig(
            provider="openrouter",
            model="routed/model",
            api_key="fake",
            base_url="https://openrouter.example/api/v1",
        ),
        fallback_provider=None,
        turn_metadata={"routed_tier": "c0"},
    )
    assert provider.profile_name == "static_openrouter_b5"
    assert [member.provider_config.model for member in provider.proposers] == [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.7-max",
    ]
    assert provider.aggregator.provider_config.model == "z-ai/glm-5.2"
    assert provider.min_successful_proposers == 3
    assert provider.proposer_timeout_seconds == 300.0
    assert provider.aggregator_timeout_seconds == 480.0
    assert provider.shuffle_candidates is False
    assert provider.quorum_grace_seconds == 10.0


def test_static_openrouter_b5_does_not_need_model_options() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter_b5",
            "model_options": [],
        }
    )

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=ProviderConfig(
            provider="openrouter",
            model="routed/model",
            api_key="fake",
            base_url="https://openrouter.example/api/v1",
        ),
        fallback_provider=None,
    )

    assert provider.profile_name == "static_openrouter_b5"
    assert [member.provider_config.model for member in provider.proposers] == [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.7-max",
    ]
    assert provider.aggregator.provider_config.model == "z-ai/glm-5.2"


def test_static_tokenrhythm_b5_mirrors_the_openrouter_lineup() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_tokenrhythm_b5",
        }
    )

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=ProviderConfig(
            provider="tokenrhythm",
            model="deepseek-v4-pro",
            api_key="fake",
            base_url="https://tokenrhythm.example/v1",
        ),
        fallback_provider=None,
    )

    assert provider.profile_name == "static_tokenrhythm_b5"
    assert [member.provider_config.model for member in provider.proposers] == [
        "deepseek-v4-pro",
        "glm-5.2",
        "kimi-k2.7-code",
        "qwen3.7-max",
    ]
    assert all(
        member.provider_config.provider == "tokenrhythm" for member in provider.proposers
    )
    assert provider.aggregator.provider_config.provider == "tokenrhythm"
    assert provider.aggregator.provider_config.model == "glm-5.2"
    # Same aggregation defaults as the static OpenRouter profile.
    assert provider.min_successful_proposers == 3
    assert provider.proposer_timeout_seconds == 300.0
    assert provider.aggregator_timeout_seconds == 480.0
    assert provider.shuffle_candidates is False
    assert provider.quorum_grace_seconds == 10.0


def test_static_b5_mode_tables_agree_across_gateway_and_provider() -> None:
    # gateway must not be imported from provider, so the selection-mode →
    # provider table exists on both sides; this pins them together.
    from typing import get_args

    from opensquilla.gateway.config import (
        STATIC_B5_SELECTION_MODE_PROVIDERS,
        LlmEnsembleConfig,
    )
    from opensquilla.provider.ensemble import STATIC_B5_PROFILES

    assert {
        mode: profile.provider_id for mode, profile in STATIC_B5_PROFILES.items()
    } == STATIC_B5_SELECTION_MODE_PROVIDERS
    literal_modes = set(
        get_args(LlmEnsembleConfig.model_fields["selection_mode"].annotation)
    )
    assert literal_modes == {
        "router_dynamic",
        "custom_b5",
        *STATIC_B5_SELECTION_MODE_PROVIDERS,
    }


def test_router_dynamic_ensemble_allows_empty_custom_model_options() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "selection_mode": "router_dynamic",
            "model_options": [],
        }
    )

    assert cfg.llm_ensemble.model_options == []


def test_router_dynamic_ignores_legacy_default_openrouter_model_options() -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "api_key": "fake",
            "base_url": "https://api.deepseek.com",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "model_options": [
                "deepseek/deepseek-v4-pro",
                "z-ai/glm-5.2",
                "qwen/qwen3.7-plus",
                "deepseek/deepseek-v4-flash",
                "qwen/qwen3.7-max",
                "moonshotai/kimi-k2.6",
                "moonshotai/kimi-k2.7-code",
                "minimax/minimax-m3",
            ],
        },
        squilla_router={
            "enabled": True,
            "tiers": {
                "c0": {"provider": "deepseek", "model": "deepseek-v4-flash"},
                "c1": {"provider": "deepseek", "model": "deepseek-v4-flash"},
                "c2": {"provider": "deepseek", "model": "deepseek-v4-pro"},
                "c3": {"provider": "deepseek", "model": "deepseek-v4-pro"},
            },
        },
    )
    inherited = ProviderConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="fake",
        base_url="https://api.deepseek.com",
    )

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1"},
    )

    pool = provider.selection_plan["candidate_pool"]
    assert all(candidate["source"] != "legacy_model_options" for candidate in pool)
    assert all(candidate["provider"] != "openrouter" for candidate in pool)


def test_llm_ensemble_validates_selection_mode() -> None:
    with pytest.raises(ValueError, match="selection_mode"):
        GatewayConfig(llm_ensemble={"selection_mode": "static_unknown"})


def test_llm_ensemble_model_options_are_operator_configurable() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "model_options": [" custom/model ", "custom/model", "other/model"],
        }
    )

    assert cfg.llm_ensemble.model_options == ["custom/model", "other/model"]


def test_router_dynamic_keeps_non_default_legacy_model_options_with_source() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "model_options": ["vendor/custom-model"],
        }
    )
    inherited = ProviderConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="fake",
        base_url="https://api.deepseek.com",
    )

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1"},
    )

    pool = provider.selection_plan["candidate_pool"]
    legacy = next(candidate for candidate in pool if candidate["model"] == "vendor/custom-model")
    assert legacy["provider"] == "openrouter"
    assert legacy["source"] == "legacy_model_options"


def test_router_dynamic_uses_structured_candidates_with_source() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "candidates": [
                {
                    "provider": "openrouter",
                    "model": "qwen/qwen3.7-max",
                    "source": "custom",
                    "enabled": True,
                },
                {
                    "provider": "openrouter",
                    "model": "disabled/model",
                    "source": "custom",
                    "enabled": False,
                },
            ],
        }
    )
    inherited = ProviderConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="fake",
        base_url="https://api.deepseek.com",
    )

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c2"},
    )

    pool = provider.selection_plan["candidate_pool"]
    assert any(
        candidate["provider"] == "openrouter"
        and candidate["model"] == "qwen/qwen3.7-max"
        and candidate["source"] == "custom"
        for candidate in pool
    )
    assert all(candidate["model"] != "disabled/model" for candidate in pool)


def test_build_ensemble_provider_inherits_current_openrouter_credentials() -> None:
    cfg = GatewayConfig(llm_ensemble={"enabled": True})
    inherited = ProviderConfig(
        provider="openrouter",
        model="routed/model",
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
        proxy="http://proxy.local:7890",
        provider_routing={"z-ai/glm-5.2": "z-ai"},
    )

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=inherited,
        fallback_provider=None,
    )

    members = [*provider.proposers, provider.aggregator]
    assert all(member.provider_config.api_key == "fake" for member in members)
    assert all(
        member.provider_config.base_url == "https://openrouter.example/api/v1"
        for member in members
    )
    assert all(member.provider_config.proxy == "http://proxy.local:7890" for member in members)
    assert provider.aggregator.provider_config.provider_routing == {"z-ai/glm-5.2": "z-ai"}


def test_router_dynamic_ensemble_uses_small_c0_slot_template() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "min_successful_proposers": 4,
        }
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
    )

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c0", "routing_confidence": 0.93},
    )

    assert provider.profile_name == "router_dynamic/c0"
    assert [member.label for member in provider.proposers] == ["anchor", "cheap_contrast"]
    assert [member.provider_config.model for member in provider.proposers][0] == (
        "deepseek/deepseek-v4-flash"
    )
    assert len(provider.proposers) == 2
    assert provider.min_successful_proposers == 2
    assert provider.selection_plan["slot_template"] == ["anchor", "cheap_contrast"]
    assert provider.selection_plan["aggregator_slot"] == "aggregator_fast"
    assert provider.selection_plan["duplicate_policy"] == "selected_penalty"
    assert provider.proposer_timeout_seconds == 3600.0
    assert provider.aggregator_timeout_seconds == 3600.0
    assert provider.quorum_grace_seconds == 0.0


def test_router_dynamic_ensemble_uses_slot_specific_c2_selection() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "model_options": [
                "deepseek/deepseek-v4-pro",
                "z-ai/glm-5.2",
                "google/gemini-3-flash-preview",
                "qwen/qwen3.7-plus",
                "anthropic/claude-opus-4.8",
            ],
        }
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="z-ai/glm-5.2",
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
    )

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c2", "routing_confidence": 0.82},
    )

    assert provider.profile_name == "router_dynamic/c2"
    assert [member.label for member in provider.proposers] == [
        "anchor",
        "adjacent_tier_check",
        "orthogonal_family",
    ]
    assert provider.proposers[0].provider_config.model == "z-ai/glm-5.2"
    assert provider.selection_plan["aggregator_slot"] == "aggregator_strong"
    assert provider.selection_plan["slots"][1]["slot"] == "adjacent_tier_check"
    assert provider.selection_plan["slots"][2]["slot"] == "orthogonal_family"
    assert provider.selection_plan["aggregator"]["slot"] == "aggregator_strong"
    assert provider.selection_plan["candidate_pool_size"] >= 5


def test_static_openrouter_b5_ensemble_locks_members_across_routed_tiers() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter_b5",
            "min_successful_proposers": 9,
            "shuffle_candidates": False,
        }
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="routed/model",
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
        proxy="http://proxy.local:7890",
        provider_routing={"z-ai/glm-5.2": "z-ai"},
    )
    expected_proposers = [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.7-max",
    ]

    for tier in ("c0", "c1", "c2", "c3"):
        provider = build_ensemble_provider_from_config(
            config=cfg,
            inherited_provider_config=inherited,
            fallback_provider=None,
            turn_metadata={"routed_tier": tier, "routing_confidence": 0.99},
        )

        assert provider.profile_name == "static_openrouter_b5"
        assert [member.provider_config.model for member in provider.proposers] == expected_proposers
        assert provider.aggregator.provider_config.model == "z-ai/glm-5.2"
        assert provider.selection_plan == {
            "strategy": "static_openrouter_b5",
            "profile": "static_openrouter_b5",
            "proposer_models": expected_proposers,
            "aggregator_model": "z-ai/glm-5.2",
            "proposer_count": 4,
            "configured_min_successful_proposers": 9,
            "effective_min_successful_proposers": 4,
            "configured_proposer_timeout_seconds": 3600.0,
            "effective_proposer_timeout_seconds": 300.0,
            "configured_aggregator_timeout_seconds": 3600.0,
            "effective_aggregator_timeout_seconds": 480.0,
            "configured_shuffle_candidates": False,
            "effective_shuffle_candidates": False,
            "quorum_grace_seconds": 10.0,
        }
        assert provider.min_successful_proposers == 4
        assert provider.proposer_timeout_seconds == 300.0
        assert provider.aggregator_timeout_seconds == 480.0
        assert provider.shuffle_candidates is False
        assert provider.quorum_grace_seconds == 10.0
        members = [*provider.proposers, provider.aggregator]
        assert all(member.provider_config.provider == "openrouter" for member in members)
        assert all(member.provider_config.api_key == "fake" for member in members)
        assert all(
            member.provider_config.base_url == "https://openrouter.example/api/v1"
            for member in members
        )
        assert all(member.provider_config.proxy == "http://proxy.local:7890" for member in members)
        assert all(
            member.provider_config.provider_routing == {"z-ai/glm-5.2": "z-ai"}
            for member in members
        )


def test_static_openrouter_b5_ensemble_uses_profile_effective_defaults() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter_b5",
        }
    )
    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=ProviderConfig(
            provider="openrouter",
            model="routed/model",
            api_key="fake",
            base_url="https://openrouter.example/api/v1",
        ),
        fallback_provider=None,
    )

    assert cfg.llm_ensemble.min_successful_proposers == 1
    assert cfg.llm_ensemble.proposer_timeout_seconds == 3600.0
    assert cfg.llm_ensemble.aggregator_timeout_seconds == 3600.0
    assert cfg.llm_ensemble.shuffle_candidates is True
    assert provider.min_successful_proposers == 3
    assert provider.proposer_timeout_seconds == 300.0
    assert provider.aggregator_timeout_seconds == 480.0
    assert provider.shuffle_candidates is False
    assert provider.quorum_grace_seconds == 10.0


def test_static_openrouter_b5_ensemble_preserves_custom_effective_values() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter_b5",
            "min_successful_proposers": 2,
            "proposer_timeout_seconds": 180.0,
            "aggregator_timeout_seconds": 900.0,
            "shuffle_candidates": False,
        }
    )
    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=ProviderConfig(
            provider="openrouter",
            model="routed/model",
            api_key="fake",
            base_url="https://openrouter.example/api/v1",
        ),
        fallback_provider=None,
    )

    assert provider.min_successful_proposers == 2
    assert provider.proposer_timeout_seconds == 180.0
    assert provider.aggregator_timeout_seconds == 900.0
    assert provider.shuffle_candidates is False


def _custom_b5_config(**overrides: object) -> GatewayConfig:
    payload: dict[str, object] = {
        "enabled": True,
        "selection_mode": "custom_b5",
        "candidates": [
            {"provider": "volcengine", "model": "doubao-2.0-pro", "role": "primary"},
            {"provider": "volcengine", "model": "deepseek-v4-flash", "role": "fast_check"},
            {"provider": "volcengine", "model": "kimi-k2.6", "role": "contrast"},
            {"provider": "volcengine", "model": "deepseek-v4-pro", "role": "aggregator"},
        ],
    }
    payload.update(overrides)
    return GatewayConfig(llm_ensemble=payload)


def _volcengine_inherited() -> ProviderConfig:
    return ProviderConfig(
        provider="volcengine",
        model="deepseek-v4-pro",
        api_key="fake",
        base_url="https://volcengine.example/api/v3",
    )


def test_custom_b5_builds_role_labelled_proposers_and_single_aggregator() -> None:
    provider = build_ensemble_provider_from_config(
        config=_custom_b5_config(),
        inherited_provider_config=_volcengine_inherited(),
        fallback_provider=None,
    )

    assert provider.profile_name == "custom_b5"
    assert [member.label for member in provider.proposers] == [
        "primary",
        "fast_check",
        "contrast",
    ]
    assert [member.provider_config.model for member in provider.proposers] == [
        "doubao-2.0-pro",
        "deepseek-v4-flash",
        "kimi-k2.6",
    ]
    assert provider.aggregator.provider_config.model == "deepseek-v4-pro"
    assert provider.selection_plan["aggregator"]["source"] == "candidate_role"


def test_custom_b5_uses_fixed_lineup_effective_defaults_with_auto_quorum() -> None:
    cfg = _custom_b5_config()
    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=_volcengine_inherited(),
        fallback_provider=None,
    )

    # Stored legacy defaults are replaced by the fixed-lineup family; quorum
    # is derived as N-1 for the 3-proposer lineup.
    assert cfg.llm_ensemble.min_successful_proposers == 1
    assert provider.min_successful_proposers == 2
    assert provider.proposer_timeout_seconds == 300.0
    assert provider.aggregator_timeout_seconds == 480.0
    assert provider.shuffle_candidates is False
    assert provider.quorum_grace_seconds == 10.0


def test_custom_b5_preserves_explicit_quorum_and_timeouts() -> None:
    cfg = _custom_b5_config(
        min_successful_proposers=3,
        proposer_timeout_seconds=120.0,
        aggregator_timeout_seconds=600.0,
    )
    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=_volcengine_inherited(),
        fallback_provider=None,
    )

    assert provider.min_successful_proposers == 3
    assert provider.proposer_timeout_seconds == 120.0
    assert provider.aggregator_timeout_seconds == 600.0


def test_custom_b5_without_aggregator_row_inherits_the_routed_model() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": [
                {"provider": "volcengine", "model": "doubao-2.0-pro"},
                {"provider": "volcengine", "model": "kimi-k2.6"},
            ],
        }
    )
    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=_volcengine_inherited(),
        fallback_provider=None,
    )

    assert provider.aggregator.provider_config.model == "deepseek-v4-pro"
    assert provider.selection_plan["aggregator"]["source"] == "inherited_model"


def test_custom_b5_disabled_candidates_are_excluded_from_the_lineup() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": [
                {"provider": "volcengine", "model": "doubao-2.0-pro"},
                {"provider": "volcengine", "model": "kimi-k2.6"},
                {"provider": "volcengine", "model": "deepseek-v4-flash", "enabled": False},
            ],
        }
    )
    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=_volcengine_inherited(),
        fallback_provider=None,
    )

    assert [member.provider_config.model for member in provider.proposers] == [
        "doubao-2.0-pro",
        "kimi-k2.6",
    ]


def test_custom_b5_validation_rejects_undersized_and_oversized_lineups() -> None:
    with pytest.raises(Exception, match="at least 2"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "selection_mode": "custom_b5",
                "candidates": [{"provider": "a", "model": "m1"}],
            }
        )
    with pytest.raises(Exception, match="at most 6"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "selection_mode": "custom_b5",
                "candidates": [
                    {"provider": "a", "model": f"m{i}"} for i in range(7)
                ],
            }
        )


def test_custom_b5_validation_rejects_quorum_above_proposer_count() -> None:
    with pytest.raises(Exception, match="min_successful_proposers"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "selection_mode": "custom_b5",
                "min_successful_proposers": 4,
                "candidates": [
                    {"provider": "a", "model": "m1"},
                    {"provider": "a", "model": "m2"},
                ],
            }
        )


def test_candidate_roles_normalize_and_reject_dual_aggregators() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "candidates": [
                {"provider": "a", "model": "m1", "role": "AGGREGATOR"},
                {"provider": "a", "model": "m2", "role": "definitely-not-a-role"},
            ],
        }
    )
    assert cfg.llm_ensemble.candidates[0].role == "aggregator"
    # Unknown roles coerce to unassigned instead of failing gateway boot.
    assert cfg.llm_ensemble.candidates[1].role == ""

    with pytest.raises(Exception, match="at most one"):
        GatewayConfig(
            llm_ensemble={
                "candidates": [
                    {"provider": "a", "model": "m1", "role": "aggregator"},
                    {"provider": "a", "model": "m2", "role": "aggregator"},
                ],
            }
        )


def test_custom_b5_lineup_ready_gates_on_member_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider.ensemble import custom_b5_lineup_ready

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "volcengine",
            "model": "deepseek-v4-pro",
            "api_key": "fake",
            "base_url": "https://volcengine.example/api/v3",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": [
                {"provider": "volcengine", "model": "doubao-2.0-pro"},
                {"provider": "openrouter", "model": "z-ai/glm-5.2"},
            ],
        },
    )
    ready, reason = custom_b5_lineup_ready(cfg)
    assert ready is False
    assert reason == "missing_credential:openrouter"

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    ready, reason = custom_b5_lineup_ready(cfg)
    assert ready is True
    assert reason == ""


def test_custom_b5_resolves_each_non_primary_member_from_its_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider.ensemble import custom_b5_lineup_ready

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "volcengine",
            "model": "doubao-primary",
            "api_key": "volc-key",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        },
        llm_profiles={
            "openai": LlmProviderProfile(
                api_key="openai-profile-key",
                base_url="https://openai-profile.example/v1",
                proxy="http://openai-proxy.example:8080",
            ),
            "deepseek": LlmProviderProfile(
                api_key="deepseek-profile-key",
                base_url="https://deepseek-profile.example/v1",
            ),
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": [
                {"provider": "volcengine", "model": "doubao-proposer"},
                {"provider": "openai", "model": "gpt-proposer"},
                {
                    "provider": "deepseek",
                    "model": "deepseek-aggregator",
                    "role": "aggregator",
                },
            ],
        },
    )
    inherited = ProviderConfig(
        provider="volcengine",
        model="doubao-primary",
        api_key="volc-key",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
    )

    assert custom_b5_lineup_ready(cfg, inherited) == (True, "")
    ensemble = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=inherited,
        fallback_provider=None,
    )

    members = [*ensemble.proposers, ensemble.aggregator]
    by_provider = {member.provider_config.provider: member.provider_config for member in members}
    assert by_provider["volcengine"].api_key == "volc-key"
    assert by_provider["volcengine"].replay_provider_state is False
    assert by_provider["openai"].api_key == "openai-profile-key"
    assert by_provider["openai"].base_url == "https://openai-profile.example/v1"
    assert by_provider["openai"].proxy == "http://openai-proxy.example:8080"
    assert by_provider["openai"].replay_provider_state is False
    assert by_provider["deepseek"].api_key == "deepseek-profile-key"
    assert by_provider["deepseek"].base_url == "https://deepseek-profile.example/v1"
    assert by_provider["deepseek"].replay_provider_state is False


def test_cross_provider_ensemble_disables_replay_on_internal_fallback_adapters() -> None:
    from opensquilla.provider.anthropic import AnthropicProvider
    from opensquilla.provider.openai import OpenAIProvider

    cfg = GatewayConfig(
        llm={
            "provider": "volcengine",
            "model": "doubao-primary",
            "api_key": "primary-key",
        },
        llm_profiles={
            "openai": LlmProviderProfile(api_key="profile-key"),
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": [
                {"provider": "volcengine", "model": "doubao-proposer"},
                {"provider": "openai", "model": "gpt-proposer"},
                {
                    "provider": "openai",
                    "model": "gpt-aggregator",
                    "role": "aggregator",
                },
            ],
        },
    )
    inherited = ProviderConfig(
        provider="volcengine",
        model="doubao-primary",
        api_key="primary-key",
    )
    fallbacks = [
        OpenAIProvider(
            api_key="primary-key",
            model="deepseek/deepseek-v4-pro",
            provider_kind="openrouter",
            replay_provider_state=True,
        ),
        AnthropicProvider(
            api_key="primary-key",
            model="minimax-primary",
            replay_provider_state=True,
        ),
    ]

    for fallback in fallbacks:
        build_ensemble_provider_from_config(
            config=cfg,
            inherited_provider_config=inherited,
            fallback_provider=fallback,
        )
        assert fallback._replay_provider_state is False
        if isinstance(fallback, OpenAIProvider):
            wire_messages = _build_openai_wire_messages(
                [
                    Message(
                        role="assistant",
                        content="portable answer",
                        reasoning_content="foreign-private-reasoning",
                    )
                ],
                ChatConfig(
                    thinking=True,
                    model_capabilities=ModelCapabilities(
                        supports_reasoning=True,
                        reasoning_format="openrouter",
                    ),
                ),
                policy=compat_policy_for_kind("openrouter"),
                provider_kind="openrouter",
                model="deepseek/deepseek-v4-pro",
                replay_provider_state=fallback._replay_provider_state,
                reasoning_echo_turns=None,
            )
            assert "reasoning_content" not in wire_messages[0]


@pytest.mark.asyncio
async def test_cross_provider_ensemble_disables_late_plugin_selector_fallback_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.runtime import _SelectorFallbackProvider
    from opensquilla.provider.selector import ModelSelector, SelectorConfig
    from opensquilla.provider.types import DoneEvent, ErrorEvent, TextDeltaEvent

    plugin_fallback_config = ProviderConfig(
        provider="anthropic",
        model="plugin-fallback",
        api_key="plugin-test-key",
        replay_provider_state=True,
    )

    class _Plugin:
        def failover_hook(self, primary_failure: Exception) -> list[ProviderConfig]:
            del primary_failure
            return [plugin_fallback_config]

    class _PrimaryAdapter:
        provider_name = "openrouter"

        def __init__(self) -> None:
            self.replay_provider_state = True

        def disable_provider_state_replay(self) -> None:
            self.replay_provider_state = False

    class _FallbackAdapter:
        provider_name = "anthropic"

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            yield TextDeltaEvent(text="fallback answer")
            yield DoneEvent(model="plugin-fallback", input_tokens=1, output_tokens=1)

    selector_builds: list[ProviderConfig] = []

    def build_selector_provider(provider_config: ProviderConfig):
        selector_builds.append(provider_config)
        if provider_config.model == "plugin-fallback":
            return _FallbackAdapter()
        return _PrimaryAdapter()

    monkeypatch.setattr(
        "opensquilla.provider.selector._build_provider",
        build_selector_provider,
    )

    class _MemberAdapter:
        def __init__(self, model: str) -> None:
            self.model = model
            self.provider_name = "openai"

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            if self.model == "aggregator-model":
                yield ErrorEvent(message="rate limited", code="429")
                return
            yield TextDeltaEvent(text="candidate")
            yield DoneEvent(model=self.model, input_tokens=1, output_tokens=1)

    monkeypatch.setattr(
        "opensquilla.provider.ensemble._build_provider",
        lambda provider_config: _MemberAdapter(provider_config.model),
    )

    shared_selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="volcengine",
                model="primary-model",
                api_key="primary-test-key",
                replay_provider_state=True,
            )
        ),
        plugin=_Plugin(),
    )
    turn_selector = shared_selector.clone()
    direct_fallback = turn_selector.resolve()
    cfg = GatewayConfig(
        llm={
            "provider": "volcengine",
            "model": "primary-model",
            "api_key": "primary-test-key",
        },
        llm_profiles={
            "openai": LlmProviderProfile(api_key="profile-test-key"),
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "min_successful_proposers": 1,
            "shuffle_candidates": False,
            "candidates": [
                {"provider": "volcengine", "model": "proposer-model"},
                {"provider": "volcengine", "model": "proposer-model-2"},
                {
                    "provider": "openai",
                    "model": "aggregator-model",
                    "role": "aggregator",
                },
            ],
        },
    )
    ensemble = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=turn_selector.current_config,
        fallback_provider=direct_fallback,
        _fallback_selector=turn_selector,
    )
    wrapper = _SelectorFallbackProvider(ensemble, turn_selector)

    events = [
        event
        async for event in wrapper.chat([Message(role="user", content="synthetic")])
    ]

    assert any(
        isinstance(event, TextDeltaEvent) and event.text == "fallback answer"
        for event in events
    )
    assert selector_builds[-1].model == "plugin-fallback"
    assert selector_builds[-1].replay_provider_state is False
    assert turn_selector.current_config.replay_provider_state is False
    assert direct_fallback.replay_provider_state is False
    assert plugin_fallback_config.replay_provider_state is True
    assert shared_selector.current_config.replay_provider_state is True


def test_custom_b5_uses_shared_session_pinned_profile_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.selector_override import (
        acquire_profile_credential,
        report_profile_credential_failure,
    )
    from opensquilla.gateway.llm_runtime import (
        reset_profile_credential_pools,
    )
    from opensquilla.provider.ensemble import custom_b5_lineup_ready

    env_a = "OPENSQUILLA_TEST_ENSEMBLE_OPENAI_A"
    env_b = "OPENSQUILLA_TEST_ENSEMBLE_OPENAI_B"
    key_a = "sk-test-ensemble-a"
    key_b = "sk-test-ensemble-b"
    monkeypatch.setenv(env_a, key_a)
    monkeypatch.setenv(env_b, key_b)
    reset_profile_credential_pools()
    cfg = GatewayConfig(
        llm={
            "provider": "volcengine",
            "model": "doubao-primary",
            "api_key": "volc-key",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        },
        llm_profiles={
            "openai": LlmProviderProfile(api_key_env_pool=[env_a, env_b]),
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": [
                {"provider": "volcengine", "model": "doubao-proposer"},
                {"provider": "openai", "model": "gpt-proposer"},
                {
                    "provider": "openai",
                    "model": "gpt-aggregator",
                    "role": "aggregator",
                },
            ],
        },
    )
    inherited = ProviderConfig(
        provider="volcengine",
        model="doubao-primary",
        api_key="volc-key",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
    )

    try:
        assert custom_b5_lineup_ready(
            cfg,
            inherited,
            credential_pool_acquirer=acquire_profile_credential,
            session_key="ensemble-session",
        ) == (True, "")
        first = build_ensemble_provider_from_config(
            config=cfg,
            inherited_provider_config=inherited,
            fallback_provider=None,
            _credential_pool_acquirer=acquire_profile_credential,
            _credential_pool_failure_reporter=report_profile_credential_failure,
            _session_key="ensemble-session",
        )
        first_openai_keys = {
            member.provider_config.api_key
            for member in [*first.proposers, first.aggregator]
            if member.provider_config.provider == "openai"
        }
        assert len(first_openai_keys) == 1
        first_key = first_openai_keys.pop()

        openai_member = next(
            member
            for member in [*first.proposers, first.aggregator]
            if member.provider_config.provider == "openai"
        )
        first._report_member_credential_failure(
            openai_member,
            message="invalid api key",
            code="401",
        )
        second = build_ensemble_provider_from_config(
            config=cfg,
            inherited_provider_config=inherited,
            fallback_provider=None,
            _credential_pool_acquirer=acquire_profile_credential,
            _credential_pool_failure_reporter=report_profile_credential_failure,
            _session_key="ensemble-session",
        )
        second_openai_keys = {
            member.provider_config.api_key
            for member in [*second.proposers, second.aggregator]
            if member.provider_config.provider == "openai"
        }
        assert second_openai_keys == ({key_a, key_b} - {first_key})
    finally:
        reset_profile_credential_pools()
