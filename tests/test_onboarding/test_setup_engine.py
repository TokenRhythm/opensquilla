"""Tests for the shared onboarding setup engine."""

from __future__ import annotations

import tomllib

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.config_store import load_config
from opensquilla.onboarding.section_status import SectionStatus
from opensquilla.onboarding.setup_engine import SetupEngine
from opensquilla.onboarding.status import get_onboarding_status


def test_setup_engine_applies_provider_and_router_without_persisting_secret(tmp_path):
    target = tmp_path / "config.toml"
    engine = SetupEngine(path=target)

    engine.apply(
        "provider",
        {
            "providerId": "deepseek",
            "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
        },
    )
    engine.apply("router", {"mode": "recommended"})
    result = engine.persist()

    data = tomllib.loads(target.read_text())
    assert result.path == target
    assert data["llm"]["provider"] == "deepseek"
    assert data["llm"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert "api_key" not in data["llm"]
    assert data["squilla_router"]["tier_profile"] == "deepseek"
    assert "tiers" not in data["squilla_router"]


def test_setup_engine_enables_openrouter_image_default_on_first_configuration():
    engine = SetupEngine(config=GatewayConfig())

    result = engine.apply(
        "provider",
        {
            "providerId": "openrouter",
            "model": "openai/gpt-test",
            "apiKey": "synthetic-openrouter-key",
        },
    )

    image = result.config.image_generation
    assert image.enabled is True
    assert image.binding == "follow_llm"
    assert image.primary == "openrouter/google/gemini-3.1-flash-image-preview"


@pytest.mark.parametrize("intent", ["", False])
def test_setup_engine_rejects_invalid_image_generation_intent(intent):
    engine = SetupEngine(config=GatewayConfig())

    with pytest.raises(ValueError, match="image_generation_intent"):
        engine.apply(
            "provider",
            {
                "providerId": "openrouter",
                "model": "openai/gpt-test",
                "apiKey": "synthetic-openrouter-key",
                "imageGenerationIntent": intent,
            },
        )


def test_setup_engine_optional_key_preservation_is_explicit_and_legacy_safe():
    def configured_engine() -> SetupEngine:
        return SetupEngine(
            config=GatewayConfig(
                llm={
                    "provider": "custom",
                    "model": "model-a",
                    "api_key": "sk-optional",
                    "base_url": "https://llm.example.test/v1",
                }
            )
        )

    legacy = configured_engine()
    legacy.apply(
        "provider",
        {
            "providerId": "custom",
            "model": "model-b",
            "baseUrl": "https://llm.example.test/v1",
        },
    )
    assert legacy.config.llm.api_key == ""

    preserving = configured_engine()
    preserving.apply(
        "provider",
        {
            "providerId": "custom",
            "model": "model-b",
            "baseUrl": "https://llm.example.test/v1",
            "preserveApiKey": True,
        },
    )
    assert preserving.config.llm.api_key == "sk-optional"


def test_setup_engine_can_derive_provider_model_from_router_default_tier(tmp_path):
    target = tmp_path / "config.toml"
    engine = SetupEngine(path=target)

    engine.apply(
        "provider",
        {
            "providerId": "deepseek",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
        },
    )
    result = engine.persist()

    data = tomllib.loads(target.read_text())
    assert result.path == target
    assert data["llm"]["provider"] == "deepseek"
    assert data["llm"]["model"] == "deepseek-v4-flash"
    assert data["squilla_router"]["tier_profile"] == "deepseek"
    assert load_config(target).squilla_router.default_tier == "c1"


def test_setup_engine_passes_preset_id_through_to_provider_upsert(tmp_path):
    # The CLI's --preset flag rides this payload key; a synthesized preset
    # must persist the custom-mode shape (no tier_profile, expanded tiers).
    target = tmp_path / "config.toml"
    engine = SetupEngine(path=target)

    engine.apply(
        "provider",
        {
            "providerId": "groq",
            "model": "llama-3.3-70b",
            "apiKeyEnv": "GROQ_API_KEY",
            "presetId": "groq",
        },
    )
    engine.persist()

    data = tomllib.loads(target.read_text())
    assert data["llm"]["provider"] == "groq"
    assert load_config(target).squilla_router.enabled is True
    assert "tier_profile" not in data["squilla_router"]
    tier = data["squilla_router"]["tiers"]["c0"]
    assert tier["provider"] == "groq"
    assert tier["model"] == "llama-3.3-70b"


def test_setup_engine_router_tier_override_keeps_direct_fallback_model(tmp_path):
    target = tmp_path / "config.toml"
    engine = SetupEngine(path=target)

    engine.apply(
        "provider",
        {
            "providerId": "openai",
            "apiKeyEnv": "OPENAI_API_KEY",
        },
    )
    assert engine.config.llm.model == "gpt-5.4-mini"
    engine.apply(
        "router",
        {
            "mode": "recommended",
            "defaultTier": "c2",
            "tiers": {
                "c2": {
                    "provider": "openai",
                    "model": "gpt-5.5-custom",
                    "thinkingLevel": "high",
                }
            },
        },
    )
    assert engine.config.llm.model == "gpt-5.4-mini"
    engine.persist()

    data = tomllib.loads(target.read_text())
    assert data["llm"]["model"] == "gpt-5.4-mini"
    assert "tier_profile" not in data["squilla_router"]
    assert data["squilla_router"]["default_tier"] == "c2"
    assert data["squilla_router"]["tiers"]["c2"]["model"] == "gpt-5.5-custom"
    assert data["squilla_router"]["tiers"]["c2"]["thinking_level"] == "high"


def test_setup_engine_next_steps_do_not_include_secret(tmp_path):
    engine = SetupEngine(path=tmp_path / "config.toml")
    engine.apply(
        "provider",
        {
            "providerId": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
            "apiKey": "sk-secret",
        },
    )

    text = engine.preview_next_steps()

    assert "sk-secret" not in text
    assert "opensquilla gateway start" in text
    assert "openrouter" in text


def test_setup_engine_image_generation_can_use_custom_env_reference(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_TEST_IMAGE_KEY", "sk-image-env")
    target = tmp_path / "config.toml"
    engine = SetupEngine(path=target)

    engine.apply(
        "image-generation",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKeyEnv": "OPENSQUILLA_TEST_IMAGE_KEY",
        },
    )
    engine.persist()

    data = tomllib.loads(target.read_text())
    provider = data["image_generation"]["providers"]["openrouter"]
    assert load_config(target).image_generation.providers.openrouter.api_key == ""
    assert provider["api_key_env"] == "OPENSQUILLA_TEST_IMAGE_KEY"


def test_setup_engine_forwards_image_format_size_fallbacks_and_explicit_resets(tmp_path):
    target = tmp_path / "config.toml"
    engine = SetupEngine(path=target)

    engine.apply(
        "image-generation",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKey": "sk-first",
            "baseUrl": "https://images.example.test/v1",
            "size": "1536x1024",
            "outputFormat": "webp",
            "fallbacks": ["openai/gpt-image-1"],
        },
    )
    engine.apply(
        "image-generation",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKey": "sk-second",
            "baseUrl": "",
            "fallbacks": [],
            "clearFallbacks": True,
        },
    )
    engine.persist()

    saved = load_config(target).image_generation
    assert saved.size == "1536x1024"
    assert saved.output_format == "webp"
    assert saved.fallbacks == []
    assert saved.providers.openrouter.base_url == "https://openrouter.ai/api/v1"


def test_setup_engine_accepts_short_capability_section_aliases(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    target = tmp_path / "config.toml"
    engine = SetupEngine(path=target)

    engine.apply("image", {"enabled": False})
    engine.apply(
        "memory",
        {
            "providerId": "local",
            "onnxDir": "models/bge",
        },
    )
    engine.persist()

    data = tomllib.loads(target.read_text())
    assert load_config(target).image_generation.enabled is False
    assert data["memory"]["embedding"]["provider"] == "local"
    assert data["memory"]["embedding"]["local"]["onnx_dir"] == "models/bge"


def test_setup_engine_catalog_includes_memory_embedding():
    engine = SetupEngine()

    payload = engine.catalog("memory-embedding")

    provider_ids = {p["providerId"] for p in payload["memoryEmbeddingProviders"]}
    assert {"auto", "local", "openai", "ollama", "none"} <= provider_ids
    assert all("whatYouNeed" in p for p in payload["memoryEmbeddingProviders"])


def test_setup_engine_applies_ensemble_with_keep_current_semantics(tmp_path):
    import pytest

    target = tmp_path / "config.toml"
    engine = SetupEngine(path=target)
    engine.apply(
        "ensemble",
        {
            "enabled": True,
            "selectionMode": "router_dynamic",
            "modelOptions": ["prov/model-a", "prov/model-b"],
            "minSuccessfulProposers": 2,
            "proposerMaxRetries": 2,
            "allFailedPolicy": "error",
        },
    )
    engine.persist()

    data = tomllib.loads(target.read_text())
    ensemble = data["llm_ensemble"]
    assert ensemble["enabled"] is True
    assert ensemble["selection_mode"] == "router_dynamic"
    assert ensemble["model_options"] == ["prov/model-a", "prov/model-b"]
    assert ensemble["min_successful_proposers"] == 2
    assert ensemble["proposer_max_retries"] == 2
    assert ensemble["all_failed_policy"] == "error"
    assert engine.warnings == []

    # A partial payload must only touch the keys it names.
    second = SetupEngine(path=target)
    second.apply("ensemble", {"enabled": False})
    second.persist()

    data = tomllib.loads(target.read_text())
    ensemble = data["llm_ensemble"]
    assert ensemble["enabled"] is False
    assert ensemble["selection_mode"] == "router_dynamic"
    assert ensemble["model_options"] == ["prov/model-a", "prov/model-b"]
    assert ensemble["min_successful_proposers"] == 2
    assert ensemble["proposer_max_retries"] == 2
    assert ensemble["all_failed_policy"] == "error"

    with pytest.raises(ValueError, match="modelOptions must be a list"):
        SetupEngine(path=target).apply("ensemble", {"modelOptions": "not-a-list"})


def test_setup_engine_accepts_ensemble_section_aliases(tmp_path):
    import tomllib as _tomllib

    for alias in ("ensemble", "llm-ensemble", "llm_ensemble"):
        target = tmp_path / f"{alias.replace('_', '-')}.toml"
        # Seed the non-default state: the alias must observably apply the
        # disable, not exit cleanly while persisting nothing (the effective
        # value equals the model default on a fresh config, so a no-op
        # would previously pass this test).
        target.write_text("[llm_ensemble]\nenabled = true\n", encoding="utf-8")
        engine = SetupEngine(path=target)
        engine.apply(alias, {"enabled": False})
        engine.persist()
        data = _tomllib.loads(target.read_text())
        assert data["llm_ensemble"]["enabled"] is False, alias
        assert load_config(target).llm_ensemble.enabled is False


def test_setup_engine_provider_none_payload_values_keep_stored_fields(tmp_path):
    # None payload values mean "not passed": a same-provider re-save keeps
    # the stored model/base_url/proxy instead of coercing None to "None" or
    # resetting to derived defaults.
    target = tmp_path / "config.toml"
    target.write_text(
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "custom/model-x"\n'
        'api_key = "sk-old"\n'
        'base_url = "https://gateway.example.test/v1"\n'
        'proxy = "http://127.0.0.1:7890"\n',
        encoding="utf-8",
    )
    engine = SetupEngine(path=target)

    engine.apply(
        "provider",
        {
            "providerId": "openrouter",
            "model": None,
            "apiKey": "sk-new",
            "apiKeyEnv": None,
            "baseUrl": None,
            "proxy": None,
        },
    )
    engine.persist()

    cfg = load_config(target)
    assert cfg.llm.model == "custom/model-x"
    assert cfg.llm.base_url == "https://gateway.example.test/v1"
    assert cfg.llm.proxy == "http://127.0.0.1:7890"
    assert cfg.llm.api_key == "sk-new"


def test_setup_engine_search_none_payload_values_keep_stored_settings(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        'search_provider = "brave"\n'
        'search_api_key = "sk-old"\n'
        "search_max_results = 9\n"
        'search_proxy = "http://127.0.0.1:7890"\n'
        "search_use_env_proxy = true\n"
        'search_fallback_policy = "network"\n'
        "search_diagnostics = true\n",
        encoding="utf-8",
    )
    engine = SetupEngine(path=target)

    engine.apply(
        "search",
        {
            "providerId": "brave",
            "apiKey": "sk-new",
            "maxResults": None,
            "proxy": None,
            "useEnvProxy": None,
            "fallbackPolicy": None,
            "diagnostics": None,
        },
    )
    engine.persist()

    cfg = load_config(target)
    assert cfg.search_max_results == 9
    assert cfg.search_proxy == "http://127.0.0.1:7890"
    assert cfg.search_use_env_proxy is True
    assert cfg.search_fallback_policy == "network"
    assert cfg.search_diagnostics is True
    assert cfg.search_api_key == "sk-new"


def test_setup_engine_image_enabled_none_keeps_stored_disabled_flag(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENSQUILLA_TEST_IMAGE_KEY", "sk-image-env")
    target = tmp_path / "config.toml"
    target.write_text(
        "[image_generation]\n"
        "enabled = false\n"
        'primary = "openrouter/google/gemini-3.1-flash-image-preview"\n'
        "\n"
        "[image_generation.providers.openrouter]\n"
        'api_key_env = "OPENSQUILLA_TEST_IMAGE_KEY"\n',
        encoding="utf-8",
    )
    engine = SetupEngine(path=target)

    engine.apply(
        "image-generation",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKeyEnv": "OPENSQUILLA_TEST_IMAGE_KEY",
            "enabled": None,
        },
    )
    engine.persist()

    assert load_config(target).image_generation.enabled is False


def test_setup_engine_image_enabled_none_defaults_to_enabled_for_fresh_config(
    tmp_path, monkeypatch
):
    # A config that never stored the flag keeps the legacy
    # configure-implies-enable behavior for a first-time setup.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-image-env")
    target = tmp_path / "config.toml"
    engine = SetupEngine(path=target)

    engine.apply(
        "image-generation",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "enabled": None,
        },
    )
    engine.persist()

    assert load_config(target).image_generation.enabled is True


def test_setup_engine_loads_legacy_image_endpoint_mismatch_as_degraded(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        "[image_generation]\n"
        "enabled = true\n"
        'primary = "openrouter/google/gemini-3.1-flash-image-preview"\n'
        "\n"
        "[image_generation.providers.openrouter]\n"
        'api_key = "sk-synthetic-image"\n'
        'base_url = "https://api.openai.com/v1"\n',
        encoding="utf-8",
    )

    engine = SetupEngine(path=target)
    status = get_onboarding_status(engine.config)

    assert engine.config.image_generation.enabled is True
    assert status.sections["image_generation"] is SectionStatus.DEGRADED
    assert status.image_generation_configured is False
    assert status.section_details["image_generation"]["actionRequired"] is True
