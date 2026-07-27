"""Failover must realign routed_model telemetry to the model that runs.

Same invariant the explicit-model override realignment enforces
(prompt_assembler_stage, commit 966df982): ``metadata["routed_model"]`` is
read by RouterDecisionEvent and comprehensive-savings pricing, so after a
selector failover it must name the fallback model, and route-savings figures
computed for the abandoned model no longer apply.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.runtime import TurnRunner, _SelectorFallbackProvider
from opensquilla.engine.types import DoneEvent as EngineDoneEvent
from opensquilla.engine.types import RouterDecisionEvent
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    ProviderRequestCorrelation,
    TextDeltaEvent,
)
from opensquilla.tools.types import CallerKind, ToolContext


class _StubSelector:
    def __init__(self, fallback_model: str) -> None:
        self._fallback_model = fallback_model

    def next_fallback_after_failure(self, exc: Exception) -> object:
        return object()

    @property
    def current_config(self) -> SimpleNamespace:
        return SimpleNamespace(provider="fallback-provider", model=self._fallback_model)


def test_fallback_realigns_routed_model_and_drops_savings() -> None:
    metadata: dict[str, object] = {
        "routed_model": "expensive/model",
        "savings_pct": 12.5,
        "savings_max_price_per_m": 3.0,
        "savings_routed_price_per_m": 0.5,
    }
    wrapper = _SelectorFallbackProvider(
        object(),
        _StubSelector("cheap/fallback"),
        turn_metadata=metadata,
    )

    assert wrapper.fallback_after_invalid_response("upstream 503") is True

    assert metadata["routed_model"] == "cheap/fallback"
    assert metadata["executed_provider"] == "fallback-provider"
    assert metadata["executed_model"] == "cheap/fallback"
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


class _ChainSelector:
    """Two-link chain selector: primary fails, one fallback hop remains."""

    def __init__(self, *, primary_fails: bool) -> None:
        self._primary_fails = primary_fails
        self.current_config = SimpleNamespace(model=PRIMARY_MODEL)

    def clone(self) -> _ChainSelector:
        return self

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)

    def resolve(self) -> _ChainProvider:
        return _ChainProvider(PRIMARY_MODEL, fail=self._primary_fails)

    def next_fallback_after_failure(self, exc: Exception) -> _ChainProvider:
        self.current_config = SimpleNamespace(model=FALLBACK_MODEL)
        return _ChainProvider(FALLBACK_MODEL, fail=False)


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
                },
            ),
            provider,
        )

    return routed_pipeline


async def _run_turn_events(
    monkeypatch: Any,
    *,
    primary_fails: bool,
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
        )
    ]


async def test_precontent_fallback_emits_corrective_router_decision_before_done(
    monkeypatch: Any,
) -> None:
    events = await _run_turn_events(monkeypatch, primary_fails=True)

    router_events = [event for event in events if isinstance(event, RouterDecisionEvent)]
    assert len(router_events) == 2

    initial, corrective = router_events
    assert initial.model == PRIMARY_MODEL
    assert initial.source == "router"
    assert initial.fallback is False

    assert corrective.model == FALLBACK_MODEL
    assert corrective.source == "fallback"
    assert corrective.fallback is True
    assert corrective.savings_pct == 0.0

    done_events = [event for event in events if isinstance(event, EngineDoneEvent)]
    assert len(done_events) == 1
    assert done_events[0].model == FALLBACK_MODEL
    assert done_events[0].routed_model == FALLBACK_MODEL
    assert events.index(corrective) < events.index(done_events[0])


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
