"""Offline failure-injection tests for the provider retry/rotate/fallback chain.

All scenarios run without network or credentials: a ``FailureInjector``
scripts classified provider failures in front of a local fake provider, so
each hop of the chain (same-model retry, terminal surface, fallback-model
rotation) is asserted from synthetic outcomes only.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

import pytest
import structlog.testing

from opensquilla.engine import Agent, AgentConfig
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider.failures import (
    ProviderFailureKind,
    ProviderRecoveryAction,
    classify_provider_error,
    decide_recovery_action,
)
from opensquilla.provider.types import ChatConfig, FailureInjector, Message, synthetic_failure_event


class _FakeProvider:
    """Offline provider double; registered name so failures classify by family."""

    provider_name = "openai"

    def __init__(self, streams: list[list[Any]] | None = None) -> None:
        self.streams = streams or [
            [ProviderText(text="ok"), ProviderDone(stop_reason="stop", input_tokens=3)]
        ]
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        index = len(self.calls)
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


def _agent(
    provider: _FakeProvider,
    injector: FailureInjector | None = None,
    **config_kwargs: Any,
) -> Agent:
    config_kwargs.setdefault("retry_base_backoff_ms", 0)
    config_kwargs.setdefault("retry_max_backoff_ms", 0)
    return Agent(
        provider=provider,
        config=AgentConfig(**config_kwargs),
        failure_injector=injector,
    )


# ---------------------------------------------------------------------------
# Retry hop: retryable injected failures back off and retry on the same model
# ---------------------------------------------------------------------------


async def test_retryable_failures_then_success_drives_each_retry_hop() -> None:
    provider = _FakeProvider()
    injector = FailureInjector(
        script=[
            ProviderFailureKind.RATE_LIMITED,
            ProviderFailureKind.PROVIDER_OVERLOADED,
            "succeed",
        ]
    )
    agent = _agent(provider, injector)

    with structlog.testing.capture_logs() as captured:
        events = [event async for event in agent.run_turn("hello")]

    # Hop 1 + 2: retried, never surfaced; hop 3: delegated to the provider.
    assert not any(event.kind == "error" for event in events)
    done = next(event for event in events if event.kind == "done")
    assert done.text == "ok"
    assert len(provider.calls) == 1  # only the "succeed" outcome reached it
    assert injector.consumed == [
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.PROVIDER_OVERLOADED,
        "succeed",
    ]
    retry_logs = [entry for entry in captured if entry.get("event") == "provider.retry"]
    assert [entry["kind"] for entry in retry_logs] == [
        ProviderFailureKind.RATE_LIMITED.value,
        ProviderFailureKind.PROVIDER_OVERLOADED.value,
    ]


async def test_exhausted_script_delegates_every_further_call() -> None:
    injector = FailureInjector(script=[])
    assert injector.next_outcome() == "succeed"
    assert injector.consumed == []

    provider = _FakeProvider()
    agent = _agent(provider, FailureInjector(script=[ProviderFailureKind.RATE_LIMITED]))

    events = [event async for event in agent.run_turn("hello")]

    # One injected failure, then the exhausted script passes through.
    assert not any(event.kind == "error" for event in events)
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# Surface hop: non-retryable failures terminate without touching the provider
# ---------------------------------------------------------------------------


async def test_auth_invalid_surfaces_without_retry() -> None:
    provider = _FakeProvider()
    injector = FailureInjector(script=[ProviderFailureKind.AUTH_INVALID])
    agent = _agent(provider, injector)

    with structlog.testing.capture_logs() as captured:
        events = [event async for event in agent.run_turn("hello")]

    error = next(event for event in events if event.kind == "error")
    assert error.code == "401"
    assert error.message == "The model provider rejected the configured credentials."
    assert error.failure_kind == ProviderFailureKind.AUTH_INVALID.value
    assert provider.calls == []  # surfaced before any provider contact
    assert injector.consumed == [ProviderFailureKind.AUTH_INVALID]
    assert not any(entry.get("event") == "provider.retry" for entry in captured)


async def test_retry_exhaustion_surfaces_terminal_error() -> None:
    provider = _FakeProvider()
    injector = FailureInjector(
        script=[ProviderFailureKind.RATE_LIMITED, ProviderFailureKind.RATE_LIMITED]
    )
    agent = _agent(provider, injector, max_provider_retries=1)

    events = [event async for event in agent.run_turn("hello")]

    error = next(event for event in events if event.kind == "error")
    assert error.code == "429"
    assert provider.calls == []
    assert len(injector.consumed) == 2


# ---------------------------------------------------------------------------
# Rotate + fallback hops: recovery decisions and fallback-model ordering
# ---------------------------------------------------------------------------


def test_injected_kinds_map_to_rotation_decisions() -> None:
    # The pre-content rotate hop keys off decide_recovery_action; pin the
    # decisions the injector's synthetic failures produce.
    assert (
        decide_recovery_action(ProviderFailureKind.RATE_LIMITED)
        is ProviderRecoveryAction.FALLBACK_PROVIDER
    )
    assert (
        decide_recovery_action(ProviderFailureKind.PROVIDER_OVERLOADED)
        is ProviderRecoveryAction.RETRY_THEN_FALLBACK
    )
    assert (
        decide_recovery_action(ProviderFailureKind.AUTH_INVALID)
        is ProviderRecoveryAction.FAIL_CONFIG
    )


def test_fallback_model_rotation_order_after_exhaustion() -> None:
    from opensquilla.engine.fallback import FallbackPolicy

    policy = FallbackPolicy(
        max_retries=1,
        fallback_models=["model-primary", "model-fb-1", "model-fb-2"],
    )
    # Retry budget exhausts...
    assert policy.should_retry(ProviderFailureKind.RATE_LIMITED, 0) is True
    assert policy.should_retry(ProviderFailureKind.RATE_LIMITED, 1) is False
    # ...then the fallback chain rotates in declared order and terminates.
    assert policy.get_fallback_model("model-primary") == "model-fb-1"
    assert policy.get_fallback_model("model-fb-1") == "model-fb-2"
    assert policy.get_fallback_model("model-fb-2") is None
    # A model outside the chain enters at the head.
    assert policy.get_fallback_model("model-unlisted") == "model-primary"


# ---------------------------------------------------------------------------
# Exception outcomes and synthetic-shape parity
# ---------------------------------------------------------------------------


async def test_exception_outcome_raises_from_the_injected_stream() -> None:
    injector = FailureInjector(script=[TimeoutError("injected transport timeout")])
    provider = _FakeProvider()

    stream = injector.chat(provider, [Message(role="user", content="hello")])
    with pytest.raises(TimeoutError, match="injected transport timeout"):
        await anext(stream)
    assert provider.calls == []


@pytest.mark.parametrize("kind", list(ProviderFailureKind))
def test_synthetic_failure_shapes_round_trip_classification(
    kind: ProviderFailureKind,
) -> None:
    event = synthetic_failure_event(kind)
    status_code = int(event.code) if str(event.code).isdigit() else None
    assert (
        classify_provider_error(
            "openai", status_code, raw_code=event.code, message=event.message
        )
        is kind
    )


# ---------------------------------------------------------------------------
# Production inertness: no injector -> path unchanged
# ---------------------------------------------------------------------------


def test_failure_injector_defaults_to_none_on_the_agent() -> None:
    parameter = inspect.signature(Agent.__init__).parameters["failure_injector"]
    assert parameter.default is None
    assert Agent(provider=_FakeProvider())._failure_injector is None


async def test_no_injector_and_empty_script_produce_identical_turns() -> None:
    plain_provider = _FakeProvider()
    injected_provider = _FakeProvider()
    plain_agent = _agent(plain_provider, injector=None)
    injected_agent = _agent(injected_provider, injector=FailureInjector(script=[]))

    plain_events = [event async for event in plain_agent.run_turn("hello")]
    injected_events = [event async for event in injected_agent.run_turn("hello")]

    assert [(e.kind, getattr(e, "text", None)) for e in plain_events] == [
        (e.kind, getattr(e, "text", None)) for e in injected_events
    ]
    assert len(plain_provider.calls) == len(injected_provider.calls) == 1
    # The delegated call is argument-identical to the direct one.
    assert plain_provider.calls[0]["messages"] == injected_provider.calls[0]["messages"]
    assert plain_provider.calls[0]["tools"] == injected_provider.calls[0]["tools"]
    plain_config = plain_provider.calls[0]["config"]
    injected_config = injected_provider.calls[0]["config"]
    assert plain_config.turn_deadline_at_monotonic is not None
    assert injected_config.turn_deadline_at_monotonic is not None
    # Each turn owns a distinct monotonic deadline. Normalize only that
    # runtime clock value; every other field, including other excluded runtime
    # fields, must remain argument-identical.
    assert plain_config.model_copy(
        update={"turn_deadline_at_monotonic": None}
    ) == injected_config.model_copy(update={"turn_deadline_at_monotonic": None})
