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

    def override_model(self, model: str) -> None:
        self._cfg = ProviderConfig(
            provider=self._cfg.provider,
            model=model,
            api_key=self._cfg.api_key,
            base_url=self._cfg.base_url,
            proxy=self._cfg.proxy,
            provider_routing=self._cfg.provider_routing,
        )

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
