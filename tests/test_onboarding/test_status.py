"""Tests for OnboardingStatus derivation."""

from __future__ import annotations

import pytest

from opensquilla.gateway.config import GatewayConfig, LlmProviderConfig
from opensquilla.onboarding.mutations import upsert_channel
from opensquilla.onboarding.status import get_onboarding_status


def test_default_provider_with_no_key_needs_onboarding():
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="",
        base_url="https://openrouter.ai/api/v1",
    )
    s = get_onboarding_status(cfg)
    assert s.needs_onboarding is True


def test_provider_with_key_is_configured():
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="sk-x",
        base_url="https://openrouter.ai/api/v1",
    )
    s = get_onboarding_status(cfg)
    assert s.llm_configured is True
    assert s.llm_source == "explicit"
    assert s.needs_onboarding is False


def test_provider_with_env_key_is_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="",
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
    )

    s = get_onboarding_status(cfg)

    assert s.llm_configured is True
    assert s.llm_source == "env"
    assert s.needs_onboarding is False


def test_provider_with_default_env_var_is_configured(monkeypatch):
    # Config omits api_key and api_key_env; the provider's default env var
    # resolves, so onboarding reports the provider configured (source "env").
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="",
        base_url="https://openrouter.ai/api/v1",
    )

    s = get_onboarding_status(cfg)

    assert s.llm_configured is True
    assert s.llm_source == "env"
    assert s.needs_onboarding is False


def test_runtime_secret_marker_keeps_env_source_after_resolution(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="",
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
    )
    resolve_llm_runtime_config(cfg)

    s = get_onboarding_status(cfg)

    assert cfg.llm.api_key == "sk-from-env"
    assert "llm.api_key" in cfg._runtime_secret_paths
    assert s.llm_configured is True
    assert s.llm_source == "env"


def test_provider_with_missing_env_key_needs_onboarding(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="",
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
    )

    s = get_onboarding_status(cfg)

    assert s.llm_configured is False
    assert s.llm_source == "missing_env"
    assert s.needs_onboarding is True


def test_matching_llm_key_does_not_configure_image_generation_until_enabled(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="sk-or",
        base_url="https://openrouter.ai/api/v1",
    )
    s = get_onboarding_status(cfg)
    assert s.image_generation_configured is False
    assert s.image_generation_enabled is False
    assert s.image_generation_state["mode"] == "unconfigured"
    assert s.image_generation_state["operatorManaged"] is False
    assert s.image_generation_state["recommendation"] == {
        "providerId": "openrouter",
        "reason": "active_llm_provider",
        "canReuseCredential": True,
        "actionRequired": True,
    }
    assert s.image_generation_source == "none"
    assert s.image_generation_provider == ""


def test_enabled_image_generation_can_use_matching_llm_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="sk-or",
        base_url="https://openrouter.ai/api/v1",
    )
    cfg.image_generation.enabled = True
    s = get_onboarding_status(cfg)
    assert s.image_generation_configured is True
    assert s.image_generation_enabled is True
    assert s.image_generation_source == "llm_fallback"
    assert s.image_generation_provider == "openrouter"
    assert s.image_generation_state["effective"]["providerId"] == "openrouter"


def test_image_generation_reuses_a_demoted_tokenrhythm_profile(monkeypatch):
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "openrouter/auto",
            "api_key": "synthetic-primary-key",
            "base_url": "https://openrouter.ai/api/v1",
        },
        llm_profiles={
            "tokenrhythm": {
                "model": "deepseek-v4-flash",
                "api_key": "synthetic-profile-key",
                "base_url": "https://tokenrhythm.studio/v1",
            }
        },
        image_generation={
            "enabled": True,
            "binding": "custom",
            "primary": "tokenrhythm/qwen-image-2.0",
        },
    )

    status = get_onboarding_status(cfg)
    option = next(
        item
        for item in status.image_generation_state["credentialOptions"]
        if item["providerId"] == "tokenrhythm"
    )

    assert status.image_generation_source == "llm_fallback"
    assert status.image_generation_state["effective"]["credentialOwner"] == "profile"
    assert option == {
        "providerId": "tokenrhythm",
        "available": True,
        "source": "llm_fallback",
        "owner": "profile",
        "kind": "direct",
        "envKey": "",
        "reason": "ready",
    }


def test_explicit_missing_image_env_does_not_fall_back_to_profile(monkeypatch):
    monkeypatch.delenv("MISSING_IMAGE_ONLY_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "openrouter/auto",
            "api_key": "synthetic-primary-key",
            "base_url": "https://openrouter.ai/api/v1",
        },
        llm_profiles={
            "tokenrhythm": {
                "api_key": "synthetic-profile-key",
                "base_url": "https://tokenrhythm.studio/v1",
            }
        },
        image_generation={
            "enabled": True,
            "primary": "tokenrhythm/qwen-image-2.0",
            "providers": {
                "tokenrhythm": {"api_key_env": "MISSING_IMAGE_ONLY_KEY"}
            },
        },
    )

    status = get_onboarding_status(cfg)
    option = next(
        item
        for item in status.image_generation_state["credentialOptions"]
        if item["providerId"] == "tokenrhythm"
    )

    assert status.image_generation_source == "missing_env"
    assert option["available"] is False
    assert option["owner"] == "image"
    assert option["reason"] == "image_env_missing"


def test_explicit_default_image_env_does_not_fall_back_to_profile(monkeypatch):
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "openrouter/auto",
            "api_key": "synthetic-primary-key",
            "base_url": "https://openrouter.ai/api/v1",
        },
        llm_profiles={
            "tokenrhythm": {
                "api_key": "synthetic-profile-key",
                "base_url": "https://tokenrhythm.studio/v1",
            }
        },
        image_generation={
            "enabled": True,
            "primary": "tokenrhythm/qwen-image-2.0",
            "providers": {
                "tokenrhythm": {"api_key_env": "TOKENRHYTHM_API_KEY"}
            },
        },
    )

    status = get_onboarding_status(cfg)
    option = next(
        item
        for item in status.image_generation_state["credentialOptions"]
        if item["providerId"] == "tokenrhythm"
    )

    assert status.image_generation_source == "missing_env"
    assert option["available"] is False
    assert option["owner"] == "image"
    assert option["envKey"] == "TOKENRHYTHM_API_KEY"
    assert option["reason"] == "image_env_missing"


def test_legacy_default_image_route_reports_effective_tokenrhythm_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="tokenrhythm",
        model="deepseek-v4-flash",
        api_key="synthetic-tokenrhythm-key",
        base_url="https://tokenrhythm.studio/v1",
    )
    cfg.image_generation.enabled = True

    status = get_onboarding_status(cfg)

    assert status.image_generation_provider == "tokenrhythm"
    assert status.image_generation_source == "llm_fallback"
    assert status.image_generation_state["effective"]["providerId"] == "tokenrhythm"


def test_other_active_llm_keeps_tokenrhythm_as_non_intrusive_image_recommendation(
    monkeypatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "deepseek",
            "model": "deepseek-chat",
            "api_key": "synthetic-llm-key",
        },
        image_generation={
            "enabled": True,
            "binding": "custom",
            "primary": "openai/gpt-image-1",
            "providers": {"openai": {"api_key": "synthetic-image-key"}},
        },
    )

    recommendation = get_onboarding_status(cfg).image_generation_state["recommendation"]

    assert recommendation == {
        "providerId": "tokenrhythm",
        "reason": "recommended_standalone",
        "canReuseCredential": False,
        "actionRequired": False,
    }


def test_image_generation_disabled_is_not_configured():
    cfg = GatewayConfig()
    cfg.image_generation.enabled = False
    cfg.image_generation.providers.openai.api_key = "sk-openai"
    s = get_onboarding_status(cfg)
    assert s.image_generation_configured is False
    assert s.image_generation_enabled is False


def test_ollama_without_key_is_configured():
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="ollama",
        model="llama3",
        api_key="",
        base_url="http://localhost:11434",
    )
    s = get_onboarding_status(cfg)
    assert s.llm_configured is True
    assert s.memory_embedding_configured is True
    assert s.memory_embedding_provider == "auto"
    assert s.sections["memory_embedding"].value == "ok"
    assert s.section_details["llm"]["required"] is True
    assert s.section_details["channels"]["optional"] is True


def test_no_key_provider_reports_not_required_source():
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="ollama",
        model="llama3",
        api_key="",
        base_url="http://localhost:11434",
    )

    s = get_onboarding_status(cfg)

    assert s.llm_source == "not_required"
    assert s.section_details["llm"]["detail"] == "no key required"


@pytest.mark.parametrize(
    "provider_id",
    ["byteplus_coding_plan", "volcengine_coding_plan"],
)
def test_coding_plan_provider_without_key_is_missing_not_key_free(provider_id):
    # Coding-plan providers are runtime-supported and require real env keys;
    # an empty key is missing setup, never "no key required".
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(provider=provider_id, model="m", api_key="")

    s = get_onboarding_status(cfg)

    assert s.llm_configured is False
    assert s.llm_source == "none"
    assert s.sections["llm"].value == "missing"
    assert "no key required" not in str(s.section_details["llm"])


def test_zero_channels_means_not_messaging_configured():
    cfg = GatewayConfig()
    s = get_onboarding_status(cfg)
    assert s.channel_count == 0
    assert s.channels_configured is False


def test_channel_present_marks_configured():
    cfg = GatewayConfig()
    res = upsert_channel(
        cfg,
        entry_payload={"type": "slack", "name": "w", "token": "x", "signing_secret": "ss"},
    )
    s = get_onboarding_status(res.config)
    assert s.channel_count == 1
    assert s.channels_configured is True


def test_section_details_mark_only_provider_as_first_run_blocking():
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(provider="", model="", api_key="")
    cfg.search_provider = "brave"
    cfg.search_api_key = ""
    cfg.search_api_key_env = ""

    s = get_onboarding_status(cfg)

    assert s.section_details["llm"]["blocking"] is True
    assert s.section_details["search"]["actionRequired"] is True
    assert s.section_details["search"]["blocking"] is False


def test_router_detail_explains_provider_dependency_before_provider_setup():
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(provider="", model="", api_key="")

    s = get_onboarding_status(cfg)

    assert (
        s.section_details["router"]["detail"]
        == "uses SquillaRouter after provider setup"
    )
    assert s.section_details["router"]["actionRequired"] is False


def test_optional_search_action_does_not_block_first_run():
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="sk-x",
        base_url="https://openrouter.ai/api/v1",
    )
    cfg.search_provider = "brave"
    cfg.search_api_key = ""
    cfg.search_api_key_env = ""

    s = get_onboarding_status(cfg)

    assert s.section_details["search"]["actionRequired"] is True
    assert s.section_details["search"]["blocking"] is False
    assert s.needs_onboarding is False


def test_optional_image_generation_action_does_not_block_first_run(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="deepseek",
        model="m",
        api_key="sk-x",
        base_url="https://api.deepseek.com/v1",
    )
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openai/gpt-image-1"

    s = get_onboarding_status(cfg)

    assert s.section_details["image_generation"]["actionRequired"] is True
    assert s.section_details["image_generation"]["blocking"] is False
    assert s.needs_onboarding is False


def test_explicit_remote_memory_embedding_action_blocks_gateway_start():
    cfg = GatewayConfig()
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="sk-x",
        base_url="https://openrouter.ai/api/v1",
    )
    cfg.memory.embedding.provider = "openai"
    cfg.memory.embedding.remote.api_key = ""

    s = get_onboarding_status(cfg)

    assert s.section_details["memory_embedding"]["actionRequired"] is True
    assert s.section_details["memory_embedding"]["blocking"] is True
    assert s.needs_onboarding is True


def test_ensemble_section_detail_reflects_selection_mode():
    cfg = GatewayConfig()
    cfg.llm_ensemble.enabled = True

    s = get_onboarding_status(cfg)

    assert s.sections["ensemble"].value == "ok"
    detail = s.section_details["ensemble"]
    assert detail["label"] == "LLM ensemble"
    assert str(detail["detail"]).startswith("selection mode: static_openrouter_b5")


def test_ensemble_section_detail_shows_disabled():
    cfg = GatewayConfig()
    cfg.llm_ensemble.enabled = False

    s = get_onboarding_status(cfg)

    assert s.sections["ensemble"].value == "optional"
    assert s.section_details["ensemble"]["detail"] == "disabled"


def test_ensemble_credential_status_reports_unsupported_provider():
    """The per-provider credential rows must use the same "unsupported"
    enumerant as llm_source for a registered but runtime-unsupported
    provider — never a satisfied-looking "not_required"."""
    from opensquilla.onboarding.status import _llm_provider_credential_status

    cfg = GatewayConfig()
    row = _llm_provider_credential_status(cfg, "github_copilot")

    assert row["source"] == "unsupported"
    assert row["available"] is False
