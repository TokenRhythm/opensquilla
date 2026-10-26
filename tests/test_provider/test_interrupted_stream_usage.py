from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from opensquilla.engine.usage_accounting import (
    has_known_provider_usage_receipt,
    normalize_provider_usage,
    usd_to_nanos,
)
from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import ChatConfig, DoneEvent, ErrorEvent, Message


class _InterruptedBody(httpx.AsyncByteStream):
    def __init__(
        self, request: httpx.Request, frames: list[dict[str, Any]],
        error: type[httpx.RequestError] | None,
    ) -> None:
        self.request = request
        self.frames = frames
        self.error = error
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for frame in self.frames:
            yield f"data: {json.dumps(frame)}\n\n".encode()
        if self.error is not None:
            raise self.error("synthetic interrupted SSE", request=self.request)

    async def aclose(self) -> None:
        self.closed = True


def _mock_stream(
    monkeypatch: pytest.MonkeyPatch, frames: list[dict[str, Any]],
    error: type[httpx.RequestError] | None,
) -> list[_InterruptedBody]:
    streams: list[_InterruptedBody] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = _InterruptedBody(request, frames, error)
        streams.append(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    transport = httpx.MockTransport(respond)
    real_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_client(*args, **{**kwargs, "transport": transport})

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return streams


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ReadError, None])
@pytest.mark.parametrize("output_received", [False, True])
async def test_anthropic_retains_known_input_without_inventing_output_or_final_cost(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[httpx.RequestError] | None, output_received: bool,
) -> None:
    frames = [
        {"type": "message_start", "message": {
            "id": "synthetic-message", "usage": {"input_tokens": 15},
        }},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "synthetic partial"}},
        {"type": "content_block_stop", "index": 0},
    ]
    if output_received:
        frames.append({"type": "message_delta", "usage": {"output_tokens": 4},
                       "delta": {"stop_reason": "end_turn"}})
    streams = _mock_stream(monkeypatch, frames, error_type)
    provider = AnthropicProvider(api_key="synthetic-key", model="claude-sonnet-4-6")
    events = [event async for event in provider.chat(
        [Message(role="user", content="synthetic request")],
        config=ChatConfig(physical_attempt_limit=1),
    )]

    assert not any(isinstance(event, DoneEvent) for event in events)
    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.recovery_origin == (
        "unknown" if error_type is None else "read_timeout"
        if error_type is httpx.ReadTimeout else "transport_disconnect"
    )
    assert has_known_provider_usage_receipt(error)
    result = normalize_provider_usage(
        error, default_provider="anthropic", default_model="claude-sonnet-4-6",
        completed_at_ms=1, resolve_estimates=False,
    )
    assert result.input_tokens == 15
    assert result.output_tokens == (4 if output_received else 0)
    assert result.missing_usage_entries == 1
    assert result.billed_cost_nanos == 0
    assert result.cost_source == "unavailable"
    assert result.items[0].token_usage_known is output_received
    meters = {meter.metric: meter for meter in result.items[0].meters}
    assert meters["input_tokens"].known
    assert meters["input_tokens"].quantity == "15"
    assert meters["output_tokens"].known is output_received
    assert meters["output_tokens"].quantity == ("4" if output_received else None)
    assert result.items[0].settlement is not None
    assert result.items[0].settlement.authority == "unknown"
    assert streams[0].closed


async def test_anthropic_start_usage_remains_an_incomplete_subtotal_in_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.usage_accounting import (
        UsageAccountingScope,
        UsageExecutionContext,
        account_provider_stream,
        bind_usage_accounting_scope,
    )
    from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
    from opensquilla.provider.types import TextDeltaEvent
    from opensquilla.session.models import SessionNode
    from opensquilla.session.storage import SessionStorage

    partial_text = "More generated content without a final usage update. " * 64
    streams = _mock_stream(monkeypatch, [
        {"type": "message_start", "message": {
            "id": "synthetic-message", "usage": {"input_tokens": 100, "output_tokens": 1},
        }},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": partial_text}},
    ], httpx.ReadTimeout)
    provider = AnthropicProvider(api_key="synthetic-key", model="claude-sonnet-4-6")
    storage = await SessionStorage.open(":memory:")
    try:
        await storage.upsert_session(SessionNode(
            session_key="agent:main:anthropic-subtotal", session_id="subtotal-session",
        ))
        await storage.initialize_usage_ledger(1)
        with bind_usage_accounting_scope(UsageAccountingScope(
            sink=SessionUsageEventSink(storage),
            context=UsageExecutionContext(
                execution_id="subtotal-execution", agent_run_id="subtotal-run",
                session_id="subtotal-session", turn_id="subtotal-turn",
            ),
        )):
            events = [event async for event in account_provider_stream(
                lambda: provider.chat(
                    [Message(role="user", content="synthetic request")],
                    config=ChatConfig(physical_attempt_limit=1),
                ),
                provider="anthropic", model="claude-sonnet-4-6",
            )]

        assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == (
            partial_text
        )
        assert not any(isinstance(event, DoneEvent) for event in events)
        error = next(event for event in events if isinstance(event, ErrorEvent))
        assert error.recovery_origin == "read_timeout"
        assert error.usage_missing_count > 0
        result = normalize_provider_usage(
            error, default_provider="anthropic", default_model="claude-sonnet-4-6",
            completed_at_ms=1,
        )
        assert (result.input_tokens, result.output_tokens) == (100, 1)
        assert result.billed_cost_nanos == 0
        assert result.estimated_cost_nanos > 0
        assert result.items[0].settlement is not None
        assert result.items[0].settlement.authority == "local_estimate"

        projection = await storage.get_turn_usage_projection(
            session_id="subtotal-session", session_epoch=0, turn_id="subtotal-turn",
        )
        assert projection is not None
        assert (projection["input_tokens"], projection["output_tokens"]) == (100, 1)
        assert projection["billed_cost"] == 0
        assert projection["has_billed_component"] is False
        assert projection["has_estimated_component"] is True
        assert projection["cost_usd"] == pytest.approx(
            result.estimated_cost_nanos / 1_000_000_000,
        )
        assert projection["coverage_status"] == "usage_unknown"
        assert projection["usage_unknown"] is True
        assert projection["missing_cost_entries"] > 0
        assert projection["cost_source"] == "mixed"
        projections = await storage.get_turn_usage_projections(
            session_id="subtotal-session", session_epoch=0, turn_ids=["subtotal-turn"],
        )
        assert projections["subtotal-turn"] == projection
        assert streams[0].closed
    finally:
        await storage.close()


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ReadError, None])
@pytest.mark.parametrize("usage", [{}, {"prompt_tokens": 15}, {
    "prompt_tokens": 15, "completion_tokens": 4,
}, {"prompt_tokens": -1, "completion_tokens": 4}])
@pytest.mark.parametrize("pending", [False, True])
async def test_native_receipt_without_all_token_counters_keeps_its_billing_authority(
    monkeypatch: pytest.MonkeyPatch, error_type: type[httpx.RequestError] | None,
    usage: dict[str, int], pending: bool,
) -> None:
    _mock_stream(monkeypatch, [
        {"choices": [{"index": 0, "delta": {"content": "synthetic partial"}}]},
        {"choices": [], "usage": usage, "cost_cny": 0.20925, "billing_pending": pending},
    ], error_type)
    provider = OpenAIProvider(
        api_key="synthetic-key", model="synthetic-model", provider_kind="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
    )
    events = [event async for event in provider.chat(
        [Message(role="user", content="synthetic request")],
        config=ChatConfig(physical_attempt_limit=1),
    )]

    assert not any(isinstance(event, DoneEvent) for event in events)
    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.recovery_origin == (
        "unknown" if error_type is None else "read_timeout"
        if error_type is httpx.ReadTimeout else "transport_disconnect"
    )
    assert has_known_provider_usage_receipt(error)
    result = normalize_provider_usage(
        error, default_provider="tokenrhythm", default_model="synthetic-model",
        completed_at_ms=1, resolve_estimates=False,
    )
    assert result.missing_usage_entries == 1
    assert result.billed_cost_nanos == (0 if pending else usd_to_nanos(0.03))
    assert result.cost_source == ("unavailable" if pending else "provider_billed")
    assert result.items[0].token_usage_known is all(
        type(usage.get(key)) is int and usage[key] >= 0
        for key in ("prompt_tokens", "completion_tokens")
    )
    receipt = result.items[0].billing_receipt
    assert receipt is not None
    assert receipt.currency == "CNY"
    assert receipt.status == ("pending" if pending else "confirmed")
    assert receipt.amount_nanos == 209_250_000
    assert receipt.usd_equivalent_nanos == (None if pending else usd_to_nanos(0.03))


@pytest.mark.parametrize("failure", ["upstream", "protocol"])
@pytest.mark.parametrize("pending", [False, True])
async def test_openai_retains_received_native_bill_on_non_transport_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str, pending: bool,
) -> None:
    _mock_stream(monkeypatch, [
        {"choices": [{"index": 0, "delta": {"content": "synthetic partial"}}]},
        {"choices": [], "usage": {"prompt_tokens": 15, "completion_tokens": 4},
         "cost_cny": 0.20925, "billing_pending": pending},
        {"error": {"code": "server_error", "message": "synthetic failure"}}
        if failure == "upstream" else {"choices": "malformed"},
    ], None)
    provider = OpenAIProvider(
        api_key="synthetic-key", model="synthetic-model", provider_kind="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
    )
    events = [event async for event in provider.chat(
        [Message(role="user", content="synthetic request")],
        config=ChatConfig(physical_attempt_limit=1),
    )]
    assert not any(isinstance(event, DoneEvent) for event in events)
    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == ("server_error" if failure == "upstream" else "invalid_stream_frame")
    assert error.recovery_origin == "unknown"
    result = normalize_provider_usage(
        error, default_provider="tokenrhythm", default_model="synthetic-model",
        completed_at_ms=1, resolve_estimates=False,
    )
    assert result.input_tokens == 15
    assert result.output_tokens == 4
    assert result.missing_usage_entries == 1
    assert result.billed_cost_nanos == (0 if pending else usd_to_nanos(0.03))
    receipt = result.items[0].billing_receipt
    assert receipt is not None
    assert receipt.status == ("pending" if pending else "confirmed")
    assert receipt.amount_nanos == 209_250_000


@pytest.mark.parametrize("failure", ["upstream", "protocol"])
async def test_anthropic_retains_partial_usage_on_non_transport_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    _mock_stream(monkeypatch, [
        {"type": "message_start", "message": {
            "id": "synthetic-message", "usage": {"input_tokens": 15},
        }},
        {"type": "error", "error": {"type": "overloaded_error", "message": "synthetic failure"}}
        if failure == "upstream" else [],
    ], None)
    provider = AnthropicProvider(api_key="synthetic-key", model="claude-sonnet-4-6")
    events = [event async for event in provider.chat(
        [Message(role="user", content="synthetic request")],
        config=ChatConfig(physical_attempt_limit=1),
    )]
    assert not any(isinstance(event, DoneEvent) for event in events)
    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == ("overloaded_error" if failure == "upstream" else "invalid_stream_frame")
    assert error.recovery_origin == "unknown"
    result = normalize_provider_usage(
        error, default_provider="anthropic", default_model="claude-sonnet-4-6",
        completed_at_ms=1, resolve_estimates=False,
    )
    assert result.input_tokens == 15
    assert result.missing_usage_entries == 1
    assert not result.items[0].token_usage_known
    meters = {meter.metric: meter for meter in result.items[0].meters}
    assert meters["output_tokens"].quantity is None
