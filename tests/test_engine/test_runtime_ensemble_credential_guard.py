from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner, _SelectorFallbackProvider
from opensquilla.gateway.config import GatewayConfig, SquillaRouterConfig
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    EnsembleProvider,
    ErrorEvent,
    Message,
    TextDeltaEvent,
)
from opensquilla.provider.selector import (
    ModelSelector,
    ProviderConfig,
    SelectorConfig,
)


class _Provider:
    provider_name = "fake"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        raise AssertionError("credential-guard tests must not start provider chat")

    async def list_models(self) -> list[Any]:
        return []


class _FakeSelector:
    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str = "base-model",
    ) -> None:
        self._cfg = ProviderConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url="https://example.invalid/api",
        )

    @property
    def current_config(self) -> ProviderConfig:
        return self._cfg

    @property
    def active_provider_id(self) -> str:
        return self._cfg.provider

    def override_model(self, model: str) -> None:
        self._cfg = ProviderConfig(
            provider=self._cfg.provider,
            model=model,
            api_key=self._cfg.api_key,
            base_url=self._cfg.base_url,
            proxy=self._cfg.proxy,
            provider_routing=self._cfg.provider_routing,
        )

    def override_provider_config(self, config: ProviderConfig) -> None:
        self._cfg = config

    def disable_provider_state_replay(self) -> None:
        return None

    def resolve(self) -> _Provider:
        return _Provider()


class _ScriptedProvider:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        events: list[Any],
        calls: list[str],
    ) -> None:
        self.provider_name = provider
        self.model = model
        self._events = events
        self._calls = calls

    async def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self._calls.append(self.model)
        for event in self._events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


def _static_b5_config(**ensemble_overrides: Any) -> GatewayConfig:
    return GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm_ensemble={"enabled": True, **ensemble_overrides},
    )


async def test_static_b5_wrap_skipped_without_openrouter_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    runner = TurnRunner(provider_selector=None, config=_static_b5_config())
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")
    single_provider = _Provider()

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:test",
        single_provider,
        selector,
        [],
        "system prompt",
        [],
    )

    # A keyless static profile can never run a member; the turn must keep the
    # plain single-model provider without ensemble labels or fallback budgets.
    assert not isinstance(provider, EnsembleProvider)
    assert turn.metadata["ensemble_wrap_skipped_reason"] == (
        "static_openrouter_b5_no_credential"
    )
    assert "ensemble_enabled" not in turn.metadata


async def test_static_b5_wraps_when_openrouter_env_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")
    runner = TurnRunner(provider_selector=None, config=_static_b5_config())
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:test",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert turn.metadata["ensemble_enabled"] is True
    assert "ensemble_wrap_skipped_reason" not in turn.metadata


async def test_static_b5_wraps_when_active_provider_is_keyed_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    runner = TurnRunner(provider_selector=None, config=_static_b5_config())
    selector = _FakeSelector(provider="openrouter", api_key="sk-or-synthetic")

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:test",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert turn.metadata["ensemble_enabled"] is True
    assert "ensemble_wrap_skipped_reason" not in turn.metadata


async def test_static_tokenrhythm_b5_wrap_skipped_without_tokenrhythm_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    # An OpenRouter key must not unlock the tokenrhythm profile.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="static_tokenrhythm_b5"),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:test",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert not isinstance(provider, EnsembleProvider)
    assert turn.metadata["ensemble_wrap_skipped_reason"] == (
        "static_tokenrhythm_b5_no_credential"
    )
    assert "ensemble_enabled" not in turn.metadata


async def test_static_tokenrhythm_b5_wraps_when_active_provider_is_keyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="static_tokenrhythm_b5"),
    )
    selector = _FakeSelector(provider="tokenrhythm", api_key="sk-tr-synthetic")

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:test",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert turn.metadata["ensemble_enabled"] is True
    assert "ensemble_wrap_skipped_reason" not in turn.metadata


@pytest.mark.parametrize(
    ("routed_tier", "expected_model", "expect_ensemble"),
    [
        ("c0", "qwen3.7-flash", False),
        ("c1", "deepseek-v4-flash-0731", False),
        ("c2", "glm-5.2", False),
        # Shared C3 triggers the global plan without replacing the configured
        # direct/fallback selector head.
        ("c3", "deepseek-v4-flash-0731", True),
    ],
)
async def test_tokenrhythm_router_uses_ensemble_only_for_c3(
    monkeypatch: pytest.MonkeyPatch,
    routed_tier: str,
    expected_model: str,
    expect_ensemble: bool,
) -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "deepseek-v4-flash-0731",
            "api_key": "sk-tr-synthetic",
        },
        llm_ensemble={"enabled": False},
    )
    tier = cfg.squilla_router.tiers[routed_tier]

    async def route_to_requested_tier(turn):
        turn.model = tier["model"]
        turn.metadata["routed_tier"] = routed_tier
        turn.metadata["routing_applied"] = True
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        route_to_requested_tier,
    )
    runner = TurnRunner(provider_selector=None, config=cfg)
    selector = _FakeSelector(
        provider="tokenrhythm",
        model=cfg.llm.model,
        api_key="sk-tr-synthetic",
    )

    turn, provider = await runner._run_pipeline(
        "route this request",
        f"agent:main:tier-{routed_tier}",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert selector.current_config.model == expected_model
    assert isinstance(provider, EnsembleProvider) is expect_ensemble
    if expect_ensemble:
        assert provider.profile_name == "static_tokenrhythm_b5"
        assert provider.fallback_model == "deepseek-v4-flash-0731"
        assert turn.model == "deepseek-v4-flash-0731"
        assert turn.metadata["routed_model_before_ensemble"] == "glm-5.2"
        assert turn.metadata["ensemble_activation_source"] == "router_tier"
        assert turn.metadata["ensemble_tier_binding"] == "shared"
        assert turn.metadata["ensemble_selection_mode"] == "static_tokenrhythm_b5"
    else:
        assert "ensemble_enabled" not in turn.metadata


async def test_global_ensemble_keeps_fixed_fallback_when_router_is_also_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_model = "deepseek-v4-flash-0731"
    routed_model = "glm-5.2"
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": fixed_model,
            "api_key": "sk-tr-synthetic",
        },
        squilla_router={"enabled": True},
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_tokenrhythm_b5",
        },
    )

    async def route_to_c2(turn):
        turn.model = routed_model
        turn.metadata["routed_tier"] = "c2"
        turn.metadata["routed_model"] = routed_model
        turn.metadata["routing_applied"] = True
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        route_to_c2,
    )
    selector = _FakeSelector(
        provider="tokenrhythm",
        model=fixed_model,
        api_key="sk-tr-synthetic",
    )
    runner = TurnRunner(provider_selector=None, config=cfg)

    turn, provider = await runner._run_pipeline(
        "route this globally fused request",
        "agent:main:global-ensemble-router-dual-active",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert provider.fallback_model == fixed_model
    assert selector.current_config.model == fixed_model
    assert turn.model == fixed_model
    assert turn.metadata["routed_model"] == routed_model
    assert turn.metadata["routed_model_before_ensemble"] == routed_model
    assert turn.metadata["ensemble_activation_source"] == "global"


@pytest.mark.parametrize(
    ("cross_provider_tiers", "expected_provider", "expected_api_key", "expect_resolution"),
    [
        (False, "tokenrhythm", "sk-tr-synthetic", False),
        (True, "openrouter", "sk-or-synthetic", True),
    ],
    ids=["cross-provider-flag-off", "cross-provider-flag-on"],
)
async def test_router_dynamic_anchor_uses_effective_cross_provider_boundary(
    monkeypatch: pytest.MonkeyPatch,
    cross_provider_tiers: bool,
    expected_provider: str,
    expected_api_key: str,
    expect_resolution: bool,
) -> None:
    fixed_model = "deepseek-v4-flash-0731"
    routed_model = "z-ai/glm-5.2"
    routed_provider = "openrouter"
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": fixed_model,
            "api_key": "sk-tr-synthetic",
        },
        llm_profiles={
            routed_provider: {
                "provider": routed_provider,
                "model": routed_model,
                "api_key": "sk-or-synthetic",
            }
        },
        squilla_router={
            "enabled": True,
            "preset_binding": "custom",
            "cross_provider_tiers": cross_provider_tiers,
            # Simulate a post-router upgrade or retained hold whose final
            # execution identity no longer equals the stored tier row.
            "tiers": {
                "c2": {
                    "provider": routed_provider,
                    "model": "stored-c2-model",
                }
            },
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )

    async def route_to_c2(turn):
        turn.model = routed_model
        turn.metadata["routed_tier"] = "c2"
        turn.metadata["routed_model"] = routed_model
        turn.metadata["routed_provider"] = routed_provider
        turn.metadata["routing_applied"] = True
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        route_to_c2,
    )
    selector = _FakeSelector(
        provider="tokenrhythm",
        model=fixed_model,
        api_key="sk-tr-synthetic",
    )
    runner = TurnRunner(provider_selector=None, config=cfg)

    turn, provider = await runner._run_pipeline(
        "route this dynamically fused request",
        "agent:main:router-dynamic-fixed-fallback",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert provider.profile_name == "router_dynamic/c2"
    assert provider.fallback_provider_name == "tokenrhythm"
    assert provider.fallback_model == fixed_model
    assert provider.selection_plan["anchor"]["model"] == routed_model
    assert provider.selection_plan["anchor"]["provider"] == expected_provider
    assert provider.selection_plan["anchor"]["source"] == "router_anchor"
    assert provider.proposers[0].provider_config.provider == expected_provider
    assert provider.proposers[0].provider_config.model == routed_model
    assert provider.proposers[0].provider_config.api_key == expected_api_key
    assert cfg.squilla_router.tiers["c2"]["model"] == "stored-c2-model"
    assert ("routed_provider_resolution" in turn.metadata) is expect_resolution
    assert selector.current_config.model == fixed_model
    assert turn.model == fixed_model
    assert turn.metadata["routed_model"] == routed_model


@pytest.mark.parametrize(
    ("all_failed_policy", "expected_calls", "terminal_type"),
    [
        ("fallback_single", ["deepseek-v4-flash-0731"], DoneEvent),
        ("error", [], ErrorEvent),
    ],
)
async def test_shared_c3_all_failed_policy_keeps_the_global_fallback_contract(
    monkeypatch: pytest.MonkeyPatch,
    all_failed_policy: str,
    expected_calls: list[str],
    terminal_type: type[DoneEvent] | type[ErrorEvent],
) -> None:
    fixed_model = "deepseek-v4-flash-0731"
    calls: list[str] = []
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": fixed_model,
            "api_key": "sk-tr-synthetic",
        },
        llm_ensemble={
            "enabled": False,
            "all_failed_policy": all_failed_policy,
        },
    )

    async def route_to_c3(turn):
        turn.model = "glm-5.2"
        turn.metadata["routed_tier"] = "c3"
        turn.metadata["routed_model"] = "glm-5.2"
        turn.metadata["routing_applied"] = True
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        route_to_c3,
    )
    fixed_provider = _ScriptedProvider(
        provider="tokenrhythm",
        model=fixed_model,
        events=[TextDeltaEvent(text="fixed answer"), DoneEvent(model=fixed_model)],
        calls=calls,
    )
    selector = _FakeSelector(
        provider="tokenrhythm",
        model=fixed_model,
        api_key="sk-tr-synthetic",
    )
    runner = TurnRunner(provider_selector=None, config=cfg)

    turn, provider = await runner._run_pipeline(
        "route this request",
        f"agent:main:c3-all-failed-{all_failed_policy}",
        fixed_provider,
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert provider.fallback_model == fixed_model
    assert provider.all_failed_policy == all_failed_policy
    assert selector.current_config.model == fixed_model
    # Force the deterministic pre-proposer failure boundary. ``error`` must
    # surface directly; ``fallback_single`` may invoke only the fixed model.
    provider.proposers = []
    events = [
        event
        async for event in provider.chat(
            [Message(role="user", content="hello")]
        )
    ]

    assert calls == expected_calls
    assert isinstance(events[-1], terminal_type)
    # Router telemetry keeps the dormant C3 decision, while physical execution
    # and fallback identity remain bound to the global fixed model.
    assert turn.metadata["routed_model"] == "glm-5.2"
    assert turn.metadata["routed_model_before_ensemble"] == "glm-5.2"
    assert turn.model == fixed_model


async def test_shared_c3_outer_selector_does_not_retry_the_global_fixed_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_model = "deepseek-v4-flash-0731"
    secondary_model = "qwen3.7-flash"
    calls: list[str] = []
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": fixed_model,
            "api_key": "sk-tr-synthetic",
        },
        llm_ensemble={"enabled": False, "all_failed_policy": "fallback_single"},
    )

    async def route_to_c3(turn):
        turn.model = "glm-5.2"
        turn.metadata["routed_tier"] = "c3"
        turn.metadata["routed_model"] = "glm-5.2"
        turn.metadata["routing_applied"] = True
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        route_to_c3,
    )
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="tokenrhythm",
                model=fixed_model,
                api_key="sk-tr-synthetic",
                base_url="https://tokenrhythm.studio/v1",
            ),
            fallbacks=[
                ProviderConfig(
                    provider="tokenrhythm",
                    model=secondary_model,
                    api_key="sk-tr-synthetic",
                    base_url="https://tokenrhythm.studio/v1",
                )
            ],
        )
    )
    fixed_provider = _ScriptedProvider(
        provider="tokenrhythm",
        model=fixed_model,
        events=[ErrorEvent(message="rate limited", code="429")],
        calls=calls,
    )
    runner = TurnRunner(provider_selector=None, config=cfg)
    turn, provider = await runner._run_pipeline(
        "route this request",
        "agent:main:c3-no-duplicate-fixed-fallback",
        fixed_provider,
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert [config.model for config in selector.remaining_chain()] == [
        fixed_model,
        secondary_model,
    ]
    provider.proposers = []

    def fake_build_provider(config: ProviderConfig) -> _ScriptedProvider:
        events: list[Any]
        if config.model == fixed_model:
            events = [ErrorEvent(message="rate limited again", code="429")]
        else:
            events = [
                TextDeltaEvent(text="secondary answer"),
                DoneEvent(model=config.model),
            ]
        return _ScriptedProvider(
            provider=config.provider,
            model=config.model,
            events=events,
            calls=calls,
        )

    monkeypatch.setattr(
        "opensquilla.provider.selector._build_provider",
        fake_build_provider,
    )
    wrapped = _SelectorFallbackProvider(provider, selector, turn.metadata)
    events = [
        event
        async for event in wrapped.chat(
            [Message(role="user", content="hello")]
        )
    ]

    assert calls == [fixed_model, secondary_model]
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].model == secondary_model


async def test_shared_c3_follows_an_explicit_change_to_the_global_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "deepseek-v4-flash-0731",
            "api_key": "sk-tr-synthetic",
        },
        llm_ensemble={
            "enabled": False,
            "selection_mode": "static_openrouter_b5",
        },
    )
    tier = cfg.squilla_router.tiers["c3"]

    async def route_to_c3(turn):
        turn.model = tier["model"]
        turn.metadata["routed_tier"] = "c3"
        turn.metadata["routing_applied"] = True
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        route_to_c3,
    )
    runner = TurnRunner(provider_selector=None, config=cfg)
    selector = _FakeSelector(
        provider="tokenrhythm",
        model=cfg.llm.model,
        api_key="sk-tr-synthetic",
    )

    turn, provider = await runner._run_pipeline(
        "use the shared plan",
        "agent:main:tier-c3-shared-plan",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert provider.profile_name == "static_openrouter_b5"
    assert provider.fallback_provider_name == "tokenrhythm"
    assert provider.fallback_model == "deepseek-v4-flash-0731"
    assert selector.current_config.model == "deepseek-v4-flash-0731"
    assert turn.metadata["ensemble_tier_binding"] == "shared"
    assert turn.metadata["ensemble_selection_mode"] == "static_openrouter_b5"


async def test_shared_c3_keeps_plan_credentials_when_fallback_crosses_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "deepseek-v4-flash-0731",
            "api_key": "sk-tr-synthetic",
        },
        llm_profiles={
            "groq": {
                "provider": "groq",
                "model": "groq-c3",
                "api_key": "sk-groq-synthetic",
            }
        },
        squilla_router={
            "enabled": True,
            "preset_binding": "custom",
            "cross_provider_tiers": True,
            "tiers": {
                "c3": {
                    "provider": "groq",
                    "model": "groq-c3",
                    "ensemble_enabled": True,
                }
            },
        },
        llm_ensemble={
            "enabled": False,
            "selection_mode": "static_tokenrhythm_b5",
        },
    )

    async def route_to_cross_provider_c3(turn):
        turn.model = "groq-c3"
        turn.metadata["routed_tier"] = "c3"
        turn.metadata["routed_provider"] = "groq"
        turn.metadata["routing_applied"] = True
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        route_to_cross_provider_c3,
    )
    runner = TurnRunner(provider_selector=None, config=cfg)
    selector = _FakeSelector(
        provider="tokenrhythm",
        model=cfg.llm.model,
        api_key="sk-tr-synthetic",
    )

    turn, provider = await runner._run_pipeline(
        "use the shared plan with a foreign fallback",
        "agent:main:tier-c3-cross-provider-shared-plan",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert provider.profile_name == "static_tokenrhythm_b5"
    assert provider.fallback_provider_name == "tokenrhythm"
    assert provider.fallback_model == "deepseek-v4-flash-0731"
    assert selector.current_config.provider == "tokenrhythm"
    assert selector.current_config.model == "deepseek-v4-flash-0731"
    assert {
        member.provider_config.api_key
        for member in [*provider.proposers, provider.aggregator]
    } == {"sk-tr-synthetic"}
    assert "routed_provider_resolution" not in turn.metadata
    assert "routed_provider_applied" not in turn.metadata
    assert turn.metadata["ensemble_tier_binding"] == "shared"


async def test_shared_c3_unsupported_plan_skips_to_the_global_fixed_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_model = "deepseek-v4-flash-0731"
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": fixed_model,
            "api_key": "sk-tr-synthetic",
        },
        llm_ensemble={
            "enabled": False,
            # Mark selection_mode operator-owned before injecting a synthetic
            # forward-version value that this runtime does not understand.
            "selection_mode": "static_tokenrhythm_b5",
        },
    )
    cfg.llm_ensemble.selection_mode = "future_shared_plan"  # type: ignore[assignment]

    async def route_to_c3(turn):
        turn.model = "glm-5.2"
        turn.metadata["routed_tier"] = "c3"
        turn.metadata["routed_model"] = "glm-5.2"
        turn.metadata["routing_applied"] = True
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        route_to_c3,
    )
    selector = _FakeSelector(
        provider="tokenrhythm",
        model=fixed_model,
        api_key="sk-tr-synthetic",
    )
    fixed_provider = _ScriptedProvider(
        provider="tokenrhythm",
        model=fixed_model,
        events=[],
        calls=[],
    )
    runner = TurnRunner(provider_selector=None, config=cfg)

    turn, provider = await runner._run_pipeline(
        "route this request",
        "agent:main:c3-unsupported-shared-plan",
        fixed_provider,
        selector,
        [],
        "system prompt",
        [],
    )

    assert provider is fixed_provider
    assert selector.current_config.model == fixed_model
    assert turn.model == fixed_model
    assert turn.metadata["routed_model"] == "glm-5.2"
    assert turn.metadata["ensemble_wrap_skipped_reason"] == (
        "unsupported_tier_selection_mode:future_shared_plan"
    )


async def test_c3_explicit_single_model_keeps_the_tier_local_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "deepseek-v4-flash-0731",
            "api_key": "sk-tr-synthetic",
        },
        squilla_router={
            "enabled": True,
            "preset_binding": "custom",
            "tiers": {
                "c3": {
                    "provider": "tokenrhythm",
                    "model": "glm-5.2",
                    "ensemble_enabled": False,
                }
            },
        },
        llm_ensemble={"enabled": False},
    )

    async def route_to_c3(turn):
        turn.model = "glm-5.2"
        turn.metadata["routed_tier"] = "c3"
        turn.metadata["routed_model"] = "glm-5.2"
        turn.metadata["routing_applied"] = True
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        route_to_c3,
    )
    selector = _FakeSelector(
        provider="tokenrhythm",
        model=cfg.llm.model,
        api_key="sk-tr-synthetic",
    )
    runner = TurnRunner(provider_selector=None, config=cfg)

    turn, provider = await runner._run_pipeline(
        "route this request",
        "agent:main:c3-explicit-single",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert not isinstance(provider, EnsembleProvider)
    assert selector.current_config.model == "glm-5.2"
    assert turn.model == "glm-5.2"
    assert "ensemble_enabled" not in turn.metadata


async def test_c3_legacy_selection_mode_keeps_the_tier_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "deepseek-v4-flash-0731",
            "api_key": "sk-tr-synthetic",
        },
        squilla_router={
            "enabled": True,
            "preset_binding": "custom",
            "tiers": {
                "c3": {
                    "provider": "tokenrhythm",
                    "model": "glm-5.2",
                    "ensemble_selection_mode": "static_tokenrhythm_b5",
                }
            },
        },
        # A retained tier-local profile outranks even a simultaneously enabled
        # global plan for upgrade compatibility.
        llm_ensemble={"enabled": True},
    )

    async def route_to_c3(turn):
        turn.model = "glm-5.2"
        turn.metadata["routed_tier"] = "c3"
        turn.metadata["routed_model"] = "glm-5.2"
        turn.metadata["routing_applied"] = True
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        route_to_c3,
    )
    selector = _FakeSelector(
        provider="tokenrhythm",
        model=cfg.llm.model,
        api_key="sk-tr-synthetic",
    )
    runner = TurnRunner(provider_selector=None, config=cfg)

    turn, provider = await runner._run_pipeline(
        "route this request",
        "agent:main:c3-legacy-ensemble-mode",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert selector.current_config.model == "glm-5.2"
    assert provider.fallback_model == "glm-5.2"
    assert turn.metadata["ensemble_tier_binding"] == "legacy"
    assert turn.model == "glm-5.2"


async def test_tokenrhythm_c3_uses_fixed_model_without_ensemble_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "deepseek-v4-flash-0731",
            "api_key": "",
        },
        llm_ensemble={"enabled": False},
    )
    tier = cfg.squilla_router.tiers["c3"]

    async def route_to_c3(turn):
        turn.model = tier["model"]
        turn.metadata["routed_tier"] = "c3"
        turn.metadata["routing_applied"] = True
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        route_to_c3,
    )
    runner = TurnRunner(provider_selector=None, config=cfg)
    selector = _FakeSelector(
        provider="tokenrhythm",
        model=cfg.llm.model,
        api_key="",
    )

    calls: list[str] = []
    fixed_provider = _ScriptedProvider(
        provider="tokenrhythm",
        model=cfg.llm.model,
        events=[],
        calls=calls,
    )
    turn, provider = await runner._run_pipeline(
        "route this request",
        "agent:main:tier-c3-keyless",
        fixed_provider,
        selector,
        [],
        "system prompt",
        [],
    )

    assert not isinstance(provider, EnsembleProvider)
    assert provider is fixed_provider
    assert provider.model == "deepseek-v4-flash-0731"
    assert calls == []
    assert selector.current_config.model == "deepseek-v4-flash-0731"
    assert turn.model == "deepseek-v4-flash-0731"
    assert turn.metadata["routed_model_before_ensemble"] == "glm-5.2"
    assert turn.metadata["ensemble_wrap_skipped_reason"] == (
        "static_tokenrhythm_b5_no_credential"
    )


async def test_tokenrhythm_c3_observe_route_keeps_baseline_single_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "deepseek-v4-flash-0731",
            "api_key": "sk-tr-synthetic",
        },
        llm_ensemble={"enabled": False},
    )

    async def observe_c3(turn):
        turn.metadata["baseline_model"] = turn.model
        turn.metadata["routed_tier"] = "c3"
        turn.metadata["routed_model"] = "glm-5.2"
        turn.metadata["routing_applied"] = False
        return turn

    monkeypatch.setattr(
        "opensquilla.engine.steps.apply_squilla_router",
        observe_c3,
    )
    runner = TurnRunner(provider_selector=None, config=cfg)
    selector = _FakeSelector(provider="tokenrhythm", api_key="sk-tr-synthetic")

    turn, provider = await runner._run_pipeline(
        "observe this request",
        "agent:main:tier-c3-observe",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert turn.metadata["routed_tier"] == "c3"
    assert turn.metadata["routing_applied"] is False
    assert selector.current_config.provider == "tokenrhythm"
    assert selector.current_config.model == "deepseek-v4-flash-0731"
    assert not isinstance(provider, EnsembleProvider)
    assert "ensemble_enabled" not in turn.metadata


async def test_router_dynamic_wrap_is_not_credential_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:test",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert turn.metadata["ensemble_enabled"] is True
    assert "ensemble_wrap_skipped_reason" not in turn.metadata


def _custom_b5_guard_config(candidates: list[dict[str, Any]]) -> GatewayConfig:
    return GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm={
            "provider": "groq",
            "model": "base-model",
            "api_key": "sk-groq-synthetic",
            "base_url": "https://example.invalid/api",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": candidates,
        },
    )


async def test_custom_b5_tracks_missing_member_and_preserves_quorum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = _custom_b5_guard_config(
        [
            {"provider": "groq", "model": "candidate-a"},
            {"provider": "openrouter", "model": "z-ai/glm-5.2"},
        ]
    )
    runner = TurnRunner(provider_selector=None, config=cfg)
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:test",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    by_provider = {
        member.provider_config.provider: member for member in provider.proposers
    }
    assert by_provider["groq"].ready is True
    assert by_provider["openrouter"].ready is False
    assert by_provider["openrouter"].unavailable_reason == "missing_credential"
    assert turn.metadata["ensemble_enabled"] is True
    assert "ensemble_wrap_skipped_reason" not in turn.metadata


async def test_custom_b5_wraps_when_every_member_resolves_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")
    cfg = _custom_b5_guard_config(
        [
            {"provider": "groq", "model": "candidate-a", "role": "primary"},
            {"provider": "openrouter", "model": "z-ai/glm-5.2", "role": "contrast"},
            {"provider": "groq", "model": "fuser", "role": "aggregator"},
        ]
    )
    runner = TurnRunner(provider_selector=None, config=cfg)
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:test",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert provider.profile_name == "custom_b5"
    assert [member.label for member in provider.proposers] == ["primary", "contrast"]
    assert provider.aggregator.provider_config.model == "fuser"
    assert turn.metadata["ensemble_enabled"] is True
    assert "ensemble_wrap_skipped_reason" not in turn.metadata
