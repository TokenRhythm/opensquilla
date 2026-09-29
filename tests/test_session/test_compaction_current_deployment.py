from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.gateway.config import CompactionLlmConfig, validate_compaction_deployment_write
from opensquilla.provider.ensemble import EnsembleMemberConfig, EnsembleProvider
from opensquilla.provider.failures import ProviderFailureKind
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.protocol import provider_connection_config
from opensquilla.provider.selector import (
    ModelSelector,
    ProviderConfig,
    SelectorConfig,
    build_provider_from_config,
)
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ProviderRequestCorrelation,
    TextDeltaEvent,
)
from opensquilla.session.compaction import call_compaction_provider
from opensquilla.session.compaction_deployment import (
    CompactionDeploymentIdentity,
    CompactionExecutionPlan,
    build_compaction_execution_plan_from_provider_config,
    resolve_compaction_execution_plan,
)


@pytest.mark.parametrize("active_only", [False, True])
@pytest.mark.parametrize("layout", ["prefix", "suffix"])
def test_summary_never_resolves_overrides_previous_models_or_fallbacks(
    monkeypatch, active_only, layout,
):
    monkeypatch.setenv("OPENSQUILLA_COMPACTION_PROMPT_LAYOUT", layout)
    active = ProviderConfig(provider="openai", model="current", api_key="synthetic-key")
    old = ProviderConfig(provider="openai", model="previous", api_key="old-key")
    resolved = []

    def build(config):
        resolved.append(config)
        return build_provider_from_config(config)

    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.build_provider_from_config", build,
    )
    plan = resolve_compaction_execution_plan(
        app_config=None, active_provider=None, active_provider_config=active,
        compaction_config=SimpleNamespace(provider="anthropic", model="summary-only"),
        previous_deployment_identities=(CompactionDeploymentIdentity("openai", "previous"),),
        fallback_provider_configs=(old,), active_only=active_only,
        active_chat_config=ChatConfig(max_tokens=8192, provider_context_window_tokens=64000),
    )
    assert plan is not None
    assert len(plan.candidates) == 1
    assert plan.primary.model == "current"
    assert plan.primary.max_generation_tokens == 8192
    assert plan.primary.max_output_tokens == 8192
    assert plan.max_calls is None
    assert resolved == [active]
    assert active.replay_provider_state
    assert "synthetic-key" not in repr(plan)
    assert "old-key" not in repr(plan)


def test_unavailable_current_deployment_does_not_resolve_another_model(monkeypatch):
    def fail(_):
        raise ValueError("unavailable current deployment")

    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.build_provider_from_config", fail,
    )
    plan = resolve_compaction_execution_plan(
        app_config=None, active_provider=None,
        active_provider_config=ProviderConfig(provider="openai", model="current"),
        fallback_provider_configs=(ProviderConfig(provider="ollama", model="fallback"),),
        compaction_config=SimpleNamespace(provider="ollama", model="summary-model"),
    )
    assert plan is None


def test_actual_bound_provider_wins_over_stale_routed_config():
    stale = ProviderConfig(provider="openai", model="old-base", api_key="stale-key")
    actual = build_provider_from_config(
        ProviderConfig(provider="openai", model="actual-responder", api_key="current-key"),
    )
    plan = resolve_compaction_execution_plan(
        app_config=None, active_provider=actual, active_provider_config=stale,
    )
    assert plan is not None
    assert plan.primary.provider is actual
    assert plan.primary.model == "actual-responder"


@pytest.mark.parametrize("wrapped", [False, True], ids=["physical", "selector"])
@pytest.mark.parametrize("changed_field", ["api_key", "base_url"])
def test_same_model_connection_snapshot_wins_over_stale_config(
    monkeypatch, wrapped, changed_field,
):
    stale = ProviderConfig(
        provider="openai", model="same-model", api_key="synthetic-old",
        base_url="https://old.invalid/v1",
    )
    current = replace(stale, **{changed_field: (
        "synthetic-current" if changed_field == "api_key" else "https://current.invalid/v1"
    )})
    actual = build_provider_from_config(current)
    selector = ModelSelector(SelectorConfig(primary=stale))
    forbidden = Mock(side_effect=AssertionError("summary must not route or reacquire"))
    monkeypatch.setattr(selector, "resolve", forbidden)
    monkeypatch.setattr(selector, "next_fallback", forbidden)
    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.build_provider_from_config", forbidden,
    )
    provider = _SelectorFallbackProvider(actual, selector) if wrapped else actual
    controls = ChatConfig(max_tokens=4096, provider_context_window_tokens=64000)
    plan = resolve_compaction_execution_plan(
        app_config=None, active_provider=provider, active_provider_config=stale,
        active_chat_config=controls, credential_pool_acquirer=forbidden,
    )

    assert plan is not None
    assert plan.primary.provider is actual
    assert len(plan.candidates) == 1
    assert plan.primary.model == stale.model
    assert plan.primary.provider_id == stale.provider
    assert plan.primary.max_generation_tokens == 4096
    connection = provider_connection_config(plan.primary.provider)
    assert connection.api_key == current.api_key
    assert connection.base_url == current.base_url
    assert selector.current_config is stale
    old_plan = resolve_compaction_execution_plan(
        app_config=None, active_provider=build_provider_from_config(stale),
        active_provider_config=None, active_chat_config=controls,
    )
    assert old_plan is not None
    assert plan.primary.deployment_fingerprint != old_plan.primary.deployment_fingerprint
    assert current.api_key not in repr(plan)
    forbidden.assert_not_called()


@pytest.mark.parametrize("wrapped", [False, True], ids=["physical", "selector"])
@pytest.mark.parametrize("changed_field", ["org_id", "proxy", "provider_routing", "extra_body"])
def test_summary_preserves_bound_adapter_options(monkeypatch, wrapped, changed_field):
    provider_id = "custom" if changed_field == "extra_body" else "openrouter"
    stale = ProviderConfig(
        provider=provider_id, model="same-model", api_key="synthetic-key",
        base_url="https://provider.invalid/v1",
    )
    updates = {
        "org_id": "current-organization",
        "proxy": "http://current-proxy.invalid:8080",
        "provider_routing": {"same-model": "current-upstream"},
        "extra_body": {"top_k": 25},
    }
    current = replace(stale, **{changed_field: updates[changed_field]})
    actual = build_provider_from_config(current)
    selector = ModelSelector(SelectorConfig(primary=stale))
    provider = _SelectorFallbackProvider(actual, selector) if wrapped else actual
    forbidden = Mock(side_effect=AssertionError("summary must preserve the bound adapter"))
    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.build_provider_from_config", forbidden,
    )
    plan = resolve_compaction_execution_plan(
        app_config=None, active_provider=provider, active_provider_config=stale,
        active_chat_config=ChatConfig(max_tokens=4096, provider_context_window_tokens=64000),
    )
    assert plan is not None and plan.primary.provider is actual
    assert getattr(plan.primary.provider, f"_{changed_field}") == updates[changed_field]
    if changed_field in {"provider_routing", "extra_body"}:
        projection = plan.primary.provider.project_final_request(
            [Message(role="user", content="Summarize this synthetic history.")],
            config=ChatConfig(max_tokens=4096),
        )
        if changed_field == "provider_routing":
            assert projection.payload["provider"]["order"] == ["current-upstream"]
        else:
            assert projection.payload["top_k"] == 25
    forbidden.assert_not_called()


@pytest.mark.parametrize("fails", [False, True], ids=["success", "auth-failure"])
async def test_frozen_selector_summary_keeps_accounting_and_pool_failure_hook(
    monkeypatch, fails,
):
    stale = ProviderConfig(
        provider="openai", model="same-model", api_key="synthetic-old",
        base_url="https://old.invalid/v1",
    )
    current = replace(stale, api_key="synthetic-current", base_url="https://current.invalid/v1")
    actual = build_provider_from_config(current)
    selector = ModelSelector(SelectorConfig(primary=stale))
    wrapper = _SelectorFallbackProvider(actual, selector)
    forbidden = Mock(side_effect=AssertionError("summary must not enter fallback"))
    monkeypatch.setattr(wrapper, "chat", forbidden)
    monkeypatch.setattr(selector, "resolve", forbidden)
    monkeypatch.setattr(selector, "next_fallback", forbidden)
    calls = []

    async def physical_chat(messages, tools=None, config=None):
        calls.append((messages, tools, config))
        if fails:
            yield ErrorEvent(message="invalid credential", code="401")
        else:
            yield TextDeltaEvent(text="complete summary")
            yield DoneEvent(
                stop_reason="stop", input_tokens=12, output_tokens=2, model=current.model,
            )

    monkeypatch.setattr(actual, "chat", physical_chat)
    reporter = Mock()
    plan = resolve_compaction_execution_plan(
        app_config=None, active_provider=wrapper, active_provider_config=stale,
        active_chat_config=ChatConfig(max_tokens=4096, provider_context_window_tokens=64000),
        session_key="existing-pool-pin", credential_pool_acquirer=forbidden,
        credential_pool_failure_reporter=reporter,
    )
    assert plan is not None and plan.primary.provider is actual
    sink = SimpleNamespace(start=AsyncMock(), finalize=AsyncMock(), mark_unknown=AsyncMock())
    scope = UsageAccountingScope(sink=sink, context=UsageExecutionContext(
        execution_id="summary", agent_run_id="summary-run", session_id="existing-pool-pin",
    ))
    correlation = ProviderRequestCorrelation(
        session_id="existing-pool-pin", turn_id="summary-turn",
        execution_id="summary", call_kind="auxiliary.compaction",
    )
    dispatched = Mock()
    with bind_usage_accounting_scope(scope):
        result = await call_compaction_provider(
            "old conversation", "", plan, timeout=5,
            provider_request_correlation=correlation, on_summary_call_started=dispatched,
        )
    assert result == (None if fails else "complete summary")
    assert len(calls) == 1
    assert calls[0][1] is None
    assert calls[0][2].provider_request_correlation is correlation
    sink.start.assert_awaited_once()
    dispatched.assert_called_once_with()
    assert sink.start.await_args.args[0].model == current.model
    if fails:
        reporter.assert_called_once_with(
            "openai", "existing-pool-pin", ProviderFailureKind.AUTH_INVALID,
        )
    else:
        reporter.assert_not_called()
        sink.finalize.assert_awaited_once()
        accounted = sink.finalize.await_args.args[1]
        assert (accounted.input_tokens, accounted.output_tokens) == (12, 2)
        sink.mark_unknown.assert_not_awaited()
    forbidden.assert_not_called()


def test_generation_allowance_follows_model_capacity_without_1024_cap(monkeypatch):
    catalog = ModelCatalog()
    catalog.set_user_overrides({"openai/synthetic": {
        "context_window": 64000, "max_output_tokens": 12000,
    }})
    monkeypatch.setattr("opensquilla.session.compaction_deployment.shared_catalog", lambda: catalog)
    config = ProviderConfig(provider="openai", model="synthetic", api_key="synthetic-key")
    plan = build_compaction_execution_plan_from_provider_config(config)
    assert plan.primary.max_output_tokens == 12000
    assert plan.primary.max_generation_tokens == 12000
    limited = build_compaction_execution_plan_from_provider_config(
        config, max_generation_tokens=4000,
    )
    assert limited.primary.max_generation_tokens == 4000
    assert CompactionExecutionPlan(candidates=plan.candidates, max_calls=7).max_calls == 7
    with pytest.raises(ValueError):
        CompactionExecutionPlan(candidates=plan.candidates, max_calls=0)


@pytest.mark.parametrize("wrapped", [False, True], ids=["ensemble", "selector-ensemble"])
def test_ensemble_summary_uses_sticky_fixed_responder(wrapped):
    aggregator_config = ProviderConfig(provider="openai", model="aggregator", api_key="agg-key")
    fixed_config = ProviderConfig(provider="openai", model="fixed-current", api_key="fixed-key")
    fixed = build_provider_from_config(fixed_config)
    ensemble = EnsembleProvider(
        profile_name="test", proposers=[],
        aggregator=EnsembleMemberConfig(provider_config=aggregator_config),
        fallback_provider=fixed, fallback_provider_name="openai", fallback_model="fixed-current",
        _fallback_request_budget_member=EnsembleMemberConfig(provider_config=fixed_config),
    )
    ensemble._fixed_takeover_active = True
    ensemble._fixed_takeover_role = "fixed_direct"
    request_config = ChatConfig(max_tokens=4096, provider_context_window_tokens=64000)
    physical, frozen_config, bound = ensemble.current_response_deployment(request_config)
    assert physical is fixed and frozen_config is fixed_config
    assert bound is not request_config
    active = (
        _SelectorFallbackProvider(
            ensemble, ModelSelector(SelectorConfig(primary=aggregator_config)),
        ) if wrapped else ensemble
    )
    plan = resolve_compaction_execution_plan(
        app_config=None, active_provider=active, active_provider_config=aggregator_config,
        active_chat_config=request_config,
    )
    assert plan is not None
    assert [target.model for target in plan.candidates] == ["fixed-current"]
    assert ensemble._fixed_takeover_active
    assert request_config.max_tokens == 4096


def test_deprecated_summary_config_roundtrips_without_rewriting():
    legacy = {"provider": "openai", "model": None, "protected_recent_messages": 12}
    config = CompactionLlmConfig(**legacy)
    validate_compaction_deployment_write({"compaction": legacy})
    assert config.provider == "openai" and config.model is None
    assert config.protected_recent_messages == 12
    assert legacy == {"provider": "openai", "model": None, "protected_recent_messages": 12}
