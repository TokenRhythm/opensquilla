from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.compaction_target import (
    GatewayConsumerBudget,
    build_gateway_consumer_admission,
    resolve_gateway_compaction_target,
    resolve_gateway_consumer_budget,
)
from opensquilla.gateway.config import GatewayConfig, LlmProviderProfile
from opensquilla.provider.ollama import OllamaProvider
from opensquilla.provider.protocol import provider_connection_config
from opensquilla.provider.selector import ProviderConfig
from opensquilla.session.compaction import build_compaction_config_from_provider


class _ReadOnlySelector:
    def __init__(self, current_config: ProviderConfig) -> None:
        self.current_config = current_config
        self.clone_calls = 0

    def clone(self):
        self.clone_calls += 1
        raise AssertionError("complete deployment resolution must not clone or mutate the selector")

    def remaining_chain(self) -> list[ProviderConfig]:
        return [self.current_config]


def _ctx(config: GatewayConfig, current: ProviderConfig) -> SimpleNamespace:
    return SimpleNamespace(
        config=config,
        provider_selector=_ReadOnlySelector(current),
    )


def test_explicit_compaction_pair_uses_its_complete_profile() -> None:
    config = GatewayConfig()
    config.compaction.provider = "openai"
    config.compaction.model = "gpt-explicit"
    config.llm_profiles["openai"] = LlmProviderProfile(
        api_key="explicit-profile-secret",
        base_url="https://api.openai.com/v1",
    )
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    ctx = _ctx(config, current)
    session = SimpleNamespace(
        session_key="agent:main:webchat:manual",
        provider_override="ollama",
        model_override="qwen-session",
    )

    target = resolve_gateway_compaction_target(ctx, session)

    assert target.provider_id == "openai"
    assert target.model == "gpt-explicit"
    assert target.source == "explicit_compaction"
    assert target.plan is not None
    assert target.plan.primary.provider_id == "openai"
    assert target.plan.primary.context_window_tokens > 0
    assert target.plan.primary.provider_request_max_chars > 0
    connection = provider_connection_config(target.provider)
    assert connection.api_key == "explicit-profile-secret"
    assert connection.base_url == "https://api.openai.com/v1"
    assert "explicit-profile-secret" not in repr(target)
    assert "explicit-profile-secret" not in repr(target.plan)
    assert ctx.provider_selector.clone_calls == 0
    assert current.model == "qwen-current"
    assert current.replay_provider_state is True


def test_model_only_compaction_stays_on_current_provider() -> None:
    config = GatewayConfig()
    config.compaction.model = "gpt-summary"
    current = ProviderConfig(
        provider="openai",
        model="gpt-turn",
        api_key="current-provider-secret",
        base_url="https://api.openai.com/v1",
    )
    ctx = _ctx(config, current)
    session = SimpleNamespace(session_key="agent:main:webchat:model-only")

    target = resolve_gateway_compaction_target(ctx, session)

    assert target.provider_id == "openai"
    assert target.model == "gpt-summary"
    assert target.source == "selector_current"
    assert target.plan is not None
    assert provider_connection_config(target.provider).api_key == "current-provider-secret"
    assert ctx.provider_selector.clone_calls == 0
    assert current.model == "gpt-turn"
    assert current.replay_provider_state is True


def test_model_only_compaction_ignores_stale_session_provider_provenance() -> None:
    config = GatewayConfig()
    config.compaction.model = "gpt-summary"
    current = ProviderConfig(
        provider="openai",
        model="gpt-turn",
        api_key="current-provider-secret",
        base_url="https://api.openai.com/v1",
    )
    ctx = _ctx(config, current)
    session = SimpleNamespace(
        session_key="agent:main:webchat:model-only-stale",
        provider_override=None,
        model_provider="anthropic",
        model_override=None,
        model="claude-stale",
    )

    target = resolve_gateway_compaction_target(ctx, session)

    assert target.provider_id == "openai"
    assert target.model == "gpt-summary"
    assert target.source == "selector_current"
    assert target.plan is not None
    assert provider_connection_config(target.provider).api_key == "current-provider-secret"
    assert ctx.provider_selector.clone_calls == 0


def test_manual_consumer_budget_uses_stable_base_not_last_routed_model() -> None:
    config = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "gpt-stable",
            "api_key": "dummy-key",
            "base_url": "https://api.openai.com/v1",
            "context_window_tokens": 4096,
            "max_tokens": 512,
        },
        context_budget_tokens=100_000,
    )
    current = ProviderConfig(
        provider="openai",
        model="gpt-stable",
        api_key="dummy-key",
        base_url="https://api.openai.com/v1",
    )
    budget = resolve_gateway_consumer_budget(
        _ctx(config, current),
        SimpleNamespace(
            session_key="agent:main:webchat:stable-budget",
            model=None,
            model_provider="anthropic",
            model_override="claude-routed-small",
            provider_override=None,
        ),
    )

    assert budget.provider_id == "openai"
    assert budget.model == "gpt-stable"
    assert budget.context_window_tokens == 4096
    assert budget.max_output_tokens == 512
    assert 0 < budget.provider_request_max_chars
    assert budget.next_request_reserve_tokens >= 1024
    assert budget.next_request_reserve_chars == (
        budget.next_request_reserve_tokens * 4
    )


def test_manual_consumer_admission_uses_exact_adapter_projection() -> None:
    config = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "gpt-stable",
            "api_key": "dummy-key",
            "base_url": "https://api.openai.com/v1",
            "context_window_tokens": 4096,
            "max_tokens": 512,
        },
        context_budget_tokens=100_000,
    )
    budget = resolve_gateway_consumer_budget(
        _ctx(
            config,
            ProviderConfig(
                provider="openai",
                model="gpt-stable",
                api_key="dummy-key",
                base_url="https://api.openai.com/v1",
            ),
        ),
        SimpleNamespace(session_key="agent:main:webchat:exact-admission"),
    )
    admission, fingerprint = build_gateway_consumer_admission(budget)

    assert admission("short portable checkpoint", []) is True
    assert admission("x" * 100_000, []) is False
    assert len(fingerprint) == 64


def test_manual_consumer_admission_reserves_the_next_authoritative_envelope() -> None:
    config = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "gpt-stable",
            "api_key": "dummy-key",
            "base_url": "https://api.openai.com/v1",
            "context_window_tokens": 4096,
            "max_tokens": 512,
        },
    )
    budget = resolve_gateway_consumer_budget(
        _ctx(
            config,
            ProviderConfig(
                provider="openai",
                model="gpt-stable",
                api_key="dummy-key",
                base_url="https://api.openai.com/v1",
            ),
        ),
        SimpleNamespace(session_key="agent:main:webchat:reserved-envelope"),
    )
    admission, _fingerprint = build_gateway_consumer_admission(budget)

    # This payload is below the adapter's raw cap, but durable history must
    # leave room for canonical system/tools plus the next user/media turn.
    assert budget.next_request_reserve_tokens >= 1024
    assert admission("x" * 8_000, []) is False


def test_manual_consumer_admission_fails_closed_without_projector() -> None:
    admission, _fingerprint = build_gateway_consumer_admission(
        GatewayConsumerBudget(
            provider=object(),
            provider_id="extension",
            model="unknown",
            context_window_tokens=4096,
            max_output_tokens=512,
            provider_request_max_chars=8192,
        )
    )

    assert admission("checkpoint", []) is False


def test_unavailable_explicit_target_falls_through_to_current_deployment() -> None:
    config = GatewayConfig()
    config.compaction.provider = "provider-that-does-not-exist"
    config.compaction.model = "summary-model"
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    ctx = _ctx(config, current)

    target = resolve_gateway_compaction_target(
        ctx,
        SimpleNamespace(session_key="agent:main:webchat:explicit-unavailable"),
    )

    assert target.provider_id == "ollama"
    assert target.model == "qwen-current"
    assert target.source == "selector_current"
    assert target.plan is not None
    assert ctx.provider_selector.clone_calls == 0


def test_manual_target_freezes_authorized_fallback_chain() -> None:
    config = GatewayConfig()
    current = ProviderConfig(
        provider="openai",
        model="qwen-current",
        api_key="primary-secret",
        base_url="https://api.openai.com/v1",
    )
    fallback = ProviderConfig(
        provider="openai",
        model="qwen-fallback",
        api_key="fallback-secret",
        base_url="https://api.openai.com/v1",
    )

    class _ChainSelector(_ReadOnlySelector):
        def remaining_chain(self) -> list[ProviderConfig]:
            return [current, fallback]

    target = resolve_gateway_compaction_target(
        SimpleNamespace(
            config=config,
            provider_selector=_ChainSelector(current),
        ),
        SimpleNamespace(session_key="agent:main:webchat:manual-chain"),
    )

    assert target.plan is not None
    assert [
        (candidate.provider_id, candidate.model, candidate.source)
        for candidate in target.plan.candidates
    ] == [
        ("openai", "qwen-current", "selector_current"),
        ("openai", "qwen-fallback", "selector_fallback"),
    ]
    assert [
        provider_connection_config(candidate.provider).api_key
        for candidate in target.plan.candidates
    ] == ["primary-secret", "fallback-secret"]


def test_manual_target_preserves_credential_distinct_same_model_fallback() -> None:
    config = GatewayConfig()
    current = ProviderConfig(
        provider="openai",
        model="same-model",
        api_key="primary-secret",
        base_url="https://api.openai.com/v1",
    )
    fallback = ProviderConfig(
        provider="openai",
        model="same-model",
        api_key="fallback-secret",
        base_url="https://api.openai.com/v1",
    )

    class _CredentialChainSelector(_ReadOnlySelector):
        def remaining_chain(self) -> list[ProviderConfig]:
            return [current, fallback]

    target = resolve_gateway_compaction_target(
        SimpleNamespace(
            config=config,
            provider_selector=_CredentialChainSelector(current),
        ),
        SimpleNamespace(session_key="agent:main:webchat:credential-chain"),
    )

    assert target.plan is not None
    assert [
        provider_connection_config(candidate.provider).api_key
        for candidate in target.plan.candidates
    ] == ["primary-secret", "fallback-secret"]
    assert (
        target.plan.candidates[0].deployment_fingerprint
        != target.plan.candidates[1].deployment_fingerprint
    )
    rendered = repr(target.plan)
    assert "primary-secret" not in rendered
    assert "fallback-secret" not in rendered


def test_unavailable_session_target_falls_through_to_current_deployment() -> None:
    config = GatewayConfig()
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    ctx = _ctx(config, current)
    session = SimpleNamespace(
        session_key="agent:main:webchat:session-unavailable",
        provider_override="provider-that-does-not-exist",
        model_provider=None,
        model_override="stale-model",
        model=None,
    )

    target = resolve_gateway_compaction_target(ctx, session)

    assert target.provider_id == "ollama"
    assert target.model == "qwen-current"
    assert target.source == "selector_current"
    assert target.plan is not None
    assert ctx.provider_selector.clone_calls == 0


def test_unavailable_current_target_uses_authorized_selector_fallback() -> None:
    config = GatewayConfig()
    current = ProviderConfig(
        provider="provider-that-does-not-exist",
        model="unavailable-current",
    )
    fallback = OllamaProvider(
        model="qwen-fallback",
        base_url="http://127.0.0.1:11434",
    )

    class _FallbackClone:
        def resolve(self) -> OllamaProvider:
            return fallback

    class _FallbackSelector:
        current_config = current
        clone_calls = 0

        def clone(self) -> _FallbackClone:
            self.clone_calls += 1
            return _FallbackClone()

    selector = _FallbackSelector()
    ctx = SimpleNamespace(config=config, provider_selector=selector)

    target = resolve_gateway_compaction_target(
        ctx,
        SimpleNamespace(session_key="agent:main:webchat:selector-fallback"),
    )

    assert target.provider is fallback
    assert target.provider_id == "ollama"
    assert target.model == "qwen-fallback"
    assert target.source == "selected_provider_compat"
    assert target.plan is not None
    assert selector.clone_calls == 1


def test_named_session_auth_profile_fails_closed_when_exact_profile_is_missing() -> None:
    config = GatewayConfig()
    config.llm_profiles["openai"] = LlmProviderProfile(
        api_key="session-provider-secret",
        base_url="https://api.openai.com/v1",
    )
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    ctx = _ctx(config, current)
    session = SimpleNamespace(
        session_key="agent:main:webchat:override",
        provider_override="openai",
        model_provider=None,
        model_override="gpt-session",
        model=None,
        auth_profile_override="profile-a",
    )

    target = resolve_gateway_compaction_target(ctx, session)

    assert target.provider_id == "openai"
    assert target.model == "gpt-session"
    assert target.source == "auth_profile_unresolved"
    assert target.blocked_reason == "named_auth_profile_not_found"
    assert target.provider is None
    assert target.plan is None
    assert session.provider_override == "openai"
    assert session.auth_profile_override == "profile-a"
    assert ctx.provider_selector.clone_calls == 0

    consumer = resolve_gateway_consumer_budget(ctx, session)
    admission, _fingerprint = build_gateway_consumer_admission(consumer)

    assert consumer.provider is None
    assert consumer.source == "auth_profile_unresolved"
    assert consumer.blocked_reason == "named_auth_profile_not_found"
    assert admission("deterministic checkpoint", []) is False


def test_named_session_auth_profile_binds_complete_manual_deployment() -> None:
    config = GatewayConfig()
    config.llm_profiles["openai"] = LlmProviderProfile(
        api_key="provider-default-secret",
        base_url="https://default.invalid/v1",
    )
    config.llm_profiles["openai:work"] = LlmProviderProfile(
        model="gpt-profile-default",
        api_key="named-profile-secret",
        base_url="https://api.openai.com/v1",
    )
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    ctx = _ctx(config, current)
    session = SimpleNamespace(
        session_key="agent:main:webchat:named-profile",
        provider_override="openai",
        model_provider=None,
        model_override="gpt-session",
        model=None,
        auth_profile_override="openai:work",
    )

    target = resolve_gateway_compaction_target(ctx, session)
    consumer = resolve_gateway_consumer_budget(ctx, session)

    assert target.blocked_reason == ""
    assert target.source == "session_auth_profile"
    assert target.provider_id == "openai"
    assert target.model == "gpt-session"
    assert target.plan is not None
    assert (
        provider_connection_config(target.provider).api_key
        == "named-profile-secret"
    )
    assert (
        provider_connection_config(target.provider).base_url
        == "https://api.openai.com/v1"
    )
    assert "named-profile-secret" not in repr(target)
    assert consumer.blocked_reason == ""
    assert consumer.source == "session_auth_profile"
    assert consumer.provider_id == "openai"
    assert consumer.model == "gpt-session"
    assert (
        provider_connection_config(consumer.provider).api_key
        == "named-profile-secret"
    )
    assert consumer.deployment_fingerprint


def test_named_profile_consumer_prefers_recorded_physical_pair_over_base() -> None:
    config = GatewayConfig()
    config.llm_profiles["openai:work"] = LlmProviderProfile(
        api_key="named-profile-secret",
        base_url="https://api.openai.com/v1",
    )
    current = ProviderConfig(
        provider="anthropic",
        model="claude-base",
        api_key="base-provider-secret",
        base_url="https://api.anthropic.com",
    )
    session = SimpleNamespace(
        session_key="agent:main:webchat:recorded-named-profile",
        provider_override=None,
        model_provider="openai",
        model_override="gpt-session",
        model=None,
        auth_profile_override="openai:work",
    )

    target = resolve_gateway_compaction_target(_ctx(config, current), session)
    consumer = resolve_gateway_consumer_budget(_ctx(config, current), session)

    assert target.blocked_reason == ""
    assert target.provider_id == "openai"
    assert target.model == "gpt-session"
    assert consumer.blocked_reason == ""
    assert consumer.source == "session_auth_profile"
    assert consumer.provider_id == target.provider_id
    assert consumer.model == target.model
    assert (
        provider_connection_config(consumer.provider).api_key
        == "named-profile-secret"
    )


@pytest.mark.parametrize(
    ("profile_id", "provider_override"),
    [
        ("work", "openai"),
        ("openai:work", None),
    ],
)
def test_named_profile_provider_boundary_ignores_cross_provider_provenance(
    profile_id: str,
    provider_override: str | None,
) -> None:
    config = GatewayConfig()
    config.llm_profiles[profile_id] = LlmProviderProfile(
        api_key="named-openai-secret",
        base_url="https://api.openai.com/v1",
    )
    current = ProviderConfig(
        provider="anthropic",
        model="claude-current",
        api_key="current-anthropic-secret",
        base_url="https://api.anthropic.com",
    )
    session = SimpleNamespace(
        session_key="agent:main:webchat:named-boundary",
        provider_override=provider_override,
        model_provider="anthropic",
        model_override="claude-previous",
        model="gpt-intended",
        auth_profile_override=profile_id,
    )

    target = resolve_gateway_compaction_target(_ctx(config, current), session)
    consumer = resolve_gateway_consumer_budget(_ctx(config, current), session)

    assert target.blocked_reason == ""
    assert target.provider_id == "openai"
    assert target.model == "gpt-intended"
    assert consumer.blocked_reason == ""
    assert consumer.provider_id == "openai"
    assert consumer.model == "gpt-intended"
    assert (
        provider_connection_config(target.provider).api_key
        == "named-openai-secret"
    )
    assert (
        provider_connection_config(consumer.provider).api_key
        == "named-openai-secret"
    )


def test_qualified_named_profile_rejects_conflicting_session_provider() -> None:
    config = GatewayConfig()
    config.llm_profiles["openai:work"] = LlmProviderProfile(
        api_key="named-openai-secret",
        base_url="https://api.openai.com/v1",
    )
    current = ProviderConfig(
        provider="anthropic",
        model="claude-current",
        api_key="current-anthropic-secret",
        base_url="https://api.anthropic.com",
    )
    session = SimpleNamespace(
        session_key="agent:main:webchat:named-mismatch",
        provider_override="anthropic",
        model_provider="anthropic",
        model_override="claude-previous",
        model="claude-intended",
        auth_profile_override="openai:work",
    )

    target = resolve_gateway_compaction_target(_ctx(config, current), session)
    consumer = resolve_gateway_consumer_budget(_ctx(config, current), session)

    assert target.provider is None
    assert target.blocked_reason == "named_auth_profile_provider_mismatch"
    assert consumer.provider is None
    assert consumer.blocked_reason == "named_auth_profile_provider_mismatch"


def test_bare_named_profile_without_override_stays_on_stable_base_provider() -> None:
    config = GatewayConfig()
    config.llm_profiles["work"] = LlmProviderProfile(
        api_key="named-anthropic-secret",
        base_url="https://api.anthropic.com",
    )
    current = ProviderConfig(
        provider="anthropic",
        model="claude-base",
        api_key="base-anthropic-secret",
        base_url="https://api.anthropic.com",
    )
    session = SimpleNamespace(
        session_key="agent:main:webchat:bare-profile-base-boundary",
        provider_override=None,
        model_provider="openai",
        model_override="gpt-previous",
        model="claude-intended",
        auth_profile_override="work",
    )

    target = resolve_gateway_compaction_target(_ctx(config, current), session)
    consumer = resolve_gateway_consumer_budget(_ctx(config, current), session)

    assert target.blocked_reason == ""
    assert target.provider_id == "anthropic"
    assert target.model == "claude-intended"
    assert consumer.blocked_reason == ""
    assert consumer.provider_id == "anthropic"
    assert consumer.model == "claude-intended"
    assert (
        provider_connection_config(target.provider).api_key
        == "named-anthropic-secret"
    )
    assert (
        provider_connection_config(consumer.provider).api_key
        == "named-anthropic-secret"
    )


def test_legacy_session_provider_and_model_override_bind_the_consumer_together() -> None:
    config = GatewayConfig()
    config.llm_profiles["openai"] = LlmProviderProfile(
        api_key="session-provider-secret",
        base_url="https://api.openai.com/v1",
    )
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    session = SimpleNamespace(
        session_key="agent:main:webchat:legacy-override",
        provider_override="openai",
        model_override="gpt-session",
        model=None,
        auth_profile_override=None,
    )

    consumer = resolve_gateway_consumer_budget(_ctx(config, current), session)

    assert consumer.provider_id == "openai"
    assert consumer.model == "gpt-session"
    assert consumer.source == "session_override"


def test_explicit_compaction_deployment_uses_matching_named_auth_profile() -> None:
    config = GatewayConfig()
    config.compaction.provider = "openai"
    config.compaction.model = "gpt-summary"
    config.llm_profiles["openai"] = LlmProviderProfile(
        api_key="provider-default-secret",
        base_url="https://default.invalid/v1",
    )
    config.llm_profiles["openai:work"] = LlmProviderProfile(
        model="gpt-profile-default",
        api_key="compaction-profile-secret",
        base_url="https://api.openai.com/v1",
    )
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    ctx = _ctx(config, current)
    session = SimpleNamespace(
        session_key="agent:main:webchat:explicit-profile-independent",
        provider_override="openai",
        model_override="gpt-session",
        auth_profile_override="openai:work",
    )

    target = resolve_gateway_compaction_target(ctx, session)

    assert target.source == "explicit_compaction_auth_profile"
    assert target.blocked_reason == ""
    assert target.plan is not None
    assert target.provider_id == "openai"
    assert target.model == "gpt-summary"
    assert (
        provider_connection_config(target.provider).api_key
        == "compaction-profile-secret"
    )


def test_explicit_compaction_provider_rejects_named_profile_provider_mismatch() -> None:
    config = GatewayConfig()
    config.compaction.provider = "openai"
    config.compaction.model = "gpt-summary"
    config.llm_profiles["openai"] = LlmProviderProfile(
        api_key="provider-default-secret",
        base_url="https://api.openai.com/v1",
    )
    config.llm_profiles["anthropic:work"] = LlmProviderProfile(
        api_key="named-anthropic-secret",
        base_url="https://api.anthropic.com",
    )
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    session = SimpleNamespace(
        session_key="agent:main:webchat:explicit-profile-mismatch",
        provider_override="anthropic",
        model_override="claude-session",
        auth_profile_override="anthropic:work",
    )

    target = resolve_gateway_compaction_target(_ctx(config, current), session)

    assert target.provider is None
    assert target.plan is None
    assert target.provider_id == "openai"
    assert target.model == "gpt-summary"
    assert target.source == "auth_profile_unresolved"
    assert target.blocked_reason == "named_auth_profile_provider_mismatch"


def test_bare_named_profile_uses_explicit_provider_boundary() -> None:
    config = GatewayConfig()
    config.compaction.provider = "openai"
    config.compaction.model = "gpt-summary"
    config.llm_profiles["work"] = LlmProviderProfile(
        api_key="bare-profile-secret",
        base_url="https://api.openai.com/v1",
    )
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    session = SimpleNamespace(
        session_key="agent:main:webchat:bare-profile",
        auth_profile_override="work",
    )

    target = resolve_gateway_compaction_target(_ctx(config, current), session)

    assert target.blocked_reason == ""
    assert target.provider_id == "openai"
    assert target.model == "gpt-summary"
    assert (
        provider_connection_config(target.provider).api_key
        == "bare-profile-secret"
    )


def test_named_profile_never_falls_back_to_inherited_or_registry_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "registry-secret")
    config = GatewayConfig()
    config.compaction.provider = "openai"
    config.compaction.model = "gpt-summary"
    config.llm_profiles["openai:empty"] = LlmProviderProfile(
        base_url="https://api.openai.com/v1",
    )
    current = ProviderConfig(
        provider="openai",
        model="gpt-current",
        api_key="inherited-secret",
        base_url="https://api.openai.com/v1",
    )
    session = SimpleNamespace(
        session_key="agent:main:webchat:empty-profile",
        auth_profile_override="openai:empty",
    )

    target = resolve_gateway_compaction_target(_ctx(config, current), session)

    assert target.provider is None
    assert target.plan is None
    assert target.blocked_reason == "missing_credential"


def test_recorded_physical_pair_prevents_provider_model_misbinding() -> None:
    config = GatewayConfig()
    config.llm_profiles["openai"] = LlmProviderProfile(
        api_key="explicit-provider-secret",
        base_url="https://api.openai.com/v1",
    )
    config.llm_profiles["anthropic"] = LlmProviderProfile(
        api_key="recorded-provider-secret",
        base_url="https://api.anthropic.com",
    )
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    ctx = _ctx(config, current)
    session = SimpleNamespace(
        session_key="agent:main:webchat:fallback-pair",
        provider_override="openai",
        model_provider="anthropic",
        model_override="claude-recorded",
        model="gpt-explicit",
        auth_profile_override=None,
    )

    target = resolve_gateway_compaction_target(ctx, session)

    assert target.provider_id == "anthropic"
    assert target.model == "claude-recorded"
    assert target.source == "session_model_provider"
    assert (
        provider_connection_config(target.provider).api_key
        == "recorded-provider-secret"
    )


def test_recorded_model_provider_binds_session_model() -> None:
    config = GatewayConfig()
    config.llm_profiles["openai"] = LlmProviderProfile(
        api_key="routed-provider-secret",
        base_url="https://api.openai.com/v1",
    )
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    ctx = _ctx(config, current)
    session = SimpleNamespace(
        session_key="agent:main:webchat:routed",
        provider_override=None,
        model_provider="openai",
        model_override=None,
        model="gpt-routed",
    )

    target = resolve_gateway_compaction_target(ctx, session)

    assert target.provider_id == "openai"
    assert target.model == "gpt-routed"
    assert target.source == "session_model_provider"
    assert provider_connection_config(target.provider).api_key == "routed-provider-secret"


def test_compaction_config_uses_resolved_execution_plan() -> None:
    config = GatewayConfig()
    current = ProviderConfig(
        provider="ollama",
        model="qwen-current",
        base_url="http://127.0.0.1:11434",
    )
    target = resolve_gateway_compaction_target(
        _ctx(config, current),
        SimpleNamespace(session_key="agent:main:webchat:plan"),
    )

    compaction = build_compaction_config_from_provider(
        target.provider,
        model_override=target.model,
        compaction_config=config.compaction,
        compaction_plan=target.plan,
    )

    assert compaction.llm_plan is target.plan
    assert compaction.provider == "ollama"
    assert compaction.model == "qwen-current"
    assert compaction.api_key == ""
