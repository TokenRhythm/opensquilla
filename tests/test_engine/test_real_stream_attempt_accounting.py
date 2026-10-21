from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from opensquilla.contracts.turn_execution import (
    SurfaceCapabilities,
    TurnExecutionContext,
    TurnIdentity,
)
from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.types import AnswerGenerationResetEvent, DoneEvent, ErrorEvent
from opensquilla.engine.usage import UsageTracker
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageCallResult,
    UsageCallStart,
    UsageExecutionContext,
    bind_usage_accounting_scope,
    usd_to_nanos,
)
from opensquilla.provider.openai import OpenAIProvider


class _Sink:
    def __init__(self) -> None:
        self.starts: list[UsageCallStart] = []
        self.results: list[tuple[UsageCallStart, UsageCallResult]] = []
        self.unknown: list[tuple[UsageCallStart, str]] = []

    async def start(self, call: UsageCallStart) -> None:
        self.starts.append(call)

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        self.results.append((call, result))

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


class _InterruptedBody(httpx.AsyncByteStream):
    def __init__(
        self, request: httpx.Request, *, billing: dict[str, Any], usd_cost: float | None = None,
    ) -> None:
        self.request = request
        self.billing = billing
        self.usd_cost = usd_cost
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield _sse({"model": "synthetic-model", "choices": [{
            "index": 0, "delta": {"reasoning_content": "discard reasoning"},
        }]})
        yield _sse({"choices": [{"index": 0, "delta": {"content": "discard text"}}]})
        usage: dict[str, Any] = {"prompt_tokens": 20, "completion_tokens": 3}
        if self.usd_cost is not None:
            usage["cost"] = self.usd_cost
        yield _sse({
            "choices": [], "usage": usage,
            **self.billing,
        })
        raise httpx.ReadTimeout("synthetic interrupted SSE", request=self.request)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("provider_kind", ["tokenrhythm", "openrouter"])
async def test_real_sse_failed_receipt_survives_successful_agent_recovery(
    monkeypatch: pytest.MonkeyPatch, provider_kind: str,
) -> None:
    requests: list[httpx.Request] = []
    interrupted: list[_InterruptedBody] = []

    def billing(cost: float) -> dict[str, Any]:
        if provider_kind == "tokenrhythm":
            return {"cost_cny": round(cost * 6.975, 8), "billing_pending": False}
        return {}

    def usage(cost: float) -> dict[str, Any]:
        counters: dict[str, Any] = {"prompt_tokens": 10, "completion_tokens": 2}
        if provider_kind == "openrouter":
            counters["cost"] = cost
        return counters

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            body = _InterruptedBody(
                request, billing=billing(0.03),
                usd_cost=0.03 if provider_kind == "openrouter" else None,
            )
            interrupted.append(body)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
        content = b"".join([
            _sse({"model": "synthetic-model", "choices": [{
                "index": 0, "delta": {"content": "Recovered."},
            }]}),
            _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            _sse({"choices": [], "usage": usage(0.02), **billing(0.02)}),
            b"data: [DONE]\n\n",
        ])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    transport = httpx.MockTransport(respond)
    host = "tokenrhythm.studio" if provider_kind == "tokenrhythm" else "openrouter.ai"
    provider = OpenAIProvider(
        api_key="synthetic-key", model="synthetic-model", provider_kind=provider_kind,
        base_url=f"https://{host}/v1",
    )

    async def build_client(*, timeout: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    monkeypatch.setattr(provider, "_build_http_client", build_client)
    context = TurnExecutionContext.create(
        TurnIdentity("receipt-turn", "receipt-answer", "agent:main:receipt-recovery"),
        surface=SurfaceCapabilities(
            supports_streaming=True, supports_edit=True, supports_generation_reset=True,
        ),
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1, max_provider_retries=3,
            retry_base_backoff_ms=0, retry_max_backoff_ms=0,
            provider_id=provider_kind, model_id="synthetic-model",
        ),
        execution_context=context, usage_tracker=UsageTracker(),
        session_key="agent:main:receipt-recovery",
    )
    sink = _Sink()
    scope = UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id="receipt-execution", agent_run_id="receipt-run",
        ),
    )
    with bind_usage_accounting_scope(scope):
        events = [event async for event in agent.run_turn("Complete the synthetic task.")]

    assert len(requests) == 2
    assert interrupted[0].closed
    assert json.loads(requests[0].content) == json.loads(requests[1].content)
    assert len([event for event in events if isinstance(event, AnswerGenerationResetEvent)]) == 1
    assert not any(isinstance(event, ErrorEvent) for event in events)
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.text == "Recovered."
    assert done.billed_cost == pytest.approx(0.05)
    assert done.input_tokens == 30
    assert done.output_tokens == 5
    assert done.missing_cost_entries == 1
    assert [call.call_index for call in sink.starts] == [1, 2]
    assert len({call.event_id for call in sink.starts}) == 2
    assert [result.billed_cost_nanos for _, result in sink.results] == [
        usd_to_nanos(0.03), usd_to_nanos(0.02),
    ]
    assert sum(result.missing_usage_entries for _, result in sink.results) == 1
    assert sink.unknown == []
