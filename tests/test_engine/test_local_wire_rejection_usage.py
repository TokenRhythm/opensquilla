from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageCallResult,
    UsageCallStart,
    UsageExecutionContext,
    account_provider_stream,
    bind_usage_accounting_scope,
)
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.request_proof import local_budget_rejection_event
from opensquilla.provider.types import ChatConfig, ErrorEvent, Message

_WIRE_PROOF = {
    "wire_byte_budget_exhausted": True,
    "fits_wire_byte_budget": False,
    "wire_json_bytes": 101,
    "wire_body_max_bytes": 100,
}


class _Sink:
    def __init__(self) -> None:
        self.started: list[UsageCallStart] = []
        self.not_sent: list[UsageCallStart] = []
        self.unknown: list[tuple[UsageCallStart, str]] = []
        self.finalized: list[tuple[UsageCallStart, UsageCallResult]] = []

    async def start(self, call: UsageCallStart) -> None:
        self.started.append(call)

    async def mark_not_sent(self, call: UsageCallStart) -> None:
        self.not_sent.append(call)

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        self.finalized.append((call, result))


class _LegacySink:
    """An external sink may still implement the original accounting contract."""

    def __init__(self) -> None:
        self.started: list[UsageCallStart] = []
        self.unknown: list[tuple[UsageCallStart, str]] = []

    async def start(self, call: UsageCallStart) -> None:
        self.started.append(call)

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        del call, result

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))


def _scope(sink: _Sink) -> UsageAccountingScope:
    return UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id="synthetic-wire-rejection",
            agent_run_id="synthetic-wire-rejection",
        ),
    )


class _RejectedProvider:
    provider_name = "synthetic-provider"
    retry_failed_call_safe = False

    def __init__(self, error: ErrorEvent) -> None:
        self.error = error

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[ErrorEvent]:
        del messages, tools, config
        return self._stream()

    async def _stream(self) -> AsyncIterator[ErrorEvent]:
        yield self.error


@pytest.mark.parametrize("upstream", [False, True])
async def test_direct_agent_accounts_only_dispatched_failures_as_unknown(
    upstream: bool,
) -> None:
    error = (
        ErrorEvent(code="413", http_status=413, message="synthetic upstream rejection")
        if upstream
        else local_budget_rejection_event(_WIRE_PROOF)
    )
    sink = _Sink()
    agent = Agent(
        provider=_RejectedProvider(error),
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=0,
            provider_id="synthetic-provider",
            model_id="synthetic-model",
        ),
    )
    with bind_usage_accounting_scope(_scope(sink)):
        _ = [event async for event in agent.run_turn("synthetic input")]

    assert len(sink.started) == 1
    assert not sink.finalized
    if upstream:
        assert sink.not_sent == []
        assert [call.event_id for call, _ in sink.unknown] == [sink.started[0].event_id]
    else:
        assert [call.event_id for call in sink.not_sent] == [sink.started[0].event_id]
        assert sink.unknown == []


async def test_physical_stream_adaptor_rejects_wire_body_without_http_or_unknown_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_REQUEST_MAX_BODY_BYTES", "100")
    provider = OpenAIProvider(
        api_key="synthetic-key",
        model="synthetic-model",
        base_url="https://example.test/v1",
    )

    def unexpected_stream(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("local rejection must precede HTTP dispatch")

    monkeypatch.setattr(httpx.AsyncClient, "stream", unexpected_stream)
    sink = _Sink()
    with bind_usage_accounting_scope(_scope(sink)):
        events = [
            event
            async for event in account_provider_stream(
                lambda: provider.chat(
                    [Message(role="user", content="synthetic input")],
                    config=ChatConfig(provider_request_max_chars=20_000),
                ),
                provider="synthetic-provider",
                model="synthetic-model",
            )
        ]

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0]._opensquilla_request_not_dispatched is True
    assert [call.event_id for call in sink.not_sent] == [sink.started[0].event_id]
    assert sink.unknown == []


async def test_local_character_budget_rejection_is_not_a_provider_usage_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIProvider(
        api_key="synthetic-key",
        model="synthetic-model",
        base_url="https://example.test/v1",
    )

    def unexpected_stream(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("local budget rejection must precede HTTP dispatch")

    monkeypatch.setattr(httpx.AsyncClient, "stream", unexpected_stream)
    sink = _Sink()
    with bind_usage_accounting_scope(_scope(sink)):
        events = [
            event
            async for event in account_provider_stream(
                lambda: provider.chat(
                    [Message(role="user", content="synthetic input")],
                    config=ChatConfig(provider_request_max_chars=1),
                ),
                provider="synthetic-provider",
                model="synthetic-model",
            )
        ]

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0]._opensquilla_request_not_dispatched is True
    assert [call.event_id for call in sink.not_sent] == [sink.started[0].event_id]
    assert sink.unknown == []


async def test_physical_stream_does_not_trust_upstream_budget_prose() -> None:
    sink = _Sink()

    async def upstream() -> AsyncIterator[ErrorEvent]:
        # A provider may echo a local-looking code and proof. It cannot mark
        # the adapter's private pre-dispatch evidence.
        yield ErrorEvent(
            code="provider_request_budget_exhausted",
            message=local_budget_rejection_event(_WIRE_PROOF).message,
            http_status=413,
        )

    with bind_usage_accounting_scope(_scope(sink)):
        _ = [
            event
            async for event in account_provider_stream(
                upstream,
                provider="synthetic-provider",
                model="synthetic-model",
            )
        ]

    assert sink.not_sent == []
    assert [call.event_id for call, _ in sink.unknown] == [sink.started[0].event_id]


async def test_external_legacy_sink_keeps_conservative_terminal_outcome() -> None:
    sink = _LegacySink()

    async def rejected() -> AsyncIterator[ErrorEvent]:
        yield local_budget_rejection_event(_WIRE_PROOF)

    with bind_usage_accounting_scope(_scope(sink)):
        _ = [
            event
            async for event in account_provider_stream(
                rejected, provider="synthetic-provider", model="synthetic-model",
            )
        ]

    assert len(sink.started) == 1
    assert sink.unknown == [(sink.started[0], "cancelled_before_provider_request")]


async def test_real_http_413_remains_unknown_after_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_REQUEST_MAX_BODY_BYTES", "0")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(413, json={
            "error": {
                "code": "provider_request_budget_exhausted",
                "message": local_budget_rejection_event(_WIRE_PROOF).message,
            },
        })

    provider = OpenAIProvider(
        api_key="synthetic-key",
        model="synthetic-model",
        base_url="https://example.test/v1",
    )

    async def build_client(*, timeout: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=timeout)

    monkeypatch.setattr(provider, "_build_http_client", build_client)
    sink = _Sink()
    with bind_usage_accounting_scope(_scope(sink)):
        events = [
            event
            async for event in account_provider_stream(
                lambda: provider.chat(
                    [Message(role="user", content="synthetic input")],
                    config=ChatConfig(provider_request_max_chars=20_000),
                ),
                provider="synthetic-provider",
                model="synthetic-model",
            )
        ]

    assert len(requests) == 1
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].http_status == 413
    assert events[0]._opensquilla_request_not_dispatched is False
    assert sink.not_sent == []
    assert [call.event_id for call, _ in sink.unknown] == [sink.started[0].event_id]


def test_local_budget_factory_marks_all_pre_dispatch_rejections() -> None:
    character_proof = {"fits": False, "fits_char_budget": False, "proof_budget": 1}
    assert local_budget_rejection_event(character_proof)._opensquilla_request_not_dispatched
    assert local_budget_rejection_event(_WIRE_PROOF)._opensquilla_request_not_dispatched is True
    assert replace(
        local_budget_rejection_event(_WIRE_PROOF), generation_epoch=2
    )._opensquilla_request_not_dispatched is True
