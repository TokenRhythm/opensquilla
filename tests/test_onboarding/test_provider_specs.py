"""Tests for the provider catalog."""

from __future__ import annotations

import pytest

from opensquilla.provider.registry import get_provider_spec


def test_provider_spec_requires_api_key_for_openrouter():
    spec = get_provider_spec("openrouter")
    assert spec.requires_api_key() is True


def test_provider_spec_does_not_require_api_key_for_ollama():
    spec = get_provider_spec("ollama")
    assert spec.requires_api_key() is False


def test_provider_spec_does_not_require_api_key_for_lm_studio():
    spec = get_provider_spec("lm_studio")
    assert spec.requires_api_key() is False


def test_provider_spec_does_not_require_api_key_for_ovms():
    spec = get_provider_spec("ovms")
    assert spec.requires_api_key() is False


def test_provider_spec_requires_base_url_for_azure():
    spec = get_provider_spec("azure")
    assert spec.requires_base_url() is True


def test_provider_spec_requires_api_key_for_azure():
    # Azure OpenAI requires a deployment-level API key at runtime.
    spec = get_provider_spec("azure")
    assert spec.requires_api_key() is True


def test_provider_spec_requires_base_url_for_vllm():
    spec = get_provider_spec("vllm")
    assert spec.requires_base_url() is True


def test_provider_spec_does_not_require_base_url_for_openrouter():
    spec = get_provider_spec("openrouter")
    assert spec.requires_base_url() is False


# --------------- ProviderSetupSpec catalog ---------------

from opensquilla.onboarding.provider_specs import (  # noqa: E402
    ProviderSetupSpec,
    get_provider_setup_spec,
    list_provider_setup_specs,
    provider_catalog_payload,
)

# Verified: the full agent stack has been exercised against these providers.
EXPECTED_VERIFIED = {
    "openrouter", "openai", "openai_responses", "anthropic", "ollama", "deepseek",
    "gemini", "dashscope", "moonshot", "zhipu", "qianfan",
    "volcengine", "byteplus", "tokenrhythm",
}
# Experimental: registry-runnable, offered with a visible caveat.
EXPECTED_EXPERIMENTAL = {
    "azure", "bailian_coding", "bailian_coding_cn", "kimi_coding_openai",
    "kimi_coding_anthropic", "minimax", "minimax_openai", "minimax_coding_openai",
    "minimax_coding_anthropic", "minimax_cn", "minimax_global", "mimo_openai",
    "mimo_anthropic", "mistral", "groq", "aihubmix", "vllm", "custom",
    "lm_studio", "siliconflow", "ovms", "litellm_proxy", "openai_codex",
    "volcengine_coding_plan", "volcengine_coding_plan_anthropic",
    "byteplus_coding_plan", "byteplus_coding_plan_anthropic",
    "tencent_tokenhub", "tencent_tokenhub_anthropic", "tencent_tokenhub_intl",
    "tencent_token_plan", "tencent_token_plan_anthropic",
}
EXPECTED_SUPPORTED = EXPECTED_VERIFIED | EXPECTED_EXPERIMENTAL
# No runtime support at all: never configurable.
EXPECTED_DISABLED = {
    "github_copilot",
}


def test_catalog_includes_all_supported_providers():
    ids = {s.provider_id for s in list_provider_setup_specs() if s.runtime_supported}
    assert ids == EXPECTED_SUPPORTED


def test_catalog_verification_tiers_match_expected_sets():
    specs = {s.provider_id: s for s in list_provider_setup_specs()}
    for pid in EXPECTED_VERIFIED:
        assert specs[pid].verification == "verified"
        assert "(experimental)" not in specs[pid].label
    for pid in EXPECTED_EXPERIMENTAL:
        assert specs[pid].verification == "experimental"
        assert specs[pid].label.endswith("(experimental)")


def test_catalog_marks_unsupported_providers_disabled():
    specs = {s.provider_id: s for s in list_provider_setup_specs()}
    for pid in EXPECTED_DISABLED:
        assert pid in specs
        assert specs[pid].runtime_supported is False


def test_catalog_prioritizes_tokenrhythm_then_openrouter_then_sorts_remaining():
    specs = list_provider_setup_specs()
    assert specs[0].provider_id == "tokenrhythm"
    assert specs[1].provider_id == "openrouter"
    assert [(s.label.lower(), s.provider_id) for s in specs[2:]] == sorted(
        (s.label.lower(), s.provider_id) for s in specs[2:]
    )


@pytest.mark.parametrize("provider_id", sorted(EXPECTED_SUPPORTED))
def test_supported_providers_have_label_and_backend(provider_id: str):
    spec = get_provider_setup_spec(provider_id)
    assert isinstance(spec, ProviderSetupSpec)
    assert spec.backend
    assert spec.provider_kind


def test_openrouter_has_correct_default_base_url():
    spec = get_provider_setup_spec("openrouter")
    assert spec.default_base_url == "https://openrouter.ai/api/v1"


def test_bailian_coding_regions_are_explicit_provider_choices():
    international = get_provider_setup_spec("bailian_coding")
    mainland = get_provider_setup_spec("bailian_coding_cn")

    assert international.label.startswith("Bailian Coding (International)")
    assert international.default_base_url == "https://coding-intl.dashscope.aliyuncs.com/v1"
    assert mainland.label.startswith("Bailian Coding (Mainland China)")
    assert mainland.default_base_url == "https://coding.dashscope.aliyuncs.com/v1"
    assert international.provider_kind == mainland.provider_kind == "bailian_coding"
    assert international.env_key == mainland.env_key == "BAILIAN_API_KEY"

    for spec in (international, mainland):
        assert spec.default_direct_model == "qwen3.7-plus"
        assert any("sk-sp-" in need for need in spec.what_you_need)
        model_field = next(field for field in spec.fields if field.name == "model")
        assert model_field.default == "qwen3.7-plus"
        api_field = next(field for field in spec.fields if field.name == "api_key")
        assert "sk-sp-" in api_field.description
        assert "not interchangeable" in api_field.description


def test_ollama_does_not_require_api_key_in_setup_spec():
    spec = get_provider_setup_spec("ollama")
    assert spec.accepts_api_key is True
    assert spec.requires_api_key is False
    api_field = next(f for f in spec.fields if f.name == "api_key")
    assert api_field.required is False
    assert all(f.name != "api_key_env" for f in spec.fields)


def test_api_key_providers_expose_env_key_field_in_setup_spec():
    spec = get_provider_setup_spec("openrouter")
    env_field = next(f for f in spec.fields if f.name == "api_key_env")

    assert env_field.label == "API key env"
    assert env_field.default == "OPENROUTER_API_KEY"
    assert env_field.required is False
    assert env_field.secret is False


def test_api_key_field_describes_plaintext_config_storage_accurately():
    """A pasted key is persisted as plaintext api_key in the config file and
    is consulted ahead of the env var — the description must say so instead
    of implying the paste lands under the env key."""
    spec = get_provider_setup_spec("openrouter")
    api_field = next(f for f in spec.fields if f.name == "api_key")

    assert "Stored under env key" not in api_field.description
    assert "plaintext api_key" in api_field.description
    assert "config file" in api_field.description
    assert "ahead of OPENROUTER_API_KEY" in api_field.description
    assert "Leave blank to read OPENROUTER_API_KEY" in api_field.description


def test_api_key_field_description_stays_accurate_without_an_env_key():
    # Providers without a registry env key still persist pastes in plaintext.
    for spec in list_provider_setup_specs():
        api_field = next(f for f in spec.fields if f.name == "api_key")
        if spec.env_key:
            assert spec.env_key in api_field.description
        else:
            assert api_field.description == (
                "Saved as plaintext api_key in the config file."
            )


def test_non_router_providers_explain_required_model_in_setup_spec():
    for provider_id in ("anthropic", "ollama", "openai_responses"):
        spec = get_provider_setup_spec(provider_id)
        model_field = next(f for f in spec.fields if f.name == "model")

        assert spec.router_supported is False
        assert model_field.required is True
        assert "Required" in model_field.description
        assert "router" not in model_field.description.lower()
        assert any("model" in item.lower() for item in spec.what_you_need)


@pytest.mark.parametrize(
    "provider_id",
    [
        "qianfan",
        "volcengine_coding_plan",
        "kimi_coding_openai",
        "kimi_coding_anthropic",
        "minimax",
        "minimax_cn",
        "minimax_global",
        "minimax_coding_openai",
        "minimax_coding_anthropic",
        "mimo_openai",
        "mimo_anthropic",
    ],
)
def test_inline_router_preset_providers_are_router_supported(provider_id: str):
    spec = get_provider_setup_spec(provider_id)
    row = next(row for row in provider_catalog_payload() if row["providerId"] == provider_id)
    model_field = next(f for f in spec.fields if f.name == "model")

    assert spec.router_supported is True
    assert model_field.required is False
    assert row["routerSupported"] is True
    assert row["presets"]


def test_minimax_openai_remains_direct_only_for_current_support_matrix():
    spec = get_provider_setup_spec("minimax_openai")
    row = next(row for row in provider_catalog_payload() if row["providerId"] == "minimax_openai")

    assert spec.router_supported is False
    assert row["routerSupported"] is False


def test_azure_requires_base_url_in_setup_spec():
    spec = get_provider_setup_spec("azure")
    assert spec.requires_base_url is True
    base_field = next(f for f in spec.fields if f.name == "base_url")
    assert base_field.required is True


def test_vllm_requires_base_url_in_setup_spec():
    spec = get_provider_setup_spec("vllm")
    assert spec.requires_base_url is True


def test_custom_provider_is_a_first_class_self_hosted_endpoint():
    """The generic ``custom`` id: base URL is mandatory (there is no default
    endpoint to fall back to), the API key is optional (self-hosted servers
    often run unauthenticated), and the entry is runtime-supported so it can
    be configured and probed like any other provider."""
    spec = get_provider_setup_spec("custom")

    assert spec.runtime_supported is True
    assert spec.backend == "openai_compat"
    assert spec.provider_kind == "openai"
    assert spec.deployment == "custom"
    assert spec.label.startswith("Custom OpenAI-compatible endpoint")

    assert spec.requires_base_url is True
    assert spec.default_base_url == ""
    base_field = next(f for f in spec.fields if f.name == "base_url")
    assert base_field.required is True

    assert spec.accepts_api_key is True
    assert spec.requires_api_key is False
    assert spec.env_key == "CUSTOM_LLM_API_KEY"
    api_field = next(f for f in spec.fields if f.name == "api_key")
    assert api_field.required is False

    model_field = next(f for f in spec.fields if f.name == "model")
    assert model_field.required is True


def test_custom_provider_catalog_payload_semantics():
    row = next(
        row for row in provider_catalog_payload() if row["providerId"] == "custom"
    )

    assert row["runtimeSupported"] is True
    assert row["acceptsApiKey"] is True
    assert row["requiresApiKey"] is False
    assert row["requiresBaseUrl"] is True
    assert row["envKey"] == "CUSTOM_LLM_API_KEY"
    assert row["defaultBaseUrl"] == ""
    fields = {f["name"]: f for f in row["fields"]}
    assert fields["base_url"]["required"] is True
    assert fields["api_key"]["required"] is False


def test_oauth_provider_does_not_accept_an_api_key():
    spec = get_provider_setup_spec("openai_codex")

    assert spec.env_key == "OAuth"
    assert spec.accepts_api_key is False
    assert spec.requires_api_key is False


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        get_provider_setup_spec("does-not-exist")


def test_payload_has_redaction_safe_shape():
    payload = provider_catalog_payload()
    assert isinstance(payload, list)
    assert payload
    sample = payload[0]
    assert "providerId" in sample
    assert "fields" in sample
    for f in sample["fields"]:
        assert "default" in f
        if f.get("secret"):
            assert f.get("default") in (None, "", False)


def test_payload_exposes_only_verified_runtime_supported_providers():
    payload = provider_catalog_payload()

    assert {row["providerId"] for row in payload} == EXPECTED_SUPPORTED
    assert all(row["runtimeSupported"] is True for row in payload)


def test_provider_payload_exposes_readiness_metadata_for_every_provider():
    payload = provider_catalog_payload()

    for row in payload:
        assert row["blocking"] is True
        assert row["deployment"] in {"cloud", "local", "custom", "oauth"}
        # Catalog rows are runtime-supported by construction, so every
        # one is live-probeable via onboarding.provider.probe.
        assert row["canProbe"] is True
        assert row["readmeScenarios"]
        assert row["whatYouNeed"]
