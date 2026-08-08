from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig, SquillaRouterConfig
from opensquilla.provider import ChatConfig, EnsembleProvider, Message
from opensquilla.provider.selector import ProviderConfig


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
    def __init__(self, *, provider: str, api_key: str) -> None:
        self._cfg = ProviderConfig(
            provider=provider,
            model="base-model",
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
        ("c3", "glm-5.2", True),
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
    selector = _FakeSelector(provider="tokenrhythm", api_key="sk-tr-synthetic")

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
        assert provider.fallback_model == "glm-5.2"
        assert turn.metadata["ensemble_activation_source"] == "router_tier"
        assert turn.metadata["ensemble_tier_binding"] == "shared"
        assert turn.metadata["ensemble_selection_mode"] == "static_tokenrhythm_b5"
    else:
        assert "ensemble_enabled" not in turn.metadata


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
    selector = _FakeSelector(provider="tokenrhythm", api_key="sk-tr-synthetic")

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
    assert provider.fallback_model == "glm-5.2"
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
    selector = _FakeSelector(provider="tokenrhythm", api_key="sk-tr-synthetic")

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
    assert provider.fallback_provider_name == "groq"
    assert provider.fallback_model == "groq-c3"
    assert {
        member.provider_config.api_key
        for member in [*provider.proposers, provider.aggregator]
    } == {"sk-tr-synthetic"}
    assert turn.metadata["ensemble_tier_binding"] == "shared"


async def test_tokenrhythm_c3_falls_back_to_glm_without_ensemble_credential(
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
    selector = _FakeSelector(provider="tokenrhythm", api_key="")

    turn, provider = await runner._run_pipeline(
        "route this request",
        "agent:main:tier-c3-keyless",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert not isinstance(provider, EnsembleProvider)
    assert selector.current_config.model == "glm-5.2"
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
