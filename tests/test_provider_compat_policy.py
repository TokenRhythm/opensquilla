"""Contract tests for the OpenAI-compat dialect policy layer.

The policy record is the single source of provider-kind quirks: every
registered openai_compat provider must resolve to an explicit policy, and the
request/stream behaviors that used to fork on ``provider_kind`` must follow
the policy fields.
"""

from __future__ import annotations

from opensquilla.provider.compat_policy import (
    TEXT_TOOL_DIALECT_DEEPSEEK_DSML,
    OpenAICompatPolicy,
    compat_policy_for_kind,
    known_policy_kinds,
)
from opensquilla.provider.model_identity import (
    DEEPSEEK_V4_MODEL_IDS,
    is_deepseek_v4_model_id,
)
from opensquilla.provider.openai import (
    OpenAIProvider,
    _should_replay_reasoning_content,
    _should_send_temperature,
    _uses_max_completion_tokens,
)
from opensquilla.provider.registry import list_provider_specs
from opensquilla.provider.types import ChatConfig, ModelCapabilities


def test_every_openai_compat_spec_has_explicit_policy() -> None:
    """A registered compat provider without a policy silently gets defaults —
    lock the sync so new registrations must declare their dialect."""
    missing = [
        spec.provider_id
        for spec in list_provider_specs()
        if spec.backend == "openai_compat"
        and spec.runtime_supported
        and spec.provider_kind not in known_policy_kinds()
    ]
    assert missing == [], f"openai_compat specs without a compat policy: {missing}"


def test_registry_attaches_kind_policy() -> None:
    specs = {spec.provider_id: spec for spec in list_provider_specs()}
    assert specs["openrouter"].compat.trust_billed_cost is True
    assert specs["openrouter"].compat.display_name == "OpenRouter"
    assert specs["openrouter"].compat.allow_post_terminal_noop_choice is True
    assert specs["openrouter"].compat.post_terminal_metadata_keys == frozenset(
        {"provider"}
    )
    assert specs["zhipu"].compat.official_reasoning_dialect == "zai"
    assert (
        specs["zhipu"].compat.official_reasoning_api_root
        == specs["zhipu"].default_base_url
    )
    assert specs["volcengine"].compat.tool_schema_unsupported_keywords
    assert specs["vllm"].compat.display_name == "OpenAI"  # kind-aliased to openai


def test_unknown_kind_gets_neutral_default() -> None:
    policy = compat_policy_for_kind("no-such-kind")
    assert policy == OpenAICompatPolicy()
    assert policy.display_name == "Provider"


def test_api_url_absorbs_any_version_suffix() -> None:
    for base, expected in [
        ("https://x.example/v1", "https://x.example/v1/chat/completions"),
        ("https://x.example/v4", "https://x.example/v4/chat/completions"),
        ("https://x.example/v5", "https://x.example/v5/chat/completions"),
        ("https://x.example/api/v12", "https://x.example/api/v12/chat/completions"),
        ("https://x.example", "https://x.example/v1/chat/completions"),
        ("https://x.example/v2beta", "https://x.example/v2beta/chat/completions"),
        (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        ),
    ]:
        provider = OpenAIProvider(api_key="k", model="m", base_url=base)
        assert provider._api_url("/v1/chat/completions") == expected, base


def test_tokenrhythm_v4_reasoning_is_exact_and_endpoint_scoped() -> None:
    policy = compat_policy_for_kind("tokenrhythm")
    assert policy.thinking_toggle_model_ids == frozenset()
    assert policy.default_reasoning_format == ""
    assert policy.replay_reasoning_format == ""
    assert len(policy.reasoning_model_rules) == 2
    official, custom = policy.reasoning_model_rules
    assert official.matches(
        "deepseek-v4-flash", "https://tokenrhythm.studio/v1"
    )
    assert official.matches(
        "deepseek-v4-pro-0813", "https://tokenrhythm.studio"
    )
    assert official.matches(
        "tokenrhythm/deepseek-v4-pro-0813",
        "HTTPS://TOKENRHYTHM.STUDIO:443/v1/",
    )
    assert not official.matches(
        "tokenrhythm/deepseek-v4-flash-0731",
        "https://api.tokenrhythm.studio/v1",
    )
    assert not official.matches(
        "untrusted/deepseek-v4-flash", "https://tokenrhythm.studio/v1"
    )
    assert not official.matches(
        "deepseek-v4-flash", "https://tokenrhythm.studio.evil.example/v1"
    )
    assert not official.matches(
        "deepseek-v4-pro-0813", "https://tokenrhythm.studio/v1/custom"
    )
    assert not official.matches(
        "deepseek-v4-pro-0813-preview", "https://tokenrhythm.studio/v1"
    )
    assert not official.matches(
        "deepseek-v4-pro-0813", "https://tokenrhythm.studio/v1?"
    )
    assert not official.matches(
        "deepseek-v4-pro-0813", "https://tokenrhythm.studio/v1#"
    )
    assert official.replay_scope == "tool_call_assistant"
    assert official.max_reasoning_content_utf16_units == 50_000
    assert official.reasoning_format == "deepseek"
    assert custom.matches("deepseek-v4-flash", "https://custom.example/v1")
    assert not custom.matches("deepseek-v4-pro-0813", "https://custom.example/v1")
    assert not custom.matches(
        "tokenrhythm/deepseek-v4-pro-0813",
        "https://tokenrhythm.studio/v1/custom",
    )
    assert custom.reasoning_format == ""
    assert policy.supports_native_json_schema_output is False
    # cost_cny is CNY — booking it as USD would corrupt cost rollups.
    assert policy.trust_billed_cost is False
    assert policy.allow_post_terminal_noop_choice is True
    assert policy.post_terminal_metadata_keys == frozenset(
        {
            "billing_pending",
            "cost_cny",
            "reasoning_available",
            "trace_id",
        }
    )
    assert _should_replay_reasoning_content(
        policy=policy,
        model="deepseek-v4-flash",
        caps=None,
        reasoning_rule=official,
    )
    assert _should_replay_reasoning_content(
        policy=policy,
        model="deepseek-v4-flash-0731",
        caps=None,
        reasoning_rule=official,
    )
    assert not _should_replay_reasoning_content(
        policy=policy, model="glm-5", caps=None
    )


def test_deepseek_v4_identity_includes_only_exact_0813_ids() -> None:
    assert "deepseek-v4-pro-0813" in DEEPSEEK_V4_MODEL_IDS
    assert is_deepseek_v4_model_id("deepseek-v4-pro-0813")
    assert is_deepseek_v4_model_id("tokenrhythm/deepseek-v4-pro-0813")
    assert not is_deepseek_v4_model_id("deepseek-v4-pro-0813-preview")
    assert not is_deepseek_v4_model_id("tokenrhythm/deepseek-v4-pro-0813-preview")


def test_native_json_schema_output_stays_enabled_by_default() -> None:
    assert compat_policy_for_kind("openai").supports_native_json_schema_output is True
    assert compat_policy_for_kind("openrouter").supports_native_json_schema_output is True
    assert OpenAICompatPolicy().supports_native_json_schema_output is True
    assert OpenAICompatPolicy().supports_json_object_output is False


def test_deepseek_uses_json_object_fallback_for_output_schemas() -> None:
    policy = compat_policy_for_kind("deepseek")

    assert policy.supports_native_json_schema_output is False
    assert policy.supports_json_object_output is True


def test_deepseek_replay_stays_v4_gated() -> None:
    deepseek = compat_policy_for_kind("deepseek")
    caps = ModelCapabilities(supports_reasoning=True, reasoning_format="deepseek")
    assert _should_replay_reasoning_content(
        policy=deepseek, model="deepseek-v4-pro", caps=caps
    )
    assert _should_replay_reasoning_content(
        policy=deepseek, model="deepseek-v4-pro", caps=None
    )
    # Non-V4 DeepSeek models must NOT replay even with the deepseek format.
    assert not _should_replay_reasoning_content(
        policy=deepseek, model="deepseek-chat", caps=caps
    )


def test_dsml_policy_names_only_exact_packaged_model_ids() -> None:
    expected_by_provider = {
        "deepseek": {
            "deepseek-v4-flash",
            "deepseek-v4-flash-0731",
            "deepseek-v4-pro",
        },
        "tokenrhythm": {
            "deepseek-v4-flash",
            "deepseek-v4-flash-0731",
            "deepseek-v4-pro",
            "tokenrhythm/deepseek-v4-flash",
            "tokenrhythm/deepseek-v4-flash-0731",
            "tokenrhythm/deepseek-v4-pro",
            "deepseek-v4-pro-0813",
            "tokenrhythm/deepseek-v4-pro-0813",
        },
        "openrouter": {
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-pro",
        },
    }

    for provider_kind, expected_models in expected_by_provider.items():
        profile = compat_policy_for_kind(provider_kind).text_tool_profile
        dsml_rules = [
            rule
            for rule in profile.model_rules
            if TEXT_TOOL_DIALECT_DEEPSEEK_DSML in rule.dialects
        ]
        expected_rule_count = 2 if provider_kind == "tokenrhythm" else 1
        assert len(dsml_rules) == expected_rule_count
        actual_models = {
            model
            for rule in dsml_rules
            for model in rule.model_patterns
        }
        assert actual_models == expected_models
        assert not any(
            wildcard in pattern
            for rule in dsml_rules
            for pattern in rule.model_patterns
            for wildcard in "*?["
        )


def test_dsml_policy_accepts_only_the_configured_provider_model_pair() -> None:
    allowed = {
        ("deepseek", "deepseek-v4-flash"),
        ("deepseek", "deepseek-v4-flash-0731"),
        ("deepseek", "deepseek-v4-pro"),
        ("tokenrhythm", "deepseek-v4-flash"),
        ("tokenrhythm", "deepseek-v4-flash-0731"),
        ("tokenrhythm", "deepseek-v4-pro"),
        ("tokenrhythm", "tokenrhythm/deepseek-v4-flash"),
        ("tokenrhythm", "tokenrhythm/deepseek-v4-flash-0731"),
        ("tokenrhythm", "tokenrhythm/deepseek-v4-pro"),
        ("openrouter", "deepseek/deepseek-v4-flash"),
        ("openrouter", "deepseek/deepseek-v4-pro"),
    }

    for provider_kind, model in allowed:
        dialects = compat_policy_for_kind(provider_kind).text_tool_profile.dialects_for_model(
            model
        )
        assert TEXT_TOOL_DIALECT_DEEPSEEK_DSML in dialects


def test_dsml_policy_rejects_near_misses_and_wrong_providers() -> None:
    denied = {
        ("deepseek", "deepseek-v4"),
        ("deepseek", "deepseek-v4-flash-preview"),
        ("deepseek", "vendor/deepseek-v4-flash"),
        ("tokenrhythm", "deepseek/deepseek-v4-flash"),
        ("tokenrhythm", "tokenrhythm/deepseek-v4-flash-preview"),
        ("tokenrhythm", "deepseek-v4-pro-0813"),
        ("tokenrhythm", "tokenrhythm/deepseek-v4-pro-0813-preview"),
        ("openrouter", "deepseek-v4-flash"),
        ("openrouter", "deepseek/deepseek-v4-flash-0731"),
        ("openrouter", "vendor/deepseek-v4-pro"),
        ("openai", "deepseek-v4-flash"),
        ("dashscope", "deepseek-v4-pro"),
        ("no-such-kind", "deepseek/deepseek-v4-pro"),
    }

    for provider_kind, model in denied:
        dialects = compat_policy_for_kind(provider_kind).text_tool_profile.dialects_for_model(
            model
        )
        assert TEXT_TOOL_DIALECT_DEEPSEEK_DSML not in dialects


def test_tokenrhythm_0813_dsml_is_exact_and_endpoint_scoped() -> None:
    profile = compat_policy_for_kind("tokenrhythm").text_tool_profile
    for model in (
        "deepseek-v4-pro-0813",
        "tokenrhythm/deepseek-v4-pro-0813",
    ):
        for base_url in (
            "https://tokenrhythm.studio",
            "https://tokenrhythm.studio/v1",
            "HTTPS://TOKENRHYTHM.STUDIO:443/v1/",
        ):
            assert TEXT_TOOL_DIALECT_DEEPSEEK_DSML in profile.dialects_for_model(
                model,
                base_url,
            )

    for base_url in (
        "http://tokenrhythm.studio/v1",
        "https://api.tokenrhythm.studio/v1",
        "https://tokenrhythm.studio.evil.example/v1",
        "https://customer-proxy.example/v1",
        "https://tokenrhythm.studio/v1/custom",
        "https://tokenrhythm.studio:8443/v1",
        "https://user@tokenrhythm.studio/v1",
        "https://tokenrhythm.studio/v1?tenant=x",
        "https://tokenrhythm.studio/v1#fragment",
        "https://tokenrhythm.studio/v1?",
        "https://tokenrhythm.studio/v1#",
    ):
        assert TEXT_TOOL_DIALECT_DEEPSEEK_DSML not in profile.dialects_for_model(
            "deepseek-v4-pro-0813",
            base_url,
        )


def test_openrouter_replay_follows_capability_format() -> None:
    openrouter = compat_policy_for_kind("openrouter")
    caps_or = ModelCapabilities(supports_reasoning=True, reasoning_format="openrouter")
    caps_ds = ModelCapabilities(supports_reasoning=True, reasoning_format="deepseek")
    assert _should_replay_reasoning_content(
        policy=openrouter, model="deepseek/deepseek-v4-pro", caps=caps_or
    )
    assert not _should_replay_reasoning_content(
        policy=openrouter, model="deepseek/deepseek-v4-pro", caps=caps_ds
    )
    assert not _should_replay_reasoning_content(
        policy=openrouter, model="deepseek/deepseek-v4-pro", caps=None
    )


def test_max_completion_tokens_requires_official_host() -> None:
    openai_policy = compat_policy_for_kind("openai")
    assert _uses_max_completion_tokens(
        openai_policy, "https://api.openai.com/v1", "gpt-5.5"
    )
    # vLLM/self-hosted deployments share the kind but not the host quirk.
    assert not _uses_max_completion_tokens(
        openai_policy, "http://localhost:8000/v1", "gpt-5.5"
    )
    assert not _uses_max_completion_tokens(
        openai_policy, "https://api.openai.com/v1", "gpt-4o"
    )


def test_fixed_sampling_drops_non_default_temperature() -> None:
    moonshot = compat_policy_for_kind("moonshot")
    cfg = ChatConfig(temperature=0.3)
    assert not _should_send_temperature(
        moonshot, "https://api.moonshot.ai/v1", "kimi-k2.5", cfg, None
    )
    assert _should_send_temperature(
        moonshot, "https://api.moonshot.ai/v1", "moonshot-v1-8k", cfg, None
    )
    assert _should_send_temperature(
        moonshot,
        "https://api.moonshot.ai/v1",
        "kimi-k2.5",
        ChatConfig(temperature=1.0),
        None,
    )
