from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.gateway.config import GatewayConfig, LlmProviderProfile
from opensquilla.gateway.llm_runtime import ProfileCredentialPools
from opensquilla.provider.failures import ProviderFailureKind
from opensquilla.provider.selector import ProviderConfig
from opensquilla.session.compaction_deployment import (
    CompactionDeploymentIdentity,
    CompactionExecutionPlan,
    resolve_compaction_execution_plan,
)


@dataclass
class _BuiltProvider:
    config: ProviderConfig

    def chat(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def __repr__(self) -> str:
        return f"_BuiltProvider(api_key={self.config.api_key!r})"


@pytest.fixture
def built_configs(monkeypatch: pytest.MonkeyPatch) -> list[ProviderConfig]:
    captured: list[ProviderConfig] = []

    def build(config: ProviderConfig) -> _BuiltProvider:
        captured.append(config)
        return _BuiltProvider(config)

    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.build_provider_from_config",
        build,
    )
    return captured


def _config(
    provider: str,
    model: str,
    *,
    api_key: str = "",
) -> ProviderConfig:
    return ProviderConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        provider_routing={"order": "latency"},
    )


def test_explicit_provider_and_model_are_first_and_do_not_leak_secrets(
    monkeypatch: pytest.MonkeyPatch,
    built_configs: list[ProviderConfig],
) -> None:
    explicit = _config("openai", "summary-model", api_key="explicit-secret")
    active = _config("anthropic", "current-model", api_key="current-secret")
    fallback = _config("ollama", "fallback-model")
    resolutions: list[tuple[str, str]] = []

    def resolve(
        _app_config: object,
        provider: str,
        model: str,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        resolutions.append((provider, model))
        return SimpleNamespace(ready=True, provider_config=explicit)

    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.resolve_provider_deployment",
        resolve,
    )

    plan = resolve_compaction_execution_plan(
        app_config=object(),
        active_provider=None,
        active_provider_config=active,
        fallback_provider_configs=(fallback,),
        compaction_config=SimpleNamespace(provider="openai", model="summary-model"),
        context_window_tokens=32_000,
        session_key="session-1",
    )

    assert isinstance(plan, CompactionExecutionPlan)
    assert resolutions == [("openai", "summary-model")]
    assert [(target.provider_id, target.model, target.source) for target in plan.candidates] == [
        ("openai", "summary-model", "explicit"),
        ("anthropic", "current-model", "routed_deployment"),
        ("ollama", "fallback-model", "selector_fallback"),
    ]
    assert all(config.replay_provider_state is False for config in built_configs)
    rendered = repr(plan)
    assert "explicit-secret" not in rendered
    assert "current-secret" not in rendered
    assert repr(plan.primary.provider) not in rendered


def test_explicit_target_uses_runtime_credential_pool_acquirer(
    monkeypatch: pytest.MonkeyPatch,
    built_configs: list[ProviderConfig],
) -> None:
    explicit = _config("openai", "summary-model", api_key="pooled-secret")
    observed: list[Any] = []

    def acquire(provider: str, pool: list[str], session_key: str) -> object:
        observed.append((provider, pool, session_key))
        return object()

    def report_failure(provider: str, session_key: str, failure_kind: object) -> None:
        observed.append((provider, session_key, failure_kind))

    def resolve(
        _app_config: object,
        _provider: str,
        _model: str,
        **kwargs: Any,
    ) -> SimpleNamespace:
        observed.append(kwargs.get("credential_pool_acquirer"))
        metadata = kwargs.get("turn_metadata")
        assert isinstance(metadata, dict)
        metadata["credential_pool"] = {
            "provider": "openai",
            "session_key": "session-pinned",
            "env_name": "TEST_POOL_KEY",
            "key_id": "masked-key-id",
        }
        return SimpleNamespace(ready=True, provider_config=explicit)

    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.resolve_provider_deployment",
        resolve,
    )

    plan = resolve_compaction_execution_plan(
        app_config=object(),
        active_provider=None,
        active_provider_config=None,
        compaction_config=SimpleNamespace(provider="openai", model="summary-model"),
        session_key="session-pinned",
        credential_pool_acquirer=acquire,
        credential_pool_failure_reporter=report_failure,
    )

    assert plan is not None
    assert observed == [acquire]
    assert built_configs[0].api_key == "pooled-secret"
    assert plan.primary.credential_pool_provider == "openai"
    assert plan.primary.credential_pool_session_key == "session-pinned"
    assert plan.primary.credential_pool_failure_reporter is report_failure
    assert "session-pinned" not in repr(plan.primary)
    assert "masked-key-id" not in repr(plan.primary)


def test_pooled_compaction_failure_rotates_key_on_next_plan(
    monkeypatch: pytest.MonkeyPatch,
    built_configs: list[ProviderConfig],
) -> None:
    env_a = "OPENSQUILLA_TEST_COMPACTION_POOL_A"
    env_b = "OPENSQUILLA_TEST_COMPACTION_POOL_B"
    secret_a = "sk-test-compaction-pool-a"
    secret_b = "sk-test-compaction-pool-b"
    monkeypatch.setenv(env_a, secret_a)
    monkeypatch.setenv(env_b, secret_b)
    config = GatewayConfig()
    config.llm_profiles = {
        "openai": LlmProviderProfile(api_key_env_pool=[env_a, env_b])
    }
    pools = ProfileCredentialPools()

    def acquire(provider: str, names: list[str], session_key: str):
        return pools.acquire_for_session(provider, names, session_key)

    first = resolve_compaction_execution_plan(
        app_config=config,
        active_provider=None,
        active_provider_config=None,
        compaction_config=SimpleNamespace(provider="openai", model="gpt-5.4-nano"),
        session_key="session-pinned",
        credential_pool_acquirer=acquire,
        credential_pool_failure_reporter=pools.report_failure,
    )
    assert first is not None
    first_key = built_configs[-1].api_key
    assert first_key in {secret_a, secret_b}
    assert first.primary.credential_pool_failure_reporter is not None

    first.primary.credential_pool_failure_reporter(
        first.primary.credential_pool_provider,
        first.primary.credential_pool_session_key,
        ProviderFailureKind.AUTH_INVALID,
    )
    second = resolve_compaction_execution_plan(
        app_config=config,
        active_provider=None,
        active_provider_config=None,
        compaction_config=SimpleNamespace(provider="openai", model="gpt-5.4-nano"),
        session_key="session-pinned",
        credential_pool_acquirer=acquire,
        credential_pool_failure_reporter=pools.report_failure,
    )

    assert second is not None
    assert built_configs[-1].api_key in {secret_a, secret_b}
    assert built_configs[-1].api_key != first_key


def test_model_only_override_stays_on_current_provider(
    monkeypatch: pytest.MonkeyPatch,
    built_configs: list[ProviderConfig],
) -> None:
    active = _config("anthropic", "current-model", api_key="current-secret")

    def unexpected_resolution(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("model-only compatibility must not guess a provider")

    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.resolve_provider_deployment",
        unexpected_resolution,
    )

    plan = resolve_compaction_execution_plan(
        app_config=object(),
        active_provider=None,
        active_provider_config=active,
        compaction_config=SimpleNamespace(provider="", model="summary-model"),
        context_window_tokens=24_000,
    )

    assert plan is not None
    assert (plan.primary.provider_id, plan.primary.model, plan.primary.source) == (
        "anthropic",
        "summary-model",
        "explicit_model_current_provider",
    )
    assert built_configs[0].api_key == "current-secret"
    assert built_configs[0].model == "summary-model"
    assert built_configs[0].provider == "anthropic"


def test_unavailable_explicit_candidate_continues_to_current_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
    built_configs: list[ProviderConfig],
) -> None:
    current = _config("openai", "routed-current")
    fallback = _config("ollama", "configured-fallback")

    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.resolve_provider_deployment",
        lambda *_args, **_kwargs: SimpleNamespace(
            ready=False,
            provider_config=None,
            reason="missing_credential",
        ),
    )

    plan = resolve_compaction_execution_plan(
        app_config=object(),
        active_provider=None,
        active_provider_config=current,
        fallback_provider_configs=(fallback,),
        compaction_config=SimpleNamespace(
            provider="anthropic",
            model="explicit-summary",
        ),
        context_window_tokens=64_000,
    )

    assert plan is not None
    assert [(target.provider_id, target.model) for target in plan.candidates] == [
        ("openai", "routed-current"),
        ("ollama", "configured-fallback"),
    ]
    assert [(config.provider, config.model) for config in built_configs] == [
        ("openai", "routed-current"),
        ("ollama", "configured-fallback"),
    ]


def test_router_current_previous_and_fallback_chain_is_frozen_in_order(
    built_configs: list[ProviderConfig],
) -> None:
    current = _config("openai", "routed-current")
    previous = _config("anthropic", "session-previous")
    fallback = _config("ollama", "configured-fallback")

    plan = resolve_compaction_execution_plan(
        app_config=None,
        active_provider=None,
        active_provider_config=current,
        fallback_provider_configs=(previous, fallback),
        compaction_config=None,
        context_window_tokens=64_000,
    )

    assert plan is not None
    assert [(target.provider_id, target.model) for target in plan.candidates] == [
        ("openai", "routed-current"),
        ("anthropic", "session-previous"),
        ("ollama", "configured-fallback"),
    ]
    assert [target.source for target in plan.candidates] == [
        "routed_deployment",
        "selector_fallback",
        "selector_fallback",
    ]
    assert plan.primary.context_window_tokens == 64_000
    assert all(config.replay_provider_state is False for config in built_configs)


def test_replay_isolated_targets_dedupe_but_distinct_credentials_remain(
    built_configs: list[ProviderConfig],
    caplog: pytest.LogCaptureFixture,
) -> None:
    current = ProviderConfig(
        provider="openai",
        model="shared-model",
        api_key="primary-secret",
        base_url="https://api.example.test/v1",
        org_id="shared-org",
        proxy="https://proxy.example.test",
        provider_routing={"order": "latency"},
    )
    duplicate = replace(
        current,
        provider_routing=dict(current.provider_routing),
        replay_provider_state=False,
    )
    credential_fallback = replace(
        current,
        api_key="fallback-secret",
        provider_routing=dict(current.provider_routing),
    )

    plan = resolve_compaction_execution_plan(
        app_config=None,
        active_provider=None,
        active_provider_config=current,
        fallback_provider_configs=(duplicate, credential_fallback),
    )

    assert plan is not None
    assert len(plan.candidates) == 2
    assert [config.api_key for config in built_configs] == [
        "primary-secret",
        "primary-secret",
        "fallback-secret",
    ]
    assert all(config.replay_provider_state is False for config in built_configs)
    providers = [target.provider for target in plan.candidates]
    assert all(isinstance(provider, _BuiltProvider) for provider in providers)
    assert [
        provider.config.api_key
        for provider in providers
        if isinstance(provider, _BuiltProvider)
    ] == ["primary-secret", "fallback-secret"]
    assert (
        plan.candidates[0].deployment_fingerprint
        != plan.candidates[1].deployment_fingerprint
    )
    rendered = repr(plan)
    assert "primary-secret" not in rendered
    assert "fallback-secret" not in rendered
    assert "primary-secret" not in caplog.text
    assert "fallback-secret" not in caplog.text


def test_previous_session_identity_re_resolves_rotated_credentials_per_operation(
    monkeypatch: pytest.MonkeyPatch,
    built_configs: list[ProviderConfig],
) -> None:
    secrets = iter(("first-operation-secret", "second-operation-secret"))
    resolutions: list[tuple[str, str, str]] = []

    def resolve(
        _app_config: object,
        provider: str,
        model: str,
        *,
        session_key: str,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        resolutions.append((provider, model, session_key))
        return SimpleNamespace(
            ready=True,
            provider_config=_config(provider, model, api_key=next(secrets)),
        )

    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.resolve_provider_deployment",
        resolve,
    )
    identity = CompactionDeploymentIdentity(
        provider_id="Anthropic",
        model="session-previous",
    )

    first = resolve_compaction_execution_plan(
        app_config=object(),
        active_provider=None,
        active_provider_config=None,
        previous_deployment_identities=(identity,),
        session_key="agent:main:webchat:fresh",
    )
    second = resolve_compaction_execution_plan(
        app_config=object(),
        active_provider=None,
        active_provider_config=None,
        previous_deployment_identities=(identity,),
        session_key="agent:main:webchat:fresh",
    )

    assert first is not None
    assert second is not None
    assert resolutions == [
        ("anthropic", "session-previous", "agent:main:webchat:fresh"),
        ("anthropic", "session-previous", "agent:main:webchat:fresh"),
    ]
    assert [config.api_key for config in built_configs] == [
        "first-operation-secret",
        "second-operation-secret",
    ]
    assert first.primary.provider is not second.primary.provider
    assert "secret" not in repr(identity)
    assert "secret" not in repr(first)
    assert "secret" not in repr(second)


def test_unavailable_previous_session_identity_does_not_reuse_old_config(
    monkeypatch: pytest.MonkeyPatch,
    built_configs: list[ProviderConfig],
) -> None:
    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.resolve_provider_deployment",
        lambda *_args, **_kwargs: SimpleNamespace(
            ready=False,
            provider_config=_config(
                "anthropic",
                "session-previous",
                api_key="stale-secret",
            ),
        ),
    )

    plan = resolve_compaction_execution_plan(
        app_config=object(),
        active_provider=None,
        active_provider_config=None,
        previous_deployment_identities=(
            CompactionDeploymentIdentity(
                provider_id="anthropic",
                model="session-previous",
            ),
        ),
    )

    assert plan is None
    assert built_configs == []


def test_ensemble_uses_aggregator_then_base_without_inspecting_proposers(
    built_configs: list[ProviderConfig],
) -> None:
    aggregator_config = _config("anthropic", "aggregator-model")
    base_config = _config("openai", "routed-base")
    fallback_config = _config("ollama", "fallback-single")

    class _Ensemble:
        aggregator = SimpleNamespace(
            provider_config=aggregator_config,
            ready=True,
        )

        @property
        def proposers(self) -> None:
            raise AssertionError("compaction must not inspect or invoke proposers")

        def chat(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("compaction must not invoke the full ensemble")

    plan = resolve_compaction_execution_plan(
        app_config=None,
        active_provider=_Ensemble(),
        active_provider_config=base_config,
        fallback_provider_configs=(fallback_config,),
        compaction_config=None,
        context_window_tokens=48_000,
    )

    assert plan is not None
    assert [(target.provider_id, target.model, target.source) for target in plan.candidates] == [
        ("anthropic", "aggregator-model", "ensemble_aggregator"),
        ("openai", "routed-base", "routed_deployment"),
        ("ollama", "fallback-single", "selector_fallback"),
    ]
    assert [(config.provider, config.model) for config in built_configs] == [
        ("anthropic", "aggregator-model"),
        ("openai", "routed-base"),
        ("ollama", "fallback-single"),
    ]
