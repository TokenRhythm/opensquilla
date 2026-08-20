"""Failover must realign routed_model telemetry to the model that runs.

Same invariant the explicit-model override realignment enforces
(prompt_assembler_stage, commit 966df982): ``metadata["routed_model"]`` is
read by RouterDecisionEvent and comprehensive-savings pricing, so after a
selector failover it must name the fallback model, and route-savings figures
computed for the abandoned model no longer apply.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.context_budget import ContextBudgetGovernor
from opensquilla.engine import ToolResult
from opensquilla.engine.agent import Agent
from opensquilla.engine.agent_injection import ListPendingInputProvider
from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.runtime import TurnRunner, _SelectorFallbackProvider
from opensquilla.engine.selector_override import apply_model_override
from opensquilla.engine.types import (
    AgentConfig,
    RouterDecisionEvent,
)
from opensquilla.engine.types import DoneEvent as EngineDoneEvent
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelCapabilities,
    ProviderActivityEvent,
    ProviderRequestCorrelation,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.protocol import IMAGE_INPUT_UNSUPPORTED_CODE
from opensquilla.provider.types import ContentBlockImage
from opensquilla.tools.types import CallerKind, ToolContext


class _StubSelector:
    def __init__(self, fallback_model: str) -> None:
        self._fallback_model = fallback_model

    def next_fallback_after_failure(self, exc: Exception) -> object:
        del exc
        return _SuccessfulProvider()

    @property
    def current_config(self) -> SimpleNamespace:
        return SimpleNamespace(provider="fallback-provider", model=self._fallback_model)


class _SuccessfulProvider:
    provider_name = "fallback-provider"

    async def chat(self, messages, tools=None, config=None):
        del messages, tools, config
        yield TextDeltaEvent(text="ok")
        yield DoneEvent(model="cheap/fallback")


async def test_fallback_realigns_only_when_provider_call_starts() -> None:
    metadata: dict[str, object] = {
        "routed_model": "expensive/model",
        "savings_pct": 12.5,
        "savings_max_price_per_m": 3.0,
        "savings_routed_price_per_m": 0.5,
    }
    wrapper = _SelectorFallbackProvider(
        _SuccessfulProvider(),
        _StubSelector("cheap/fallback"),
        turn_metadata=metadata,
    )

    assert wrapper.fallback_after_invalid_response("upstream 503") is True
    assert metadata["routed_model"] == "expensive/model"
    assert "executed_model" not in metadata

    _ = [
        event
        async for event in wrapper.chat(
            [Message(role="user", content="hello")],
            config=ChatConfig(),
        )
    ]

    assert metadata["routed_model"] == "cheap/fallback"
    assert metadata["executed_provider"] == "fallback-provider"
    assert metadata["executed_model"] == "cheap/fallback"
    assert metadata["router_fallback_hops"] == 1
    assert metadata["router_fallback_reason"] == "selector_fallback"
    assert metadata["savings_pct"] == 0.0
    assert metadata["savings_max_price_per_m"] == 0.0
    assert metadata["savings_routed_price_per_m"] == 0.0


def test_fallback_to_same_model_keeps_savings() -> None:
    metadata: dict[str, object] = {"routed_model": "same/model", "savings_pct": 7.0}
    wrapper = _SelectorFallbackProvider(
        object(),
        _StubSelector("same/model"),
        turn_metadata=metadata,
    )

    assert wrapper.fallback_after_invalid_response("upstream 503") is True

    assert metadata["routed_model"] == "same/model"
    assert metadata["savings_pct"] == 7.0


def test_fallback_without_metadata_is_noop() -> None:
    wrapper = _SelectorFallbackProvider(object(), _StubSelector("any/model"))
    assert wrapper.fallback_after_invalid_response("upstream 503") is True


def test_preselected_fallback_leg_derives_call_kind_only() -> None:
    wrapper = _SelectorFallbackProvider(object(), _StubSelector("fallback/model"))
    correlation = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="execution-1",
        call_kind="agent.chat",
    )
    config = ChatConfig(provider_request_correlation=correlation)

    assert wrapper._config_for_active_leg(config) is config
    assert wrapper.fallback_after_invalid_response("upstream 503") is True

    fallback_config = wrapper._config_for_active_leg(config)
    assert fallback_config is not config
    assert fallback_config.provider_request_correlation == ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="execution-1",
        call_kind="agent.chat.provider_fallback",
    )


def test_fallback_leg_rebinds_request_budget_and_model_capabilities(
    monkeypatch: Any,
) -> None:
    class _Catalog:
        def resolve_max_tokens(
            self,
            model_id: str,
            user_override: int = 0,
            provider: str = "",
        ) -> int:
            assert (model_id, user_override, provider) == (
                "fallback/model",
                0,
                "fallback-provider",
            )
            return 2_048

        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "openrouter",
            base_url: str = "",
        ) -> ModelCapabilities:
            assert (model_id, provider_name, base_url) == (
                "fallback/model",
                "fallback-provider",
                "",
            )
            return ModelCapabilities(supports_tools=False, supports_vision=False)

        def resolve_context_window_with_source(
            self,
            model_id: str,
            provider: str = "",
        ) -> tuple[int, str]:
            return 8_192, "catalog"

        def resolve_context_window(
            self,
            model_id: str,
            provider: str = "",
        ) -> int:
            return 8_192

    monkeypatch.setattr("opensquilla.engine.runtime.shared_catalog", lambda: _Catalog())
    wrapper = _SelectorFallbackProvider(
        object(),
        _StubSelector("fallback/model"),
    )
    wrapper.configure_fallback_limits(
        {("fallback-provider", "fallback/model"): (8_192, 2_048)}
    )
    original = ChatConfig(
        max_tokens=64_000,
        provider_request_max_chars=500_000,
        model_capabilities=ModelCapabilities(
            supports_tools=True,
            supports_vision=True,
        ),
    )

    assert wrapper.fallback_after_invalid_response("upstream 503") is True
    rebound = wrapper._config_for_active_leg(original)

    assert rebound is not original
    assert rebound.max_tokens == 2_048
    assert rebound.provider_request_max_chars == 17_408
    assert rebound.model_capabilities == ModelCapabilities(
        supports_tools=False,
        supports_vision=False,
    )
    assert original.max_tokens == 64_000
    assert original.provider_request_max_chars == 500_000


def test_fallback_leg_replaces_a_cap_derived_for_the_previous_leg(
    monkeypatch: Any,
) -> None:
    class _Catalog:
        def resolve_max_tokens(
            self,
            model_id: str,
            user_override: int = 0,
            provider: str = "",
        ) -> int:
            return 2_048

        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "openrouter",
            base_url: str = "",
        ) -> ModelCapabilities:
            return ModelCapabilities()

    monkeypatch.setattr("opensquilla.engine.runtime.shared_catalog", lambda: _Catalog())
    wrapper = _SelectorFallbackProvider(
        object(),
        _StubSelector("fallback/model"),
    )
    wrapper.configure_fallback_limits(
        {("fallback-provider", "fallback/model"): (32_000, 2_048)}
    )
    original = ChatConfig(
        max_tokens=2_048,
        provider_request_max_chars=17_408,
        provider_request_max_chars_explicit_cap=0,
    )

    assert wrapper.fallback_after_invalid_response("upstream 503") is True
    rebound = wrapper._config_for_active_leg(original)

    assert rebound.provider_request_max_chars > original.provider_request_max_chars
    assert rebound.context_window_tokens_global_override == 0
    assert rebound.provider_request_max_chars_explicit_cap == 0


def test_fallback_leg_preserves_global_context_window_override(
    monkeypatch: Any,
) -> None:
    class _Catalog:
        def resolve_max_tokens(
            self,
            model_id: str,
            user_override: int = 0,
            provider: str = "",
        ) -> int:
            return 2_048

        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "openrouter",
            base_url: str = "",
        ) -> ModelCapabilities:
            return ModelCapabilities()

    monkeypatch.setattr("opensquilla.engine.runtime.shared_catalog", lambda: _Catalog())
    wrapper = _SelectorFallbackProvider(
        object(),
        _StubSelector("fallback/model"),
    )
    wrapper.configure_fallback_limits(
        {("fallback-provider", "fallback/model"): (8_192, 2_048)}
    )
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(
            max_tokens=2_048,
            context_window_tokens=8_192,
            context_window_tokens_global_override=8_192,
        ),
    )
    original = agent._provider_admission_chat_config(
        "active user",
        context_window_tokens=8_192,
        max_output_tokens=2_048,
    )

    assert original.provider_request_max_chars == 17_408
    assert wrapper.fallback_after_invalid_response("upstream 503") is True
    rebound = wrapper._config_for_active_leg(original)

    assert rebound.provider_request_max_chars == 17_408
    assert rebound.context_window_tokens_global_override == 8_192
    assert rebound.provider_request_max_chars_explicit_cap == 0


def test_global_context_window_override_prevents_catalog_only_escalation(
    monkeypatch: Any,
) -> None:
    class _Selector:
        current_config = SimpleNamespace(provider="openai", model="small-model")

        def remaining_chain(self) -> list[SimpleNamespace]:
            return [
                self.current_config,
                SimpleNamespace(provider="openai", model="large-model"),
            ]

    seen: list[tuple[str, int]] = []

    def _resolve_context_window(
        _catalog: Any,
        model: str,
        *,
        provider: str = "",
        global_override: int = 0,
    ) -> tuple[int, str]:
        assert provider == "openai"
        seen.append((model, global_override))
        if global_override > 0:
            return global_override, "config"
        return (4_000, "catalog") if model == "small-model" else (32_000, "catalog")

    monkeypatch.setattr("opensquilla.engine.runtime.shared_catalog", object)
    monkeypatch.setattr(
        "opensquilla.engine.runtime.resolve_effective_context_window",
        _resolve_context_window,
    )
    wrapper = _SelectorFallbackProvider(object(), _Selector())
    config = ChatConfig(context_window_tokens_global_override=8_192)

    assert wrapper._can_escalate_local_admission_failure(config) is False
    assert seen == [("small-model", 8_192), ("large-model", 8_192)]


def test_fallback_leg_never_enlarges_an_explicit_request_cap(
    monkeypatch: Any,
) -> None:
    class _Catalog:
        def resolve_max_tokens(
            self,
            model_id: str,
            user_override: int = 0,
            provider: str = "",
        ) -> int:
            return 2_048

        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "openrouter",
            base_url: str = "",
        ) -> ModelCapabilities:
            return ModelCapabilities()

    monkeypatch.setattr("opensquilla.engine.runtime.shared_catalog", lambda: _Catalog())
    monkeypatch.setattr(
        "opensquilla.engine.runtime.resolve_effective_context_window",
        lambda *_args, **_kwargs: (32_000, "catalog"),
    )
    wrapper = _SelectorFallbackProvider(
        object(),
        _StubSelector("fallback/model"),
    )
    wrapper.configure_fallback_limits(
        {("fallback-provider", "fallback/model"): (32_000, 2_048)}
    )
    original = ChatConfig(
        max_tokens=2_048,
        provider_request_max_chars=12_345,
    )

    assert wrapper.fallback_after_invalid_response("upstream 503") is True
    rebound = wrapper._config_for_active_leg(original)

    assert rebound.provider_request_max_chars == 12_345
    assert rebound.provider_request_max_chars_explicit_cap == 12_345


def test_fallback_leg_clamps_output_and_proof_budget_without_correlation() -> None:
    wrapper = _SelectorFallbackProvider(object(), _StubSelector("fallback/model"))
    wrapper.configure_fallback_limits(
        {("FALLBACK-PROVIDER", "fallback/model"): (32_000, 8_192)}
    )
    config = ChatConfig(
        max_tokens=131_072,
        provider_request_max_chars=500_000,
    )

    assert wrapper.fallback_after_invalid_response("upstream 503") is True
    fallback_config = wrapper._config_for_active_leg(config)

    expected_proof_cap = ContextBudgetGovernor.from_values(
        context_window_tokens=32_000,
        max_output_tokens=8_192,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    ).snapshot().provider_request_max_chars
    assert fallback_config is not config
    assert fallback_config.max_tokens == 8_192
    assert fallback_config.provider_request_max_chars == expected_proof_cap
    assert fallback_config.provider_request_correlation is None


def test_fallback_leg_never_increases_small_explicit_output_limit() -> None:
    wrapper = _SelectorFallbackProvider(object(), _StubSelector("fallback/model"))
    wrapper.configure_fallback_limits(
        {("fallback-provider", "fallback/model"): (32_000, 8_192)}
    )
    config = ChatConfig(max_tokens=4_096)

    assert wrapper.fallback_after_invalid_response("upstream 503") is True
    fallback_config = wrapper._config_for_active_leg(config)

    assert fallback_config.max_tokens == config.max_tokens
    assert fallback_config.max_tokens == 4_096


def test_unknown_fallback_limit_does_not_apply_generic_default() -> None:
    wrapper = _SelectorFallbackProvider(object(), _StubSelector("unknown/model"))
    config = ChatConfig(max_tokens=131_072)

    assert wrapper.fallback_after_invalid_response("upstream 503") is True

    fallback_config = wrapper._config_for_active_leg(config)
    assert fallback_config.max_tokens == config.max_tokens
    assert fallback_config.model_capabilities.supports_vision is False


def test_each_hop_uses_the_active_physical_models_own_output_limit() -> None:
    class _MultiHopSelector:
        def __init__(self) -> None:
            self._remaining = [
                SimpleNamespace(provider="fallback-provider", model="middle/model"),
                SimpleNamespace(provider="fallback-provider", model="last/model"),
            ]
            self.current_config = SimpleNamespace(
                provider="primary-provider", model="primary/model"
            )

        @property
        def active_provider_id(self) -> str:
            return str(self.current_config.provider)

        def next_fallback_after_failure(self, exc: Exception) -> object:
            del exc
            self.current_config = self._remaining.pop(0)
            return object()

    selector = _MultiHopSelector()
    wrapper = _SelectorFallbackProvider(object(), selector)
    wrapper.configure_fallback_limits(
        {
            ("fallback-provider", "middle/model"): (64_000, 32_768),
            ("fallback-provider", "last/model"): (16_000, 4_096),
        }
    )
    original = ChatConfig(
        max_tokens=131_072,
        provider_request_max_chars=500_000,
    )

    assert wrapper.fallback_after_invalid_response("first failure") is True
    middle = wrapper._config_for_active_leg(original)
    assert middle.max_tokens == 32_768

    assert wrapper.fallback_after_invalid_response("second failure") is True
    last = wrapper._config_for_active_leg(original)
    assert last.max_tokens == 4_096
    assert last.provider_request_max_chars < middle.provider_request_max_chars


async def test_capability_constrained_fallback_skips_non_vision_candidates() -> None:
    primary_config = SimpleNamespace(provider="openrouter", model="vision-primary")
    text_config = SimpleNamespace(provider="openrouter", model="text-fallback")
    unknown_config = SimpleNamespace(provider="openrouter", model="unknown-fallback")
    vision_config = SimpleNamespace(provider="openrouter", model="vision-fallback")

    class _Provider:
        provider_name = "openrouter"

        def __init__(self, model: str) -> None:
            self.model = model
            self.calls = 0

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            self.calls += 1
            yield TextDeltaEvent(text=f"reply from {self.model}")
            yield DoneEvent(model=self.model)

    providers = {
        config.model: _Provider(config.model)
        for config in (
            primary_config,
            text_config,
            unknown_config,
            vision_config,
        )
    }

    class _Selector:
        active_provider_id = "openrouter"

        def __init__(self) -> None:
            self._chain = [
                primary_config,
                text_config,
                unknown_config,
                vision_config,
            ]
            self._index = 0

        @property
        def current_config(self):
            return self._chain[self._index]

        def remaining_chain(self):
            return list(self._chain[self._index :])

        def next_fallback_after_failure_matching(self, _exc, *, predicate):
            matching = [cfg for cfg in self._chain[self._index + 1 :] if predicate(cfg)]
            if not matching:
                raise IndexError("No fallback chain available")
            provider = providers[matching[0].model]
            self._chain = [self.current_config, *matching]
            self._index = 1
            return provider

    selector = _Selector()
    wrapper = _SelectorFallbackProvider(providers[primary_config.model], selector)
    wrapper.configure_fallback_deployment_limits(
        [
            (text_config, 0, 0, ModelCapabilities(supports_vision=False)),
            (unknown_config, 0, 0, ModelCapabilities(supports_vision=False)),
            (vision_config, 0, 0, ModelCapabilities(supports_vision=True)),
        ]
    )
    wrapper.configure_fallback_deployment_vision_support(
        [
            (text_config, "unsupported"),
            (unknown_config, "unknown"),
            (vision_config, "supported"),
        ]
    )

    selected = wrapper.fallback_after_invalid_response_with_capabilities(
        "reasoning_only",
        requires_vision=True,
    )
    events = [
        event
        async for event in wrapper.chat(
            [
                Message(
                    role="user",
                    content=[
                        ContentBlockImage(
                            media_type="image/png",
                            data="c3ludGhldGlj",
                        )
                    ],
                )
            ],
            config=ChatConfig(),
        )
    ]

    assert selected is True
    assert selector.current_config is vision_config
    assert providers[primary_config.model].calls == 0
    assert providers[text_config.model].calls == 0
    assert providers[unknown_config.model].calls == 0
    assert providers[vision_config.model].calls == 1
    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "reply from vision-fallback"
    ]


def test_capability_constrained_fallback_keeps_primary_without_vision_candidate() -> None:
    primary_config = SimpleNamespace(provider="openrouter", model="vision-primary")
    text_config = SimpleNamespace(provider="openrouter", model="text-fallback")
    unknown_config = SimpleNamespace(provider="openrouter", model="unknown-fallback")

    class _Selector:
        active_provider_id = "openrouter"

        def __init__(self) -> None:
            self._chain = [primary_config, text_config, unknown_config]
            self._index = 0
            self.fallback_builds = 0

        @property
        def current_config(self):
            return self._chain[self._index]

        def remaining_chain(self):
            return list(self._chain[self._index :])

        def next_fallback_after_failure_matching(self, _exc, *, predicate):
            matching = [cfg for cfg in self._chain[self._index + 1 :] if predicate(cfg)]
            if not matching:
                raise IndexError("No fallback chain available")
            self.fallback_builds += 1
            self._chain = [self.current_config, *matching]
            self._index = 1
            return object()

    primary_provider = object()
    selector = _Selector()
    wrapper = _SelectorFallbackProvider(primary_provider, selector)
    wrapper.configure_fallback_deployment_vision_support(
        [
            (text_config, "unsupported"),
            (unknown_config, "unknown"),
        ]
    )

    selected = wrapper.fallback_after_invalid_response_with_capabilities(
        "reasoning_only",
        requires_vision=True,
    )

    assert selected is False
    assert selector.current_config is primary_config
    assert selector.fallback_builds == 0
    assert wrapper._provider is primary_provider
    assert wrapper._used_fallback is False


def test_tokenrhythm_same_model_fallback_uses_exact_private_config_identity() -> None:
    primary = SimpleNamespace(
        provider="tokenrhythm",
        model="shared/model",
        api_key="synthetic-key-a",
        base_url="https://tokenrhythm.studio/v1",
        proxy="",
    )
    fallback_same_model = SimpleNamespace(
        provider="tokenrhythm",
        model="shared/model",
        api_key="synthetic-key-b",
        base_url="https://tokenrhythm.studio/v1",
        proxy="http://127.0.0.1:8118",
    )
    fallback_other_model = SimpleNamespace(
        provider="tokenrhythm",
        model="other/model",
        api_key="synthetic-key-c",
        base_url="https://tokenrhythm.studio/v1",
        proxy="",
    )

    class _AuthoritySelector:
        def __init__(self) -> None:
            self._chain = [primary, fallback_same_model, fallback_other_model]
            self._index = 0

        @property
        def current_config(self):
            return self._chain[self._index]

        @property
        def active_provider_id(self) -> str:
            return str(self.current_config.provider)

        def remaining_chain(self):
            return list(self._chain[self._index :])

        def next_fallback_after_failure(self, _exc: Exception) -> object:
            self._index += 1
            return object()

    metadata: dict[str, Any] = {
        "route_plan": {
            "fallback_chain": [
                {
                    "provider": "tokenrhythm",
                    "model": "shared/model",
                    "capabilities": {
                        "context_window": 1_000_000,
                        "effective_max_tokens": 131_072,
                    },
                }
            ]
        }
    }
    selector = _AuthoritySelector()
    wrapper = _SelectorFallbackProvider(
        object(),
        selector,
        turn_metadata=metadata,
    )
    wrapper.configure_fallback_deployment_limits(
        [
            (
                fallback_same_model,
                64_000,
                8_192,
                ModelCapabilities(supports_vision=False),
            ),
            (
                fallback_other_model,
                32_000,
                4_096,
                ModelCapabilities(supports_vision=True),
            ),
        ]
    )
    # A sanitized provider/model-only compatibility limit would be wrong for B.
    wrapper.configure_fallback_limits(
        {("tokenrhythm", "shared/model"): (1_000_000, 131_072)}
    )
    original = ChatConfig(max_tokens=131_072, model_capabilities=None)

    assert wrapper.fallback_after_invalid_response("first failure") is True
    first_fallback_config = wrapper._config_for_active_leg(original)
    assert first_fallback_config.max_tokens == 8_192
    assert first_fallback_config.model_capabilities.supports_vision is False
    assert wrapper.fallback_after_invalid_response("second failure") is True
    second_fallback_config = wrapper._config_for_active_leg(original)
    assert second_fallback_config.max_tokens == 4_096
    assert second_fallback_config.model_capabilities.supports_vision is True

    serialized = json.dumps(metadata, sort_keys=True)
    assert "synthetic-key-a" not in serialized
    assert "synthetic-key-b" not in serialized
    assert "synthetic-key-c" not in serialized
    assert "authority_identity" not in serialized
    assert "transport_fingerprint" not in serialized


def test_dynamic_tokenrhythm_fallback_without_exact_limit_is_not_cross_clamped() -> None:
    primary = SimpleNamespace(
        provider="tokenrhythm",
        model="shared/model",
        api_key="synthetic-known-key",
        base_url="https://tokenrhythm.studio/v1",
        proxy="",
    )
    dynamically_injected = SimpleNamespace(
        provider="tokenrhythm",
        model="shared/model",
        api_key="synthetic-dynamic-key",
        base_url="https://tokenrhythm.studio/v1",
        proxy="",
    )

    class _DynamicPluginSelector:
        def __init__(self) -> None:
            self.current_config = primary

        @property
        def active_provider_id(self) -> str:
            return "tokenrhythm"

        def next_fallback_after_failure(self, _exc: Exception) -> object:
            # Models introduced by a plugin failover hook after bootstrap have
            # no exact authority limit in the wrapper's private map.
            self.current_config = dynamically_injected
            return object()

    selector = _DynamicPluginSelector()
    wrapper = _SelectorFallbackProvider(object(), selector)
    wrapper.configure_fallback_deployment_limits([(primary, 64_000, 8_192)])
    wrapper.configure_fallback_limits(
        {("tokenrhythm", "shared/model"): (64_000, 8_192)}
    )
    original = ChatConfig(max_tokens=131_072)

    assert wrapper.fallback_after_invalid_response("dynamic plugin fallback") is True
    fallback_config = wrapper._config_for_active_leg(original)
    assert fallback_config.max_tokens == original.max_tokens
    assert fallback_config.model_capabilities.supports_vision is False


PRIMARY_MODEL = "routed-primary"
FALLBACK_MODEL = "fallback-secondary"


class _ChainProvider:
    """Scripted provider link: either fails pre-content or streams a reply."""

    provider_name = "openrouter"

    def __init__(self, model: str, *, fail: bool) -> None:
        self._model = model
        self._fail = fail

    async def chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        if self._fail:
            yield ErrorEvent(message="HTTP 404: model not found", code="404")
            return
        yield TextDeltaEvent(text=f"answer-from:{self._model}")
        yield DoneEvent(model=self._model, input_tokens=3, output_tokens=2)

    async def list_models(self) -> list[Any]:
        return []


class _ProjectingScriptProvider:
    """Script one physical leg while using its real wire projection."""

    def __init__(
        self,
        *,
        provider_name: str,
        wire_provider: OpenAIProvider,
        streams: list[list[Any]],
    ) -> None:
        self.provider_name = provider_name
        self._wire_provider = wire_provider
        self._streams = streams
        self.calls: list[dict[str, Any]] = []

    def project_final_request(
        self,
        messages: list[Message],
        tools: Any = None,
        config: ChatConfig | None = None,
        *,
        message_limit: int | None = None,
    ) -> Any:
        return self._wire_provider.project_final_request(
            messages,
            tools,
            config,
            message_limit=message_limit,
        )

    def project_message_count(
        self,
        messages: list[Message],
        config: ChatConfig | None = None,
        *,
        additional_messages: int = 0,
    ) -> Any:
        return self._wire_provider.project_message_count(
            messages,
            config,
            additional_messages=additional_messages,
        )

    def chat(
        self,
        messages: list[Message],
        tools: Any = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        canonical_before = [message.model_dump(mode="json") for message in messages]
        projection = self.project_final_request(messages, tools, config)
        canonical_after = [message.model_dump(mode="json") for message in messages]
        call_index = len(self.calls)
        self.calls.append(
            {
                "messages": messages,
                "canonical_before": canonical_before,
                "canonical_after": canonical_after,
                "payload": projection.payload,
            }
        )
        events = self._streams[call_index]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


class _ProjectingFallbackSelector:
    def __init__(
        self,
        primary: _ProjectingScriptProvider,
        fallback: _ProjectingScriptProvider,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.current_config = SimpleNamespace(
            provider="tokenrhythm",
            model="deepseek-v4-flash",
            base_url="https://tokenrhythm.studio/v1",
        )
        self._remaining_chain = [
            self.current_config,
            SimpleNamespace(
                provider="deepseek",
                model="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
            ),
        ]

    @property
    def active_provider_id(self) -> str:
        return str(self.current_config.provider)

    def remaining_chain(self) -> list[SimpleNamespace]:
        return list(self._remaining_chain)

    def next_fallback_after_failure(
        self,
        _exc: Exception,
    ) -> _ProjectingScriptProvider:
        self.current_config = self._remaining_chain[1]
        self._remaining_chain = self._remaining_chain[1:]
        return self.fallback


class _ChainSelector:
    """Two-link chain selector: primary fails, one fallback hop remains."""

    def __init__(self, *, primary_fails: bool) -> None:
        self._primary_fails = primary_fails
        self.current_config = SimpleNamespace(
            provider="openrouter",
            model=PRIMARY_MODEL,
        )
        self._remaining_chain = [
            self.current_config,
            SimpleNamespace(provider="openrouter", model=FALLBACK_MODEL),
        ]

    def clone(self) -> _ChainSelector:
        return self

    def override_model(self, model: str) -> None:
        if model == self.current_config.model:
            return
        previous_chain = list(self._remaining_chain)
        self.current_config = SimpleNamespace(provider="openrouter", model=model)
        self._remaining_chain = [self.current_config, *previous_chain]

    @property
    def active_provider_id(self) -> str:
        return str(self.current_config.provider)

    def remaining_chain(self) -> list[SimpleNamespace]:
        return list(self._remaining_chain)

    def resolve(self) -> _ChainProvider:
        return _ChainProvider(PRIMARY_MODEL, fail=self._primary_fails)

    def next_fallback_after_failure(self, exc: Exception) -> _ChainProvider:
        self.current_config = self._remaining_chain[1]
        self._remaining_chain = self._remaining_chain[1:]
        return _ChainProvider(FALLBACK_MODEL, fail=False)


async def test_physical_attempt_limit_prevents_selector_internal_fallback() -> None:
    selector = _ChainSelector(primary_fails=True)
    wrapper = _SelectorFallbackProvider(selector.resolve(), selector)

    events = [
        event
        async for event in wrapper.chat(
            [Message(role="user", content="summarize")],
            tools=[],
            config=ChatConfig(physical_attempt_limit=1),
        )
    ]

    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert selector.current_config.model == PRIMARY_MODEL


async def test_tokenrhythm_tool_reasoning_fallback_rebuilds_from_canonical_history() -> None:
    long_reasoning = "r" * 50_001
    primary = _ProjectingScriptProvider(
        provider_name="tokenrhythm",
        wire_provider=OpenAIProvider(
            api_key="synthetic-tokenrhythm-key",
            model="deepseek-v4-flash",
            base_url="https://tokenrhythm.studio/v1",
            provider_kind="tokenrhythm",
            provider_id="tokenrhythm",
        ),
        streams=[
            [
                ToolUseStartEvent(tool_use_id="tool-1", tool_name="echo"),
                ToolUseEndEvent(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "once"},
                ),
                DoneEvent(
                    stop_reason="tool_use",
                    input_tokens=3,
                    output_tokens=1,
                    reasoning_tokens=1,
                    reasoning_content=long_reasoning,
                ),
            ],
            [ErrorEvent(message="upstream unavailable", code="503")],
        ],
    )
    fallback = _ProjectingScriptProvider(
        provider_name="deepseek",
        wire_provider=OpenAIProvider(
            api_key="synthetic-deepseek-key",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            provider_kind="deepseek",
            provider_id="deepseek",
        ),
        streams=[
            [
                TextDeltaEvent(text="done"),
                DoneEvent(
                    stop_reason="stop",
                    model="deepseek-v4-flash",
                    input_tokens=4,
                    output_tokens=1,
                ),
            ]
        ],
    )
    selector = _ProjectingFallbackSelector(primary, fallback)
    provider = _SelectorFallbackProvider(primary, selector)
    tool_handler_calls = 0

    async def tool_handler(call: Any) -> ToolResult:
        nonlocal tool_handler_calls
        tool_handler_calls += 1
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool result",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            max_provider_retries=0,
            model_id="deepseek-v4-flash",
        ),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo once.",
                input_schema=ToolInputSchema(
                    properties={"value": {"type": "string"}},
                    required=["value"],
                ),
            )
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("run the tool once")]

    assert tool_handler_calls == 1
    assert len(primary.calls) == 2
    assert len(fallback.calls) == 1
    primary_post_tool = primary.calls[1]
    fallback_post_tool = fallback.calls[0]
    assert primary_post_tool["canonical_before"] == primary_post_tool["canonical_after"]
    assert fallback_post_tool["canonical_before"] == fallback_post_tool["canonical_after"]
    assert fallback_post_tool["canonical_before"] == primary_post_tool["canonical_before"]

    canonical_tool_call = next(
        message
        for message in primary_post_tool["messages"]
        if message.role == "assistant" and message.reasoning_content == long_reasoning
    )
    assert canonical_tool_call.reasoning_content == long_reasoning

    primary_wire_tool_call = next(
        message
        for message in primary_post_tool["payload"]["messages"]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    fallback_wire_tool_call = next(
        message
        for message in fallback_post_tool["payload"]["messages"]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert primary_wire_tool_call["reasoning_content"] == ""
    assert fallback_wire_tool_call["reasoning_content"] == long_reasoning
    assert any(event.kind == "done" and event.text == "done" for event in events)


async def test_unknown_primary_capability_defers_to_provider_image_validation() -> None:
    class _Provider:
        provider_name = "ensemble"

        def __init__(self) -> None:
            self.validation_calls = 0
            self.calls = 0

        def validate_chat_request(self, messages):
            del messages
            self.validation_calls += 1
            return ErrorEvent(
                message="ensemble rejects image input",
                code="ensemble_multimodal_unsupported",
            )

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            self.calls += 1
            yield DoneEvent(model="ensemble/model")

    class _Selector:
        active_provider_id = "ensemble"
        current_config = SimpleNamespace(provider="ensemble", model="ensemble/model")

    provider = _Provider()
    wrapper = _SelectorFallbackProvider(provider, _Selector())
    messages = [
        Message(
            role="user",
            content=[ContentBlockImage(media_type="image/png", data="c3ludGhldGlj")],
        )
    ]

    events = [event async for event in wrapper.chat(messages, config=ChatConfig())]

    assert provider.validation_calls == 1
    assert provider.calls == 0
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "ensemble_multimodal_unsupported"
    ]
    assert not any(isinstance(event, TextDeltaEvent) for event in events)


async def test_unknown_primary_uses_known_vision_fallback_for_image() -> None:
    class _Provider:
        provider_name = "openrouter"

        def __init__(self, *, fails: bool) -> None:
            self.fails = fails
            self.calls = 0

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            self.calls += 1
            if self.fails:
                yield ErrorEvent(message="primary unavailable", code="503")
                return
            yield TextDeltaEvent(text="image accepted by fallback")
            yield DoneEvent(model="vision-fallback")

    primary_config = SimpleNamespace(provider="openrouter", model="unknown-primary")
    fallback_config = SimpleNamespace(provider="openrouter", model="vision-fallback")

    class _Selector:
        active_provider_id = "openrouter"

        def __init__(self) -> None:
            self.primary = _Provider(fails=True)
            self.fallback = _Provider(fails=False)
            self.current_config = primary_config

        def next_fallback_after_failure(self, _exc: Exception) -> _Provider:
            self.current_config = fallback_config
            return self.fallback

    selector = _Selector()
    wrapper = _SelectorFallbackProvider(selector.primary, selector)
    wrapper.configure_fallback_deployment_limits(
        [(fallback_config, 0, 0, ModelCapabilities(supports_vision=True))]
    )
    wrapper.configure_fallback_deployment_vision_support(
        [(fallback_config, "supported")]
    )
    messages = [
        Message(
            role="user",
            content=[ContentBlockImage(media_type="image/png", data="c3ludGhldGlj")],
        )
    ]

    events = [event async for event in wrapper.chat(messages, config=ChatConfig())]

    assert selector.primary.calls == 1
    assert selector.fallback.calls == 1
    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "image accepted by fallback"
    ]


@pytest.mark.parametrize("fallback_vision_support", ["unsupported", "unknown"])
async def test_image_request_does_not_call_text_only_fallback(
    monkeypatch: Any,
    fallback_vision_support: str,
) -> None:
    class _Catalog:
        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "openrouter",
            base_url: str = "",
        ) -> ModelCapabilities:
            del model_id, provider_name, base_url
            return ModelCapabilities(supports_vision=False)

        def resolve_deployment_vision_support(self, *args, **kwargs):
            del args, kwargs
            return fallback_vision_support

    class _Provider:
        provider_name = "openrouter"

        def __init__(self, *, fails: bool) -> None:
            self.fails = fails
            self.calls = 0

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            self.calls += 1
            if self.fails:
                yield ErrorEvent(
                    message="rate limited",
                    code="429",
                    retry_after_s=901.0,
                )
                return
            yield TextDeltaEvent(text="fallback must not run")
            yield DoneEvent(model="text-fallback")

    class _Selector:
        def __init__(self) -> None:
            self.primary = _Provider(fails=True)
            self.fallback = _Provider(fails=False)
            self.current_config = SimpleNamespace(
                provider="openrouter",
                model="vision-primary",
                api_key="synthetic-shared-key",
                base_url="https://openrouter.ai/api/v1",
                org_id="",
                proxy="http://127.0.0.1:8118",
            )

        @property
        def active_provider_id(self) -> str:
            return "openrouter"

        def next_fallback_after_failure(self, _exc: Exception) -> _Provider:
            self.current_config = SimpleNamespace(
                provider="openrouter",
                model="text-fallback",
                api_key="synthetic-shared-key",
                base_url="https://openrouter.ai/api/v1",
                org_id="",
                proxy="http://127.0.0.1:8118",
            )
            return self.fallback

    monkeypatch.setattr("opensquilla.engine.runtime.shared_catalog", lambda: _Catalog())
    selector = _Selector()
    metadata: dict[str, Any] = {
        "routed_model": "vision-primary",
        "executed_provider": "openrouter",
        "executed_model": "vision-primary",
        "savings_pct": 17.0,
    }
    wrapper = _SelectorFallbackProvider(
        selector.primary,
        selector,
        turn_metadata=metadata,
    )
    messages = [
        Message(
            role="user",
            content=[ContentBlockImage(media_type="image/png", data="c3ludGhldGlj")],
        )
    ]

    events = [
        event
        async for event in wrapper.chat(
            messages,
            config=ChatConfig(
                model_capabilities=ModelCapabilities(supports_vision=True)
            ),
        )
    ]

    assert selector.primary.calls == 1
    assert selector.fallback.calls == 0
    assert not any(
        isinstance(event, ProviderActivityEvent)
        and event.phase in {"retry_wait", "fallback"}
        for event in events
    )
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        IMAGE_INPUT_UNSUPPORTED_CODE
    ]
    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert metadata["image_input_mode"] == "rejected"
    assert metadata["image_input_reason"] == (
        "capability_unknown"
        if fallback_vision_support == "unknown"
        else "model_vision_unsupported"
    )
    assert metadata["image_input_stage"] == "fallback"
    assert metadata["routed_model"] == "vision-primary"
    assert metadata["executed_model"] == "vision-primary"
    assert "router_fallback_hops" not in metadata
    assert "router_fallback_reason" not in metadata
    assert metadata["savings_pct"] == 17.0
    assert [leg["model"] for leg in metadata["execution_legs"]] == [
        "vision-primary"
    ]


async def test_invalid_response_fallback_preserves_empty_response_without_vision_candidate(
    monkeypatch: Any,
) -> None:
    class _Catalog:
        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "openrouter",
            base_url: str = "",
        ) -> ModelCapabilities:
            del model_id, provider_name, base_url
            return ModelCapabilities(supports_vision=False)

        def resolve_deployment_vision_support(self, *args, **kwargs):
            del args, kwargs
            return "unsupported"

    class _Provider:
        provider_name = "openrouter"

        def __init__(self, *, empty: bool) -> None:
            self.empty = empty
            self.calls = 0
            self.validation_calls = 0

        def validate_chat_request(self, messages):
            del messages
            self.validation_calls += 1
            if not self.empty:
                return ErrorEvent(
                    message="text-only fallback rejects image input",
                    code="unsupported_image",
                )
            return None

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            self.calls += 1
            if self.empty:
                yield DoneEvent(
                    stop_reason="length",
                    input_tokens=3,
                    output_tokens=1024,
                    reasoning_tokens=1023,
                    reasoning_content="internal reasoning",
                    model="vision-primary",
                )
                return
            yield TextDeltaEvent(text="fallback must not run")
            yield DoneEvent(model="text-fallback")

    class _Selector:
        def __init__(self) -> None:
            self.primary = _Provider(empty=True)
            self.fallback = _Provider(empty=False)
            self.primary_config = SimpleNamespace(
                provider="openrouter",
                model="vision-primary",
                base_url="",
            )
            self.fallback_config = SimpleNamespace(
                provider="openrouter",
                model="text-fallback",
                base_url="",
            )
            self.current_config = self.primary_config
            self.fallback_builds = 0

        @property
        def active_provider_id(self) -> str:
            return "openrouter"

        def remaining_chain(self):
            return [self.primary_config, self.fallback_config]

        def next_fallback_after_failure_matching(
            self,
            _exc: Exception,
            *,
            predicate,
        ) -> _Provider:
            matching = [cfg for cfg in [self.fallback_config] if predicate(cfg)]
            if not matching:
                raise IndexError("No fallback chain available")
            self.fallback_builds += 1
            self.current_config = self.fallback_config
            return self.fallback

        def next_fallback_after_failure(self, _exc: Exception) -> _Provider:
            self.current_config = self.fallback_config
            return self.fallback

    monkeypatch.setattr("opensquilla.engine.runtime.shared_catalog", lambda: _Catalog())
    selector = _Selector()
    metadata: dict[str, Any] = {
        "routed_model": "vision-primary",
        "executed_provider": "openrouter",
        "executed_model": "vision-primary",
        "savings_pct": 17.0,
    }
    wrapper = _SelectorFallbackProvider(
        selector.primary,
        selector,
        turn_metadata=metadata,
    )
    wrapper.configure_fallback_deployment_vision_support(
        [(selector.fallback_config, "unsupported")]
    )
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(
            max_provider_retries=0,
            max_turn_llm_calls=1,
            model_id="vision-primary",
            model_capabilities=ModelCapabilities(supports_vision=True),
            model_vision_support="supported",
        ),
    )
    image_message = Message(
        role="user",
        content=[ContentBlockImage(media_type="image/png", data="c3ludGhldGlj")],
    )

    events = [
        event
        async for event in agent.run_turn(
            "Describe the image.",
            extra_messages=[image_message],
        )
    ]

    assert selector.primary.calls == 1
    assert selector.fallback.calls == 0
    assert selector.fallback_builds == 0
    assert selector.primary.validation_calls == 2
    assert selector.fallback.validation_calls == 0
    error = next(event for event in events if getattr(event, "kind", "") == "error")
    assert getattr(error, "code", "") == "empty_response"
    assert "llm.max_tokens" in getattr(error, "message", "")
    assert not any(
        getattr(event, "kind", "") == "error"
        and getattr(event, "code", "") == IMAGE_INPUT_UNSUPPORTED_CODE
        for event in events
    )
    done_events = [event for event in events if isinstance(event, EngineDoneEvent)]
    if done_events:
        assert done_events[-1].input_tokens == 3
        assert done_events[-1].output_tokens == 1024
        assert done_events[-1].reasoning_tokens == 1023
    assert "image_input_mode" not in metadata
    assert "image_input_reason" not in metadata
    assert "image_input_stage" not in metadata
    assert metadata["routed_model"] == "vision-primary"
    assert metadata["executed_model"] == "vision-primary"
    assert "router_fallback_hops" not in metadata
    assert "router_fallback_reason" not in metadata
    assert metadata["savings_pct"] == 17.0
    assert [leg["model"] for leg in metadata["execution_legs"]] == [
        "vision-primary"
    ]


async def test_invalid_response_fallback_skips_text_leg_for_vision_fallback() -> None:
    primary_config = SimpleNamespace(provider="openrouter", model="vision-primary")
    text_config = SimpleNamespace(provider="openrouter", model="text-fallback")
    vision_config = SimpleNamespace(provider="openrouter", model="vision-fallback")

    class _Provider:
        provider_name = "openrouter"

        def __init__(self, model: str, *, empty: bool = False) -> None:
            self.model = model
            self.empty = empty
            self.calls = 0

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            self.calls += 1
            if self.empty:
                yield DoneEvent(
                    stop_reason="length",
                    input_tokens=3,
                    output_tokens=1024,
                    reasoning_tokens=1023,
                    reasoning_content="internal reasoning",
                    model=self.model,
                )
                return
            yield TextDeltaEvent(text=f"reply from {self.model}")
            yield DoneEvent(model=self.model)

    providers = {
        primary_config.model: _Provider(primary_config.model, empty=True),
        text_config.model: _Provider(text_config.model),
        vision_config.model: _Provider(vision_config.model),
    }

    class _Selector:
        active_provider_id = "openrouter"

        def __init__(self) -> None:
            self._chain = [primary_config, text_config, vision_config]
            self._index = 0

        @property
        def current_config(self):
            return self._chain[self._index]

        def remaining_chain(self):
            return list(self._chain[self._index :])

        def next_fallback_after_failure_matching(self, _exc, *, predicate):
            matching = [cfg for cfg in self._chain[self._index + 1 :] if predicate(cfg)]
            if not matching:
                raise IndexError("No fallback chain available")
            provider = providers[matching[0].model]
            self._chain = [self.current_config, *matching]
            self._index = 1
            return provider

    selector = _Selector()
    metadata: dict[str, Any] = {
        "routed_model": "vision-primary",
        "executed_provider": "openrouter",
        "executed_model": "vision-primary",
    }
    wrapper = _SelectorFallbackProvider(
        providers[primary_config.model],
        selector,
        turn_metadata=metadata,
    )
    wrapper.configure_fallback_deployment_limits(
        [
            (text_config, 0, 0, ModelCapabilities(supports_vision=False)),
            (vision_config, 0, 0, ModelCapabilities(supports_vision=True)),
        ]
    )
    wrapper.configure_fallback_deployment_vision_support(
        [
            (text_config, "unsupported"),
            (vision_config, "supported"),
        ]
    )
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(
            max_provider_retries=0,
            max_turn_llm_calls=2,
            model_id="vision-primary",
            model_capabilities=ModelCapabilities(supports_vision=True),
            model_vision_support="supported",
        ),
    )
    image_message = Message(
        role="user",
        content=[ContentBlockImage(media_type="image/png", data="c3ludGhldGlj")],
    )

    events = [
        event
        async for event in agent.run_turn(
            "Describe the image.",
            extra_messages=[image_message],
        )
    ]

    assert providers[primary_config.model].calls == 1
    assert providers[text_config.model].calls == 0
    assert providers[vision_config.model].calls == 1
    assert selector.current_config is vision_config
    assert any(
        getattr(event, "kind", "") == "text_delta"
        and getattr(event, "text", "") == "reply from vision-fallback"
        for event in events
    )
    assert not any(
        getattr(event, "kind", "") == "error"
        and getattr(event, "code", "") == IMAGE_INPUT_UNSUPPORTED_CODE
        for event in events
    )
    assert metadata["router_fallback_hops"] == 1
    assert [leg["model"] for leg in metadata["execution_legs"]] == [
        "vision-primary",
        "vision-fallback",
    ]


async def test_local_admission_failure_escalates_to_larger_authorized_leg(
    monkeypatch: Any,
) -> None:
    class _AdmissionProvider:
        provider_name = "openai"

        def __init__(self, model: str, *, fits: bool) -> None:
            self.model = model
            self.fits = fits
            self.network_calls = 0

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            if not self.fits:
                yield ErrorEvent(
                    message='{"reason":"provider_request_budget_exhausted"}',
                    code="provider_request_budget_exhausted",
                )
                return
            self.network_calls += 1
            yield TextDeltaEvent(text="fallback answer")
            yield DoneEvent(model=self.model)

    class _AdmissionSelector:
        def __init__(self) -> None:
            self.current_config = SimpleNamespace(
                provider="openai",
                model="small-model",
            )
            self.primary = _AdmissionProvider("small-model", fits=False)
            self.fallback = _AdmissionProvider("large-model", fits=True)

        @property
        def active_provider_id(self) -> str:
            return "openai"

        def remaining_chain(self) -> list[SimpleNamespace]:
            return [
                self.current_config,
                SimpleNamespace(provider="openai", model="large-model"),
            ]

        def next_fallback(self) -> _AdmissionProvider:
            self.current_config = SimpleNamespace(
                provider="openai",
                model="large-model",
            )
            return self.fallback

    def _resolve_context_window(
        _catalog: Any,
        model: str,
        *,
        global_override: int = 0,
        **_kwargs: Any,
    ) -> tuple[int, str]:
        assert global_override == 0
        return (4_000, "catalog") if model == "small-model" else (32_000, "catalog")

    monkeypatch.setattr(
        "opensquilla.engine.runtime.resolve_effective_context_window",
        _resolve_context_window,
    )
    selector = _AdmissionSelector()
    metadata: dict[str, object] = {"routed_model": "small-model"}
    wrapper = _SelectorFallbackProvider(
        selector.primary,
        selector,
        turn_metadata=metadata,
    )

    events = [
        event
        async for event in wrapper.chat(
            [Message(role="user", content="large request")],
            tools=[],
            config=ChatConfig(),
        )
    ]

    assert selector.primary.network_calls == 0
    assert selector.fallback.network_calls == 1
    assert any(isinstance(event, TextDeltaEvent) for event in events)
    assert metadata["executed_model"] == "large-model"
    assert metadata["router_fallback_reason"] == "local_admission_escalation"


def _routed_pipeline_fake(routed_model: str) -> Any:
    async def routed_pipeline(
        self: TurnRunner,
        message: str,
        session_key: str,
        provider: Any,
        cloned_selector: Any,
        tool_defs: list[Any],
        base_prompt: str | tuple[str, str],
        attachments: list[dict[str, Any]],
        **_: Any,
    ) -> tuple[TurnContext, Any]:
        selector_execution_chain = [
            {
                "provider": str(candidate.provider),
                "model": str(candidate.model),
            }
            for candidate in cloned_selector.remaining_chain()
        ]
        return (
            TurnContext(
                message=message,
                session_key=session_key,
                config=self._config,
                provider=provider,
                model=routed_model,
                tool_defs=tool_defs,
                system_prompt=base_prompt,
                attachments=attachments,
                metadata={
                    "routed_tier": "c1",
                    "routed_model": routed_model,
                    "baseline_model": "baseline-expensive",
                    "routing_source": "router",
                    "routing_confidence": 0.9,
                    "savings_pct": 41.0,
                    "savings_max_price_per_m": 3.0,
                    "savings_routed_price_per_m": 0.5,
                    "selector_execution_chain": selector_execution_chain,
                },
            ),
            provider,
        )

    return routed_pipeline


async def _run_turn_events(
    monkeypatch: Any,
    *,
    primary_fails: bool,
    pending_input_provider: ListPendingInputProvider | None = None,
) -> list[Any]:
    monkeypatch.setattr(TurnRunner, "_run_pipeline", _routed_pipeline_fake(PRIMARY_MODEL))
    runner = TurnRunner(provider_selector=_ChainSelector(primary_fails=primary_fails))
    return [
        event
        async for event in runner.run(
            "hi",
            "agent:main:selector-fallback-e2e",
            tool_context=ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
            history_has_persisted_user=False,
            no_memory_capture=True,
            pending_input_provider=pending_input_provider,
        )
    ]


def test_model_override_snapshots_selector_execution_candidates() -> None:
    selector = _ChainSelector(primary_fails=False)
    metadata: dict[str, object] = {}

    apply_model_override(
        selector,
        PRIMARY_MODEL,
        turn_metadata=metadata,
        realign_routed_model=False,
    )

    assert metadata["selector_execution_chain"] == [
        {"provider": "openrouter", "model": PRIMARY_MODEL},
        {"provider": "openrouter", "model": FALLBACK_MODEL},
    ]


async def test_precontent_fallback_keeps_one_route_decision_and_appends_execution_leg(
    monkeypatch: Any,
) -> None:
    events = await _run_turn_events(monkeypatch, primary_fails=True)

    router_events = [event for event in events if isinstance(event, RouterDecisionEvent)]
    assert len(router_events) == 1
    assert router_events[0].model == PRIMARY_MODEL
    assert router_events[0].source == "router"
    assert router_events[0].fallback is False

    done_events = [event for event in events if isinstance(event, EngineDoneEvent)]
    assert len(done_events) == 1
    done = done_events[0]
    assert done.model == FALLBACK_MODEL
    assert done.routed_model == FALLBACK_MODEL
    assert done.route_plan is not None
    assert done.route_plan["model"] == PRIMARY_MODEL
    assert [leg["kind"] for leg in done.execution_legs] == [
        "primary",
        "provider_fallback",
    ]
    assert [leg["model"] for leg in done.execution_legs] == [
        PRIMARY_MODEL,
        FALLBACK_MODEL,
    ]


async def test_same_turn_pending_input_preserves_route_plan_and_model(
    monkeypatch: Any,
) -> None:
    pending = ListPendingInputProvider()
    pending.append("continue with this constraint")

    events = await _run_turn_events(
        monkeypatch,
        primary_fails=False,
        pending_input_provider=pending,
    )

    router_events = [event for event in events if isinstance(event, RouterDecisionEvent)]
    assert len(router_events) == 1
    assert router_events[0].model == PRIMARY_MODEL
    done = next(event for event in events if isinstance(event, EngineDoneEvent))
    assert done.route_plan is not None
    assert done.route_plan["model"] == PRIMARY_MODEL
    assert len(done.execution_legs) == 2
    assert {leg["model"] for leg in done.execution_legs} == {PRIMARY_MODEL}
    assert {leg["plan_id"] for leg in done.execution_legs} == {
        done.route_plan["plan_id"]
    }


async def test_same_turn_pending_input_applies_after_precontent_selector_fallback(
    monkeypatch: Any,
) -> None:
    pending = ListPendingInputProvider()
    pending.append("replace the original constraint")

    events = await _run_turn_events(
        monkeypatch,
        primary_fails=True,
        pending_input_provider=pending,
    )

    assert len(pending.applications) == 1
    assert pending.applications[0].texts == ("replace the original constraint",)
    assert pending.applications[0].model_call_id == "2.0"
    done = next(event for event in events if isinstance(event, EngineDoneEvent))
    assert done.route_plan is not None
    assert done.route_plan["model"] == PRIMARY_MODEL
    assert {
        (item["provider"], item["model"])
        for item in done.route_plan["fallback_chain"]
    } >= {("openrouter", FALLBACK_MODEL)}
    assert done.model == FALLBACK_MODEL


async def test_turn_without_fallback_hop_emits_exactly_one_router_decision(
    monkeypatch: Any,
) -> None:
    events = await _run_turn_events(monkeypatch, primary_fails=False)

    router_events = [event for event in events if isinstance(event, RouterDecisionEvent)]
    assert len(router_events) == 1
    assert router_events[0].model == PRIMARY_MODEL
    assert router_events[0].source == "router"
    assert router_events[0].fallback is False

    done_events = [event for event in events if isinstance(event, EngineDoneEvent)]
    assert len(done_events) == 1
    assert done_events[0].model == PRIMARY_MODEL


async def test_blocked_cross_provider_route_passes_primary_model_to_agent_request(
    monkeypatch: Any,
) -> None:
    foreign_model = "doubao-seed-1-6-251015"

    async def blocked_pipeline(
        self: TurnRunner,
        message: str,
        session_key: str,
        provider: Any,
        cloned_selector: Any,
        tool_defs: list[Any],
        base_prompt: str | tuple[str, str],
        attachments: list[dict[str, Any]],
        **_: Any,
    ) -> tuple[TurnContext, Any]:
        return (
            TurnContext(
                message=message,
                session_key=session_key,
                config=self._config,
                provider=provider,
                model=foreign_model,
                tool_defs=tool_defs,
                system_prompt=base_prompt,
                attachments=attachments,
                metadata={
                    "routed_tier": "c0",
                    "routed_provider": "volcengine",
                    "routed_model": foreign_model,
                    "routing_source": "router",
                    "routing_applied": True,
                    "routed_provider_blocked": "missing_credential",
                    "routed_provider_fallback_reason": "missing_credential",
                    "routed_provider_fallback_provider": "openrouter",
                    "routed_provider_fallback_model": PRIMARY_MODEL,
                    "executed_provider": "openrouter",
                    "executed_model": PRIMARY_MODEL,
                },
            ),
            provider,
        )

    observed_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(TurnRunner, "_run_pipeline", blocked_pipeline)
    runner = TurnRunner(
        provider_selector=_ChainSelector(primary_fails=False),
        provider_call_observer=lambda **payload: observed_calls.append(payload),
    )

    events = [
        event
        async for event in runner.run(
            "hi",
            "agent:main:blocked-cross-provider",
            tool_context=ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
            history_has_persisted_user=False,
            no_memory_capture=True,
        )
    ]

    [router_event] = [
        event for event in events if isinstance(event, RouterDecisionEvent)
    ]
    assert router_event.model == foreign_model
    assert observed_calls
    assert observed_calls[0]["provider_id"] == "openrouter"
    assert observed_calls[0]["model"] == PRIMARY_MODEL

    [done_event] = [event for event in events if isinstance(event, EngineDoneEvent)]
    assert done_event.model == PRIMARY_MODEL
    assert done_event.routed_model == foreign_model
