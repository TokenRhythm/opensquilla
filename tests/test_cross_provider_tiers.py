"""Cross-provider router tiers (R3) with provider-state safety (R2).

The preview flag ``squilla_router.cross_provider_tiers`` lets a routed tier
execute on its own provider, with credentials from ``[llm_profiles.<id>]``
or the registry env key. Provider-bound continuity state (thinking blocks /
thought signatures) minted by another provider is never replayed to the
tier's provider.
"""

from __future__ import annotations

from types import SimpleNamespace

from opensquilla.engine.selector_override import (
    apply_model_override,
    cross_provider_tier_config,
    resolve_tier_provider_config,
)
from opensquilla.gateway.config import GatewayConfig, LlmProviderProfile
from opensquilla.provider.anthropic import _build_message_payload
from opensquilla.provider.compat_policy import compat_policy_for_kind
from opensquilla.provider.deployment import resolve_provider_deployment
from opensquilla.provider.environment import environment_value
from opensquilla.provider.openai import _build_openai_messages, _build_openai_wire_messages
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolUse,
    Message,
    ModelCapabilities,
)

# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def _config_with_flag(**profiles: LlmProviderProfile) -> GatewayConfig:
    cfg = GatewayConfig()
    cfg.squilla_router.cross_provider_tiers = True
    cfg.llm_profiles = dict(profiles)
    return cfg


def test_profile_credentials_resolve(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _config_with_flag(openai=LlmProviderProfile(api_key="sk-profile"))
    resolved = resolve_tier_provider_config(cfg, "openai", "gpt-5.4-nano")
    assert resolved is not None
    assert resolved.provider == "openai"
    assert resolved.api_key == "sk-profile"
    assert resolved.base_url == "https://api.openai.com/v1"
    assert resolved.replay_provider_state is False


def test_env_fallback_resolves(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    cfg = _config_with_flag()
    resolved = resolve_tier_provider_config(cfg, "deepseek", "deepseek-v4-flash")
    assert resolved is not None
    assert resolved.api_key == "sk-env"
    assert resolved.base_url == "https://api.deepseek.com"


def test_windows_environment_names_resolve_case_insensitively() -> None:
    environment = {"openai_profile_key": "synthetic-windows-env-secret"}
    cfg = _config_with_flag(
        openai=LlmProviderProfile(api_key_env="OPENAI_PROFILE_KEY")
    )

    resolution = resolve_provider_deployment(
        cfg,
        "openai",
        "gpt-test",
        environment_reader=lambda name: environment_value(
            name,
            environment=environment,
            case_insensitive=True,
        ),
    )

    assert resolution.ready is True
    assert resolution.credential_source == "profile_env"
    assert resolution.credential_env == "OPENAI_PROFILE_KEY"


def test_posix_environment_names_remain_case_sensitive() -> None:
    environment = {"openai_profile_key": "synthetic-posix-env-secret"}
    cfg = _config_with_flag(
        openai=LlmProviderProfile(api_key_env="OPENAI_PROFILE_KEY")
    )

    resolution = resolve_provider_deployment(
        cfg,
        "openai",
        "gpt-test",
        environment_reader=lambda name: environment_value(
            name,
            environment=environment,
            case_insensitive=False,
        ),
    )

    assert resolution.ready is False
    assert resolution.reason == "missing_credential"


def test_unresolvable_credentials_return_none(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _config_with_flag()
    assert resolve_tier_provider_config(cfg, "openai", "gpt-5.4-nano") is None
    assert resolve_tier_provider_config(cfg, "no-such-provider", "m") is None


def test_shared_resolution_reports_only_redacted_profile_provenance(monkeypatch) -> None:
    secret = "sk-test-profile-secret-never-render"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-registry-loses")
    cfg = _config_with_flag(
        openai=LlmProviderProfile(
            api_key=secret,
            base_url="https://profile.example/v1",
            proxy="http://proxy.example:8080",
        )
    )

    resolution = resolve_provider_deployment(cfg, "OPENAI", "gpt-test")

    assert resolution.ready is True
    assert resolution.provider_config is not None
    assert resolution.provider_config.api_key == secret
    assert resolution.provider_config.base_url == "https://profile.example/v1"
    assert resolution.provider_config.proxy == "http://proxy.example:8080"
    assert resolution.provider_config.replay_provider_state is False
    assert resolution.credential_source == "profile"
    assert resolution.endpoint_source == "profile"
    assert resolution.proxy_source == "profile"
    assert secret not in repr(resolution)


def test_profile_env_precedes_registry_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_PROFILE_KEY", "sk-profile-env")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-registry-env")
    cfg = _config_with_flag(
        openai=LlmProviderProfile(api_key_env="OPENAI_PROFILE_KEY")
    )

    resolution = resolve_provider_deployment(cfg, "openai", "gpt-test")

    assert resolution.ready is True
    assert resolution.provider_config is not None
    assert resolution.provider_config.api_key == "sk-profile-env"
    assert resolution.credential_source == "profile_env"
    assert resolution.credential_env == "OPENAI_PROFILE_KEY"


def test_profile_custom_origin_does_not_implicitly_use_registry_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-registry-must-not-cross-origin")
    cfg = _config_with_flag(
        openai=LlmProviderProfile(base_url="https://foreign.example/v1")
    )

    resolution = resolve_provider_deployment(cfg, "openai", "gpt-test")

    assert resolution.ready is False
    assert resolution.reason == "missing_credential"
    assert resolution.credential_source == "none"
    assert resolution.endpoint_source == "profile"
    assert resolution.provider_config is not None
    assert resolution.provider_config.api_key == ""


def test_profile_same_origin_can_use_registry_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-registry-same-origin")
    cfg = _config_with_flag(
        openai=LlmProviderProfile(base_url="https://api.openai.com:443/compatible/v1")
    )

    resolution = resolve_provider_deployment(cfg, "openai", "gpt-test")

    assert resolution.ready is True
    assert resolution.credential_source == "registry_env"
    assert resolution.endpoint_source == "profile"
    assert resolution.provider_config is not None
    assert resolution.provider_config.api_key == "sk-registry-same-origin"


def test_inherited_primary_credentials_remain_authoritative_on_custom_origin(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-registry-not-selected")
    inherited = ProviderConfig(
        provider="openai",
        model="primary-model",
        api_key="sk-inherited-primary",
        base_url="https://primary-proxy.example/v1",
    )

    resolution = resolve_provider_deployment(
        _config_with_flag(),
        "openai",
        "next-primary-model",
        inherited_provider_config=inherited,
    )

    assert resolution.ready is True
    assert resolution.credential_source == "inherited"
    assert resolution.endpoint_source == "inherited"
    assert resolution.provider_config is not None
    assert resolution.provider_config.api_key == "sk-inherited-primary"
    assert resolution.provider_config.base_url == "https://primary-proxy.example/v1"


def test_registry_env_follows_operator_owned_endpoint(monkeypatch) -> None:
    """A provider spec without a default base URL (azure-style) binds its
    registry env key to the operator-supplied endpoint, so the env fallback
    must follow the profile base_url instead of being origin-vetoed."""
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-azure-env")
    cfg = _config_with_flag(
        azure=LlmProviderProfile(base_url="https://acct.azure-endpoint.example/v1")
    )

    resolution = resolve_provider_deployment(cfg, "azure", "gpt-test")

    assert resolution.ready is True
    assert resolution.credential_source == "registry_env"
    assert resolution.credential_env == "AZURE_OPENAI_API_KEY"
    assert resolution.endpoint_source == "profile"
    assert resolution.provider_config is not None
    assert resolution.provider_config.api_key == "sk-azure-env"
    assert (
        resolution.provider_config.base_url
        == "https://acct.azure-endpoint.example/v1"
    )


def test_operator_owned_endpoint_still_requires_base_url(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-azure-env")
    cfg = _config_with_flag()

    resolution = resolve_provider_deployment(cfg, "azure", "gpt-test")

    assert resolution.ready is False
    assert resolution.reason == "missing_base_url"


def test_member_endpoint_override_cannot_reuse_inherited_primary_credential() -> None:
    inherited = ProviderConfig(
        provider="openai",
        model="primary-model",
        api_key="synthetic-inherited-secret",
        base_url="https://api.openai.com/v1",
    )

    resolution = resolve_provider_deployment(
        _config_with_flag(),
        "openai",
        "next-primary-model",
        inherited_provider_config=inherited,
        overrides=SimpleNamespace(base_url="https://foreign.example/v1"),
    )

    assert resolution.ready is False
    assert resolution.reason == "credential_endpoint_mismatch"
    assert resolution.endpoint_source == "member"
    assert resolution.provider_config is not None
    assert resolution.provider_config.api_key == ""


def test_member_endpoint_override_cannot_reuse_profile_credential() -> None:
    cfg = _config_with_flag(
        openai=LlmProviderProfile(
            api_key="synthetic-profile-secret",
            base_url="https://api.openai.com/v1",
        )
    )

    resolution = resolve_provider_deployment(
        cfg,
        "openai",
        "gpt-test",
        overrides=SimpleNamespace(base_url="https://foreign.example/v1"),
    )

    assert resolution.ready is False
    assert resolution.reason == "credential_endpoint_mismatch"
    assert resolution.endpoint_source == "member"
    assert resolution.provider_config is not None
    assert resolution.provider_config.api_key == ""


def test_member_endpoint_override_accepts_explicit_member_credential() -> None:
    cfg = _config_with_flag(
        openai=LlmProviderProfile(api_key="synthetic-profile-secret")
    )

    resolution = resolve_provider_deployment(
        cfg,
        "openai",
        "gpt-test",
        overrides=SimpleNamespace(
            api_key="synthetic-member-secret",
            base_url="https://foreign.example/v1",
        ),
    )

    assert resolution.ready is True
    assert resolution.credential_source == "member"
    assert resolution.endpoint_source == "member"
    assert resolution.provider_config is not None
    assert resolution.provider_config.api_key == "synthetic-member-secret"


# ---------------------------------------------------------------------------
# Execution gate
# ---------------------------------------------------------------------------


def test_gate_route_mode_flag_off_leaves_routed_choice_unblocked(monkeypatch) -> None:
    """Default ``tier_provider_mismatch='route'``: with the execution flag
    off, the documented contract still runs the tier's model id on the
    active provider, so the gate must not mark the routed choice blocked."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    cfg = GatewayConfig()  # flag off, mismatch policy defaults to 'route'
    metadata = {"routing_applied": True, "routed_provider": "openai"}
    assert (
        cross_provider_tier_config(cfg, metadata, "gpt-5.4-nano", active_provider_id="openrouter")
        is None
    )
    assert "routed_provider_blocked" not in metadata
    assert "routed_provider_fallback_reason" not in metadata


def test_gate_veto_mode_flag_off_blocks_routed_choice(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    cfg = GatewayConfig()  # flag off
    cfg.squilla_router.tier_provider_mismatch = "veto"
    metadata = {"routing_applied": True, "routed_provider": "openai"}
    assert (
        cross_provider_tier_config(cfg, metadata, "gpt-5.4-nano", active_provider_id="openrouter")
        is None
    )
    assert metadata["routed_provider_blocked"] == "cross_provider_tiers_disabled"
    assert metadata["routed_provider_fallback_reason"] == (
        "cross_provider_tiers_disabled"
    )


def test_flag_off_route_mode_applies_routed_model_to_primary(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    cfg = GatewayConfig()
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openrouter",
                "deepseek/deepseek-v4-pro",
                api_key="or-key",
            )
        )
    )
    metadata: dict[str, object] = {
        "routing_applied": True,
        "routed_provider": "openai",
        "routed_model": "gpt-foreign",
    }

    tier_config = cross_provider_tier_config(
        cfg,
        metadata,
        "gpt-foreign",
        active_provider_id="openrouter",
    )
    provider = apply_model_override(
        selector,
        "gpt-foreign",
        turn_metadata=metadata,
        realign_routed_model=False,
        tier_provider_config=tier_config,
    )

    assert provider is not None
    assert selector.current_config.provider == "openrouter"
    assert selector.current_config.model == "gpt-foreign"
    assert metadata["executed_provider"] == "openrouter"
    assert metadata["executed_model"] == "gpt-foreign"


def test_flag_off_veto_mode_never_applies_foreign_model_to_primary(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    cfg = GatewayConfig()
    cfg.squilla_router.tier_provider_mismatch = "veto"
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openrouter",
                "deepseek/deepseek-v4-pro",
                api_key="or-key",
            )
        )
    )
    metadata: dict[str, object] = {
        "routing_applied": True,
        "routed_provider": "openai",
        "routed_model": "gpt-foreign",
    }

    tier_config = cross_provider_tier_config(
        cfg,
        metadata,
        "gpt-foreign",
        active_provider_id="openrouter",
    )
    provider = apply_model_override(
        selector,
        "gpt-foreign",
        turn_metadata=metadata,
        realign_routed_model=False,
        tier_provider_config=tier_config,
    )

    assert provider is not None
    assert selector.current_config.provider == "openrouter"
    assert selector.current_config.model == "deepseek/deepseek-v4-pro"
    assert metadata["executed_provider"] == "openrouter"
    assert metadata["executed_model"] == "deepseek/deepseek-v4-pro"


def test_gate_executes_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    cfg = _config_with_flag()
    metadata = {"routing_applied": True, "routed_provider": "openai"}
    resolved = cross_provider_tier_config(
        cfg, metadata, "gpt-5.4-nano", active_provider_id="openrouter"
    )
    assert resolved is not None
    assert resolved.provider == "openai"


def test_gate_skips_same_provider(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    cfg = _config_with_flag()
    metadata = {"routing_applied": True, "routed_provider": "openrouter"}
    assert (
        cross_provider_tier_config(cfg, metadata, "m", active_provider_id="openrouter") is None
    )


def test_gate_blocked_by_continuity_diagnostic(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    cfg = _config_with_flag()
    metadata = {
        "routing_applied": True,
        "routed_provider": "openai",
        "provider_state_continuity": {"decision": "discard_provider_state"},
    }
    assert (
        cross_provider_tier_config(cfg, metadata, "m", active_provider_id="openrouter") is None
    )
    assert metadata["routed_provider_blocked"] == "provider_state_continuity"


# ---------------------------------------------------------------------------
# Selector application
# ---------------------------------------------------------------------------


def test_override_provider_config_switches_chain_head() -> None:
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("openrouter", "deepseek/deepseek-v4-pro", api_key="or-key")
        )
    )
    tier_cfg = ProviderConfig(
        "openai",
        "gpt-5.4-nano",
        api_key="oa-key",
        base_url="https://api.openai.com/v1",
        replay_provider_state=False,
    )
    selector.override_provider_config(tier_cfg)
    assert selector.active_provider_id == "openai"
    assert selector.current_config.api_key == "oa-key"
    # The previous primary remains reachable as a fallback.
    assert selector.has_fallback()
    assert selector._chain[1].provider == "openrouter"


def test_apply_model_override_uses_tier_config() -> None:
    selector = ModelSelector(
        SelectorConfig(primary=ProviderConfig("openrouter", "m", api_key="k"))
    )
    metadata: dict[str, object] = {"routing_applied": True}
    tier_cfg = ProviderConfig(
        "openai", "gpt-5.4-nano", api_key="oa", base_url="https://api.openai.com/v1"
    )
    provider = apply_model_override(
        selector,
        "gpt-5.4-nano",
        turn_metadata=metadata,
        realign_routed_model=False,
        tier_provider_config=tier_cfg,
    )
    assert provider is not None
    assert metadata["routed_provider_applied"] == "openai"
    assert metadata["executed_provider"] == "openai"
    assert metadata["executed_model"] == "gpt-5.4-nano"
    assert selector.active_provider_id == "openai"
    assert all(not cfg.replay_provider_state for cfg in selector.remaining_chain())


def test_return_to_primary_after_foreign_native_state_disables_replay() -> None:
    cfg = _config_with_flag()
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openrouter",
                "primary-default",
                api_key="or-key",
                replay_provider_state=True,
            )
        )
    )
    metadata: dict[str, object] = {
        "routing_applied": True,
        "routed_provider": "openrouter",
        "routed_model": "primary-next",
        "provider_state_continuity": {
            "decision": "use_portable_fallback",
            "active_state_provider": "openai",
        },
    }

    tier_config = cross_provider_tier_config(
        cfg,
        metadata,
        "primary-next",
        active_provider_id="openrouter",
    )
    provider = apply_model_override(
        selector,
        "primary-next",
        turn_metadata=metadata,
        realign_routed_model=False,
        tier_provider_config=tier_config,
    )

    assert provider is not None
    assert selector.current_config.provider == "openrouter"
    assert selector.current_config.model == "primary-next"
    assert selector.current_config.replay_provider_state is False
    assert metadata["provider_state_replay_disabled"] == "provider_transition"


def test_explicit_model_after_cross_provider_route_restores_primary() -> None:
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openrouter",
                "primary-default",
                api_key="or-key",
                replay_provider_state=False,
            )
        )
    )
    metadata: dict[str, object] = {
        "routing_applied": True,
        "routed_provider": "openai",
        "routed_model": "gpt-routed",
    }
    tier_cfg = ProviderConfig(
        "openai",
        "gpt-routed",
        api_key="oa-key",
        base_url="https://api.openai.com/v1",
        replay_provider_state=False,
    )
    apply_model_override(
        selector,
        "gpt-routed",
        turn_metadata=metadata,
        realign_routed_model=False,
        tier_provider_config=tier_cfg,
    )

    provider = apply_model_override(
        selector,
        "primary-explicit",
        turn_metadata=metadata,
        realign_routed_model=True,
    )

    assert provider is not None
    assert selector.current_config.provider == "openrouter"
    assert selector.current_config.model == "primary-explicit"
    assert selector.current_config.replay_provider_state is False
    assert metadata["executed_provider"] == "openrouter"
    assert metadata["executed_model"] == "primary-explicit"
    assert metadata["routed_provider_fallback_reason"] == "explicit_model_override"


def test_unresolved_cross_provider_never_applies_foreign_model_to_primary(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _config_with_flag()
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openrouter",
                "deepseek/deepseek-v4-pro",
                api_key="or-key",
            )
        )
    )
    metadata: dict = {
        "routing_applied": True,
        "routed_provider": "openai",
    }
    tier_config = cross_provider_tier_config(
        cfg,
        metadata,
        "gpt-foreign",
        active_provider_id="openrouter",
    )

    assert tier_config is None
    provider = apply_model_override(
        selector,
        "gpt-foreign",
        turn_metadata=metadata,
        realign_routed_model=False,
        tier_provider_config=tier_config,
    )

    assert provider is not None
    assert selector.current_config.provider == "openrouter"
    assert selector.current_config.model == "deepseek/deepseek-v4-pro"
    assert metadata["routed_provider_fallback_reason"] == "missing_credential"
    assert metadata["routed_provider_fallback_model"] == "deepseek/deepseek-v4-pro"
    assert metadata["executed_provider"] == "openrouter"
    assert metadata["executed_model"] == "deepseek/deepseek-v4-pro"


def test_explicit_primary_model_can_override_a_blocked_routed_choice() -> None:
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                "openrouter",
                "deepseek/deepseek-v4-pro",
                api_key="or-key",
            )
        )
    )
    metadata = {
        "routing_applied": True,
        "routed_provider": "openai",
        "routed_model": "gpt-foreign",
        "routed_provider_blocked": "missing_credential",
    }

    provider = apply_model_override(
        selector,
        "qwen/qwen3.7-plus",
        turn_metadata=metadata,
        realign_routed_model=True,
    )

    assert provider is not None
    assert selector.current_config.provider == "openrouter"
    assert selector.current_config.model == "qwen/qwen3.7-plus"
    assert metadata["routed_model"] == "qwen/qwen3.7-plus"
    assert metadata["executed_provider"] == "openrouter"
    assert metadata["executed_model"] == "qwen/qwen3.7-plus"


# ---------------------------------------------------------------------------
# R2: provider-bound state never crosses providers
# ---------------------------------------------------------------------------

_SIGNED_ASSISTANT = Message(
    role="assistant",
    content=[
        ContentBlockThinking(thinking="chain of thought", signature="sig-anthropic"),
        ContentBlockText(text="doing it"),
        ContentBlockToolUse(id="call_1", name="search", input={"q": "x"}),
    ],
)


def test_anthropic_drops_foreign_thinking_blocks() -> None:
    replayed = _build_message_payload(_SIGNED_ASSISTANT, model="claude-x")
    assert any(part["type"] == "thinking" for part in replayed["content"])

    stripped = _build_message_payload(
        _SIGNED_ASSISTANT, model="claude-x", replay_provider_state=False
    )
    assert not any(part["type"] == "thinking" for part in stripped["content"])
    # Text and tool_use survive.
    assert any(part["type"] == "tool_use" for part in stripped["content"])


def test_openai_skips_foreign_thought_signature() -> None:
    (replayed,) = _build_openai_messages(_SIGNED_ASSISTANT)
    assert (
        replayed["tool_calls"][0]["extra_content"]["google"]["thought_signature"]
        == "sig-anthropic"
    )

    (stripped,) = _build_openai_messages(_SIGNED_ASSISTANT, replay_provider_state=False)
    assert "extra_content" not in stripped["tool_calls"][0]


def test_openai_compat_a_to_b_drops_foreign_reasoning_content() -> None:
    messages = [
        Message(
            role="assistant",
            content="portable answer",
            reasoning_content="provider-a-private-reasoning",
        )
    ]
    config = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            reasoning_format="openrouter",
        ),
    )
    policy = compat_policy_for_kind("openrouter")

    replayed = _build_openai_wire_messages(
        messages,
        config,
        policy=policy,
        provider_kind="openrouter",
        model="deepseek/deepseek-v4-pro",
        replay_provider_state=True,
        reasoning_echo_turns=None,
    )
    stripped = _build_openai_wire_messages(
        messages,
        config,
        policy=policy,
        provider_kind="openrouter",
        model="deepseek/deepseek-v4-pro",
        replay_provider_state=False,
        reasoning_echo_turns=None,
    )

    assert replayed[0]["reasoning_content"] == "provider-a-private-reasoning"
    assert "reasoning_content" not in stripped[0]
