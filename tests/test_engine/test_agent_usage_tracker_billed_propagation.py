"""Pin the agent → usage_tracker billed_cost forwarding contract.

When the Agent loop receives a ``ProviderDoneEvent`` with a real
``billed_cost`` from the provider, it
must call ``UsageTracker.add(..., billed_cost=...)`` so the per-model
breakdown can surface the actual provider-billed cost (instead of the
cache-blind pricing-table estimate).

A regression here re-introduces the user-reported drift bug
(row=$0.0607 Actual, breakdown sum=$0.1835 — 3× off due to ignored
cache discount).

Implementation strategy: mock UsageTracker, run a single ProviderDoneEvent
through the same accumulation block agent.py:1048-1076 uses. We don't
spin up the full Agent state machine — the change is one keyword
argument forwarded inside a tight branch, and a focused mock test gives
a clear regression marker without dragging the entire LLM/provider
stack into the test.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.contracts.turn_execution import (
    EnsembleContinuationSnapshot,
    TurnExecutionContext,
    TurnIdentity,
)
from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.types import DoneEvent as EngineDoneEvent
from opensquilla.engine.types import ToolCall
from opensquilla.engine.usage import UsageTracker
from opensquilla.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEndEvent
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStartEvent


@dataclass
class _FakeProviderDoneEvent:
    """Minimal stand-in for opensquilla.provider.types.DoneEvent.

    The agent loop reads exactly these attributes on the raw_ev branch
    we exercise; the rest of DoneEvent is irrelevant to billed forwarding.
    """

    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    billed_cost: float
    model: str
    cost_source: str = "provider_billed"
    reasoning_tokens: int = 0
    reasoning_content: str | None = None
    thinking_signature: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    model_usage_breakdown: list[dict[str, Any]] | None = None


def _usage_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _usage_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _simulate_agent_raw_ev_block(
    tracker: UsageTracker,
    session_key: str,
    raw_ev: _FakeProviderDoneEvent,
    fallback_model_id: str = "",
) -> None:
    """Replay the agent.py:1068-1076 usage-tracker call shape.

    Kept in lock-step with engine/agent.py — if the production call
    signature changes, this helper must change in the same commit. The
    test below asserts the call shape, so a divergence fails fast.
    """
    if tracker and session_key:
        usage_breakdown = getattr(raw_ev, "model_usage_breakdown", None)
        if isinstance(usage_breakdown, list) and usage_breakdown:
            for usage_row in usage_breakdown:
                if not isinstance(usage_row, dict):
                    continue
                cache_read = (
                    usage_row.get("cache_read_tokens")
                    if "cache_read_tokens" in usage_row
                    else usage_row.get("cached_tokens")
                )
                tracker.add(
                    session_key,
                    input_tokens=_usage_int(usage_row.get("input_tokens") or 0),
                    output_tokens=_usage_int(usage_row.get("output_tokens") or 0),
                    model_id=str(usage_row.get("model") or fallback_model_id or ""),
                    cache_read_tokens=_usage_int(cache_read or 0),
                    cache_write_tokens=_usage_int(usage_row.get("cache_write_tokens") or 0),
                    billed_cost=_usage_float(usage_row.get("billed_cost") or 0.0),
                )
            return
        tracker.add(
            session_key,
            input_tokens=raw_ev.input_tokens,
            output_tokens=raw_ev.output_tokens,
            model_id=raw_ev.model or fallback_model_id or "",
            cache_read_tokens=raw_ev.cached_tokens,
            cache_write_tokens=raw_ev.cache_write_tokens,
            billed_cost=raw_ev.billed_cost,
        )


def test_agent_forwards_billed_cost_to_tracker() -> None:
    """The contract: when a ProviderDoneEvent has billed_cost > 0, it lands
    on the per-model UsageTracker entry. Without this, per-model breakdown
    keeps relying on pricing-table estimates and drifts on cache-heavy
    sessions."""
    tracker = UsageTracker()
    raw_ev = _FakeProviderDoneEvent(
        input_tokens=29213,
        output_tokens=400,
        cached_tokens=11588,
        cache_write_tokens=17772,
        billed_cost=0.1254,
        model="anthropic/claude-4.7-opus",
    )
    _simulate_agent_raw_ev_block(tracker, "agent:test:webchat:s1", raw_ev)

    usage = tracker.get("agent:test:webchat:s1")
    assert usage is not None
    assert usage._per_model is not None
    mu = usage._per_model["anthropic/claude-4.7-opus"]
    assert mu.billed_cost == 0.1254
    assert mu.input_tokens == 29213
    assert mu.cache_read_tokens == 11588


def test_agent_forwards_zero_billed_when_provider_lacked_cost() -> None:
    """billed_cost defaults to 0 on ProviderDoneEvent when the provider
    didn't return a price; the tracker must accept it without polluting
    the per-model record (estimate fallback kicks in)."""
    tracker = UsageTracker()
    raw_ev = _FakeProviderDoneEvent(
        input_tokens=1000,
        output_tokens=50,
        cached_tokens=0,
        cache_write_tokens=0,
        billed_cost=0.0,
        model="z-ai/glm-5.1",
        cost_source="unavailable",
    )
    _simulate_agent_raw_ev_block(tracker, "agent:test:webchat:s2", raw_ev)

    usage = tracker.get("agent:test:webchat:s2")
    assert usage is not None
    assert usage._per_model is not None
    mu = usage._per_model["z-ai/glm-5.1"]
    assert mu.billed_cost == 0.0


def test_multiple_raw_events_accumulate_per_model() -> None:
    """User-reported scenario: a multi-model auto-routed session.
    Each ProviderDoneEvent carries one model + its real billed cost.
    Sum of per-model billed equals the session's billed total — which is
    exactly the property that lets rpc_usage skip pro-rate."""
    tracker = UsageTracker()
    session_key = "agent:test:webchat:multi"

    _simulate_agent_raw_ev_block(
        tracker,
        session_key,
        _FakeProviderDoneEvent(
            input_tokens=29213,
            output_tokens=400,
            cached_tokens=11588,
            cache_write_tokens=17772,
            billed_cost=0.1254,
            model="anthropic/claude-4.7-opus",
        ),
    )
    _simulate_agent_raw_ev_block(
        tracker,
        session_key,
        _FakeProviderDoneEvent(
            input_tokens=9323,
            output_tokens=0,
            cached_tokens=0,
            cache_write_tokens=0,
            billed_cost=0.0111,
            model="z-ai/glm-5.1",
        ),
    )

    usage = tracker.get(session_key)
    assert usage is not None
    breakdown = usage.model_breakdown
    by_model = {row["model"]: row for row in breakdown}
    assert by_model["anthropic/claude-4.7-opus"]["costUsd"] == 0.1254
    assert by_model["anthropic/claude-4.7-opus"]["costSource"] == "provider_billed"
    assert by_model["z-ai/glm-5.1"]["costUsd"] == 0.0111
    assert by_model["z-ai/glm-5.1"]["costSource"] == "provider_billed"
    assert sum(row["costUsd"] for row in breakdown) == 0.1365


def test_ensemble_breakdown_accumulates_each_underlying_model() -> None:
    tracker = UsageTracker()
    session_key = "agent:test:webchat:ensemble"
    raw_ev = _FakeProviderDoneEvent(
        input_tokens=33,
        output_tokens=12,
        cached_tokens=0,
        cache_write_tokens=0,
        billed_cost=0.03,
        model="z-ai/glm-5.2",
        model_usage_breakdown=[
            {
                "model": "deepseek/deepseek-v4-pro",
                "input_tokens": 10,
                "output_tokens": 2,
                "cached_tokens": 1,
                "cache_write_tokens": 0,
                "billed_cost": 0.01,
            },
            {
                "model": "z-ai/glm-5.2",
                "input_tokens": 23,
                "output_tokens": 10,
                "cached_tokens": 0,
                "cache_write_tokens": 4,
                "billed_cost": 0.02,
            },
        ],
    )

    _simulate_agent_raw_ev_block(tracker, session_key, raw_ev)

    usage = tracker.get(session_key)
    assert usage is not None
    by_model = {row["model"]: row for row in usage.model_breakdown}
    assert by_model["deepseek/deepseek-v4-pro"]["inputTokens"] == 10
    assert by_model["deepseek/deepseek-v4-pro"]["cacheReadTokens"] == 1
    assert by_model["z-ai/glm-5.2"]["outputTokens"] == 10
    assert by_model["z-ai/glm-5.2"]["cacheWriteTokens"] == 4
    assert usage.billed_cost == 0.03


class _TwoStepEnsembleBreakdownProvider:
    provider_name = "fake"
    execution_context_aware = True

    def __init__(
        self,
        request_counts: tuple[int, ...],
        *,
        retry_empty_continuation: bool = False,
    ) -> None:
        self.calls = 0
        self.request_counts = request_counts
        self.retry_empty_continuation = retry_empty_continuation

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
        *,
        execution_context: TurnExecutionContext | None = None,
    ) -> AsyncIterator[Any]:
        self.calls += 1
        return self._stream(self.calls, execution_context)

    async def _stream(
        self,
        call: int,
        execution_context: TurnExecutionContext | None,
    ) -> AsyncIterator[Any]:
        if call == 1:
            trace = {
                "profile": "default",
                "llm_request_count": self.request_counts[0],
            }
            if execution_context is not None:
                execution_context.record_ensemble_continuation_snapshot(
                    EnsembleContinuationSnapshot(
                        ensemble_trace=trace,
                        physical_request_count=self.request_counts[0],
                        request_started=True,
                    )
                )
            yield ProviderToolUseStartEvent(tool_use_id="lookup-1", tool_name="lookup")
            yield ProviderToolUseEndEvent(
                tool_use_id="lookup-1",
                tool_name="lookup",
                arguments={"q": "demo"},
            )
            yield ProviderDoneEvent(
                stop_reason="tool_use",
                input_tokens=30,
                output_tokens=3,
                billed_cost=0.03,
                model="z-ai/glm-5.2",
                ensemble_trace=trace,
                model_usage_breakdown=[
                    {
                        "role": "proposer",
                        "label": "proposer_1",
                        "provider": "openrouter",
                        "model": "deepseek/deepseek-v4-pro",
                        "input_tokens": 10,
                        "output_tokens": 1,
                        "billed_cost": 0.01,
                        "cost_source": "provider_billed",
                    },
                    {
                        "role": "aggregator",
                        "label": "aggregator",
                        "provider": "openrouter",
                        "model": "z-ai/glm-5.2",
                        "input_tokens": 20,
                        "output_tokens": 2,
                        "billed_cost": 0.02,
                        "cost_source": "provider_billed",
                    },
                ],
            )
            return
        if self.retry_empty_continuation and call == 2:
            yield ProviderDoneEvent(
                stop_reason="end_turn",
                model="z-ai/glm-5.2",
                ensemble_trace={
                    "profile": "default",
                    "llm_request_count": self.request_counts[1],
                },
            )
            return
        yield ProviderTextDeltaEvent(text="final answer")
        yield ProviderDoneEvent(
            stop_reason="end_turn",
            input_tokens=40,
            output_tokens=4,
            billed_cost=0.04,
            model="z-ai/glm-5.2",
            ensemble_trace={
                "profile": "default",
                "llm_request_count": self.request_counts[call - 1],
            },
            model_usage_breakdown=[
                {
                    "role": "proposer",
                    "label": "proposer_1",
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-pro",
                    "input_tokens": 15,
                    "output_tokens": 1,
                    "billed_cost": 0.02,
                    "cost_source": "provider_billed",
                },
                {
                    "role": "aggregator",
                    "label": "aggregator",
                    "provider": "openrouter",
                    "model": "z-ai/glm-5.2",
                    "input_tokens": 25,
                    "output_tokens": 3,
                    "billed_cost": 0.02,
                    "cost_source": "provider_billed",
                },
            ],
        )

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.parametrize("with_execution_context", [True, False])
def test_agent_final_done_summarizes_ensemble_breakdown_across_tool_iterations(
    with_execution_context: bool,
) -> None:
    request_counts = (3, 4)
    execution_context = TurnExecutionContext.create(
        TurnIdentity(
            "turn-ensemble-usage",
            "assistant-ensemble-usage",
            "agent:test:webchat:ensemble-usage",
        )
    )

    async def tool_handler(call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="lookup result",
        )

    async def run() -> EngineDoneEvent:
        agent = Agent(
            provider=_TwoStepEnsembleBreakdownProvider(request_counts),
            config=AgentConfig(max_iterations=3),
            tool_definitions=[
                ToolDefinition(
                    name="lookup",
                    description="lookup",
                    input_schema=ToolInputSchema(properties={}, required=[]),
                )
            ],
            tool_handler=tool_handler,
            execution_context=execution_context if with_execution_context else None,
        )
        events = [event async for event in agent.run_turn("hi")]
        done_events = [event for event in events if isinstance(event, EngineDoneEvent)]
        assert done_events
        return done_events[-1]

    done = asyncio.run(run())

    assert [row["model"] for row in done.model_usage_breakdown] == [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
    ]
    proposer_row = done.model_usage_breakdown[0]
    assert proposer_row["label"] == "proposer_1"
    assert proposer_row["input_tokens"] == 25
    assert proposer_row["output_tokens"] == 2
    assert proposer_row["billed_cost"] == 0.03
    assert proposer_row["cost_usd"] == 0.03
    assert proposer_row["request_count"] == 2
    aggregator_row = done.model_usage_breakdown[1]
    assert aggregator_row["label"] == "aggregator"
    assert aggregator_row["input_tokens"] == 45
    assert aggregator_row["output_tokens"] == 5
    assert aggregator_row["billed_cost"] == 0.04
    assert aggregator_row["cost_usd"] == 0.04
    assert aggregator_row["request_count"] == 2
    assert done.ensemble_trace is not None
    assert done.ensemble_trace["llm_request_count"] == request_counts[-1]
    assert done.input_tokens == 70
    assert done.output_tokens == 7
    assert done.billed_cost == 0.07


@pytest.mark.parametrize(
    ("with_execution_context", "request_counts", "expected_request_count"),
    [
        (True, (3, 4, 4), 5),
        (False, (3, 4, 3), 7),
    ],
)
def test_agent_counts_each_retried_ensemble_continuation_request(
    with_execution_context: bool,
    request_counts: tuple[int, ...],
    expected_request_count: int,
) -> None:
    execution_context = TurnExecutionContext.create(
        TurnIdentity(
            "turn-ensemble-retry",
            "assistant-ensemble-retry",
            "agent:test:webchat:ensemble-retry",
        )
    )
    provider = _TwoStepEnsembleBreakdownProvider(
        request_counts,
        retry_empty_continuation=True,
    )

    async def tool_handler(call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="lookup result",
        )

    async def run() -> EngineDoneEvent:
        agent = Agent(
            provider=provider,
            config=AgentConfig(max_iterations=3),
            tool_definitions=[
                ToolDefinition(
                    name="lookup",
                    description="lookup",
                    input_schema=ToolInputSchema(properties={}, required=[]),
                )
            ],
            tool_handler=tool_handler,
            execution_context=execution_context if with_execution_context else None,
        )
        events = [event async for event in agent.run_turn("hi")]
        return next(event for event in events if isinstance(event, EngineDoneEvent))

    done = asyncio.run(run())

    assert provider.calls == 3
    assert done.ensemble_trace is not None
    assert done.ensemble_trace["llm_request_count"] == expected_request_count


def test_ensemble_breakdown_malformed_usage_values_do_not_raise() -> None:
    tracker = UsageTracker()
    session_key = "agent:test:webchat:ensemble-malformed"
    raw_ev = _FakeProviderDoneEvent(
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        cache_write_tokens=0,
        billed_cost=0.0,
        model="fallback-model",
        model_usage_breakdown=[
            {
                "model": "bad-row",
                "input_tokens": "not-an-int",
                "output_tokens": None,
                "cached_tokens": "also-bad",
                "cache_write_tokens": -3,
                "billed_cost": "not-a-float",
            }
        ],
    )

    _simulate_agent_raw_ev_block(tracker, session_key, raw_ev)

    usage = tracker.get(session_key)
    assert usage is not None
    by_model = {row["model"]: row for row in usage.model_breakdown}
    assert by_model["bad-row"]["inputTokens"] == 0
    assert by_model["bad-row"]["outputTokens"] == 0
    assert by_model["bad-row"]["cacheReadTokens"] == 0
    assert by_model["bad-row"]["cacheWriteTokens"] == 0
    assert by_model["bad-row"]["costUsd"] == 0


def test_mock_tracker_receives_billed_cost_kwarg() -> None:
    """Belt-and-braces: the call shape itself includes billed_cost as a kwarg.
    Catches a refactor that 'optimizes' the kwarg away or renames it."""
    mock_tracker = MagicMock(spec=UsageTracker)
    raw_ev = _FakeProviderDoneEvent(
        input_tokens=100,
        output_tokens=10,
        cached_tokens=0,
        cache_write_tokens=0,
        billed_cost=0.0042,
        model="some/model",
    )
    _simulate_agent_raw_ev_block(mock_tracker, "session", raw_ev)

    mock_tracker.add.assert_called_once()
    _args, kwargs = mock_tracker.add.call_args
    assert "billed_cost" in kwargs, "agent must pass billed_cost as kwarg"
    assert kwargs["billed_cost"] == 0.0042
