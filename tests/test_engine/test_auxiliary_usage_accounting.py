from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageCallResult,
    UsageCallStart,
    UsageExecutionContext,
    account_provider_stream,
    bind_usage_accounting_scope,
)
from opensquilla.memory.dream.runner import _run_complete
from opensquilla.memory.session_flush import ProviderCompletionError, _provider_complete
from opensquilla.onboarding.probe import probe_llm_provider
from opensquilla.provider.types import DoneEvent, ErrorEvent, Message, TextDeltaEvent
from opensquilla.tools.builtin.media import _complete_from_stream


@dataclass
class _RecordingSink:
    started: list[UsageCallStart] = field(default_factory=list)
    finalized: list[tuple[UsageCallStart, UsageCallResult]] = field(default_factory=list)
    unknown: list[tuple[UsageCallStart, str]] = field(default_factory=list)

    async def start(self, call: UsageCallStart) -> None:
        self.started.append(call)

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        self.finalized.append((call, result))

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))


def _scope(sink: _RecordingSink) -> UsageAccountingScope:
    return UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id="aux-execution",
            agent_run_id="aux-run",
            turn_id="aux-turn",
            session_id="session-1",
            session_epoch=2,
            agent_id="main",
            run_kind="agent",
        ),
    )


class _StreamProvider:
    provider_name = "test-provider"
    model = "test-model"

    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.calls = 0

    def chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        del args, kwargs
        self.calls += 1
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        for event in self.events:
            yield event


async def _session_flush_completion(provider: Any) -> str:
    result = await _provider_complete(
        provider,
        messages=[Message(role="user", content="hello")],
        max_tokens=32,
    )
    return result.text


async def _dream_completion(provider: Any) -> str:
    return await _run_complete(
        provider,
        [Message(role="user", content="hello")],
        32,
    )


async def _media_completion(provider: Any) -> str:
    return await _complete_from_stream(
        provider,
        [Message(role="user", content="hello")],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner",
    [_session_flush_completion, _dream_completion, _media_completion],
)
async def test_auxiliary_chat_done_is_accounted_once(
    runner: Callable[[Any], Awaitable[str]],
) -> None:
    sink = _RecordingSink()
    provider = _StreamProvider(
        [
            TextDeltaEvent(text="ok"),
            DoneEvent(
                input_tokens=5,
                output_tokens=2,
                billed_cost=0.000000123,
                cost_source="provider_billed",
                model="test-model",
            ),
        ]
    )

    with bind_usage_accounting_scope(_scope(sink)):
        assert await runner(provider) == "ok"

    assert provider.calls == 1
    assert len(sink.started) == 1
    assert len(sink.finalized) == 1
    assert sink.finalized[0][0].event_id == sink.started[0].event_id
    assert sink.finalized[0][1].billed_cost_nanos == 123
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_auxiliary_chat_error_is_closed_as_unknown_before_returning() -> None:
    sink = _RecordingSink()
    provider = _StreamProvider([ErrorEvent(message="denied", code="401")])

    with bind_usage_accounting_scope(_scope(sink)):
        with pytest.raises(ProviderCompletionError, match="denied"):
            await _session_flush_completion(provider)

    assert len(sink.started) == 1
    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, "provider_error:401")
    ]


class _BlockingProvider:
    provider_name = "test-provider"
    model = "test-model"

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    def chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        del args, kwargs
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        self.entered.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - preserve the async-generator shape
            yield None


@pytest.mark.asyncio
async def test_auxiliary_chat_cancellation_is_closed_as_unknown() -> None:
    sink = _RecordingSink()
    provider = _BlockingProvider()

    async def run() -> None:
        with bind_usage_accounting_scope(_scope(sink)):
            await _media_completion(provider)

    task = asyncio.create_task(run())
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sink.started) == 1
    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, "cancelled")
    ]


class _AccountingSelectorWrapper:
    """Minimal selector-like wrapper that owns its physical-leg accounting."""

    accounts_physical_usage = True
    provider_name = "selector"
    model = "selector-model"

    def __init__(self, provider: _StreamProvider) -> None:
        self.provider = provider

    def chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        return account_provider_stream(
            lambda: self.provider.chat(*args, **kwargs),
            provider=self.provider.provider_name,
            model=self.provider.model,
        )


@pytest.mark.asyncio
async def test_auxiliary_chat_does_not_double_wrap_selector_accounting() -> None:
    sink = _RecordingSink()
    physical = _StreamProvider(
        [TextDeltaEvent(text="ok"), DoneEvent(input_tokens=3, output_tokens=1)]
    )
    selector = _AccountingSelectorWrapper(physical)

    with bind_usage_accounting_scope(_scope(sink)):
        assert await _dream_completion(selector) == "ok"

    assert physical.calls == 1
    assert len(sink.started) == 1
    assert len(sink.finalized) == 1
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_auxiliary_chat_closes_selector_owned_error_accounting() -> None:
    sink = _RecordingSink()
    selector = _AccountingSelectorWrapper(
        _StreamProvider([ErrorEvent(message="busy", code="503")])
    )

    with bind_usage_accounting_scope(_scope(sink)):
        with pytest.raises(ProviderCompletionError, match="busy"):
            await _session_flush_completion(selector)

    assert len(sink.started) == 1
    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, "provider_error:503")
    ]


class _MetadataMustNotBeReadProvider(_StreamProvider):
    def provider_metadata(self) -> Any:
        raise AssertionError("metadata must not be read without an accounting scope")


@pytest.mark.asyncio
async def test_auxiliary_chat_without_scope_preserves_direct_stream_behavior() -> None:
    provider = _MetadataMustNotBeReadProvider(
        [TextDeltaEvent(text="ok"), DoneEvent(input_tokens=1, output_tokens=1)]
    )

    assert await _media_completion(provider) == "ok"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_onboarding_probe_accounts_when_it_inherits_a_turn_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _RecordingSink()
    provider = _StreamProvider([DoneEvent(input_tokens=1, output_tokens=1)])
    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: provider,
    )

    def chat_stream_factory(provider: Any, messages: Any, config: Any) -> Any:
        return account_provider_stream(
            lambda: provider.chat(messages, config=config),
            provider="openai",
            model="gpt-test",
        )

    with bind_usage_accounting_scope(_scope(sink)):
        result = await probe_llm_provider(
            provider_id="openai",
            model="gpt-test",
            api_key="sk-test",
            chat_stream_factory=chat_stream_factory,
        )

    assert result.ok is True
    assert provider.calls == 1
    assert len(sink.started) == 1
    assert len(sink.finalized) == 1
    assert sink.unknown == []
