from __future__ import annotations

import copy
from pathlib import Path

import pytest
import tomli_w

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.model_routing import (
    capture_model_routing_config,
    durable_model_routing_config_snapshot,
    restore_durable_model_routing_config_snapshot,
)
from opensquilla.onboarding.config_store import load_config, persist_config
from opensquilla.onboarding.mutations import upsert_llm_provider, upsert_router
from opensquilla.onboarding.recommendation_policy import (
    apply_recommendation,
    current_recommendation_rows,
    initialize_new_recommendations,
)
from opensquilla.provider.ensemble import (
    build_ensemble_provider_from_config,
    ensemble_runtime_status,
)
from opensquilla.provider.selector import ProviderConfig


def recommendation(provider: str, kind: str, revision: str = "selected-v1") -> dict:
    roles = ["c0", "c1", "c2", "c3"] if kind == "router" else [
        "proposer_1", "proposer_2", "proposer_3", "aggregator",
    ]
    return {
        "providerId": provider, "kind": kind, "revision": revision, "source": "remote",
        "rows": [
            {"role": role, "provider": provider, "model": f"synthetic-{revision}-{role}",
             "thinkingLevel": [None, "medium", "high", "low"][index]}
            for index, role in enumerate(roles)
        ],
    }


@pytest.mark.parametrize("provider", ["openrouter", "tokenrhythm"])
def test_saved_router_survives_reload_and_other_settings(provider: str, tmp_path: Path) -> None:
    config = GatewayConfig(llm={
        "provider": provider, "model": "synthetic-primary", "api_key": "synthetic-key",
    })
    config.state_dir = str(tmp_path / "state")
    config.llm_ensemble.proposer_max_retries = 2
    other_scheme = config.llm_ensemble.model_dump()
    original_llm = config.llm.model_dump()
    plan = recommendation(provider, "router")
    apply_recommendation(config, plan)
    assert config.llm.model_dump() == original_llm
    assert config.llm_ensemble.model_dump() == other_scheme
    path = tmp_path / "config.toml"
    persist_config(config, path=path)
    reloaded = load_config(path)
    assert reloaded.llm.provider == provider
    assert current_recommendation_rows(reloaded, provider, "router") == plan["rows"]
    assert reloaded.squilla_router.tier_profile is None
    changed = upsert_llm_provider(
        reloaded, provider_id=provider, model="synthetic-new-fixed", api_key="synthetic-key",
    )
    assert current_recommendation_rows(changed.config, provider, "router") == plan["rows"]
    snapshot = capture_model_routing_config(reloaded, session_mode="router")
    assert snapshot.squilla_router.tiers == reloaded.squilla_router.tiers
    edited = copy.deepcopy(reloaded.squilla_router.tiers)
    edited["c0"]["model"] = "synthetic-user-choice"
    edited_config = upsert_router(reloaded, tiers=edited).config
    persist_config(edited_config, path=path)
    assert load_config(path).squilla_router.tiers["c0"]["model"] == "synthetic-user-choice"


@pytest.mark.parametrize("provider", ["openrouter", "tokenrhythm"])
def test_ensemble_models_and_thinking_are_frozen_for_running_and_recovered_tasks(
    provider: str, tmp_path: Path,
) -> None:
    cfg = GatewayConfig(llm={"provider": provider, "model": "synthetic-primary"})
    router = cfg.squilla_router.model_dump()
    cfg.llm_ensemble.proposer_timeout_seconds = 47
    cfg.llm_ensemble.proposer_max_retries = 2
    plan = recommendation(provider, "ensemble")
    apply_recommendation(cfg, plan)
    assert cfg.squilla_router.model_dump() == router
    assert cfg.llm_ensemble.enabled is False
    snapshot = capture_model_routing_config(cfg, session_mode="ensemble")
    durable = durable_model_routing_config_snapshot(snapshot)
    restored = restore_durable_model_routing_config_snapshot(
        durable, session_mode="ensemble", session_routing_revision=None,
        session_routing_source="session_persisted",
    )
    apply_recommendation(cfg, recommendation(provider, "ensemble", "selected-v2"))
    old = restored.overlay_live_config(cfg)
    member_provider = ProviderConfig(provider=provider, model="synthetic-primary", api_key="dummy")
    ensemble = build_ensemble_provider_from_config(
        config=old, inherited_provider_config=member_provider,
        fallback_provider=None, turn_metadata={},
    )
    assert [row.provider_config.model for row in ensemble.proposers] == [
        row["model"] for row in plan["rows"][:-1]
    ]
    assert [row.thinking for row in ensemble.proposers] == [None, "medium", "high"]
    assert ensemble.aggregator.thinking == "low"
    assert old.llm_ensemble.proposer_timeout_seconds == 47
    assert old.llm_ensemble.proposer_max_retries == 2
    path = tmp_path / "config.toml"
    persist_config(cfg, path=path)
    assert current_recommendation_rows(load_config(path), provider, "ensemble") == recommendation(
        provider, "ensemble", "selected-v2",
    )["rows"]


@pytest.mark.parametrize("provider", ["openrouter", "tokenrhythm"])
@pytest.mark.parametrize("retries", [None, 0, 2])
def test_ensemble_recommendation_preserves_c3_retry_policy(
    provider: str, retries: int | None, tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(tomli_w.dumps({
        "config_version": 2,
        "llm": {"provider": provider, "model": "synthetic-primary", "api_key": "synthetic-key"},
        "squilla_router": {"enabled": True, "tiers": {
            "c3": {"provider": provider, "model": "synthetic-c3", "ensemble_enabled": True},
        }},
        "llm_ensemble": {
            "selection_mode": f"static_{provider}_b5",
            **({"proposer_max_retries": retries} if retries is not None else {}),
        },
    }), encoding="utf-8")
    cfg = load_config(path)
    explicit_fields = set(cfg.llm_ensemble.model_fields_set)
    expected_retries = 1 if retries is None else retries
    expected_source = "c3_default" if retries is None else "configured"

    def assert_retry_policy(config: GatewayConfig) -> None:
        status = ensemble_runtime_status(config)
        assert status["effectiveProposerMaxRetries"] == expected_retries
        assert status["proposerMaxRetriesSource"] == expected_source
        accepted = capture_model_routing_config(config, session_mode="router")
        ensemble = build_ensemble_provider_from_config(
            config=accepted.overlay_live_config(config),
            inherited_provider_config=ProviderConfig(
                provider=provider, model="synthetic-primary", api_key="synthetic-key",
            ),
            fallback_provider=None,
            turn_metadata={"routed_tier": "c3", "ensemble_activation_source": "router_tier"},
        )
        assert ensemble.proposer_max_retries == expected_retries
        assert ensemble.selection_plan["proposer_max_retries_source"] == expected_source

    assert_retry_policy(cfg)
    for revision in ("selected-v1", "selected-v2"):
        apply_recommendation(cfg, recommendation(provider, "ensemble", revision))
        assert_retry_policy(cfg)
        assert cfg.llm_ensemble.model_fields_set == explicit_fields | {
            "recommended_profiles", "selection_mode",
        }
    persist_config(cfg, path=path)
    assert_retry_policy(load_config(path))


def test_provider_ensemble_snapshots_remain_independent() -> None:
    cfg = GatewayConfig()
    apply_recommendation(cfg, recommendation("openrouter", "ensemble"))
    saved = copy.deepcopy(cfg.llm_ensemble.recommended_profiles["openrouter"])
    apply_recommendation(cfg, recommendation("tokenrhythm", "ensemble"))
    assert cfg.llm_ensemble.recommended_profiles["openrouter"] == saved


def test_invalid_plan_cannot_change_current_config() -> None:
    cfg = GatewayConfig()
    before = cfg.model_dump()
    plan = recommendation("tokenrhythm", "router")
    plan["rows"].pop()
    with pytest.raises(ValueError, match="exactly C0"):
        apply_recommendation(cfg, plan)
    assert cfg.model_dump() == before
    plan = recommendation("anthropic", "router")
    with pytest.raises(ValueError, match="unsupported"):
        apply_recommendation(cfg, plan)
    assert cfg.model_dump() == before


def test_first_configuration_uses_available_recommendations_only_once(monkeypatch) -> None:
    from opensquilla.provider import recommendations

    revision = "first"
    monkeypatch.setattr(recommendations, "get_recommendation", lambda cfg, p, k: recommendation(
        p, k, revision,
    ))
    cfg = GatewayConfig()
    initialize_new_recommendations(cfg)
    original = cfg.model_dump()
    revision = "later"
    initialize_new_recommendations(cfg)
    assert cfg.model_dump() == original
    foreign = GatewayConfig(llm={"provider": "anthropic", "model": "synthetic-primary"})
    before = foreign.model_dump()
    initialize_new_recommendations(foreign)
    assert foreign.model_dump() == before


def test_existing_unmarked_ladder_is_not_replaced_by_defaults() -> None:
    tiers = {name: {"provider": "tokenrhythm", "model": f"synthetic-{name}",
                    "thinking_level": "medium"} for name in ("c0", "c1", "c2", "c3")}
    cfg = GatewayConfig(
        llm={"provider": "tokenrhythm"},
        squilla_router={"preset_binding": "follow_primary", "tiers": tiers},
    )
    cfg.initialize_router_profile_defaults()
    assert cfg.squilla_router.tiers == tiers


def test_first_initialization_preserves_explicit_router_and_ensemble(monkeypatch) -> None:
    from opensquilla.provider import recommendations

    monkeypatch.setattr(recommendations, "get_recommendation", lambda *args: pytest.fail(
        "explicit configuration must not request a replacement",
    ))
    cfg = GatewayConfig(
        squilla_router={"preset_binding": "custom", "tiers": {
            name: {"model": f"synthetic-{name}"} for name in ("c0", "c1", "c2", "c3")
        }},
        llm_ensemble={"selection_mode": "static_openrouter_b5"},
    )
    before = cfg.model_dump()
    initialize_new_recommendations(cfg)
    assert cfg.model_dump() == before


def test_incompatible_initial_recommendation_keeps_bundled_defaults(monkeypatch) -> None:
    from opensquilla.provider import recommendations

    monkeypatch.setattr(recommendations, "get_recommendation", lambda cfg, p, k: recommendation(
        p, k,
    ))
    cfg = GatewayConfig(llm_ensemble={"min_successful_proposers": 4})
    before = cfg.llm_ensemble.model_dump()
    initialize_new_recommendations(cfg)
    assert cfg.llm_ensemble.model_dump() == before


@pytest.mark.parametrize("thinking", [None, "off"])
def test_adopting_thinking_clears_legacy_overrides(thinking, tmp_path: Path) -> None:
    from opensquilla.engine.steps.squilla_router import _tier_thinking_level

    cfg = GatewayConfig(llm={"provider": "tokenrhythm"})
    cfg.squilla_router.tiers["c0"].update(thinking="high", supports_thinking=True)
    path = tmp_path / "config.toml"
    persist_config(cfg, path=path)
    cfg = load_config(path)
    plan = recommendation("tokenrhythm", "router")
    plan["rows"][0]["thinkingLevel"] = thinking
    apply_recommendation(cfg, plan)
    persist_config(cfg, path=path)
    tier = load_config(path).squilla_router.tiers["c0"]
    assert "thinking" not in tier
    assert "supports_thinking" not in tier
    assert _tier_thinking_level(tier) is None
