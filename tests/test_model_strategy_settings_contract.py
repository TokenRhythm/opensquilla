from __future__ import annotations

from opensquilla.gateway.config import GatewayConfig, _router_tier_profile_defaults
from opensquilla.onboarding.mutations import upsert_llm_provider, upsert_router
from opensquilla.provider.recommendations import get_recommendation


def test_fresh_config_uses_model_router_not_ensemble() -> None:
    cfg = GatewayConfig()

    assert cfg.squilla_router.enabled is True
    assert cfg.llm_ensemble.enabled is False


def test_openrouter_chat_model_save_seeds_materialized_router_recommendation() -> None:
    res = upsert_llm_provider(
        GatewayConfig(),
        provider_id="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="sk-test",
    )

    router = res.config.squilla_router
    recommendation = get_recommendation(res.config, "openrouter", "router")
    assert router.enabled is True
    assert router.tier_profile is None
    assert router.preset_binding == "follow_primary"
    assert router.recommendation_revision == recommendation["revision"]
    for row in recommendation["rows"]:
        tier = router.tiers[row["role"]]
        assert tier["provider"] == row["provider"]
        assert tier["model"] == row["model"]
        assert tier.get("thinking_level") == row["thinkingLevel"]


def test_non_packaged_chat_model_save_seeds_synthesized_router_tiers() -> None:
    res = upsert_llm_provider(
        GatewayConfig(),
        provider_id="anthropic",
        model="claude-sonnet-4-6",
        api_key="sk-test",
    )

    router = res.config.squilla_router
    assert router.enabled is True
    assert router.tier_profile is None
    assert set(router.tiers) >= {"c0", "c1", "c2", "c3"}
    for tier_name in ("c0", "c1", "c2", "c3"):
        assert router.tiers[tier_name]["provider"] == "anthropic"
        assert router.tiers[tier_name]["model"] == "claude-sonnet-4-6"


def test_legacy_openrouter_mix_exact_preset_save_writes_canonical_recommended() -> None:
    cfg = GatewayConfig(
        # openrouter-mix requires the openrouter provider, which is no longer
        # the built-in default.
        llm={"provider": "openrouter"},
        squilla_router={
            "enabled": True,
            "tier_profile": None,
            "tiers": _router_tier_profile_defaults("openrouter"),
        }
    )

    res = upsert_router(cfg, mode="openrouter-mix")

    assert res.public_payload["mode"] == "recommended"
    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile == "openrouter"


def test_legacy_openrouter_mix_model_difference_saves_custom_inline_tiers() -> None:
    tiers = _router_tier_profile_defaults("openrouter")
    tiers["c3"] = dict(tiers["c3"], model="z-ai/glm-5.2")
    cfg = GatewayConfig(
        # openrouter-mix requires the openrouter provider, which is no longer
        # the built-in default.
        llm={"provider": "openrouter"},
        squilla_router={
            "enabled": True,
            "tier_profile": None,
            "tiers": tiers,
        }
    )

    res = upsert_router(cfg, mode="openrouter-mix", tiers=tiers)

    assert res.public_payload["mode"] == "custom"
    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile is None
    assert res.config.squilla_router.tiers["c3"]["model"] == "z-ai/glm-5.2"


def test_legacy_openrouter_mix_description_difference_saves_custom_inline_tiers() -> None:
    tiers = _router_tier_profile_defaults("openrouter")
    tiers["c1"] = dict(tiers["c1"], description="Operator edited description.")
    cfg = GatewayConfig(
        # openrouter-mix requires the openrouter provider, which is no longer
        # the built-in default.
        llm={"provider": "openrouter"},
        squilla_router={
            "enabled": True,
            "tier_profile": None,
            "tiers": tiers,
        }
    )

    res = upsert_router(cfg, mode="openrouter-mix", tiers=tiers)

    assert res.public_payload["mode"] == "custom"
    assert res.config.squilla_router.tier_profile is None
    assert res.config.squilla_router.tiers["c1"]["description"] == "Operator edited description."
