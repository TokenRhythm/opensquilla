"""Provider-profile mutation contracts for mixed-provider routing."""

from __future__ import annotations

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.mutations import (
    LlmProfileActivationError,
    activate_llm_profile,
    remove_llm_profile,
    upsert_llm_profile,
)
from opensquilla.onboarding.status import get_onboarding_status
from opensquilla.provider.deployment import resolve_provider_deployment


def test_profile_upsert_redacts_secret_and_keeps_credential_sources() -> None:
    cfg = GatewayConfig()

    result = upsert_llm_profile(
        cfg,
        provider_id="OpenAI",
        api_key="sk-profile",
        api_key_env="OPENAI_PROFILE_KEY",
        api_key_env_pool=["OPENAI_POOL_A", "OPENAI_POOL_A", "OPENAI_POOL_B"],
    )

    profile = result.config.llm_profiles["openai"]
    assert profile.api_key == "sk-profile"
    assert profile.api_key_env == "OPENAI_PROFILE_KEY"
    assert profile.api_key_env_pool == ["OPENAI_POOL_A", "OPENAI_POOL_B"]
    assert result.public_payload["api_key"] == "***"
    assert result.restart_required is False


def test_profile_keep_current_secret_is_same_origin_only() -> None:
    cfg = GatewayConfig(
        llm_profiles={
            "custom": {
                "model": "custom-deployment-model",
                "api_key": "old-secret",
                "api_key_env": "CUSTOM_KEY",
                "api_key_env_pool": ["CUSTOM_POOL"],
                "base_url": "https://one.example/v1",
            }
        }
    )

    same_origin = upsert_llm_profile(
        cfg,
        provider_id="custom",
        preserve_api_key=True,
        base_url="https://one.example/v2",
    )
    same = same_origin.config.llm_profiles["custom"]
    assert same.model == "custom-deployment-model"
    assert same.api_key == "old-secret"
    assert same.api_key_env == "CUSTOM_KEY"
    assert same.api_key_env_pool == ["CUSTOM_POOL"]

    changed_origin = upsert_llm_profile(
        cfg,
        provider_id="custom",
        preserve_api_key=True,
        base_url="https://two.example/v1",
    )
    changed = changed_origin.config.llm_profiles["custom"]
    # A model preference is not credential provenance: changing the endpoint
    # clears every reusable secret source, but preserves an omitted model.
    assert changed.model == "custom-deployment-model"
    assert changed.api_key == ""
    assert changed.api_key_env == ""
    assert changed.api_key_env_pool == []


def test_profile_origin_change_does_not_reacquire_registry_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-registry-must-not-cross-origin")
    cfg = GatewayConfig(
        llm_profiles={
            "openai": {
                "api_key": "old-profile-secret",
                "base_url": "https://api.openai.com/v1",
            }
        }
    )

    changed = upsert_llm_profile(
        cfg,
        provider_id="openai",
        preserve_api_key=True,
        base_url="https://foreign.example/v1",
    ).config
    resolution = resolve_provider_deployment(changed, "openai", "gpt-test")

    assert changed.llm_profiles["openai"].api_key == ""
    assert resolution.ready is False
    assert resolution.reason == "missing_credential"
    assert resolution.provider_config is not None
    assert resolution.provider_config.api_key == ""


def test_profile_upsert_allows_credentialless_draft() -> None:
    result = upsert_llm_profile(GatewayConfig(), provider_id="deepseek")

    assert result.config.llm_profiles["deepseek"].api_key == ""
    assert result.changed is True


def test_profile_upsert_sets_preserves_and_clears_direct_model() -> None:
    secret = "synthetic-profile-model-secret"
    cfg = GatewayConfig(
        llm_profiles={
            "openai": {
                "model": "gpt-saved",
                "api_key": secret,
            }
        }
    )

    updated = upsert_llm_profile(
        cfg,
        provider_id="openai",
        model="  gpt-updated  ",
        preserve_api_key=True,
    )
    assert updated.config.llm_profiles["openai"].model == "gpt-updated"
    assert updated.config.llm_profiles["openai"].api_key == secret
    assert updated.public_payload["model"] == "gpt-updated"
    assert secret not in repr(updated.public_payload)

    preserved = upsert_llm_profile(
        updated.config,
        provider_id="openai",
        preserve_api_key=True,
    )
    assert preserved.config.llm_profiles["openai"].model == "gpt-updated"

    cleared = upsert_llm_profile(
        preserved.config,
        provider_id="openai",
        model="   ",
        preserve_api_key=True,
    )
    assert cleared.config.llm_profiles["openai"].model == ""
    assert cleared.public_payload["model"] == ""
    assert secret not in repr(cleared.public_payload)


def test_profile_upsert_normalizes_case_variant_and_preserves_secret() -> None:
    cfg = GatewayConfig(llm_profiles={"OpenAI": {"api_key": "old-secret"}})
    cfg.mark_runtime_secret("llm_profiles.OpenAI.api_key")

    result = upsert_llm_profile(
        cfg,
        provider_id="openai",
        preserve_api_key=True,
    )

    assert list(result.config.llm_profiles) == ["openai"]
    assert result.config.llm_profiles["openai"].api_key == "old-secret"
    assert "llm_profiles.OpenAI.api_key" not in result.config._runtime_secret_paths
    assert "llm_profiles.openai.api_key" in result.config._runtime_secret_paths


def test_profile_remove_rejects_router_reference() -> None:
    cfg = GatewayConfig(llm_profiles={"openai": {"api_key_env": "OPENAI_PROFILE_KEY"}})
    cfg.squilla_router.tiers["c2"] = {"provider": "openai", "model": "gpt-5-mini"}

    with pytest.raises(ValueError, match="squilla_router.tiers.c2"):
        remove_llm_profile(cfg, provider_id="openai")


def test_profile_remove_rejects_ensemble_reference() -> None:
    cfg = GatewayConfig(
        llm_profiles={"openai": {"api_key_env": "OPENAI_PROFILE_KEY"}},
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": [
                {"provider": "openai", "model": "gpt-5-mini", "role": "primary"},
                {"provider": "deepseek", "model": "deepseek-chat", "role": "contrast"},
                {"provider": "gemini", "model": "gemini-flash", "role": "aggregator"},
            ],
        },
    )

    with pytest.raises(ValueError, match=r"llm_ensemble\.candidates\.0"):
        remove_llm_profile(cfg, provider_id="openai")


def test_profile_remove_rejects_disabled_ensemble_reference() -> None:
    cfg = GatewayConfig(
        llm_profiles={"openai": {"api_key_env": "OPENAI_PROFILE_KEY"}},
        llm_ensemble={
            "enabled": False,
            "selection_mode": "custom_b5",
            "candidates": [
                {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "role": "primary",
                },
                {
                    "provider": "gemini",
                    "model": "gemini-flash",
                    "role": "contrast",
                },
                {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "role": "critic",
                    "enabled": False,
                },
            ],
        },
    )

    with pytest.raises(ValueError, match=r"llm_ensemble\.candidates\.2"):
        remove_llm_profile(cfg, provider_id="openai")


def test_profile_remove_rejects_disabled_static_ensemble_reference() -> None:
    cfg = GatewayConfig(
        llm_profiles={"openrouter": {"api_key_env": "OPENROUTER_PROFILE_KEY"}},
        llm_ensemble={
            "enabled": False,
            "selection_mode": "static_openrouter_b5",
        },
    )

    with pytest.raises(ValueError, match="llm_ensemble.selection_mode"):
        remove_llm_profile(cfg, provider_id="openrouter")


def test_profile_remove_unused_entry() -> None:
    cfg = GatewayConfig(llm_profiles={"openai": {"api_key_env": "OPENAI_PROFILE_KEY"}})

    result = remove_llm_profile(cfg, provider_id="openai")

    assert "openai" not in result.config.llm_profiles
    assert result.public_payload == {"provider": "openai", "removed": True}


def test_profile_remove_accepts_historical_case_variant() -> None:
    cfg = GatewayConfig(llm_profiles={"OpenAI": {"api_key_env": "OPENAI_PROFILE_KEY"}})

    result = remove_llm_profile(cfg, provider_id="openai")

    assert result.config.llm_profiles == {}
    assert result.public_payload == {"provider": "openai", "removed": True}


def test_profile_activation_atomically_swaps_primary_without_touching_routes() -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "gpt-test",
            "api_key": "old-primary-secret",
            "base_url": "https://api.openai.com/v1",
            "max_tokens": 321,
            "thinking": "high",
        },
        llm_profiles={
            "DeepSeek": {
                "api_key": "new-primary-secret",
                "base_url": "https://api.deepseek.com/v1",
                "proxy": "http://profile-proxy.invalid:8080",
            },
            "OPENAI": {"api_key": "stale-profile-secret"},
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": [
                {"provider": "deepseek", "model": "deepseek-chat", "role": "primary"},
                {"provider": "openai", "model": "gpt-test", "role": "contrast"},
                {"provider": "deepseek", "model": "deepseek-chat", "role": "aggregator"},
            ],
        },
        squilla_router={
            "preset_binding": "custom",
            "cross_provider_tiers": True,
        },
    )
    router_before = cfg.squilla_router.model_dump(mode="python")
    ensemble_before = cfg.llm_ensemble.model_dump(mode="python")

    result = activate_llm_profile(
        cfg,
        provider_id="deepseek",
        model="deepseek-chat",
    )

    activated = result.config
    assert activated.llm.provider == "deepseek"
    assert activated.llm.model == "deepseek-chat"
    assert activated.llm.api_key == "new-primary-secret"
    assert activated.llm.proxy == "http://profile-proxy.invalid:8080"
    assert activated.llm.max_tokens == 0
    assert activated.llm.thinking is None
    assert list(activated.llm_profiles) == ["openai"]
    assert activated.llm_profiles["openai"].model == "gpt-test"
    assert activated.llm_profiles["openai"].api_key == "old-primary-secret"
    assert activated.squilla_router.model_dump(mode="python") == router_before
    assert activated.llm_ensemble.model_dump(mode="python") == ensemble_before
    assert "new-primary-secret" not in repr(result.public_payload)
    assert "old-primary-secret" not in repr(result.public_payload)


def test_profile_activation_model_precedence_is_request_then_profile_then_default() -> None:
    base = {
        "llm": {
            "provider": "openai",
            "model": "gpt-old-primary",
            "api_key": "synthetic-old-primary-secret",
        },
        "squilla_router": {"preset_binding": "follow_primary"},
    }

    explicit = activate_llm_profile(
        GatewayConfig(
            **base,
            llm_profiles={
                "deepseek": {
                    "model": "deepseek-profile-model",
                    "api_key": "synthetic-deepseek-secret",
                }
            },
        ),
        provider_id="deepseek",
        model="  deepseek-request-model  ",
    ).config
    assert explicit.llm.model == "deepseek-request-model"

    saved = activate_llm_profile(
        GatewayConfig(
            **base,
            llm_profiles={
                "deepseek": {
                    "model": "deepseek-profile-model",
                    "api_key": "synthetic-deepseek-secret",
                }
            },
        ),
        provider_id="deepseek",
    ).config
    assert saved.llm.model == "deepseek-profile-model"

    legacy_without_model = activate_llm_profile(
        GatewayConfig(
            **base,
            llm_profiles={"deepseek": {"api_key": "synthetic-deepseek-secret"}},
        ),
        provider_id="deepseek",
    ).config
    assert legacy_without_model.llm.model == "deepseek-v4-flash"


def test_profile_activation_without_saved_or_provider_default_model_fails_closed() -> None:
    secret = "synthetic-anthropic-profile-secret"
    cfg = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "gpt-old-primary",
            "api_key": "synthetic-old-primary-secret",
        },
        llm_profiles={"anthropic": {"api_key": secret}},
        squilla_router={"preset_binding": "follow_primary"},
    )
    before = cfg.model_dump(mode="python")

    with pytest.raises(LlmProfileActivationError) as error:
        activate_llm_profile(cfg, provider_id="anthropic")

    assert error.value.reason == "missing_model"
    assert cfg.model_dump(mode="python") == before
    assert secret not in str(error.value)


def test_profile_activation_round_trip_restores_each_provider_direct_model() -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "gpt-saved-primary",
            "api_key": "synthetic-openai-secret",
        },
        llm_profiles={
            "deepseek": {
                "model": "deepseek-saved-profile",
                "api_key": "synthetic-deepseek-secret",
            }
        },
        squilla_router={"preset_binding": "follow_primary"},
    )

    deepseek_active = activate_llm_profile(cfg, provider_id="deepseek").config
    assert deepseek_active.llm.model == "deepseek-saved-profile"
    assert deepseek_active.llm_profiles["openai"].model == "gpt-saved-primary"

    openai_active = activate_llm_profile(deepseek_active, provider_id="openai").config
    assert openai_active.llm.model == "gpt-saved-primary"
    assert openai_active.llm_profiles["deepseek"].model == "deepseek-saved-profile"


def test_profile_activation_managed_router_follows_primary_and_preserves_controls() -> None:
    cfg = GatewayConfig(
        llm={"provider": "openai", "model": "gpt-test", "api_key": "old-secret"},
        llm_profiles={"deepseek": {"api_key": "new-secret"}},
        squilla_router={
            "enabled": True,
            "tier_profile": "openai",
            "preset_binding": "follow_primary",
            "default_tier": "c2",
            "visual_mode": "legacy_grid",
            "tier_provider_mismatch": "veto",
            "confidence_threshold": 0.71,
        },
        llm_ensemble={
            "enabled": False,
            "candidate_max_chars": 12_345,
            "record_candidates": True,
        },
    )
    ensemble_before = cfg.llm_ensemble.model_dump(mode="python")

    activated = activate_llm_profile(
        cfg,
        provider_id="deepseek",
        model="deepseek-chat",
    ).config

    router = activated.squilla_router
    assert router.enabled is True
    assert router.preset_binding == "follow_primary"
    assert router.tier_profile == "deepseek"
    assert router.default_tier == "c2"
    assert router.visual_mode == "legacy_grid"
    assert router.tier_provider_mismatch == "veto"
    assert router.confidence_threshold == 0.71
    assert {router.tiers[name]["provider"] for name in ("c0", "c1", "c2", "c3")} == {
        "deepseek"
    }
    assert activated.llm.model == "deepseek-chat"
    assert activated.llm_ensemble.model_dump(mode="python") == ensemble_before


@pytest.mark.parametrize("binding", [None, "custom"])
def test_profile_activation_custom_or_legacy_conflict_is_fail_closed(binding) -> None:
    router = {
        "enabled": True,
        "tier_profile": "openai",
        "cross_provider_tiers": False,
    }
    if binding is not None:
        router["preset_binding"] = binding
    cfg = GatewayConfig(
        llm={"provider": "openai", "model": "gpt-test", "api_key": "old-secret"},
        llm_profiles={"deepseek": {"api_key": "new-secret"}},
        squilla_router=router,
    )
    router_before = cfg.squilla_router.model_dump(mode="python")

    with pytest.raises(LlmProfileActivationError) as error:
        activate_llm_profile(
            cfg,
            provider_id="deepseek",
            model="deepseek-chat",
        )

    assert error.value.reason == "router_provider_conflict"
    assert error.value.details["conflictProviders"] == ["openai"]
    assert cfg.llm.provider == "openai"
    assert cfg.squilla_router.model_dump(mode="python") == router_before


@pytest.mark.parametrize(
    ("router_action", "expected_binding", "expected_enabled", "expected_cross"),
    [
        ("use_recommended", "follow_primary", True, False),
        ("enable_cross_provider", "custom", True, True),
        ("disable", "custom", False, False),
    ],
)
def test_profile_activation_resolves_custom_router_conflict_explicitly(
    router_action,
    expected_binding,
    expected_enabled,
    expected_cross,
) -> None:
    cfg = GatewayConfig(
        llm={"provider": "openai", "model": "gpt-test", "api_key": "old-secret"},
        llm_profiles={"deepseek": {"api_key": "new-secret"}},
        squilla_router={
            "enabled": True,
            "tier_profile": "openai",
            "preset_binding": "custom",
            "cross_provider_tiers": False,
            "default_tier": "c2",
            "visual_mode": "legacy_grid",
            "confidence_threshold": 0.69,
        },
        llm_ensemble={
            "enabled": False,
            "candidate_max_chars": 11_111,
            "record_candidates": True,
        },
    )
    ensemble_before = cfg.llm_ensemble.model_dump(mode="python")

    activated = activate_llm_profile(
        cfg,
        provider_id="deepseek",
        model="deepseek-chat",
        router_action=router_action,
    ).config

    router = activated.squilla_router
    assert router.preset_binding == expected_binding
    assert router.enabled is expected_enabled
    assert router.cross_provider_tiers is expected_cross
    assert router.default_tier == "c2"
    assert router.visual_mode == "legacy_grid"
    assert router.confidence_threshold == 0.69
    if router_action == "use_recommended":
        assert router.tier_profile == "deepseek"
        assert router.tiers["c0"]["provider"] == "deepseek"
    else:
        assert router.tier_profile is None
        assert router.tiers["c0"]["provider"] == "openai"
    assert activated.llm_ensemble.model_dump(mode="python") == ensemble_before


@pytest.mark.parametrize(
    ("profile", "environment", "provider", "model"),
    [
        (
            {"api_key_env": "OPENSQUILLA_ACTIVATE_PROFILE_KEY"},
            {"OPENSQUILLA_ACTIVATE_PROFILE_KEY": "synthetic-env-secret"},
            "deepseek",
            "deepseek-chat",
        ),
        (
            {},
            {"DEEPSEEK_API_KEY": "synthetic-registry-secret"},
            "deepseek",
            "deepseek-chat",
        ),
        ({}, {}, "ollama", "llama3.1"),
    ],
)
def test_profile_activation_accepts_env_registry_env_and_keyless_profiles(
    monkeypatch,
    profile,
    environment,
    provider,
    model,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    cfg = GatewayConfig(
        llm={"provider": "openai", "model": "gpt-test", "api_key": "old-secret"},
        llm_profiles={provider: profile},
        squilla_router={"preset_binding": "follow_primary"},
    )

    activated = activate_llm_profile(cfg, provider_id=provider, model=model).config

    assert activated.llm.provider == provider
    assert activated.llm.model == model
    assert activated.llm.api_key == ""


def test_profile_activation_rejects_pool_and_unexecutable_draft(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    pooled = GatewayConfig(
        llm_profiles={"deepseek": {"api_key_env_pool": ["DEEPSEEK_POOL_A"]}}
    )
    with pytest.raises(LlmProfileActivationError) as pool_error:
        activate_llm_profile(pooled, provider_id="deepseek", model="deepseek-chat")
    assert pool_error.value.reason == "primary_pool_unsupported"

    draft = GatewayConfig(llm_profiles={"deepseek": {}})
    with pytest.raises(LlmProfileActivationError) as draft_error:
        activate_llm_profile(draft, provider_id="deepseek", model="deepseek-chat")
    assert draft_error.value.reason == "missing_credential"
    assert draft.llm.provider == "tokenrhythm"
    assert "deepseek" in draft.llm_profiles


def test_profile_activation_moves_runtime_secret_and_endpoint_provenance() -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "gpt-test",
            "api_key": "old-env-secret",
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://env-openai.invalid/v1",
            "proxy": "http://env-proxy.invalid:8080",
        },
        llm_profiles={"DeepSeek": {"api_key": "target-env-secret"}},
        squilla_router={"preset_binding": "follow_primary"},
    )
    cfg.mark_runtime_secret("llm.api_key")
    cfg.mark_runtime_secret("llm_profiles.DeepSeek.api_key")
    cfg.record_runtime_override(
        "llm.base_url", "https://api.openai.com/v1", cfg.llm.base_url
    )
    cfg.record_runtime_override("llm.proxy", "", cfg.llm.proxy)

    activated = activate_llm_profile(
        cfg, provider_id="deepseek", model="deepseek-chat"
    ).config

    assert "llm.api_key" in activated._runtime_secret_paths
    assert "llm_profiles.openai.api_key" in activated._runtime_secret_paths
    assert "llm_profiles.DeepSeek.api_key" not in activated._runtime_secret_paths
    assert activated.llm_profiles["openai"].base_url == "https://api.openai.com/v1"
    assert activated.llm_profiles["openai"].proxy == ""
    assert "llm.base_url" not in activated.runtime_field_overrides()
    assert "llm.proxy" not in activated.runtime_field_overrides()
    persisted = activated.to_toml_dict()
    assert "api_key" not in persisted["llm"]
    assert "api_key" not in persisted["llm_profiles"]["openai"]


def test_profile_status_reports_primary_eligibility(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={"provider": "openai", "model": "gpt-test", "api_key": "old-secret"},
        llm_profiles={
            "deepseek": {"api_key": "ready-secret"},
            "gemini": {"api_key_env_pool": ["GEMINI_POOL_A"]},
        },
    )

    rows = {
        row["provider"]: row for row in get_onboarding_status(cfg).llm_profile_status
    }

    assert rows["openai"]["primaryEligible"] is False
    assert rows["openai"]["primaryBlockReason"] == "already_active"
    assert rows["deepseek"]["primaryEligible"] is True
    assert rows["deepseek"]["primaryBlockReason"] == ""
    assert rows["gemini"]["primaryEligible"] is False
    assert rows["gemini"]["primaryBlockReason"] == "primary_pool_unsupported"


def test_profile_status_does_not_treat_route_member_model_as_direct_model() -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "gpt-primary",
            "api_key": "synthetic-openai-secret",
        },
        llm_profiles={
            # Anthropic has no catalog defaultDirectModel. A Router member is
            # still not a saved direct/fallback preference for activation.
            "anthropic": {"api_key": "synthetic-anthropic-secret"},
        },
    )
    cfg.squilla_router.tiers["c0"] = {
        "provider": "anthropic",
        "model": "claude-route-only",
    }

    rows = {
        row["provider"]: row for row in get_onboarding_status(cfg).llm_profile_status
    }

    assert rows["anthropic"]["ready"] is True
    assert rows["anthropic"]["primaryEligible"] is False
    assert rows["anthropic"]["primaryBlockReason"] == "missing_model"
