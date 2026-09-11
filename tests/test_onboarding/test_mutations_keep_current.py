"""Keep-current semantics for onboarding mutations.

Omitted parameters (``None``) must never reset stored provider, search, or
router settings — re-saving for a key rotation has to be loss-free.
"""

from __future__ import annotations

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.mutations import (
    upsert_llm_provider,
    upsert_router,
    upsert_search_provider,
)

_HAND_TIERS = {
    "c0": {"provider": "openrouter", "model": "hand-c0"},
    "c1": {"provider": "openrouter", "model": "hand-c1"},
    "c2": {"provider": "openrouter", "model": "hand-c2"},
    "c3": {"provider": "openrouter", "model": "hand-c3"},
}


def _config_with_hand_tiers() -> GatewayConfig:
    return GatewayConfig(
        llm={"provider": "openrouter", "model": "hand-c1", "api_key": "sk-old"},
        squilla_router={
            "enabled": True,
            "tier_profile": None,
            "tiers": {name: dict(tier) for name, tier in _HAND_TIERS.items()},
        },
    )


def _configured_openrouter() -> GatewayConfig:
    res = upsert_llm_provider(
        GatewayConfig(),
        provider_id="openrouter",
        model="corp-model",
        api_key="sk-old",
        base_url="https://llm.corp.example/v1",
        proxy="http://127.0.0.1:7890",
        provider_routing={"corp-model": "corp-upstream"},
    )
    cfg = res.config
    cfg.llm.max_tokens = 4096
    cfg.llm.thinking = "high"
    return cfg


# --- B1-1: provider re-save keeps every field not explicitly passed ---------


def test_upsert_llm_provider_same_provider_resave_keeps_unspecified_fields():
    cfg = _configured_openrouter()

    res = upsert_llm_provider(cfg, provider_id="openrouter", api_key="sk-rotated")

    llm = res.config.llm
    assert llm.api_key == "sk-rotated"
    assert llm.model == "corp-model"
    assert llm.base_url == "https://llm.corp.example/v1"
    assert llm.proxy == "http://127.0.0.1:7890"
    assert llm.provider_routing == {"corp-model": "corp-upstream"}
    assert llm.max_tokens == 4096
    assert llm.thinking == "high"


def test_upsert_llm_provider_public_payload_reflects_kept_values():
    cfg = _configured_openrouter()

    res = upsert_llm_provider(cfg, provider_id="openrouter", api_key="sk-rotated")

    assert res.public_payload["model"] == "corp-model"
    assert res.public_payload["base_url"] == "https://llm.corp.example/v1"
    assert res.public_payload["proxy"] == "http://127.0.0.1:7890"
    assert res.public_payload["provider_routing"] == {"corp-model": "corp-upstream"}


def test_upsert_llm_provider_explicit_values_still_override_stored():
    cfg = _configured_openrouter()

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="new-model",
        api_key="sk-rotated",
        base_url="https://other.example/v1",
        proxy="",
        provider_routing={},
    )

    llm = res.config.llm
    assert llm.model == "new-model"
    assert llm.base_url == "https://other.example/v1"
    assert llm.proxy == ""
    assert llm.provider_routing == {}


def test_upsert_llm_provider_empty_model_string_keeps_legacy_default_chain():
    # model="" (explicit empty, the legacy RPC spelling) still derives the
    # router-tier default rather than keeping the stored model.
    cfg = _configured_openrouter()

    res = upsert_llm_provider(cfg, provider_id="openrouter", model="", api_key="sk-r")

    assert res.config.llm.model != "corp-model"
    assert res.config.llm.model


def test_upsert_llm_provider_provider_switch_does_not_carry_unrelated_fields():
    cfg = _configured_openrouter()

    res = upsert_llm_provider(
        cfg, provider_id="deepseek", model="deepseek-chat", api_key="sk-ds"
    )

    llm = res.config.llm
    assert llm.provider == "deepseek"
    assert llm.proxy == ""
    assert llm.provider_routing == {}
    assert llm.base_url != "https://llm.corp.example/v1"
    assert llm.max_tokens == 0
    assert llm.thinking is None


def test_upsert_custom_provider_preserves_extra_body_until_provider_switch():
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "local-model",
            "base_url": "http://127.0.0.1:8000/v1",
            "extra_body": {"top_k": 40},
        }
    )

    kept = upsert_llm_provider(cfg, provider_id="custom", model="local-model")
    switched = upsert_llm_provider(
        kept.config,
        provider_id="ollama",
        model="local-model",
        base_url="http://127.0.0.1:11434",
        router_action="disable",
    )

    assert kept.config.llm.extra_body == {"top_k": 40}
    assert switched.config.llm.extra_body == {}


def test_upsert_llm_provider_same_provider_resave_keeps_custom_router_tiers():
    cfg = _config_with_hand_tiers()

    res = upsert_llm_provider(cfg, provider_id="openrouter", api_key="sk-rotated")

    router = res.config.squilla_router
    assert router.tier_profile is None
    for tier in ("c0", "c1", "c2", "c3"):
        assert router.tiers[tier]["model"] == f"hand-{tier}"


def test_upsert_llm_provider_same_provider_resave_still_reconciles_untouched_tiers():
    # Guard for the skip rule: default (never hand-edited) tiers still get
    # the packaged compact profile on a same-provider save.
    res = upsert_llm_provider(
        GatewayConfig(), provider_id="openrouter", model="m", api_key="sk-x"
    )

    assert res.config.squilla_router.tier_profile == "openrouter"


def test_upsert_llm_provider_same_provider_model_change_reseeds_preset_seeded_tiers():
    # Tiers a previous save seeded from the provider preset are not a
    # hand-edited ladder: a model change must keep refreshing them.
    first = upsert_llm_provider(
        GatewayConfig(), provider_id="groq", model="model-a", api_key="sk-g"
    )
    second = upsert_llm_provider(
        first.config, provider_id="groq", model="model-b", api_key=""
    )

    router = second.config.squilla_router
    for tier in ("c0", "c1", "c2", "c3"):
        assert router.tiers[tier]["model"] == "model-b"


# --- B1-2: search re-save keeps omitted settings ------------------------------


def test_upsert_search_provider_omitted_fields_keep_current():
    first = upsert_search_provider(
        GatewayConfig(),
        provider_id="brave",
        api_key="brave-key",
        max_results=9,
        proxy="http://127.0.0.1:7890",
        use_env_proxy=True,
        fallback_policy="network",
        diagnostics=True,
    )

    second = upsert_search_provider(
        first.config, provider_id="brave", api_key="brave-rotated"
    )

    cfg = second.config
    assert cfg.search_api_key == "brave-rotated"
    assert cfg.search_max_results == 9
    assert cfg.search_proxy == "http://127.0.0.1:7890"
    assert cfg.search_use_env_proxy is True
    assert cfg.search_fallback_policy == "network"
    assert cfg.search_diagnostics is True


def test_upsert_search_provider_explicit_values_override_stored():
    first = upsert_search_provider(
        GatewayConfig(),
        provider_id="brave",
        api_key="brave-key",
        max_results=9,
        proxy="http://127.0.0.1:7890",
        use_env_proxy=True,
        fallback_policy="network",
        diagnostics=True,
    )

    second = upsert_search_provider(
        first.config,
        provider_id="brave",
        max_results=3,
        proxy="",
        use_env_proxy=False,
        fallback_policy="off",
        diagnostics=False,
    )

    cfg = second.config
    assert cfg.search_api_key == "brave-key"  # blank key keeps current
    assert cfg.search_max_results == 3
    assert cfg.search_proxy == ""
    assert cfg.search_use_env_proxy is False
    assert cfg.search_fallback_policy == "off"
    assert cfg.search_diagnostics is False


def test_upsert_search_provider_keep_current_applies_across_provider_switch():
    # max_results/proxy/... are global search settings, so they survive a
    # provider switch when omitted.
    first = upsert_search_provider(
        GatewayConfig(),
        provider_id="brave",
        api_key="brave-key",
        max_results=9,
        diagnostics=True,
    )

    second = upsert_search_provider(first.config, provider_id="duckduckgo")

    assert second.config.search_provider == "duckduckgo"
    assert second.config.search_max_results == 9
    assert second.config.search_diagnostics is True


# --- B1-3: disable preserves the ladder; custom re-enable restores it --------


def test_upsert_router_disable_preserves_inline_custom_tiers():
    cfg = _config_with_hand_tiers()

    res = upsert_router(cfg, mode="disabled")

    router = res.config.squilla_router
    assert router.enabled is False
    assert router.tier_profile is None
    for tier in ("c0", "c1", "c2", "c3"):
        assert router.tiers[tier]["model"] == f"hand-{tier}"


def test_upsert_router_disable_then_custom_reenable_restores_hand_built_tiers():
    cfg = _config_with_hand_tiers()

    disabled = upsert_router(cfg, mode="disabled")
    reenabled = upsert_router(disabled.config, mode="custom")

    router = reenabled.config.squilla_router
    assert router.enabled is True
    assert router.tier_profile is None
    for tier in ("c0", "c1", "c2", "c3"):
        assert router.tiers[tier]["model"] == f"hand-{tier}"


def test_upsert_router_custom_keeps_inline_hand_tiers_when_tiers_omitted():
    # Even without a disable/enable round trip, a custom-mode save that
    # passes no tiers must not reset an inline hand-built ladder.
    cfg = _config_with_hand_tiers()

    res = upsert_router(cfg, mode="custom")

    router = res.config.squilla_router
    for tier in ("c0", "c1", "c2", "c3"):
        assert router.tiers[tier]["model"] == f"hand-{tier}"


def test_upsert_router_recommended_reenable_still_resets_to_packaged_profile():
    # "recommended" remains an explicit reset to the packaged profile.
    cfg = _config_with_hand_tiers()

    disabled = upsert_router(cfg, mode="disabled")
    reenabled = upsert_router(disabled.config, mode="recommended")

    router = reenabled.config.squilla_router
    assert router.enabled is True
    assert router.tier_profile == "openrouter"
    assert router.tiers["c0"]["model"] != "hand-c0"


def test_upsert_router_disabled_ladder_survives_toml_round_trip():
    cfg = _config_with_hand_tiers()

    disabled = upsert_router(cfg, mode="disabled")
    persisted = disabled.config.to_toml_dict()["squilla_router"]
    reloaded = GatewayConfig(squilla_router=persisted, llm={"provider": "openrouter"})

    assert reloaded.squilla_router.enabled is False
    assert reloaded.squilla_router.tiers["c2"]["model"] == "hand-c2"
