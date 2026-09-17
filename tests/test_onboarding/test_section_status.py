"""Per-section verifier behaviour and the ``needs_onboarding`` reduction."""

from __future__ import annotations

import pytest

from opensquilla.gateway.config import (
    GatewayConfig,
    LlmProviderConfig,
    MemoryEmbeddingConfig,
    SlackChannelEntry,
)
from opensquilla.onboarding.section_status import (
    SectionStatus,
    channels_section_status,
    ensemble_section_status,
    image_generation_section_status,
    llm_section_status,
    memory_embedding_section_status,
    needs_onboarding,
    router_section_status,
    search_section_status,
)
from opensquilla.onboarding.status import get_onboarding_status


@pytest.fixture()
def cfg() -> GatewayConfig:
    return GatewayConfig()


# ── llm ─────────────────────────────────────────────────────────────────────

def test_llm_missing_when_provider_unset(cfg):
    cfg.llm = LlmProviderConfig(provider="", model="", api_key="")
    assert llm_section_status(cfg) is SectionStatus.MISSING


def test_llm_ok_with_explicit_api_key(cfg):
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="sk-x",
        base_url="https://openrouter.ai/api/v1",
    )
    assert llm_section_status(cfg) is SectionStatus.OK


def test_llm_ok_with_env_key_present(cfg, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="",
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
    )
    assert llm_section_status(cfg) is SectionStatus.OK


def test_llm_degraded_when_env_key_missing(cfg, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="",
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
    )
    assert llm_section_status(cfg) is SectionStatus.DEGRADED


def test_llm_unknown_for_unsupported_provider(cfg):
    cfg.llm = LlmProviderConfig(provider="no-such-provider", model="m")
    assert llm_section_status(cfg) is SectionStatus.UNKNOWN


def test_llm_ok_with_default_env_var_when_config_omits_key(cfg, monkeypatch):
    # No explicit api_key / api_key_env, but the provider's default env var is
    # present — mirrors the runtime (resolve_llm_runtime_config) and image-gen.
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="",
        base_url="https://openrouter.ai/api/v1",
    )
    assert llm_section_status(cfg) is SectionStatus.OK


def test_llm_missing_without_key_or_default_env_var(cfg, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="",
        base_url="https://openrouter.ai/api/v1",
    )
    assert llm_section_status(cfg) is SectionStatus.MISSING


# ── router ──────────────────────────────────────────────────────────────────

def test_router_disabled_is_optional(cfg):
    cfg.squilla_router.enabled = False
    assert router_section_status(cfg) is SectionStatus.OPTIONAL


def test_router_enabled_is_ok(cfg):
    cfg.squilla_router.enabled = True
    assert router_section_status(cfg) is SectionStatus.OK


# ── ensemble ────────────────────────────────────────────────────────────────

def test_ensemble_enabled_is_ok(cfg):
    cfg.llm_ensemble.enabled = True
    assert ensemble_section_status(cfg) is SectionStatus.OK


def test_ensemble_disabled_is_optional(cfg):
    cfg.llm_ensemble.enabled = False
    assert ensemble_section_status(cfg) is SectionStatus.OPTIONAL


def test_ensemble_never_blocks_onboarding(cfg):
    # The ensemble reuses the provider credential; there is nothing section-
    # local to verify, so it must never produce a blocking/action state.
    from opensquilla.onboarding.status import get_onboarding_status

    for enabled in (True, False):
        cfg.llm_ensemble.enabled = enabled
        detail = get_onboarding_status(cfg).section_details["ensemble"]
        assert detail["blocking"] is False
        assert detail["actionRequired"] is False
        assert detail["required"] is False


def test_ensemble_status_reports_candidate_provider_credentials(cfg, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Construct with the llm section so the router ladder follows the provider;
    # a post-construction reassignment would leave the built-in default
    # provider's ladder in place and add its provider to the credential list.
    cfg = GatewayConfig(
        llm={
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "api_key": "sk-deepseek",
            "base_url": "https://api.deepseek.com",
        }
    )
    cfg.llm_ensemble.enabled = True
    cfg.llm_ensemble.selection_mode = "router_dynamic"
    cfg.llm_ensemble.candidates = [
        {
            "provider": "openrouter",
            "model": "qwen/qwen3.7-max",
            "source": "custom",
            "enabled": True,
        }
    ]

    status = get_onboarding_status(cfg)

    assert status.ensemble_credential_status == (
        {
            "provider": "deepseek",
            "available": True,
            "source": "explicit",
            "envKey": "DEEPSEEK_API_KEY",
        },
        {
            "provider": "openrouter",
            "available": False,
            "source": "missing_env",
            "envKey": "OPENROUTER_API_KEY",
        },
    )


def test_ensemble_status_uses_profile_credential_resolution(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "api_key": "sk-deepseek",
        },
        llm_profiles={
            "OpenAI": {
                "api_key": "sk-profile",
            }
        },
    )
    cfg.llm_ensemble.enabled = True
    cfg.llm_ensemble.selection_mode = "router_dynamic"
    cfg.llm_ensemble.candidates = [
        {
            "provider": "openai",
            "model": "gpt-5-mini",
            "source": "custom",
            "enabled": True,
        }
    ]

    status = get_onboarding_status(cfg)

    by_provider = {
        str(row["provider"]): row for row in status.ensemble_credential_status
    }
    assert by_provider["openai"] == {
        "provider": "openai",
        "available": True,
        "source": "explicit",
        "envKey": "OPENAI_API_KEY",
    }


def test_llm_credential_status_reports_explicit_key(cfg):
    cfg.llm = LlmProviderConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="sk-deepseek-secret-123456",
        api_key_env="",
        base_url="https://api.deepseek.com",
    )

    status = get_onboarding_status(cfg)

    cred = status.llm_credential_status
    assert cred["provider"] == "deepseek"
    assert cred["available"] is True
    assert cred["source"] == "explicit"
    assert cred["envKey"] == "DEEPSEEK_API_KEY"
    assert cred["masked"] != "sk-deepseek-secret-123456"
    assert cred["masked"].endswith("3456")
    assert cred["revealAllowed"] is False


def test_llm_credential_status_reports_env_key(cfg, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-env-654321")
    cfg.llm = LlmProviderConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
    )

    status = get_onboarding_status(cfg)

    cred = status.llm_credential_status
    assert cred["provider"] == "deepseek"
    assert cred["available"] is True
    assert cred["source"] == "env"
    assert cred["envKey"] == "DEEPSEEK_API_KEY"
    assert cred["masked"] != "sk-deepseek-env-654321"
    assert cred["masked"].endswith("4321")
    assert cred["revealAllowed"] is False


def test_llm_credential_status_reports_missing_env(cfg, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "synthetic-generic-key")
    cfg.llm = LlmProviderConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
    )

    status = get_onboarding_status(cfg)

    assert status.llm_credential_status == {
        "provider": "deepseek",
        "available": False,
        "source": "missing_env",
        "envKey": "DEEPSEEK_API_KEY",
        "masked": "",
        "revealAllowed": False,
    }


def test_llm_credential_status_treats_runtime_secret_cache_as_env(cfg, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-current")
    cfg.llm = LlmProviderConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="sk-runtime-cache",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
    )
    cfg.mark_runtime_secret("llm.api_key")

    status = get_onboarding_status(cfg)

    cred = status.llm_credential_status
    assert cred["provider"] == "deepseek"
    assert cred["available"] is True
    assert cred["source"] == "env"
    assert cred["envKey"] == "DEEPSEEK_API_KEY"
    assert cred["masked"] != "sk-runtime-cache"
    assert cred["masked"].endswith("rent")
    assert cred["revealAllowed"] is False


def test_llm_credential_status_hides_runtime_cache_when_env_missing(cfg, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY_ENV", raising=False)
    cfg.llm = LlmProviderConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="sk-runtime-cache",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
    )
    cfg.mark_runtime_secret("llm.api_key")

    status = get_onboarding_status(cfg)

    assert status.llm_credential_status == {
        "provider": "deepseek",
        "available": False,
        "source": "missing_env",
        "envKey": "DEEPSEEK_API_KEY",
        "masked": "",
        "revealAllowed": False,
    }


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("custom", "https://custom.example.test/v1"),
        ("ollama", "http://localhost:11434"),
        ("lm_studio", "http://localhost:1234/v1"),
        ("ovms", "http://localhost:8000/v3"),
        ("vllm", "http://localhost:8000/v1"),
    ],
)
def test_optional_llm_credential_status_reports_explicit_key(
    cfg, monkeypatch, provider, base_url
):
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    cfg.llm = LlmProviderConfig(
        provider=provider,
        model="test-model",
        api_key="sk-optional-secret-123456",
        base_url=base_url,
    )

    cred = get_onboarding_status(cfg).llm_credential_status

    assert cred["available"] is True
    assert cred["source"] == "explicit"
    assert cred["masked"] != "sk-optional-secret-123456"
    assert cred["masked"].endswith("3456")


def test_optional_llm_credential_status_uses_visible_default_env(cfg, monkeypatch):
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "sk-custom-env-654321")
    cfg.llm = LlmProviderConfig(
        provider="custom",
        model="test-model",
        api_key="",
        api_key_env="",
        base_url="https://custom.example.test/v1",
    )

    cred = get_onboarding_status(cfg).llm_credential_status

    assert cred["available"] is True
    assert cred["source"] == "env"
    assert cred["envKey"] == "CUSTOM_LLM_API_KEY"
    assert cred["masked"].endswith("4321")


def test_optional_llm_credential_status_without_key_is_not_required(cfg, monkeypatch):
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)
    cfg.llm = LlmProviderConfig(
        provider="custom",
        model="test-model",
        api_key="",
        api_key_env="",
        base_url="https://custom.example.test/v1",
    )

    cred = get_onboarding_status(cfg).llm_credential_status

    assert cred["available"] is True
    assert cred["source"] == "not_required"
    assert cred["masked"] == ""


def test_optional_llm_credential_status_reports_configured_missing_env(cfg, monkeypatch):
    monkeypatch.delenv("PRIVATE_CUSTOM_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "synthetic-generic-key")
    cfg.llm = LlmProviderConfig(
        provider="custom",
        model="test-model",
        api_key="",
        api_key_env="PRIVATE_CUSTOM_KEY",
        base_url="https://custom.example.test/v1",
    )

    cred = get_onboarding_status(cfg).llm_credential_status

    assert cred["available"] is False
    assert cred["source"] == "missing_env"
    assert cred["envKey"] == "PRIVATE_CUSTOM_KEY"


# ── search ──────────────────────────────────────────────────────────────────

def test_search_unset_is_optional(cfg, monkeypatch):
    cfg.search_provider = ""
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert search_section_status(cfg) is SectionStatus.OPTIONAL


def test_search_duckduckgo_default_is_ok(cfg):
    cfg.search_provider = "duckduckgo"
    cfg.search_api_key = ""
    cfg.search_api_key_env = ""
    assert search_section_status(cfg) is SectionStatus.OK


def test_search_brave_with_explicit_key_is_ok(cfg):
    cfg.search_provider = "brave"
    cfg.search_api_key = "secret"
    assert search_section_status(cfg) is SectionStatus.OK


def test_search_brave_without_credentials_is_missing(cfg, monkeypatch):
    cfg.search_provider = "brave"
    cfg.search_api_key = ""
    cfg.search_api_key_env = ""
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert search_section_status(cfg) is SectionStatus.MISSING


def test_search_brave_with_default_env_var_is_ok(cfg, monkeypatch):
    from opensquilla.onboarding.search_specs import get_search_provider_setup_spec

    # No explicit key/env declared, but the provider's default env var resolves.
    cfg.search_provider = "brave"
    cfg.search_api_key = ""
    cfg.search_api_key_env = ""
    monkeypatch.setenv(get_search_provider_setup_spec("brave").env_key, "from-env")
    assert search_section_status(cfg) is SectionStatus.OK


def test_search_brave_with_env_key_missing_is_degraded(cfg, monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    cfg.search_provider = "brave"
    cfg.search_api_key = ""
    cfg.search_api_key_env = "BRAVE_API_KEY"
    assert search_section_status(cfg) is SectionStatus.DEGRADED


def test_search_unknown_provider_is_unknown(cfg):
    cfg.search_provider = "no-such-search"
    assert search_section_status(cfg) is SectionStatus.UNKNOWN


# ── channels ────────────────────────────────────────────────────────────────

def test_channels_empty_is_optional(cfg):
    cfg.channels.channels.clear()
    assert channels_section_status(cfg) is SectionStatus.OPTIONAL


def test_channels_all_disabled_is_optional(cfg):
    cfg.channels.channels.clear()
    cfg.channels.channels.append(
        SlackChannelEntry(name="work", enabled=False, token="x")
    )
    assert channels_section_status(cfg) is SectionStatus.OPTIONAL


def test_channels_any_enabled_is_ok(cfg):
    cfg.channels.channels.clear()
    cfg.channels.channels.append(
        SlackChannelEntry(name="work", enabled=True, token="x")
    )
    assert channels_section_status(cfg) is SectionStatus.OK


# ── image generation ────────────────────────────────────────────────────────

def test_image_generation_disabled_is_optional(cfg):
    cfg.image_generation.enabled = False
    assert image_generation_section_status(cfg) is SectionStatus.OPTIONAL


def test_image_generation_enabled_without_credentials_is_missing(cfg, monkeypatch):
    cfg.image_generation.enabled = True
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # No provider credentials anywhere, LLM provider is not the image provider.
    cfg.llm = LlmProviderConfig(provider="openrouter", model="m", api_key="")
    assert image_generation_section_status(cfg) is SectionStatus.MISSING


def test_follow_llm_image_generation_is_dormant_for_other_active_provider(cfg):
    cfg.image_generation.enabled = True
    cfg.image_generation.binding = "follow_llm"
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    cfg.image_generation.providers.openrouter.api_key = "synthetic-image-key"
    cfg.llm = LlmProviderConfig(
        provider="openai",
        model="gpt-test",
        api_key="synthetic-openai-key",
    )

    status = get_onboarding_status(cfg)

    assert image_generation_section_status(cfg) is SectionStatus.OPTIONAL
    assert status.image_generation_state["mode"] == "follow_llm"
    assert status.image_generation_state["operatorManaged"] is False
    assert status.image_generation_state["effective"] == {
        "enabled": False,
        "available": False,
        "dormant": True,
        "providerId": "openrouter",
        "primary": "openrouter/google/gemini-3.1-flash-image-preview",
        "credentialSource": "none",
        "credentialOwner": "none",
        "reason": "active_provider_mismatch",
    }
    recommendation = status.image_generation_state["recommendation"]
    assert recommendation["providerId"] == "openai"
    assert recommendation["canReuseCredential"] is True

    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="openai/gpt-test",
        api_key="synthetic-openrouter-key",
        base_url="https://openrouter.ai/api/v1",
    )
    restored = get_onboarding_status(cfg)
    assert image_generation_section_status(cfg) is SectionStatus.OK
    assert restored.image_generation_state["effective"]["dormant"] is False
    assert restored.image_generation_state["effective"]["available"] is True


def test_follow_llm_image_generation_reuses_resolved_llm_env_reference(
    cfg,
    monkeypatch,
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("CUSTOM_OPENROUTER_KEY", "synthetic-env-key")
    cfg.image_generation.enabled = True
    cfg.image_generation.binding = "follow_llm"
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="openai/gpt-test",
        api_key_env="CUSTOM_OPENROUTER_KEY",
        base_url="https://openrouter.ai/api/v1",
    )

    status = get_onboarding_status(cfg)

    assert image_generation_section_status(cfg) is SectionStatus.OK
    assert status.image_generation_source == "llm_fallback"
    assert status.image_generation_env_key == "CUSTOM_OPENROUTER_KEY"
    assert status.image_generation_state["effective"]["available"] is True


def test_follow_llm_image_generation_is_dormant_for_custom_same_provider_endpoint(cfg):
    cfg.image_generation.enabled = True
    cfg.image_generation.binding = "follow_llm"
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    cfg.image_generation.providers.openrouter.api_key = "synthetic-image-key"
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="compatible-model",
        api_key="synthetic-llm-key",
        base_url="https://compatible.example.test/v1",
    )

    status = get_onboarding_status(cfg)

    assert image_generation_section_status(cfg) is SectionStatus.OPTIONAL
    assert status.image_generation_state["effective"]["dormant"] is True
    assert status.image_generation_state["effective"]["available"] is False
    assert status.image_generation_state["recommendation"] == {
        "providerId": "tokenrhythm",
        "reason": "recommended_standalone",
        "canReuseCredential": False,
        "actionRequired": True,
    }


def test_follow_llm_image_generation_reports_missing_llm_env_reference(
    cfg,
    monkeypatch,
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_OPENROUTER_KEY", raising=False)
    cfg.image_generation.enabled = True
    cfg.image_generation.binding = "follow_llm"
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="openai/gpt-test",
        api_key_env="CUSTOM_OPENROUTER_KEY",
        base_url="https://openrouter.ai/api/v1",
    )

    status = get_onboarding_status(cfg)

    assert image_generation_section_status(cfg) is SectionStatus.DEGRADED
    assert status.image_generation_source == "missing_env"
    assert status.image_generation_env_key == "CUSTOM_OPENROUTER_KEY"
    assert status.image_generation_state["effective"]["available"] is False


def test_image_generation_unknown_provider_reference_is_unknown(cfg, monkeypatch):
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "no-such-provider/no-such-model"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    status = get_onboarding_status(cfg)

    assert image_generation_section_status(cfg) is SectionStatus.UNKNOWN
    assert status.image_generation_configured is False
    assert status.image_generation_provider == "no-such-provider"
    assert status.section_details["image_generation"]["detail"] == (
        "no-such-provider (unknown image provider)"
    )


def test_image_generation_env_key_reference_missing_is_degraded(cfg, monkeypatch):
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openai/gpt-image-1"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_IMAGE_KEY", raising=False)
    cfg.llm = LlmProviderConfig(provider="openrouter", model="m", api_key="")
    # Wire an explicit env reference to a variable that is not set.
    openai_provider = cfg.image_generation.providers.openai
    openai_provider.api_key = ""
    openai_provider.api_key_env = "CUSTOM_IMAGE_KEY"
    assert image_generation_section_status(cfg) is SectionStatus.DEGRADED


def test_image_generation_missing_custom_env_is_not_masked_by_default_env(
    cfg,
    monkeypatch,
):
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openai/gpt-image-1"
    cfg.llm = LlmProviderConfig(provider="openrouter", model="m", api_key="")
    openai_provider = cfg.image_generation.providers.openai
    openai_provider.api_key = ""
    openai_provider.api_key_env = "CUSTOM_IMAGE_KEY"
    monkeypatch.delenv("CUSTOM_IMAGE_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "default-env-key")

    assert image_generation_section_status(cfg) is SectionStatus.DEGRADED


def test_image_generation_custom_default_primary_is_not_masked_by_other_provider(
    cfg,
    monkeypatch,
):
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openai/gpt-image-1"
    cfg.llm = LlmProviderConfig(provider="openrouter", model="m", api_key="sk-or")
    openai_provider = cfg.image_generation.providers.openai
    openai_provider.api_key = ""
    openai_provider.api_key_env = "CUSTOM_IMAGE_KEY"
    monkeypatch.delenv("CUSTOM_IMAGE_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert image_generation_section_status(cfg) is SectionStatus.DEGRADED


def test_image_generation_official_endpoint_provider_mismatch_is_degraded(cfg):
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    openrouter_provider = cfg.image_generation.providers.openrouter
    openrouter_provider.api_key = "sk-synthetic-image"
    openrouter_provider.base_url = "https://api.openai.com/v1"

    status = get_onboarding_status(cfg)

    assert image_generation_section_status(cfg) is SectionStatus.DEGRADED
    assert status.image_generation_configured is False
    assert status.image_generation_provider == "openrouter"
    assert status.image_generation_source == "explicit"
    detail = status.section_details["image_generation"]
    assert detail["status"] == "degraded"
    assert detail["actionRequired"] is True
    assert detail["blocking"] is False
    assert detail["detail"] == (
        "openrouter (endpoint/provider mismatch: configured openai official endpoint)"
    )


def test_image_generation_custom_compatible_endpoint_remains_ok(cfg):
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    openrouter_provider = cfg.image_generation.providers.openrouter
    openrouter_provider.api_key = "sk-synthetic-image"
    openrouter_provider.base_url = "https://images.example.test/v1"

    assert image_generation_section_status(cfg) is SectionStatus.OK


@pytest.mark.parametrize(
    "base_url",
    [
        "not-a-url",
        " https://openrouter.ai/api/v1 ",
        "https://openrouter.ai:invalid/v1",
        "https://openrouter.ai:99999/v1",
        "https://openrouter.ai/api/v1?tenant=test",
        "https://openrouter.ai/api/v1#fragment",
        "https://user:secret@openrouter.ai/api/v1",
    ],
)
def test_image_generation_invalid_endpoint_is_degraded(cfg, base_url):
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    openrouter_provider = cfg.image_generation.providers.openrouter
    openrouter_provider.api_key = "sk-synthetic-image"
    openrouter_provider.base_url = base_url

    status = get_onboarding_status(cfg)

    assert image_generation_section_status(cfg) is SectionStatus.DEGRADED
    assert status.image_generation_configured is False
    assert status.section_details["image_generation"]["detail"] == (
        "openrouter (invalid image endpoint; use an absolute http:// or https:// URL)"
    )


def test_image_generation_malformed_legacy_model_ref_is_unknown(cfg):
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openrouter/"
    cfg.image_generation.providers.openrouter.api_key = "sk-synthetic-image"

    status = get_onboarding_status(cfg)

    assert image_generation_section_status(cfg) is SectionStatus.UNKNOWN
    assert status.image_generation_configured is False
    assert status.section_details["image_generation"]["detail"] == (
        "invalid image provider/model reference"
    )


def test_image_generation_mismatch_is_not_hidden_by_a_healthy_fallback(cfg):
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    cfg.image_generation.fallbacks = ["openai/gpt-image-1"]
    cfg.image_generation.providers.openrouter.api_key = "sk-synthetic-openrouter"
    cfg.image_generation.providers.openrouter.base_url = "https://api.openai.com/v1"
    cfg.image_generation.providers.openai.api_key = "sk-synthetic-openai"

    assert image_generation_section_status(cfg) is SectionStatus.DEGRADED


def test_image_generation_default_env_does_not_apply_to_custom_endpoint(cfg, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-default-origin-key")
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    cfg.image_generation.providers.openrouter.base_url = "https://images.example.test/v1"
    cfg.llm = LlmProviderConfig(provider="openai", model="m", api_key="")

    assert image_generation_section_status(cfg) is SectionStatus.MISSING


def test_image_generation_inherited_llm_endpoint_mismatch_is_degraded(cfg):
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    cfg.image_generation.providers.openrouter.api_key = "sk-synthetic-image"
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="sk-synthetic-llm",
        base_url="https://api.openai.com/v1",
    )

    assert image_generation_section_status(cfg) is SectionStatus.DEGRADED


def test_image_generation_cross_origin_llm_key_is_degraded(cfg, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    cfg.image_generation.providers.openrouter.base_url = "https://images.example.test/v1"
    cfg.llm = LlmProviderConfig(
        provider="openrouter",
        model="m",
        api_key="sk-synthetic-llm",
        base_url="https://openrouter.ai/api/v1",
    )

    status = get_onboarding_status(cfg)

    assert image_generation_section_status(cfg) is SectionStatus.DEGRADED
    assert status.image_generation_configured is False
    assert status.image_generation_source == "none"
    assert status.image_generation_provider == "openrouter"
    assert status.section_details["image_generation"]["detail"] == (
        "openrouter (LLM key cannot be reused across image endpoint origins)"
    )


# ── memory embedding ────────────────────────────────────────────────────────

def test_memory_embedding_auto_is_ok(cfg):
    cfg.memory.embedding = MemoryEmbeddingConfig(provider="auto")
    assert memory_embedding_section_status(cfg) is SectionStatus.OK


def test_memory_embedding_none_is_optional(cfg):
    cfg.memory.embedding = MemoryEmbeddingConfig(provider="none")
    assert memory_embedding_section_status(cfg) is SectionStatus.OPTIONAL


def test_memory_embedding_remote_without_key_is_missing(cfg):
    cfg.memory.embedding = MemoryEmbeddingConfig(provider="openai")
    assert memory_embedding_section_status(cfg) is SectionStatus.MISSING


def test_memory_embedding_remote_with_missing_env_key_is_degraded(cfg, monkeypatch):
    monkeypatch.delenv("OPENAI_EMBEDDINGS_API_KEY", raising=False)
    cfg.memory.embedding = MemoryEmbeddingConfig(
        provider="openai",
        remote={"api_key_env": "OPENAI_EMBEDDINGS_API_KEY"},
    )

    assert memory_embedding_section_status(cfg) is SectionStatus.DEGRADED


def test_memory_embedding_remote_with_env_key_is_ok(cfg, monkeypatch):
    monkeypatch.setenv("OPENAI_EMBEDDINGS_API_KEY", "mem-env-key")
    cfg.memory.embedding = MemoryEmbeddingConfig(
        provider="openai",
        remote={"api_key_env": "OPENAI_EMBEDDINGS_API_KEY"},
    )

    assert memory_embedding_section_status(cfg) is SectionStatus.OK


def test_memory_embedding_remote_with_key_is_ok(cfg):
    cfg.memory.embedding = MemoryEmbeddingConfig(
        provider="openai",
        remote={"api_key": "sk-embedding"},
    )
    assert memory_embedding_section_status(cfg) is SectionStatus.OK


# ── needs_onboarding reduction ───────────────────────────────────────────────

def test_needs_onboarding_false_when_all_ok_or_optional():
    sections = {
        "llm": SectionStatus.OK,
        "router": SectionStatus.OPTIONAL,
        "search": SectionStatus.OK,
        "channels": SectionStatus.OPTIONAL,
        "image_generation": SectionStatus.OPTIONAL,
        "memory_embedding": SectionStatus.OK,
    }
    assert needs_onboarding(sections) is False


def test_needs_onboarding_false_when_optional_section_missing():
    sections = {
        "llm": SectionStatus.OK,
        "router": SectionStatus.OPTIONAL,
        "search": SectionStatus.MISSING,
        "channels": SectionStatus.OPTIONAL,
        "image_generation": SectionStatus.OPTIONAL,
        "memory_embedding": SectionStatus.OK,
    }
    assert needs_onboarding(sections) is False


def test_needs_onboarding_true_when_required_section_degraded():
    sections = {
        "llm": SectionStatus.DEGRADED,
        "router": SectionStatus.OK,
        "search": SectionStatus.OK,
        "channels": SectionStatus.OPTIONAL,
        "image_generation": SectionStatus.OPTIONAL,
        "memory_embedding": SectionStatus.OK,
    }
    assert needs_onboarding(sections) is True


def test_needs_onboarding_false_when_optional_section_unknown():
    sections = {
        "llm": SectionStatus.OK,
        "router": SectionStatus.UNKNOWN,
        "search": SectionStatus.OK,
        "channels": SectionStatus.OPTIONAL,
        "image_generation": SectionStatus.OPTIONAL,
        "memory_embedding": SectionStatus.OK,
    }
    assert needs_onboarding(sections) is False


# ── shared image-generation provider resolution ─────────────────────────────

def test_image_generation_provider_resolution_has_a_single_implementation():
    """status.py must reuse section_status's provider resolution verbatim.

    The two modules once carried textually diverging copies of this ~50-line
    helper; pin the import so any future re-fork fails loudly.
    """
    from opensquilla.onboarding import section_status, status

    assert (
        status._configured_image_generation_provider_ids
        is section_status._configured_image_generation_provider_ids
    )


def test_default_image_generation_primary_matches_config_default():
    from opensquilla.gateway.config import ImageGenerationConfig
    from opensquilla.onboarding.section_status import DEFAULT_IMAGE_GENERATION_PRIMARY

    assert (
        DEFAULT_IMAGE_GENERATION_PRIMARY
        == ImageGenerationConfig.model_fields["primary"].default
    )
    assert DEFAULT_IMAGE_GENERATION_PRIMARY == GatewayConfig().image_generation.primary


def test_both_call_sites_resolve_the_same_providers(cfg, monkeypatch):
    """Verifier and status annotations must agree on the resolved providers."""
    from opensquilla.onboarding import section_status
    from opensquilla.onboarding.status import get_onboarding_status

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg.image_generation.enabled = True
    cfg.image_generation.primary = "openrouter/google/gemini-3.1-flash-image-preview"
    cfg.image_generation.providers.openrouter.api_key = "sk-dummy-image"

    resolved = section_status._configured_image_generation_provider_ids(cfg)
    s = get_onboarding_status(cfg)

    assert resolved == ["openrouter"]
    assert s.image_generation_provider == "openrouter"
    assert image_generation_section_status(cfg) is SectionStatus.OK
    assert s.image_generation_configured is True
