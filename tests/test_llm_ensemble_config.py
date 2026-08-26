from __future__ import annotations

import copy
import gc
import hashlib
import json
import sys
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opensquilla.eval.draco_experiment_config import load_draco_experiment_config
from opensquilla.gateway.config import (
    FixedFourTierV2Config,
    GatewayConfig,
    LlmEnsembleConfig,
    LlmProviderProfile,
)
from opensquilla.provider import ranking_router
from opensquilla.provider.compat_policy import compat_policy_for_kind
from opensquilla.provider.ensemble import build_ensemble_provider_from_config
from opensquilla.provider.openai import _build_openai_wire_messages
from opensquilla.provider.ranking_router import (
    DynamicRankingError,
    _is_validated_ranking_config,
    _prepare_effective_ranking_config,
    canonical_json_bytes,
    canonical_json_sha256,
    load_model_registry_snapshot,
)
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import ChatConfig, Message, ModelCapabilities

ROOT = Path(__file__).resolve().parents[1]


def test_llm_ensemble_defaults_to_disabled_for_model_router_first_install() -> None:
    cfg = GatewayConfig()

    ensemble = cfg.llm_ensemble
    assert cfg.squilla_router.enabled is True
    assert ensemble.enabled is False
    assert ensemble.mode == "multiple"
    assert ensemble.selection_mode == "static_openrouter"
    assert ensemble.ranking_user_profile_generation_enabled is False
    assert ensemble.ranking_user_profile_enabled is False
    assert ensemble.ranking_thinking_assignment_enabled is False
    assert ensemble.proposer_tools is False
    assert ensemble.aggregator_tools is True
    assert ensemble.min_successful_proposers == 1
    assert ensemble.proposer_backup_count == 2
    assert ensemble.proposer_recovery_max_additional_calls == 3
    assert ensemble.proposer_max_tokens_cap == 65_536
    assert ensemble.proposer_visible_answer_reserve_tokens == 4_096
    assert ensemble.model_options == []
    assert ensemble.candidates == []
    assert ensemble.candidate_max_chars == 24_000
    assert ensemble.proposer_timeout_seconds == 3600.0
    assert ensemble.aggregator_timeout_seconds == 3600.0
    assert ensemble.aggregator_serving_chain_timeout_seconds == 120.0
    assert ensemble.latency_class == "normal"
    assert ensemble.admission.enabled is True
    assert ensemble.admission.global_max_in_flight == 24
    assert ensemble.admission.provider_default_max_in_flight == 8
    assert ensemble.admission.deployment_default_max_in_flight == 4
    assert ensemble.admission.queue_timeout_seconds == 5.0
    assert ensemble.canary_rollout.enabled is False
    assert ensemble.canary_rollout.policy_version == "router-canary-v1"
    assert ensemble.canary_rollout.global_basis_points == 0
    assert ensemble.canary_rollout.allowed_risks == ["low"]
    assert ensemble.canary_rollout.require_schema_valid_analysis is True
    assert ensemble.canary_rollout.min_analyzer_confidence == pytest.approx(0.8)
    assert ensemble.canary_rollout.proposer.basis_points == 0
    assert ensemble.canary_rollout.proposer.max_candidates_per_decision == 0
    assert ensemble.canary_rollout.aggregator.basis_points == 0
    assert ensemble.canary_rollout.aggregator.max_candidates_per_decision == 0
    auto_rollback = ensemble.canary_rollout.auto_rollback
    assert auto_rollback.enabled is False
    assert auto_rollback.window_max_attempts == 50
    assert auto_rollback.window_max_age_seconds == 300
    assert auto_rollback.max_consecutive_provider_failures == 3
    assert auto_rollback.max_rate_limited_count == 0
    assert auto_rollback.max_usage_missing_count == 0
    assert auto_rollback.rollback_cooldown_seconds == 3_600
    assert auto_rollback.half_open_successes_required == 3
    assert auto_rollback.scope_retention_seconds == 7 * 24 * 60 * 60
    assert ensemble.shuffle_candidates is True
    assert ensemble.candidate_order_seed is None
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
        _provider_health_ledger=object(),
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
    assert provider.aggregator_serving_chain_timeout_seconds == 120.0
    assert provider.latency_class == "normal"
    assert provider._admission_settings is None
    assert provider.shuffle_candidates is False
    assert provider.candidate_order_seed is None
    assert provider.quorum_grace_seconds == 10.0
    assert provider._provider_health_ledger is None


def test_llm_ensemble_multiple_serialization_golden_is_canonical() -> None:
    default_cfg = GatewayConfig()
    explicit_cfg = GatewayConfig(llm_ensemble={"mode": "multiple"})

    default_dump = default_cfg.llm_ensemble.model_dump(mode="json")
    explicit_dump = explicit_cfg.llm_ensemble.model_dump(mode="json")
    default_public = default_cfg.to_public_dict()["llm_ensemble"]
    explicit_public = explicit_cfg.to_public_dict()["llm_ensemble"]

    assert canonical_json_sha256(default_dump) == (
        "2a69626bf4918049a171852399f6aea0d2474ae90d136d657daf0e93a6532a62"
    )
    assert canonical_json_bytes(explicit_dump) == canonical_json_bytes(default_dump)
    assert canonical_json_bytes(default_public) == canonical_json_bytes(default_dump)
    assert canonical_json_bytes(explicit_public) == canonical_json_bytes(default_dump)


def test_fixed_four_tier_config_is_absent_from_legacy_default_serialization() -> None:
    ensemble = GatewayConfig().llm_ensemble

    assert ensemble.four_tier_mapping is None
    assert "four_tier_mapping" not in ensemble.model_dump(mode="json")


@pytest.mark.parametrize(
    ("legacy", "expected_mode", "expected_selection_mode"),
    [
        ({"mode": "b5_fusion"}, "multiple", "static_openrouter"),
        (
            {
                "enabled": True,
                "mode": "router_single",
                "selection_mode": "router_dynamic",
            },
            "single",
            "router_dynamic",
        ),
        (
            {"selection_mode": "static_openrouter_b5"},
            "multiple",
            "static_openrouter",
        ),
        (
            {"selection_mode": "static_tokenrhythm_b5"},
            "multiple",
            "static_tokenrhythm",
        ),
        (
            {
                "selection_mode": "custom_b5",
                "candidates": [
                    {"provider": "a", "model": "m1"},
                    {"provider": "b", "model": "m2"},
                ],
            },
            "multiple",
            "custom",
        ),
    ],
)
def test_llm_ensemble_released_values_parse_as_canonical_values(
    legacy: dict[str, object],
    expected_mode: str,
    expected_selection_mode: str,
) -> None:
    ensemble = GatewayConfig(llm_ensemble=legacy).llm_ensemble

    assert ensemble.mode == expected_mode
    assert ensemble.selection_mode == expected_selection_mode
    serialized = ensemble.model_dump(mode="json")
    assert serialized["mode"] == expected_mode
    assert serialized["selection_mode"] == expected_selection_mode


@pytest.mark.parametrize("legacy_mode", ["fixed_four_tier_v2", "fixed-four-tier-v2"])
def test_four_tier_mapping_rejects_development_selection_mode_names(
    legacy_mode: str,
) -> None:
    with pytest.raises(ValidationError, match="selection_mode"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "mode": "single",
                "selection_mode": legacy_mode,
            }
        )


@pytest.mark.parametrize(
    "removed_subtree",
    ["fixed_four_tier_v2", "fixed-four-tier-v2"],
)
def test_four_tier_mapping_rejects_development_config_subtree_name(
    removed_subtree: str,
) -> None:
    with pytest.raises(ValueError, match="unsupported llm_ensemble configuration"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "mode": "single",
                "selection_mode": "four_tier_mapping",
                removed_subtree: {"mock_seed": 7},
            }
        )


def test_llm_ensemble_released_mode_aliases_canonicalize_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_LLM_ENSEMBLE_MODE", "b5_fusion")
    assert LlmEnsembleConfig().mode == "multiple"

    monkeypatch.setenv("OPENSQUILLA_LLM_ENSEMBLE_ENABLED", "true")
    monkeypatch.setenv("OPENSQUILLA_LLM_ENSEMBLE_MODE", "router_single")
    monkeypatch.setenv(
        "OPENSQUILLA_LLM_ENSEMBLE_SELECTION_MODE",
        "router_dynamic",
    )
    ensemble = LlmEnsembleConfig()
    assert ensemble.mode == "single"
    assert ensemble.model_dump(mode="json")["mode"] == "single"


@pytest.mark.parametrize(
    "llm_ensemble",
    [
        {"mode": "single", "selection_mode": "router_dynamic"},
        {"mode": "single", "enabled": True},
    ],
)
def test_router_single_requires_enabled_router_dynamic(
    llm_ensemble: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match=r"single.*requires"):
        GatewayConfig(llm_ensemble=llm_ensemble)


def test_fixed_four_tier_v2_defaults_match_the_frozen_ladder() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "mode": "single",
            "selection_mode": "four_tier_mapping",
            "four_tier_mapping": {"mock_seed": 20260826},
        }
    )

    route = cfg.llm_ensemble.four_tier_mapping
    assert route is not None
    assert route.mock_seed == 20260826
    assert route.schema_version == "fixed-four-tier-v2-mock-v2"
    assert route.default_new_task_tier == "c1"
    assert {
        tier: (
            deployment.provider,
            deployment.model,
            deployment.reasoning,
            deployment.deployment_version,
        )
        for tier, deployment in route.tiers.items()
    } == {
        "c0": (
            "openrouter",
            "qwen/qwen3.7-flash",
            "thinking",
            "qwen3.7-flash-thinking",
        ),
        "c1": (
            "openrouter",
            "deepseek/deepseek-v4-flash",
            "max",
            "deepseek-v4-flash-0731",
        ),
        "c2": (
            "openrouter",
            "deepseek/deepseek-v4-pro",
            "max",
            "deepseek-v4-pro-0813",
        ),
        "c3": ("openrouter", "z-ai/glm-5.3", "max", "glm-5.3"),
    }


def test_fixed_four_tier_v2_can_be_parked_without_losing_its_mode() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": False,
            "mode": "single",
            "selection_mode": "four_tier_mapping",
        }
    )

    assert cfg.llm_ensemble.enabled is False
    assert cfg.llm_ensemble.mode == "single"
    assert cfg.llm_ensemble.selection_mode == "four_tier_mapping"
    assert cfg.llm_ensemble.four_tier_mapping is not None


def test_fixed_four_tier_v2_requires_single_execution_topology() -> None:
    with pytest.raises(ValueError, match=r"four_tier_mapping.*single"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "mode": "multiple",
                "selection_mode": "four_tier_mapping",
            }
        )


@pytest.mark.parametrize("mock_seed", [True, -1, 1 << 64])
def test_fixed_four_tier_v2_rejects_invalid_mock_seed(mock_seed: object) -> None:
    with pytest.raises(ValueError, match="mock_seed"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "mode": "single",
                "selection_mode": "four_tier_mapping",
                "four_tier_mapping": {"mock_seed": mock_seed},
            }
        )


def test_fixed_four_tier_v2_requires_exactly_four_tiers() -> None:
    with pytest.raises(ValueError, match="exactly c0, c1, c2, and c3"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "mode": "single",
                "selection_mode": "four_tier_mapping",
                "four_tier_mapping": {
                    "tiers": {
                        "c0": {
                            "provider": "openrouter",
                            "model": "qwen/qwen3.7-flash",
                            "reasoning": "thinking",
                            "deployment_version": "qwen3.7-flash-thinking",
                        }
                    }
                },
            }
        )


def test_fixed_four_tier_v2_rejects_ladder_override() -> None:
    defaults = FixedFourTierV2Config().model_dump(mode="json")
    defaults["tiers"]["c3"]["model"] = "some/other-model"

    with pytest.raises(ValueError, match="frozen ladder"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "mode": "single",
                "selection_mode": "four_tier_mapping",
                "four_tier_mapping": defaults,
            }
        )


def test_fixed_four_tier_v2_rejects_explicit_null_config() -> None:
    with pytest.raises(ValueError, match="four_tier_mapping must be an object"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "mode": "single",
                "selection_mode": "four_tier_mapping",
                "four_tier_mapping": None,
            }
        )


def test_fixed_four_tier_v1_config_migrates_without_mutating_input() -> None:
    legacy = FixedFourTierV2Config().model_dump(mode="json")
    legacy["schema_version"] = "fixed-four-tier-v2-mock-v1"
    legacy["max_session_states"] = 128
    for deployment in legacy["tiers"].values():
        deployment.pop("deployment_version")
    original = copy.deepcopy(legacy)

    gateway = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "mode": "single",
            "selection_mode": "four_tier_mapping",
            "four_tier_mapping": legacy,
        }
    )
    migrated = gateway.llm_ensemble.four_tier_mapping

    assert legacy == original
    assert migrated is not None
    assert migrated.schema_version == "fixed-four-tier-v2-mock-v2"
    assert {tier: deployment.deployment_version for tier, deployment in migrated.tiers.items()} == {
        "c0": "qwen3.7-flash-thinking",
        "c1": "deepseek-v4-flash-0731",
        "c2": "deepseek-v4-pro-0813",
        "c3": "glm-5.3",
    }
    serialized = migrated.model_dump(mode="json")
    assert "max_session_states" not in serialized


def test_fixed_four_tier_config_and_nested_ladder_are_deeply_immutable() -> None:
    route = FixedFourTierV2Config(mock_seed=7)
    detached_dump = route.model_dump(mode="json")

    with pytest.raises(ValidationError, match="frozen"):
        route.default_new_task_tier = "c2"
    with pytest.raises(TypeError, match="immutable"):
        route.tiers["c0"] = route.tiers["c1"]
    with pytest.raises(ValidationError, match="frozen"):
        route.tiers["c0"].model = "other/model"

    deep_copy = route.model_copy(deep=True)
    with pytest.raises(TypeError, match="immutable"):
        deep_copy.tiers.clear()
    detached_dump["tiers"]["c0"]["model"] = "other/model"
    assert route.tiers["c0"].model == "qwen/qwen3.7-flash"


@pytest.mark.parametrize(
    "selection_mode",
    [
        "static_openrouter",
        "static_tokenrhythm",
        "router_dynamic",
        "router_tree_baseline",
    ],
)
def test_legacy_selection_modes_do_not_materialize_fixed_v2_config(
    selection_mode: str,
) -> None:
    ensemble = LlmEnsembleConfig(selection_mode=selection_mode)

    assert ensemble.selection_mode == selection_mode
    assert ensemble.four_tier_mapping is None
    assert "four_tier_mapping" not in ensemble.model_dump(mode="json")


def test_router_single_ignores_fusion_only_aggregator_cross_validation() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "mode": "single",
            "selection_mode": "router_dynamic",
            "aggregator_max_tokens_cap": 2,
            "aggregator_visible_answer_reserve_tokens": 2,
            "aggregator_recovery_mode": "off",
            "aggregator_recovery_top_k": 1,
            "ranking_config_override": {"aggregator": {"candidate_count": 3}},
            "candidates": [
                {"provider": "a", "model": "m1", "role": "aggregator"},
                {"provider": "b", "model": "m2", "role": "aggregator"},
            ],
        }
    )

    ensemble = cfg.llm_ensemble
    assert ensemble.mode == "single"
    assert ensemble.aggregator_visible_answer_reserve_tokens == 2
    assert len(ensemble.candidates) == 2
    assert ensemble.ranking_config_effective_snapshot()["aggregator"]["candidate_count"] == 3


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"aggregator_max_tokens_cap": 1}, "aggregator_max_tokens_cap"),
        ({"aggregator_recovery_top_k": 4}, "aggregator_recovery_top_k"),
        (
            {
                "proposer_max_tokens_cap": 4_096,
                "proposer_visible_answer_reserve_tokens": 4_096,
            },
            "proposer_visible_answer_reserve_tokens",
        ),
    ],
)
def test_router_single_keeps_field_bounds_and_proposer_validation(
    overrides: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "mode": "single",
                "selection_mode": "router_dynamic",
                **overrides,
            }
        )


def test_llm_ensemble_admission_normalizes_runtime_limit_keys() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "admission": {
                "global_max_in_flight": 12,
                "provider_limits": {" OpenRouter ": 5},
                "deployment_limits": {"OpenRouter/Anthropic/Claude": 3},
                "deployment_weights": {"OpenRouter/Anthropic/Claude": 2},
            }
        }
    )

    admission = cfg.llm_ensemble.admission
    assert admission.global_max_in_flight == 12
    assert admission.provider_limits == {"openrouter": 5}
    assert admission.deployment_limits == {"openrouter/anthropic/claude": 3}
    assert admission.deployment_weights == {"openrouter/anthropic/claude": 2}


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_limits", {"openrouter": 0}),
        ("deployment_limits", {"missing-model": 1}),
        ("deployment_weights", {"openrouter/model": True}),
    ],
)
def test_llm_ensemble_rejects_invalid_admission_limits(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="admission|positive|provider_limits"):
        GatewayConfig(llm_ensemble={"admission": {field: value}})


@pytest.mark.parametrize(
    "field,value",
    [
        ("global_max_in_flight", True),
        ("provider_default_max_in_flight", 2.5),
        ("deployment_default_max_in_flight", "4"),
        ("queue_timeout_seconds", True),
        ("queue_timeout_seconds", "5"),
        ("queue_timeout_seconds", float("nan")),
        ("queue_timeout_seconds", float("inf")),
    ],
)
def test_llm_ensemble_rejects_coerced_or_nonfinite_admission_scalars(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="admission|finite|integer"):
        GatewayConfig(llm_ensemble={"admission": {field: value}})


def test_llm_ensemble_accepts_bounded_proposer_canary_rollout() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "canary_rollout": {
                "enabled": True,
                "global_basis_points": 500,
                "proposer": {
                    "basis_points": 250,
                    "max_candidates_per_decision": 1,
                },
            }
        }
    )

    rollout = cfg.llm_ensemble.canary_rollout
    assert rollout.enabled is True
    assert rollout.global_basis_points == 500
    assert rollout.proposer.basis_points == 250
    assert rollout.proposer.max_candidates_per_decision == 1
    assert rollout.aggregator.basis_points == 0


def test_llm_ensemble_accepts_explicit_persistent_canary_auto_rollback() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "canary_rollout": {
                "enabled": True,
                "global_basis_points": 500,
                "proposer": {
                    "basis_points": 250,
                    "max_candidates_per_decision": 1,
                    "min_observations": 20,
                },
                "auto_rollback": {
                    "enabled": True,
                    "window_max_attempts": 50,
                },
            }
        }
    )

    auto_rollback = cfg.llm_ensemble.canary_rollout.auto_rollback
    assert auto_rollback.enabled is True
    assert auto_rollback.window_max_attempts == 50
    assert auto_rollback.active_attempt_lease_seconds == 3_600


@pytest.mark.parametrize(
    "auto_rollback",
    [
        {"enabled": "true"},
        {"enabled": True, "window_max_attempts": 20},
        {
            "enabled": True,
            "rollback_cooldown_seconds": 60,
            "active_attempt_lease_seconds": 61,
        },
        {
            "enabled": True,
            "window_max_age_seconds": 301,
            "scope_retention_seconds": 300,
        },
        {"enabled": False, "unexpected": 1},
    ],
)
def test_llm_ensemble_rejects_unsafe_persistent_canary_auto_rollback(
    auto_rollback: dict[str, Any],
) -> None:
    rollout: dict[str, Any] = {"auto_rollback": auto_rollback}
    if auto_rollback.get("enabled") is True:
        rollout.update(
            {
                "enabled": True,
                "global_basis_points": 100,
                "proposer": {
                    "basis_points": 100,
                    "max_candidates_per_decision": 1,
                    "min_observations": 50,
                },
            }
        )
    with pytest.raises(ValueError, match="canary|extra|boolean|window|cooldown|retention"):
        GatewayConfig(llm_ensemble={"canary_rollout": rollout})


def test_llm_ensemble_rejects_auto_rollback_without_live_rollout() -> None:
    with pytest.raises(ValueError, match="requires the live rollout"):
        GatewayConfig(llm_ensemble={"canary_rollout": {"auto_rollback": {"enabled": True}}})


@pytest.mark.parametrize(
    "rollout",
    [
        {"enabled": "true"},
        {"global_basis_points": True},
        {"min_analyzer_confidence": 0.79},
        {"min_analyzer_confidence": 10**10_000},
        {"allowed_risks": ["medium"]},
        {"require_schema_valid_analysis": False},
        {
            "enabled": True,
            "global_basis_points": 100,
            "proposer": {"basis_points": 0, "max_candidates_per_decision": 0},
        },
        {
            "global_basis_points": 100,
            "proposer": {"basis_points": 101, "max_candidates_per_decision": 1},
        },
        {
            "global_basis_points": 100,
            "proposer": {"basis_points": 100, "max_candidates_per_decision": 0},
        },
        {
            "global_basis_points": 100,
            "aggregator": {"basis_points": 1, "max_candidates_per_decision": 1},
        },
        {"aggregator": {"min_observations": 49}},
        {"aggregator": {"max_failure_basis_points": 201}},
    ],
)
def test_llm_ensemble_rejects_unsafe_canary_rollout(rollout: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="canary|router-canary"):
        GatewayConfig(llm_ensemble={"canary_rollout": rollout})


@pytest.mark.parametrize(
    "rollout",
    [
        {"unexpected_policy_field": True},
        {"proposer": {"unexpected_role_field": 1}},
    ],
)
def test_llm_ensemble_canary_rollout_rejects_unknown_fields(
    rollout: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="extra|unexpected"):
        GatewayConfig(llm_ensemble={"canary_rollout": rollout})


def test_llm_ensemble_canary_rollout_validates_assignment() -> None:
    rollout = GatewayConfig().llm_ensemble.canary_rollout

    with pytest.raises(ValueError, match="confidence|greater than or equal"):
        rollout.min_analyzer_confidence = 0.1
    with pytest.raises(ValueError, match="less than or equal"):
        rollout.proposer.max_failure_basis_points = 501


@pytest.mark.parametrize("seed", [0, (1 << 64) - 1])
def test_llm_ensemble_accepts_uint64_candidate_order_seed(seed: int) -> None:
    cfg = GatewayConfig(llm_ensemble={"candidate_order_seed": seed})

    assert cfg.llm_ensemble.candidate_order_seed == seed


@pytest.mark.parametrize("seed", [True, -1, 1 << 64])
def test_llm_ensemble_rejects_invalid_candidate_order_seed(seed: object) -> None:
    with pytest.raises(ValueError, match="candidate_order_seed"):
        GatewayConfig(llm_ensemble={"candidate_order_seed": seed})


def test_router_dynamic_legacy_backup_count_must_match_ranking_config() -> None:
    with pytest.raises(
        ValueError,
        match="legacy compatibility input.*must match",
    ):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "selection_mode": "router_dynamic",
                "proposer_backup_count": 1,
            }
        )

    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "proposer_backup_count": 1,
            "ranking_config_override": {"proposer_count": {"backup_count": 1}},
        }
    )
    assert (
        cfg.llm_ensemble.ranking_config_effective_snapshot()["proposer_count"]["backup_count"] == 1
    )


@pytest.mark.parametrize(
    ("recovery_mode", "recovery_top_k", "candidate_count"),
    [
        ("serving", 2, 3),
        ("off", 3, 2),
    ],
)
def test_router_dynamic_rejects_ranked_aggregator_chain_runtime_would_truncate(
    recovery_mode: str,
    recovery_top_k: int,
    candidate_count: int,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"aggregator\.candidate_count.*exceeds the executable",
    ):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "selection_mode": "router_dynamic",
                "aggregator_recovery_mode": recovery_mode,
                "aggregator_recovery_top_k": recovery_top_k,
                "ranking_config_override": {"aggregator": {"candidate_count": candidate_count}},
            }
        )


@pytest.mark.parametrize(
    ("recovery_mode", "recovery_top_k", "candidate_count"),
    [
        ("serving", 2, 2),
        ("off", 3, 1),
    ],
)
def test_router_dynamic_accepts_fully_executable_ranked_aggregator_chain(
    recovery_mode: str,
    recovery_top_k: int,
    candidate_count: int,
) -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "aggregator_recovery_mode": recovery_mode,
            "aggregator_recovery_top_k": recovery_top_k,
            "ranking_config_override": {"aggregator": {"candidate_count": candidate_count}},
        }
    )

    assert (
        cfg.llm_ensemble.ranking_config_effective_snapshot()["aggregator"]["candidate_count"]
        == candidate_count
    )


def test_router_dynamic_runtime_validates_actual_ranking_input_chain() -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "aggregator_recovery_top_k": 3,
            "ranking_config_override": {"aggregator": {"candidate_count": 3}},
        },
    )
    actual_ranking_config = cfg.llm_ensemble.ranking_config_effective_snapshot()
    actual_ranking_config["aggregator"]["candidate_count"] = 2
    # The request carries a narrower authenticated ranking input than the
    # startup snapshot.  Runtime validation must inspect this actual input,
    # rather than falsely rejecting against the superseded count of three.
    cfg.llm_ensemble.aggregator_recovery_top_k = 2

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=ProviderConfig(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key="fake",
        ),
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={"ranking_config": actual_ranking_config},
    )

    assert provider.selection_plan["aggregator_recovery_top_k"] == 2
    assert len(provider.selection_plan["aggregator_candidates"]) == 2
    assert provider._admission_settings is not None
    assert provider._admission_settings.global_max_in_flight == 24

    cfg.llm_ensemble.latency_class = "experiment"
    experiment_provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=ProviderConfig(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key="fake",
        ),
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={"ranking_config": actual_ranking_config},
    )
    assert experiment_provider._admission_settings is None


def test_llm_ensemble_proposer_recovery_can_be_explicitly_disabled() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "proposer_recovery_max_additional_calls": 0,
        }
    )

    assert cfg.llm_ensemble.proposer_recovery_max_additional_calls == 0


def test_llm_ensemble_rejects_proposer_reserve_at_or_above_cap() -> None:
    with pytest.raises(ValueError, match="proposer_visible_answer_reserve_tokens"):
        GatewayConfig(
            llm_ensemble={
                "proposer_max_tokens_cap": 4_096,
                "proposer_visible_answer_reserve_tokens": 4_096,
            }
        )


def test_llm_ensemble_user_profile_switches_are_independent() -> None:
    generation_cfg = GatewayConfig(
        llm_ensemble={
            "selection_mode": "router_dynamic",
            "ranking_user_profile_generation_enabled": True,
            "ranking_user_profile_enabled": False,
        }
    )

    assert generation_cfg.llm_ensemble.ranking_user_profile_generation_enabled is True
    assert generation_cfg.llm_ensemble.ranking_user_profile_enabled is False
    serialized = generation_cfg.to_toml_dict()["llm_ensemble"]
    assert serialized["ranking_user_profile_generation_enabled"] is True
    assert serialized["ranking_user_profile_enabled"] is False

    application_cfg = GatewayConfig(
        llm_ensemble={
            "selection_mode": "router_dynamic",
            "ranking_user_profile_enabled": True,
        }
    )
    assert application_cfg.llm_ensemble.ranking_user_profile_generation_enabled is False
    assert application_cfg.llm_ensemble.ranking_user_profile_enabled is True
    serialized_application = application_cfg.to_toml_dict()["llm_ensemble"]
    assert serialized_application["ranking_user_profile_generation_enabled"] is False
    assert serialized_application["ranking_user_profile_enabled"] is True


def test_llm_ensemble_thinking_assignment_switch_is_opt_in_and_serialized() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "selection_mode": "router_dynamic",
            "ranking_thinking_assignment_enabled": True,
        }
    )

    assert cfg.llm_ensemble.ranking_thinking_assignment_enabled is True
    serialized = cfg.to_toml_dict()["llm_ensemble"]
    assert serialized["ranking_thinking_assignment_enabled"] is True


def test_llm_ensemble_ranking_override_enables_thinking_and_freezes_resolution() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "selection_mode": "router_dynamic",
            "ranking_config_override": {"thinking_assignment": {"enabled": True}},
        }
    )

    ensemble = cfg.llm_ensemble
    resolution = ensemble.ranking_config_resolution_snapshot()
    assert ensemble.ranking_thinking_assignment_enabled is True
    assert resolution["thinking_assignment_enabled"] is True
    assert resolution["base_config"]["schema_version"] == "step2-ranking-config-v3"
    assert resolution["effective_config"]["schema_version"] == "step2-ranking-config-v4"
    assert resolution["effective_config"]["thinking_assignment"]["enabled"] is True


def test_llm_ensemble_can_refreeze_an_allowlisted_historical_ranking_base() -> None:
    cfg = GatewayConfig(llm_ensemble={"selection_mode": "router_dynamic"})

    historical = cfg.llm_ensemble.freeze_ranking_config(base_version="step2-ranking-2026-08-02.2")

    assert historical["base_config"]["config_version"] == ("step2-ranking-2026-08-02.2")
    assert historical["base_sha256"] == (
        "71be283f94095bc3ced34d39ae9ed58abbaa7e4d273b0a074e7e8a4a6e4b5fc6"
    )
    assert "role_reliability" not in historical["base_config"]
    assert cfg.llm_ensemble.ranking_config_resolution_snapshot() == historical


def test_llm_ensemble_prepared_ranking_cache_reuses_only_authenticated_graphs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = GatewayConfig(llm_ensemble={"selection_mode": "router_dynamic"})
    ensemble = cfg.llm_ensemble
    prepared = ensemble.prepared_ranking_config()
    expected = ensemble.ranking_config_resolution_snapshot()
    original_validate = ranking_router._validate_ranking_config
    validation_count = 0

    def counted_validate(*args: Any, **kwargs: Any) -> Any:
        nonlocal validation_count
        validation_count += 1
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(ranking_router, "_validate_ranking_config", counted_validate)
    for _ in range(100):
        cached = ensemble.prepared_ranking_config()
        assert cached is prepared
        assert (
            _prepare_effective_ranking_config(
                cached,
                thinking_assignment_enabled=(ensemble.ranking_thinking_assignment_enabled),
            )
            is prepared
        )

    assert validation_count == 0
    assert _is_validated_ranking_config(prepared) is True
    assert canonical_json_sha256(prepared) == expected["effective_sha256"]
    assert json.loads(canonical_json_bytes(prepared)) == expected["effective_config"]

    # A byte-identical ordinary dict has no cache authority. It is validated
    # and detached before becoming a distinct immutable root.
    external = ensemble.ranking_config_effective_snapshot()
    external_prepared = _prepare_effective_ranking_config(
        external,
        thinking_assignment_enabled=ensemble.ranking_thinking_assignment_enabled,
    )
    assert validation_count == 1
    assert external_prepared is not prepared
    external["rerank"]["similarity_penalty_weight"] = 0.0
    assert external_prepared["rerank"]["similarity_penalty_weight"] != 0.0


def test_router_single_freezes_the_authenticated_prepared_ranking_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ranking_override = {"aggregator": {"candidate_count": 3}}
    fusion = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "mode": "multiple",
            "selection_mode": "router_dynamic",
            "aggregator_recovery_top_k": 3,
            "ranking_config_override": ranking_override,
        }
    ).llm_ensemble
    single = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "mode": "single",
            "selection_mode": "router_dynamic",
            "aggregator_recovery_mode": "off",
            "aggregator_recovery_top_k": 1,
            "ranking_config_override": ranking_override,
        }
    ).llm_ensemble

    assert single._ranking_config_frozen_state is not None
    single_resolution = single.ranking_config_resolution_snapshot()
    fusion_resolution = fusion.ranking_config_resolution_snapshot()
    prepared = single.prepared_ranking_config()
    assert single_resolution["effective_sha256"] == fusion_resolution["effective_sha256"]
    assert canonical_json_sha256(prepared) == single_resolution["effective_sha256"]
    assert json.loads(canonical_json_bytes(prepared)) == single_resolution["effective_config"]
    assert _is_validated_ranking_config(prepared) is True

    original_validate = ranking_router._validate_ranking_config
    validation_count = 0

    def counted_validate(*args: Any, **kwargs: Any) -> Any:
        nonlocal validation_count
        validation_count += 1
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(ranking_router, "_validate_ranking_config", counted_validate)
    for _ in range(100):
        assert single.prepared_ranking_config() is prepared
    copied = single.model_copy(deep=True)
    assert copied.prepared_ranking_config() is prepared
    assert validation_count == 0


def test_llm_ensemble_prepared_ranking_cache_refreeze_evicts_without_turn_drift() -> None:
    cfg = GatewayConfig(llm_ensemble={"selection_mode": "router_dynamic"})
    ensemble = cfg.llm_ensemble
    old_prepared = ensemble.prepared_ranking_config()
    old_reference = weakref.ref(old_prepared)
    old_sha256 = canonical_json_sha256(old_prepared)

    ensemble.ranking_config_override = {"thinking_assignment": {"enabled": True}}
    new_resolution = ensemble.freeze_ranking_config()
    new_prepared = ensemble.prepared_ranking_config()

    assert new_prepared is not old_prepared
    assert old_prepared["schema_version"] == "step2-ranking-config-v3"
    assert new_prepared["schema_version"] == "step2-ranking-config-v4"
    assert ensemble.ranking_thinking_assignment_enabled is True
    assert canonical_json_sha256(new_prepared) == new_resolution["effective_sha256"]
    assert canonical_json_sha256(new_prepared) != old_sha256
    assert canonical_json_sha256(old_prepared) == old_sha256
    assert (
        _prepare_effective_ranking_config(
            old_prepared,
            thinking_assignment_enabled=False,
        )
        is old_prepared
    )

    del old_prepared
    gc.collect()
    assert old_reference() is None


def test_llm_ensemble_prepared_ranking_cache_deepcopy_and_gc_lifecycle() -> None:
    cfg = GatewayConfig(llm_ensemble={"selection_mode": "router_dynamic"})
    prepared = cfg.llm_ensemble.prepared_ranking_config()
    prepared_reference = weakref.ref(prepared)
    copied = cfg.model_copy(deep=True)

    assert copied.llm_ensemble.prepared_ranking_config() is prepared
    del prepared
    del cfg
    gc.collect()
    assert prepared_reference() is not None

    del copied
    gc.collect()
    assert prepared_reference() is None


def test_llm_ensemble_prepared_ranking_cache_concurrent_refreeze_pairs_state() -> None:
    cfg = GatewayConfig(llm_ensemble={"selection_mode": "router_dynamic"})
    ensemble = cfg.llm_ensemble
    current = ensemble.freeze_ranking_config()
    historical = ensemble.freeze_ranking_config(base_version="step2-ranking-2026-08-02.2")
    allowed_hashes = {current["effective_sha256"], historical["effective_sha256"]}

    def refreeze(base_version: str | None) -> None:
        for _ in range(20):
            ensemble.freeze_ranking_config(base_version=base_version)

    def read_pairs() -> None:
        for _ in range(200):
            state = ensemble._ranking_config_frozen_state
            assert state is not None
            resolution, prepared = state
            assert _is_validated_ranking_config(prepared) is True
            assert resolution["effective_sha256"] in allowed_hashes
            assert (
                canonical_json_sha256(resolution["effective_config"])
                == (resolution["effective_sha256"])
            )
            assert canonical_json_sha256(prepared) == resolution["effective_sha256"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(refreeze, None),
            executor.submit(
                refreeze,
                "step2-ranking-2026-08-02.2",
            ),
            executor.submit(read_pairs),
            executor.submit(read_pairs),
        ]
        for future in futures:
            future.result()


def test_llm_ensemble_explicit_legacy_thinking_switch_conflict_fails_closed() -> None:
    with pytest.raises(ValueError, match="conflicts with the legacy"):
        GatewayConfig(
            llm_ensemble={
                "selection_mode": "router_dynamic",
                "ranking_thinking_assignment_enabled": False,
                "ranking_config_override": {"thinking_assignment": {"enabled": True}},
            }
        )


def test_llm_ensemble_serving_chain_timeout_serializes_and_reaches_provider() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter",
            "aggregator_serving_chain_timeout_seconds": 45.0,
        }
    )

    serialized = cfg.to_toml_dict()["llm_ensemble"]
    assert serialized["aggregator_serving_chain_timeout_seconds"] == 45.0

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

    assert provider.aggregator_serving_chain_timeout_seconds == 45.0
    assert provider.selection_plan["aggregator_serving_chain_timeout_seconds"] == 45.0


def test_static_openrouter_b5_does_not_need_model_options() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter",
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
            "selection_mode": "static_tokenrhythm",
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
    assert all(member.provider_config.provider == "tokenrhythm" for member in provider.proposers)
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
    literal_modes = set(get_args(LlmEnsembleConfig.model_fields["selection_mode"].annotation))
    assert literal_modes == {
        "router_dynamic",
        "router_tree_baseline",
        "custom",
        "four_tier_mapping",
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
    # The packaged JSON snapshot remains visible for replay, but deployments
    # without an OpenRouter credential are removed by availability filtering.
    openrouter_ids = {
        candidate["identity"] for candidate in pool if candidate["provider"] == "openrouter"
    }
    proposer_filters = provider.selection_plan["hard_filter"]["proposer_results"]
    assert openrouter_ids
    assert all(
        "credential_unavailable" in row["reasons"]
        for row in proposer_filters
        if row["identity"] in openrouter_ids
    )


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


def test_router_dynamic_registry_all_uses_every_frozen_source_model() -> None:
    experiment = load_draco_experiment_config(
        ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
    ).config
    assert experiment.g1_routing is not None
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "base_url": "https://openrouter.example/api/v1",
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
    )

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={"registry_allowlist": experiment.g1_routing.model_dump(mode="json")},
        _provider_health_ledger=object(),
    )

    plan = provider.selection_plan
    assert provider._provider_health_ledger is None
    assert "runtime_health_filter" not in plan
    source_registry = load_model_registry_snapshot(
        base_version=experiment.g1_routing.source_registry_snapshot_version
    )
    expected = {
        f"openrouter:{row['registry_facts']['model_id']}" for row in source_registry["models"]
    }
    assert plan["candidate_pool_size"] == len(expected)
    assert {row["identity"] for row in plan["candidate_pool"]} == expected
    assert set(plan["selected_P"]) <= expected
    assert plan["selected_A"] in expected
    assert plan["proposer_sample_count"] == len(plan["selected_P"])
    assert len(plan["proposer_models"]) == len(plan["selected_P"])
    assert plan["aggregator_model"] == plan["selected_A"].partition(":")[2]
    allowlist = plan["candidate_allowlist"]
    assert allowlist["profile_id"] == experiment.g1_routing.profile_id
    assert allowlist["policy"] == "all_registry_models"
    assert allowlist["candidate_scope"] == "registry_all"
    assert allowlist["candidate_count"] == len(expected)
    assert allowlist["input_candidate_count"] == len(expected)
    assert allowlist["excluded_candidate_count"] == 0
    assert len(allowlist["expected_routes_sha256"]) == 64
    assert allowlist["expected_source_registry_snapshot_sha256"] == (
        experiment.g1_routing.expected_source_registry_snapshot_sha256
    )


def test_router_dynamic_new_registry_contract_requires_explicit_policy() -> None:
    experiment = load_draco_experiment_config(
        ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
    ).config
    assert experiment.g1_routing is not None
    contract = experiment.g1_routing.model_dump(mode="json", exclude_none=True)
    contract["candidate_scope"] = "registry_all"
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )

    with pytest.raises(ValueError, match="policy differs from candidate scope"):
        build_ensemble_provider_from_config(
            config=cfg,
            inherited_provider_config=ProviderConfig(
                provider="openrouter",
                model="deepseek/deepseek-v4-pro",
                api_key="fake",
            ),
            fallback_provider=None,
            turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
            ranking_inputs={"registry_allowlist": contract},
        )


def test_router_dynamic_explicit_registry_allowlist_still_filters_pool() -> None:
    experiment = load_draco_experiment_config(
        ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
    ).config
    assert experiment.g1_routing is not None
    routes = {
        "deepseek/deepseek-v4-pro": "deepseek",
        "z-ai/glm-5.2": "z-ai",
    }
    contract = experiment.g1_routing.model_dump(mode="json", exclude_none=True)
    contract.update(
        {
            "candidate_scope": "exact_routes",
            "policy": "exact_openrouter_routes",
            "expected_candidate_count": len(routes),
            "expected_routes": routes,
            "expected_routes_sha256": hashlib.sha256(
                json.dumps(
                    routes,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
    )
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "base_url": "https://openrouter.example/api/v1",
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
    )

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={"registry_allowlist": contract},
    )

    expected = {f"openrouter:{model}" for model in routes}
    plan = provider.selection_plan
    assert plan["candidate_pool_size"] == len(routes)
    assert {row["identity"] for row in plan["candidate_pool"]} == expected
    assert plan["candidate_allowlist"]["policy"] == "exact_openrouter_routes"
    source_registry = load_model_registry_snapshot(
        base_version=experiment.g1_routing.source_registry_snapshot_version
    )
    assert plan["candidate_allowlist"]["excluded_candidate_count"] == (
        len(source_registry["models"]) - len(routes)
    )


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
        member.provider_config.base_url == "https://openrouter.example/api/v1" for member in members
    )
    assert all(member.provider_config.proxy == "http://proxy.local:7890" for member in members)
    assert provider.aggregator.provider_config.provider_routing == {"z-ai/glm-5.2": "z-ai"}


@pytest.mark.parametrize("routed_tier", ["c0", "t1"])
def test_router_dynamic_ensemble_uses_step2_default_tier1_single_proposer_bound(
    routed_tier: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "opensquilla.provider.tree_baseline_router",
        None,
    )
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
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
        turn_metadata={"routed_tier": routed_tier, "routing_confidence": 0.93},
    )

    assert provider.profile_name == "router_dynamic/c0"
    assert [member.label for member in provider.proposers] == ["proposer_1"]
    assert len(provider.proposers) == 1
    assert provider.min_successful_proposers == 1
    assert provider.selection_plan["configured_min_successful_proposers"] == 1
    assert provider.selection_plan["effective_min_successful_proposers"] == 1
    assert provider.selection_plan["ranking_version"] == "step2-ranking-v2"
    assert provider.selection_plan["user_profile_enabled"] is False
    assert provider.selection_plan["N_min"] == 1
    assert provider.selection_plan["N_max"] == 1
    assert provider.selection_plan["selected_P"] == [
        f"{provider.proposers[0].provider_config.provider}:"
        f"{provider.proposers[0].provider_config.model}"
    ]
    assert provider.selection_plan["selected_A"] == (
        f"{provider.aggregator.provider_config.provider}:"
        f"{provider.aggregator.provider_config.model}"
    )
    assert provider.selection_plan["selection_steps"][0]["step"] == 1
    assert provider.proposer_timeout_seconds == 3600.0
    assert provider.aggregator_timeout_seconds == 3600.0
    assert provider.quorum_grace_seconds == 2.0
    assert provider.selection_plan["quorum_grace_seconds"] == 2.0


def test_router_dynamic_explicit_quorum_one_is_not_treated_as_auto() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "min_successful_proposers": 1,
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

    assert "min_successful_proposers" in cfg.llm_ensemble.model_fields_set
    assert provider.selection_plan["N_min"] == 2
    assert len(provider.proposers) >= 2
    assert provider.min_successful_proposers == 1
    assert provider.selection_plan["configured_min_successful_proposers"] == 1
    assert provider.selection_plan["effective_min_successful_proposers"] == 1
    assert provider.selection_plan["proposer_recovery_policy"]["quorum_required"] == 1


def test_router_dynamic_explicit_quorum_two_lifts_c0_selection_bound() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "min_successful_proposers": 2,
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

    assert provider.selection_plan["N_min"] == 2
    assert provider.selection_plan["N_max"] == 2
    assert len(provider.proposers) >= 2
    assert provider.min_successful_proposers == 2
    assert provider.selection_plan["effective_min_successful_proposers"] == 2
    assert provider.selection_plan["proposer_recovery_policy"]["quorum_required"] == 2


def test_router_dynamic_explicit_unreachable_quorum_fails_before_provider_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = load_draco_experiment_config(
        ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
    ).config
    assert experiment.g1_routing is not None
    routes = {"deepseek/deepseek-v4-pro": "deepseek"}
    contract = experiment.g1_routing.model_dump(mode="json", exclude_none=True)
    contract.update(
        {
            "candidate_scope": "exact_routes",
            "policy": "exact_openrouter_routes",
            "expected_candidate_count": len(routes),
            "expected_routes": routes,
            "expected_routes_sha256": hashlib.sha256(
                json.dumps(
                    routes,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
    )
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "base_url": "https://openrouter.example/api/v1",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "min_successful_proposers": 2,
        },
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
    )
    provider_constructions: list[bool] = []

    def unexpected_provider_construction(
        *args: object,
        **kwargs: object,
    ) -> None:
        provider_constructions.append(True)
        raise AssertionError("provider construction must not be reached")

    monkeypatch.setattr(
        "opensquilla.provider.ensemble.EnsembleProvider",
        unexpected_provider_construction,
    )

    with pytest.raises(
        DynamicRankingError,
        match=r"quorum requires 2 proposer\(s\).*hard filtering left 1 eligible",
    ) as exc_info:
        build_ensemble_provider_from_config(
            config=cfg,
            inherited_provider_config=inherited,
            fallback_provider=None,
            turn_metadata={"routed_tier": "c0", "routing_confidence": 0.93},
            ranking_inputs={"registry_allowlist": contract},
        )

    assert exc_info.value.reason == "proposer_recovery_quorum_unreachable"
    assert provider_constructions == []


@pytest.mark.parametrize(
    "invalid_quorum",
    [
        pytest.param(0, id="zero"),
        pytest.param(False, id="boolean-false"),
        pytest.param("2", id="numeric-string"),
    ],
)
def test_router_dynamic_runtime_invalid_quorum_fails_before_ranking_or_provider(
    invalid_quorum: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
        }
    )
    cfg.llm_ensemble.min_successful_proposers = invalid_quorum
    ranking_calls: list[bool] = []
    provider_constructions: list[bool] = []

    def unexpected_ranking(
        *args: object,
        **kwargs: object,
    ) -> None:
        ranking_calls.append(True)
        raise AssertionError("ranking must not be reached")

    def unexpected_provider_construction(
        *args: object,
        **kwargs: object,
    ) -> None:
        provider_constructions.append(True)
        raise AssertionError("provider construction must not be reached")

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.rank_models",
        unexpected_ranking,
    )
    monkeypatch.setattr(
        "opensquilla.provider.ensemble.EnsembleProvider",
        unexpected_provider_construction,
    )

    with pytest.raises(
        ValueError,
        match=(
            r"router_dynamic llm_ensemble\.min_successful_proposers "
            r"must be a non-boolean integer >= 1"
        ),
    ):
        build_ensemble_provider_from_config(
            config=cfg,
            inherited_provider_config=ProviderConfig(
                provider="openrouter",
                model="deepseek/deepseek-v4-flash",
                api_key="fake",
                base_url="https://openrouter.example/api/v1",
            ),
            fallback_provider=None,
            turn_metadata={"routed_tier": "c0", "routing_confidence": 0.93},
        )

    assert ranking_calls == []
    assert provider_constructions == []


def test_router_dynamic_ensemble_uses_step2_greedy_c2_selection() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_user_profile_enabled": True,
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
    assert provider.selection_plan["user_profile_enabled"] is True
    assert [member.label for member in provider.proposers] == [
        f"proposer_{index + 1}" for index in range(len(provider.proposers))
    ]
    assert 2 <= len(provider.proposers) <= 3
    assert provider.min_successful_proposers == 2
    assert provider.selection_plan["N_min"] == 2
    assert provider.selection_plan["N_max"] == 3
    assert len(provider.selection_plan["selection_steps"]) == len(provider.proposers)
    assert "Score_agg" in provider.selection_plan["aggregator"]["selected"]
    assert provider.selection_plan["hard_filter"]["eligible_proposer_ids"]
    assert provider.selection_plan["candidate_pool_size"] >= 5


@pytest.mark.parametrize(
    (
        "routed_tier",
        "expected_tier",
        "expected_slots",
        "aggregator_slot",
    ),
    [
        ("c0", "c0", ["anchor", "cheap_contrast"], "aggregator_fast"),
        ("t1", "c1", ["anchor", "balanced_contrast"], "aggregator_balanced"),
        (
            "c2",
            "c2",
            ["anchor", "adjacent_tier_check", "orthogonal_family"],
            "aggregator_strong",
        ),
        (
            "c3",
            "c3",
            ["anchor", "strong_critic", "orthogonal_family", "fast_sanity"],
            "aggregator_strong",
        ),
    ],
)
def test_router_tree_baseline_restores_legacy_local_tree_templates(
    routed_tier: str,
    expected_tier: str,
    expected_slots: list[str],
    aggregator_slot: str,
) -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_tree_baseline",
            "min_successful_proposers": 9,
            "model_options": [
                "deepseek/deepseek-v4-flash",
                "deepseek/deepseek-v4-pro",
                "z-ai/glm-5.2",
                "qwen/qwen3.7-max",
                "anthropic/claude-opus-4.8",
            ],
        }
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model={
            "c0": "deepseek/deepseek-v4-flash",
            "c1": "deepseek/deepseek-v4-pro",
            "c2": "z-ai/glm-5.2",
            "c3": "anthropic/claude-opus-4.8",
        }[expected_tier],
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
    )

    provider = build_ensemble_provider_from_config(
        config=cfg,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": routed_tier, "routing_confidence": 0.91},
    )

    assert provider.profile_name == f"router_tree_baseline/{expected_tier}"
    assert [member.label for member in provider.proposers] == expected_slots
    assert provider.selection_plan["strategy"] == "router_tree_baseline"
    assert provider.selection_plan["router_source"] == "squilla_router_local_tree"
    assert provider.selection_plan["uses_remote_task_analyzer"] is False
    assert provider.selection_plan["aggregator_slot"] == aggregator_slot
    assert provider.selection_plan["algorithm_version"] == "legacy-router-dynamic-v1"
    assert len(provider.selection_plan["config_hash"]) == 64
    assert provider.min_successful_proposers == len(expected_slots)
    assert provider.selection_plan["selected_P"] == [
        f"{member.provider_config.provider}:{member.provider_config.model}"
        for member in provider.proposers
    ]
    assert provider.selection_plan["selected_A"] == (
        f"{provider.aggregator.provider_config.provider}:"
        f"{provider.aggregator.provider_config.model}"
    )


def test_static_openrouter_b5_ensemble_locks_members_across_routed_tiers() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter",
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
            "latency_class": "normal",
            "aggregator_serving_chain_timeout_seconds": 120.0,
            "configured_shuffle_candidates": False,
            "effective_shuffle_candidates": False,
            "quorum_grace_seconds": 10.0,
            "selection_mode": "static_openrouter",
            "aggregator_recovery_mode": "serving",
            "aggregator_recovery_top_k": 3,
            "aggregator_max_tokens_cap": 65_536,
            "aggregator_visible_answer_reserve_tokens": 8_192,
            "aggregator_candidates": ["openrouter:z-ai/glm-5.2"],
            "selected_P": [f"openrouter:{model}" for model in expected_proposers],
            "selected_A": "openrouter:z-ai/glm-5.2",
        }
        assert provider.aggregator.provider_config.replay_provider_state is True
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
            "selection_mode": "static_openrouter",
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
            "selection_mode": "static_openrouter",
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
        "selection_mode": "custom",
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
            "selection_mode": "custom",
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
            "selection_mode": "custom",
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
                "selection_mode": "custom",
                "candidates": [{"provider": "a", "model": "m1"}],
            }
        )
    with pytest.raises(Exception, match="at most 6"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "selection_mode": "custom",
                "candidates": [{"provider": "a", "model": f"m{i}"} for i in range(7)],
            }
        )


def test_custom_b5_validation_rejects_quorum_above_proposer_count() -> None:
    with pytest.raises(Exception, match="min_successful_proposers"):
        GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "selection_mode": "custom",
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
            "selection_mode": "custom",
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
            "selection_mode": "custom",
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
            "selection_mode": "custom",
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
            "selection_mode": "custom",
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

    events = [event async for event in wrapper.chat([Message(role="user", content="synthetic")])]

    assert any(
        isinstance(event, TextDeltaEvent) and event.text == "fallback answer" for event in events
    )
    assert selector_builds[-1].model == "plugin-fallback"
    assert selector_builds[-1].replay_provider_state is False
    assert turn_selector.current_config.replay_provider_state is False
    assert direct_fallback.replay_provider_state is False
    assert plugin_fallback_config.replay_provider_state is True
    assert shared_selector.current_config.replay_provider_state is True


@pytest.mark.asyncio
async def test_selector_fallback_cannot_bypass_routed_thinking_policy() -> None:
    from opensquilla.engine.runtime import _SelectorFallbackProvider
    from opensquilla.provider.types import ErrorEvent

    class _ManagedEnsemble:
        provider_name = "ensemble"
        enforces_routed_thinking_policy = True

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            yield ErrorEvent(message="rate limited", code="429")

    class _UnsafeFallback:
        provider_name = "fallback"

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            yield ErrorEvent(message="must not run", code="unsafe")

    class _Selector:
        active_provider_id = "openrouter"
        current_config = ProviderConfig(
            provider="openrouter",
            model="unsafe-fallback",
        )

        def __init__(self) -> None:
            self.fallback_calls = 0

        def next_fallback_after_failure(self, error):
            del error
            self.fallback_calls += 1
            return _UnsafeFallback()

    selector = _Selector()
    wrapper = _SelectorFallbackProvider(_ManagedEnsemble(), selector)

    events = [event async for event in wrapper.chat([Message(role="user", content="synthetic")])]

    assert selector.fallback_calls == 0
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "429"
    assert wrapper.fallback_after_invalid_response("empty response") is False
    assert selector.fallback_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "expected_mutations"),
    [("direct", 0), ("benched_skip", 1), ("plugin_replacement", 1)],
)
async def test_selector_fallback_cannot_bypass_live_canary_governance(
    route: str,
    expected_mutations: int,
) -> None:
    from opensquilla.engine.runtime import _SelectorFallbackProvider
    from opensquilla.provider.types import ErrorEvent

    calls: list[str] = []

    class _Provider:
        provider_name = "openrouter"

        def __init__(self, model: str) -> None:
            self.model = model

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            calls.append(self.model)
            yield ErrorEvent(message="rate limited", code="429")

    class _Primary(_Provider):
        selector_fallback_governance_active = True
        enforces_routed_thinking_policy = False

        @staticmethod
        def selector_fallback_allowed(
            provider: object,
            model: object,
        ) -> bool:
            return f"{provider}:{model}" != "openrouter:canary"

    class _Ledger:
        def record_failure(self, *args, **kwargs) -> None:
            del args, kwargs

        def record_success(self, *args, **kwargs) -> None:
            del args, kwargs

        @staticmethod
        def eligible(provider: str, model: str, candidates) -> bool:
            del provider, candidates
            return model != "enabled-benched"

    class _Selector:
        def __init__(self) -> None:
            self._index = 0
            self.mutations = 0
            self._chain = [
                ProviderConfig("openrouter", "primary"),
                ProviderConfig(
                    "openrouter",
                    "canary" if route == "direct" else "enabled-benched",
                ),
            ]
            if route == "benched_skip":
                self._chain.append(ProviderConfig("openrouter", "canary"))

        @property
        def current_config(self) -> ProviderConfig:
            return self._chain[self._index]

        @property
        def active_provider_id(self) -> str:
            return self.current_config.provider

        def remaining_chain(self) -> list[ProviderConfig]:
            return self._chain[self._index :]

        def has_fallback(self) -> bool:
            return self._index < len(self._chain) - 1

        def next_fallback_after_failure(self, error) -> _Provider:
            del error
            self.mutations += 1
            if route == "plugin_replacement":
                self._chain = [
                    self.current_config,
                    ProviderConfig("openrouter", "canary"),
                ]
            self._index = 1
            return _Provider(self.current_config.model)

        def next_fallback(self) -> _Provider:
            self.mutations += 1
            self._index += 1
            return _Provider(self.current_config.model)

    selector = _Selector()
    wrapper = _SelectorFallbackProvider(
        _Primary("primary"),
        selector,
        health_ledger=_Ledger(),
    )

    events = [event async for event in wrapper.chat([Message(role="user", content="synthetic")])]

    assert calls == ["primary"]
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "429"
    assert wrapper.fallback_after_invalid_response("empty response") is False
    assert calls == ["primary"]
    assert selector.mutations == expected_mutations


@pytest.mark.asyncio
@pytest.mark.parametrize("flip_phase", ["primary_close", "lazy_dispatch"])
async def test_selector_fallback_rechecks_canary_status_at_physical_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    flip_phase: str,
) -> None:
    import opensquilla.engine.runtime as runtime
    from opensquilla.provider.types import ErrorEvent

    status = {"value": "enabled"}
    calls: list[str] = []
    metadata: dict[str, Any] = {}

    class _Primary:
        provider_name = "openrouter"
        selector_fallback_governance_active = True
        enforces_routed_thinking_policy = False

        @staticmethod
        def selector_fallback_allowed(provider: object, model: object) -> bool:
            del provider, model
            return status["value"] != "canary"

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            calls.append("primary")
            try:
                yield ErrorEvent(
                    message="rate limited",
                    code="429",
                    request_started=True,
                    physical_request_count=1,
                )
            finally:
                if flip_phase == "primary_close":
                    status["value"] = "canary"

    class _Fallback:
        provider_name = "openrouter"

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            calls.append("fallback")
            yield ErrorEvent(message="must not run", code="unsafe")

    class _Selector:
        active_provider_id = "openrouter"

        def __init__(self) -> None:
            self._index = 0
            self._chain = [
                ProviderConfig("openrouter", "primary"),
                ProviderConfig("openrouter", "fallback"),
            ]

        @property
        def current_config(self) -> ProviderConfig:
            return self._chain[self._index]

        def remaining_chain(self) -> list[ProviderConfig]:
            return self._chain[self._index :]

        def has_fallback(self) -> bool:
            return self._index < len(self._chain) - 1

        def next_fallback_after_failure(self, error) -> _Fallback:
            del error
            self._index = 1
            return _Fallback()

    real_account_provider_stream = runtime.account_provider_stream
    if flip_phase == "lazy_dispatch":
        accounting_calls = 0

        async def flip_before_lazy_factory(stream_factory, **kwargs):
            nonlocal accounting_calls
            accounting_calls += 1
            if accounting_calls == 2:
                status["value"] = "canary"
            async for account_event in real_account_provider_stream(
                stream_factory,
                **kwargs,
            ):
                yield account_event

        monkeypatch.setattr(
            runtime,
            "account_provider_stream",
            flip_before_lazy_factory,
        )

    primary = _Primary()
    wrapper = runtime._SelectorFallbackProvider(
        primary,
        _Selector(),
        turn_metadata=metadata,
    )

    events = [event async for event in wrapper.chat([Message(role="user", content="synthetic")])]

    assert calls == ["primary"]
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    if flip_phase == "primary_close":
        assert events[0].code == "429"
    else:
        assert events[0].code == "ensemble_canary_fallback_blocked"
        assert events[0].request_started is False
        assert events[0].physical_request_count == 0
    assert wrapper.primary is primary
    assert "router_fallback_hops" not in metadata
    assert "executed_model" not in metadata


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
            "selection_mode": "custom",
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


def test_tree_baseline_uses_shared_session_pinned_profile_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.selector_override import (
        acquire_profile_credential,
        report_profile_credential_failure,
    )
    from opensquilla.gateway.llm_runtime import reset_profile_credential_pools

    env_a = "OPENSQUILLA_TEST_TREE_OPENROUTER_A"
    env_b = "OPENSQUILLA_TEST_TREE_OPENROUTER_B"
    key_a = "sk-test-tree-a"
    key_b = "sk-test-tree-b"
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
            "openrouter": LlmProviderProfile(api_key_env_pool=[env_a, env_b]),
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_tree_baseline",
        },
    )
    inherited = ProviderConfig(
        provider="volcengine",
        model="doubao-primary",
        api_key="volc-key",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
    )

    try:
        first = build_ensemble_provider_from_config(
            config=cfg,
            inherited_provider_config=inherited,
            fallback_provider=None,
            _credential_pool_acquirer=acquire_profile_credential,
            _credential_pool_failure_reporter=report_profile_credential_failure,
            _session_key="tree-session",
        )
        first_members = [*first.proposers, first.aggregator]
        assert first.selection_plan["model_options_source"] == "frozen_default"
        first_openrouter_members = [
            member for member in first_members if member.provider_config.provider == "openrouter"
        ]
        first_keys = {member.provider_config.api_key for member in first_openrouter_members}
        assert len(first_keys) == 1
        first_key = first_keys.pop()
        assert all(
            member.credential_pool_session_key == "tree-session"
            for member in first_openrouter_members
        )

        first._report_member_credential_failure(
            first_openrouter_members[0],
            message="invalid api key",
            code="401",
        )
        second = build_ensemble_provider_from_config(
            config=cfg,
            inherited_provider_config=inherited,
            fallback_provider=None,
            _credential_pool_acquirer=acquire_profile_credential,
            _credential_pool_failure_reporter=report_profile_credential_failure,
            _session_key="tree-session",
        )
        second_keys = {
            member.provider_config.api_key
            for member in [*second.proposers, second.aggregator]
            if member.provider_config.provider == "openrouter"
        }
        assert second_keys == ({key_a, key_b} - {first_key})
    finally:
        reset_profile_credential_pools()
