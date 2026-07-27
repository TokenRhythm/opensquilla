"""Contract tests for the OpenAI-compat dialect policy layer.

The policy record is the single source of provider-kind quirks: every
registered openai_compat provider must resolve to an explicit policy, and the
request/stream behaviors that used to fork on ``provider_kind`` must follow
the policy fields.
"""

from __future__ import annotations

from opensquilla.provider.compat_policy import (
    OpenAICompatPolicy,
    compat_policy_for_kind,
    known_policy_kinds,
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


def test_tokenrhythm_never_toggles_thinking_but_replays_v4_reasoning() -> None:
    """TokenRhythm rejects unknown request fields (a DeepSeek ``thinking``
    toggle is an UNKNOWN_FIELD 400), so the policy must never declare toggle
    ids or a default reasoning format; the V4 ids keep only the
    reasoning_content replay requirement."""
    policy = compat_policy_for_kind("tokenrhythm")
    assert policy.thinking_toggle_model_ids == frozenset()
    assert policy.default_reasoning_format == ""
    assert policy.replay_reasoning_format == ""
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
        policy=policy, model="deepseek-v4-flash", caps=None
    )
    assert not _should_replay_reasoning_content(
        policy=policy, model="glm-5", caps=None
    )


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
