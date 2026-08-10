from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.subagent import SubagentSpec
from opensquilla.engine.types import ArtifactEvent, ErrorEvent
from opensquilla.engine.usage import UsageTracker, model_usage_cost_fields
from opensquilla.engine.usage_accounting import UsageCallResult, UsageCallStart
from opensquilla.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import (
    DoneEvent as ProviderDone,
)
from opensquilla.provider import (
    ErrorEvent as ProviderError,
)
from opensquilla.provider import (
    TextDeltaEvent as ProviderText,
)
from opensquilla.provider import (
    ToolUseEndEvent as ProviderToolUseEnd,
)
from opensquilla.provider import (
    ToolUseStartEvent as ProviderToolUseStart,
)
from opensquilla.provider.types import ProviderBillingReceipt


class _RecordingUsageSink:
    def __init__(self) -> None:
        self.started: list[UsageCallStart] = []
        self.finalized: list[tuple[UsageCallStart, UsageCallResult]] = []
        self.unknown: list[tuple[UsageCallStart, str]] = []

    async def start(self, call: UsageCallStart) -> None:
        self.started.append(call)

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        self.finalized.append((call, result))

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))


class _LoopingToolProvider:
    provider_name = "fake"

    def __init__(self, *, final_on_call: int | None = None) -> None:
        self.final_on_call = final_on_call
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        call_number = len(self.calls)
        return self._stream(call_number)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if self.final_on_call == call_number:
            yield ProviderText(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return

        tool_id = f"tool-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_id, tool_name="echo")
        yield ProviderToolUseEnd(
            tool_use_id=tool_id,
            tool_name="echo",
            arguments={"value": "again"},
        )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []




class _ToolThenProviderErrorProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        call_number = len(self.calls)
        return self._stream(call_number)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo")
            yield ProviderToolUseEnd(
                tool_use_id="tool-1",
                tool_name="echo",
                arguments={"value": "again"},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderError(message="request timed out", code="request_error")

    async def list_models(self) -> list[Any]:
        return []

class _DoneUsageProvider:
    provider_name = "fake"

    def __init__(
        self,
        *,
        input_tokens: int = 1,
        output_tokens: int = 1,
        billed_cost: float = 0.0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.billed_cost = billed_cost
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="done")
        yield ProviderDone(
            stop_reason="stop",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            billed_cost=self.billed_cost,
        )

    async def list_models(self) -> list[Any]:
        return []


class _DoneBreakdownProvider:
    provider_name = "fake"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="done")
        yield ProviderDone(
            stop_reason="stop",
            input_tokens=2000,
            output_tokens=1000,
            billed_cost=0.01,
            model="z-ai/glm-5.2",
            cost_source="mixed",
            model_usage_breakdown=[
                {
                    "role": "proposer",
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-pro-20260423",
                    "input_tokens": 1000,
                    "output_tokens": 1000,
                    "billed_cost": 0.0,
                    "cost_source": "none",
                },
                {
                    "role": "aggregator",
                    "provider": "openrouter",
                    "model": "z-ai/glm-5.2",
                    "input_tokens": 1000,
                    "output_tokens": 0,
                    "billed_cost": 0.01,
                    "cost_source": "provider_billed",
                },
            ],
        )

    async def list_models(self) -> list[Any]:
        return []


class _DoneTokenRhythmBreakdownProvider:
    provider_name = "tokenrhythm"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="done")
        yield ProviderDone(
            stop_reason="stop",
            input_tokens=2000,
            output_tokens=200,
            model="glm-5.2",
            model_usage_breakdown=[
                {
                    "role": "proposer",
                    "provider": "tokenrhythm",
                    "model": "deepseek-v4-pro",
                    "input_tokens": 1000,
                    "output_tokens": 100,
                },
                {
                    "role": "aggregator",
                    "provider": "tokenrhythm",
                    "model": "glm-5.2",
                    "input_tokens": 1000,
                    "output_tokens": 100,
                },
            ],
        )

    async def list_models(self) -> list[Any]:
        return []


class _ArtifactThenProviderErrorProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        call_number = len(self.calls)
        return self._stream(call_number)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            tool_id = "publish-1"
            yield ProviderToolUseStart(
                tool_use_id=tool_id,
                tool_name="publish_artifact",
            )
            yield ProviderToolUseEnd(
                tool_use_id=tool_id,
                tool_name="publish_artifact",
                arguments={"path": "report.txt"},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return

        yield ProviderError(message="request timed out", code="request_error")

    async def list_models(self) -> list[Any]:
        return []


async def _echo_tool(call: Any) -> ToolResult:
    return ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content="ok",
    )


async def _error_tool(call: Any) -> ToolResult:
    return ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content="invalid arguments",
        is_error=True,
    )


def _echo_definition() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="Echo.",
        input_schema=ToolInputSchema(
            properties={"value": {"type": "string"}},
            required=["value"],
        ),
    )


def _publish_artifact_definition() -> ToolDefinition:
    return ToolDefinition(
        name="publish_artifact",
        description="Publish an artifact.",
        input_schema=ToolInputSchema(
            properties={"path": {"type": "string"}},
            required=["path"],
        ),
    )


async def _artifact_tool(call: Any) -> ToolResult:
    return ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content="published",
        artifacts=[
            {
                "id": "art-1",
                "name": "report.txt",
                "mime": "text/plain",
                "size": 12,
                "sha256": "a" * 64,
                "session_id": "session-1",
                "session_key": "agent:main:webchat:session-1",
                "source": "publish_artifact",
                "created_at": "2026-05-06T12:00:00Z",
                "download_url": "/api/v1/artifacts/art-1",
            }
        ],
    )


def test_agent_iteration_defaults_are_unbounded() -> None:
    assert AgentConfig().max_iterations == 0
    assert SubagentSpec(task="check").max_iterations == 0
    assert AgentConfig().max_turn_llm_calls == 0
    assert AgentConfig().max_turn_input_tokens == 0
    assert AgentConfig().max_turn_output_tokens == 0
    assert AgentConfig().max_turn_billed_cost_usd == 0.0
    assert AgentConfig().max_turn_tool_errors == 0
    assert AgentConfig().length_capped_continuations == 3


@pytest.mark.asyncio
async def test_agent_default_max_iterations_allows_long_tool_loop_to_finish() -> None:
    provider = _LoopingToolProvider(final_on_call=101)
    agent = Agent(
        provider=provider,
        config=AgentConfig(),
        tool_definitions=[_echo_definition()],
        tool_handler=_echo_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 101
    assert not any(event.kind == "error" and event.code == "max_iterations" for event in events)
    assert any(event.kind == "done" and event.text == "done" for event in events)


@pytest.mark.asyncio
async def test_agent_finalizes_when_tool_loop_reaches_max_iterations() -> None:
    provider = _LoopingToolProvider(final_on_call=2)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        tool_definitions=[_echo_definition()],
        tool_handler=_echo_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert provider.calls[-1][-1].role == "user"
    assert "Do not call tools" in str(provider.calls[-1][-1].content)
    assert "Do not call tools" not in "\n".join(
        str(message.content) for message in agent._history
    )
    assert not any(event.kind == "state" and event.state.value == "error" for event in events)
    assert not any(event.kind == "error" and event.code == "max_iterations" for event in events)
    assert any(event.kind == "done" and event.text == "done" for event in events)


@pytest.mark.asyncio
async def test_agent_reports_partial_max_iterations_after_finalization_attempt_fails() -> None:
    provider = _LoopingToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        tool_definitions=[_echo_definition()],
        tool_handler=_echo_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert not any(event.kind == "state" and event.state.value == "error" for event in events)
    assert any(
        event.kind == "done"
        and "best partial result" in event.text
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_returns_partial_when_max_iteration_finalization_provider_fails() -> None:
    provider = _ToolThenProviderErrorProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, max_provider_retries=0),
        tool_definitions=[_echo_definition()],
        tool_handler=_echo_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert not any(event.kind == "state" and event.state.value == "error" for event in events)
    assert not any(event.kind == "error" for event in events)
    done_texts = [event.text for event in events if event.kind == "done"]
    assert len(done_texts) == 1
    assert done_texts[0].count("best partial result") == 1


@pytest.mark.asyncio
async def test_agent_allows_final_response_on_last_iteration() -> None:
    provider = _LoopingToolProvider(final_on_call=2)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[_echo_definition()],
        tool_handler=_echo_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert not any(event.kind == "error" and event.code == "max_iterations" for event in events)
    assert any(event.kind == "done" and event.text == "done" for event in events)


@pytest.mark.asyncio
async def test_agent_emits_artifact_event_independent_of_tool_result_text() -> None:
    provider = _LoopingToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        tool_definitions=[_echo_definition()],
        tool_handler=_artifact_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    artifact_events = [event for event in events if isinstance(event, ArtifactEvent)]
    assert len(artifact_events) == 1
    assert artifact_events[0].id == "art-1"
    assert artifact_events[0].download_url == "/api/v1/artifacts/art-1"


@pytest.mark.asyncio
async def test_agent_synthesizes_final_artifact_response_without_provider_call() -> None:
    provider = _ArtifactThenProviderErrorProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3, max_provider_retries=0),
        tool_definitions=[_publish_artifact_definition()],
        tool_handler=_artifact_tool,
    )

    events = [event async for event in agent.run_turn("publish a report")]

    assert len(provider.calls) == 1
    assert any(isinstance(event, ArtifactEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(event.kind == "warning" for event in events)
    assert any(
        event.kind == "done"
        and event.text == "The generated file is ready: report.txt."
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_synthesizes_final_artifact_response_before_extra_llm_call() -> None:
    provider = _ArtifactThenProviderErrorProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3, max_turn_llm_calls=1),
        tool_definitions=[_publish_artifact_definition()],
        tool_handler=_artifact_tool,
    )

    events = [event async for event in agent.run_turn("publish a report")]

    assert len(provider.calls) == 1
    assert any(isinstance(event, ArtifactEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(event.kind == "warning" for event in events)
    assert any(
        event.kind == "done"
        and event.text == "The generated file is ready: report.txt."
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_stops_when_turn_input_token_budget_is_exceeded() -> None:
    provider = _LoopingToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=100,
            max_turn_input_tokens=1,
            max_turn_output_tokens=0,
            max_turn_billed_cost_usd=0,
            max_turn_tool_errors=0,
        ),
        tool_definitions=[_echo_definition()],
        tool_handler=_echo_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(
        event.kind == "error" and event.code == "turn_input_token_budget_exceeded"
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_stops_when_turn_llm_call_budget_is_exceeded() -> None:
    provider = _LoopingToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=100,
            max_turn_llm_calls=1,
        ),
        tool_definitions=[_echo_definition()],
        tool_handler=_echo_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(
        event.kind == "error" and event.code == "turn_llm_call_budget_exceeded"
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_stops_when_turn_output_token_budget_is_exceeded() -> None:
    provider = _DoneUsageProvider(output_tokens=10)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_turn_output_tokens=5),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(
        event.kind == "error" and event.code == "turn_output_token_budget_exceeded"
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_done_event_uses_current_turn_real_billed_usage_delta() -> None:
    tracker = UsageTracker()
    session_key = "agent:test:webchat:s1"
    tracker.add(
        session_key,
        input_tokens=100,
        output_tokens=10,
        model_id="deepseek/deepseek-v4-pro-20260423",
        billed_cost=0.050,
    )
    provider = _DoneUsageProvider(input_tokens=9, output_tokens=4, billed_cost=0.123)
    agent = Agent(
        provider=provider,
        config=AgentConfig(model_id="deepseek/deepseek-v4-pro-20260423"),
        usage_tracker=tracker,
        session_key=session_key,
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    assert done.input_tokens == 9
    assert done.output_tokens == 4
    assert done.cost_usd == pytest.approx(0.123)
    assert done.billed_cost == pytest.approx(0.123)
    assert done.cost_source == "provider_billed"
    assert done.session_totals is not None
    assert done.session_totals.billed_cost == pytest.approx(0.173)


@pytest.mark.asyncio
async def test_agent_enriches_model_usage_breakdown_with_estimated_costs() -> None:
    tracker = UsageTracker()
    session_key = "agent:test:webchat:ensemble-costs"
    agent = Agent(
        provider=_DoneBreakdownProvider(),
        config=AgentConfig(model_id="z-ai/glm-5.2"),
        usage_tracker=tracker,
        session_key=session_key,
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    # deepseek/deepseek-v4-pro static price is $0.435/M in, $0.87/M out (see
    # engine/pricing.py): (1000 * 0.435 + 1000 * 0.87) / 1e6 == 0.001305.
    assert done.cost_source == "mixed"
    assert done.cost_usd == pytest.approx(0.011305)
    assert done.billed_cost == pytest.approx(0.01)
    deepseek_row = done.model_usage_breakdown[0]
    assert deepseek_row["model"] == "deepseek/deepseek-v4-pro-20260423"
    assert deepseek_row["cost_usd"] == pytest.approx(0.001305)
    assert deepseek_row["estimated_cost_usd"] == pytest.approx(0.001305)
    assert deepseek_row["billed_cost_usd"] == 0.0
    assert deepseek_row["cost_source"] == "opensquilla_estimate"
    aggregator_row = done.model_usage_breakdown[1]
    assert aggregator_row["cost_usd"] == pytest.approx(0.01)
    assert aggregator_row["billed_cost_usd"] == pytest.approx(0.01)
    assert aggregator_row["cost_source"] == "provider_billed"


@pytest.mark.asyncio
async def test_agent_ensemble_breakdown_uses_member_provider_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    tracker = UsageTracker()
    session_key = "agent:test:webchat:tokenrhythm-ensemble-costs"
    agent = Agent(
        provider=_DoneTokenRhythmBreakdownProvider(),
        config=AgentConfig(model_id="glm-5.2", provider_id="tokenrhythm"),
        usage_tracker=tracker,
        session_key=session_key,
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    expected_costs = [
        model_usage_cost_fields(
            model_id=model,
            provider="tokenrhythm",
            input_tokens=1000,
            output_tokens=100,
            billed_cost=0.0,
        )["cost_usd"]
        for model in ("deepseek-v4-pro", "glm-5.2")
    ]
    assert [row["cost_usd"] for row in done.model_usage_breakdown] == pytest.approx(
        expected_costs,
        abs=1e-6,
    )
    breakdown_sum = sum(row["cost_usd"] for row in done.model_usage_breakdown)
    assert done.cost_source == "opensquilla_estimate"
    assert done.cost_usd == pytest.approx(breakdown_sum, abs=1e-6)


@pytest.mark.asyncio
async def test_agent_stops_when_turn_billed_cost_budget_is_exceeded() -> None:
    provider = _DoneUsageProvider(billed_cost=0.25)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_turn_billed_cost_usd=0.1),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(
        event.kind == "error" and event.code == "turn_billed_cost_budget_exceeded"
        for event in events
    )


class _ErrorUsageReceiptProvider:
    """Ensemble-style error that still carries provider_billed receipts."""

    provider_name = "ensemble"

    def __init__(self, breakdown: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[list[Message]] = []
        self.breakdown = breakdown or [
            {
                "role": "proposer",
                "label": "proposer-a",
                "attempt_index": 1,
                "model": "proposer-a",
                "provider": "tokenrhythm",
                "input_tokens": 100,
                "output_tokens": 10,
                "reasoning_tokens": 7,
                "cache_read_tokens": 6,
                "cache_write_tokens": 2,
                "billed_cost": 0.25,
                "cost_source": "provider_billed",
            }
        ]

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderError(
            message="aggregator failed after proposer usage",
            code="500",
            model_usage_breakdown=self.breakdown,
        )

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_agent_stops_when_error_path_billed_cost_budget_is_exceeded() -> None:
    """One trusted Error receipt must reconcile every turn accounting surface."""
    provider = _ErrorUsageReceiptProvider()
    sink = _RecordingUsageSink()
    tracker = UsageTracker()
    session_key = "agent:test:webchat:error-receipt"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_id="ensemble",
            model_id="ensemble/test",
            max_turn_billed_cost_usd=0.1,
            max_iterations=5,
        ),
        usage_event_sink=sink,
        usage_tracker=tracker,
        session_key=session_key,
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    assert any(
        event.kind == "error" and event.code == "turn_billed_cost_budget_exceeded"
        for event in events
    )
    assert len(provider.calls) == 1
    assert len(sink.started) == 1
    assert len(sink.finalized) == 1
    assert sink.unknown == []
    ledger_result = sink.finalized[0][1]
    assert ledger_result.completed_at_ms > 0
    assert ledger_result.billed_cost_nanos == 250_000_000
    assert (
        ledger_result.input_tokens,
        ledger_result.output_tokens,
        ledger_result.reasoning_tokens,
        ledger_result.cache_read_tokens,
        ledger_result.cache_write_tokens,
    ) == (100, 10, 7, 6, 2)
    assert done.input_tokens == 100
    assert done.output_tokens == 10
    assert done.reasoning_tokens == 7
    assert done.cached_tokens == 6
    assert done.cache_write_tokens == 2
    assert done.model == "ensemble/test"
    assert done.provider == "ensemble"
    assert done.cost_usd == pytest.approx(0.25)
    assert done.billed_cost == pytest.approx(0.25)
    assert done.cost_source == "provider_billed"
    assert done.session_totals is not None
    assert done.session_totals.billed_cost == pytest.approx(0.25)
    assert len(done.model_usage_breakdown) == 1
    [usage_row] = done.model_usage_breakdown
    assert usage_row["role"] == "proposer"
    assert usage_row["label"] == "proposer-a"
    assert usage_row["billed_cost_usd"] == pytest.approx(0.25)
    assert usage_row["cost_source"] == "provider_billed"
    tracker_snapshot = tracker.session_snapshot(session_key)
    assert tracker_snapshot is not None
    assert tracker_snapshot.input_tokens == 100
    assert tracker_snapshot.output_tokens == 10
    assert tracker_snapshot.cache_read_tokens == 6
    assert tracker_snapshot.cache_write_tokens == 2
    assert tracker_snapshot.billed_cost == pytest.approx(0.25)


class _DoneThenErrorUsageReceiptProvider:
    provider_name = "ensemble"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo")
            yield ProviderToolUseEnd(
                tool_use_id="tool-1",
                tool_name="echo",
                arguments={"value": "again"},
            )
            yield ProviderDone(
                stop_reason="tool_use",
                input_tokens=50,
                output_tokens=5,
                reasoning_tokens=3,
                cached_tokens=4,
                cache_write_tokens=1,
                billed_cost=0.50,
                cost_source="provider_billed",
                model="aggregator-a",
                model_usage_breakdown=[
                    {
                        "role": "aggregator",
                        "label": "aggregator-a",
                        "attempt_index": 1,
                        "provider": "tokenrhythm",
                        "model": "aggregator-a",
                        "input_tokens": 50,
                        "output_tokens": 5,
                        "reasoning_tokens": 3,
                        "cache_read_tokens": 4,
                        "cache_write_tokens": 1,
                        "billed_cost": 0.50,
                        "cost_source": "provider_billed",
                    }
                ],
            )
            return
        yield ProviderError(
            message="aggregator failed after proposer usage",
            code="500",
            model_usage_breakdown=[
                {
                    "role": "proposer",
                    "label": "proposer-b",
                    "attempt_index": 2,
                    "provider": "tokenrhythm",
                    "model": "proposer-b",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "reasoning_tokens": 7,
                    "cache_read_tokens": 6,
                    "cache_write_tokens": 2,
                    "billed_cost": 0.25,
                    "cost_source": "provider_billed",
                }
            ],
        )

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_done_then_error_receipt_reconciles_gate_ledger_and_report() -> None:
    provider = _DoneThenErrorUsageReceiptProvider()
    sink = _RecordingUsageSink()
    tracker = UsageTracker()
    session_key = "agent:test:webchat:done-then-error-receipt"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_id="ensemble",
            model_id="ensemble/test",
            max_turn_billed_cost_usd=0.60,
            max_iterations=5,
        ),
        tool_definitions=[_echo_definition()],
        tool_handler=_echo_tool,
        usage_event_sink=sink,
        usage_tracker=tracker,
        session_key=session_key,
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 2
    assert any(
        event.kind == "error" and event.code == "turn_billed_cost_budget_exceeded"
        for event in events
    )
    assert len(sink.started) == 2
    assert len(sink.finalized) == 2
    assert sink.unknown == []
    assert sum(result.billed_cost_nanos for _, result in sink.finalized) == 750_000_000
    assert done.billed_cost == pytest.approx(0.75)
    assert done.cost_usd == pytest.approx(0.75)
    assert done.cost_source == "provider_billed"
    assert (
        done.input_tokens,
        done.output_tokens,
        done.reasoning_tokens,
        done.cached_tokens,
        done.cache_write_tokens,
    ) == (150, 15, 10, 10, 3)
    assert done.session_totals is not None
    assert done.session_totals.billed_cost == pytest.approx(0.75)
    assert sum(row["billed_cost_usd"] for row in done.model_usage_breakdown) == pytest.approx(
        0.75
    )
    tracker_snapshot = tracker.session_snapshot(session_key)
    assert tracker_snapshot is not None
    assert tracker_snapshot.billed_cost == pytest.approx(0.75)
    assert tracker_snapshot.input_tokens == 150
    assert tracker_snapshot.output_tokens == 15
    assert tracker_snapshot.cache_read_tokens == 10
    assert tracker_snapshot.cache_write_tokens == 3


def _usd_billing_receipt(
    *,
    status: str,
    amount_nanos: int | None,
    usd_nanos: int | None,
) -> ProviderBillingReceipt:
    return ProviderBillingReceipt(
        currency="USD",
        status=status,  # type: ignore[arg-type]
        amount_nanos=amount_nanos,
        usd_equivalent_nanos=usd_nanos,
        fx_native_per_usd_nanos=1_000_000_000,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row_overrides", "expected_billed", "expect_budget_error"),
    [
        ({"billed_cost": 0.25, "cost_source": "provider_billed"}, 0.25, True),
        ({"billed_cost": 0.25, "cost_source": "openrouter_usage"}, 0.25, True),
        ({"billed_cost": 0.25}, 0.25, True),
        ({"billed_cost_usd": 0.25, "cost_source": "provider_billed"}, 0.25, True),
        ({"billedCost": 0.25, "costSource": "provider_billed"}, 0.25, True),
        (
            {
                "billed_cost": 0.05,
                "billing_receipt": _usd_billing_receipt(
                    status="confirmed",
                    amount_nanos=300_000_000,
                    usd_nanos=300_000_000,
                ),
            },
            0.30,
            True,
        ),
        (
            {
                "billed_cost": 0.0,
                "billing_receipt": _usd_billing_receipt(
                    status="confirmed",
                    amount_nanos=0,
                    usd_nanos=0,
                ),
            },
            0.0,
            False,
        ),
        (
            {
                "billed_cost": 0.25,
                "cost_source": "provider_billed",
                "billing_receipt": _usd_billing_receipt(
                    status="pending",
                    amount_nanos=250_000_000,
                    usd_nanos=None,
                ),
            },
            0.0,
            False,
        ),
    ],
    ids=[
        "provider-billed",
        "openrouter-usage",
        "legacy-positive-bill",
        "billed-cost-usd-alias",
        "camel-case-aliases",
        "confirmed-authoritative-amount",
        "confirmed-zero",
        "pending",
    ],
)
async def test_error_receipt_variants_use_canonical_billed_budget_semantics(
    row_overrides: dict[str, Any],
    expected_billed: float,
    expect_budget_error: bool,
) -> None:
    provider = _ErrorUsageReceiptProvider(
        [
            {
                "role": "proposer",
                "label": "variant",
                "provider": "fake",
                "model": "model-a",
                "input_tokens": 10,
                "output_tokens": 2,
                **row_overrides,
            }
        ]
    )
    sink = _RecordingUsageSink()
    tracker = UsageTracker()
    session_key = f"agent:test:webchat:error-variant:{expected_billed}"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_id="fake",
            model_id="model-a",
            max_provider_retries=0,
            max_turn_billed_cost_usd=0.10,
        ),
        usage_event_sink=sink,
        usage_tracker=tracker,
        session_key=session_key,
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    assert (
        any(
            event.kind == "error"
            and event.code == "turn_billed_cost_budget_exceeded"
            for event in events
        )
        is expect_budget_error
    )
    assert len(provider.calls) == 1
    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].billed_cost_nanos == round(
        expected_billed * 1_000_000_000
    )
    assert done.billed_cost == pytest.approx(expected_billed)
    assert done.session_totals is not None
    assert done.session_totals.billed_cost == pytest.approx(expected_billed)
    tracker_snapshot = tracker.session_snapshot(session_key)
    assert tracker_snapshot is not None
    assert tracker_snapshot.billed_cost == pytest.approx(expected_billed)
    assert sum(
        row["billed_cost_usd"] for row in done.model_usage_breakdown
    ) == pytest.approx(expected_billed)
    receipt = row_overrides.get("billing_receipt")
    if isinstance(receipt, ProviderBillingReceipt) and receipt.status == "pending":
        assert all(
            row["cost_source"] != "provider_billed"
            for row in done.model_usage_breakdown
        )


class _DoneAndErrorSamePhysicalCallProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls += 1
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderDone(
            stop_reason="stop",
            input_tokens=10,
            output_tokens=2,
            billed_cost=0.25,
            cost_source="provider_billed",
            provider="fake",
            model="model-a",
        )
        yield ProviderError(
            message="late duplicate terminal event",
            code="500",
            model_usage_breakdown=[
                {
                    "provider": "fake",
                    "model": "model-a",
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "billed_cost": 0.25,
                    "cost_source": "provider_billed",
                }
            ],
        )

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_done_and_error_for_one_physical_call_are_counted_once() -> None:
    provider = _DoneAndErrorSamePhysicalCallProvider()
    sink = _RecordingUsageSink()
    tracker = UsageTracker()
    session_key = "agent:test:webchat:duplicate-terminal"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_id="fake",
            model_id="model-a",
            max_provider_retries=0,
        ),
        usage_event_sink=sink,
        usage_tracker=tracker,
        session_key=session_key,
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    assert provider.calls == 1
    assert len(sink.started) == 1
    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].billed_cost_nanos == 250_000_000
    assert done.billed_cost == pytest.approx(0.25)
    assert done.input_tokens == 10
    assert done.output_tokens == 2
    tracker_snapshot = tracker.session_snapshot(session_key)
    assert tracker_snapshot is not None
    assert tracker_snapshot.billed_cost == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_two_billed_error_retries_are_each_counted_once() -> None:
    provider = _ErrorUsageReceiptProvider()
    provider.provider_name = "fake"
    sink = _RecordingUsageSink()
    tracker = UsageTracker()
    session_key = "agent:test:webchat:error-retries"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_id="fake",
            model_id="model-a",
            max_iterations=5,
            max_provider_retries=2,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            max_turn_billed_cost_usd=0.60,
        ),
        usage_event_sink=sink,
        usage_tracker=tracker,
        session_key=session_key,
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    assert len(provider.calls) == 3
    assert any(
        event.kind == "error" and event.code == "turn_billed_cost_budget_exceeded"
        for event in events
    )
    assert len(sink.started) == 3
    assert len(sink.finalized) == 3
    assert {call.event_id for call, _ in sink.finalized} == {
        call.event_id for call in sink.started
    }
    assert sum(result.billed_cost_nanos for _, result in sink.finalized) == 750_000_000
    assert done.billed_cost == pytest.approx(0.75)
    tracker_snapshot = tracker.session_snapshot(session_key)
    assert tracker_snapshot is not None
    assert tracker_snapshot.billed_cost == pytest.approx(0.75)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tracker", [False, True])
async def test_provider_only_error_receipt_keeps_billed_report_without_model(
    with_tracker: bool,
) -> None:
    provider = _ErrorUsageReceiptProvider(
        [
            {
                "provider": "fake",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": 0.25,
                "cost_source": "provider_billed",
            }
        ]
    )
    sink = _RecordingUsageSink()
    tracker = UsageTracker() if with_tracker else None
    session_key = "agent:test:webchat:provider-only-error-receipt"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_id="fake",
            max_provider_retries=0,
            max_turn_billed_cost_usd=0.10,
        ),
        usage_event_sink=sink,
        usage_tracker=tracker,
        session_key=session_key,
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    assert done.model == "unknown"
    assert done.provider == "fake"
    assert done.billed_cost == pytest.approx(0.25)
    assert done.model_usage_breakdown[0]["model"] == "unknown"
    assert done.model_usage_breakdown[0]["billed_cost_usd"] == pytest.approx(0.25)
    if tracker is not None:
        assert done.session_totals is not None
        assert done.session_totals.billed_cost == pytest.approx(0.25)
        tracker_snapshot = tracker.session_snapshot(session_key)
        assert tracker_snapshot is not None
        assert tracker_snapshot.billed_cost == pytest.approx(0.25)
    else:
        assert done.session_totals is None


@pytest.mark.asyncio
@pytest.mark.parametrize("include_billed_row", [False, True])
async def test_error_receipt_report_uses_member_prices_without_tracker(
    include_billed_row: bool,
) -> None:
    pending_row = {
        "role": "proposer",
        "label": "pending",
        "provider": "tokenrhythm",
        "model": "deepseek-v4-pro",
        "input_tokens": 1_000,
        "output_tokens": 100,
        "billed_cost": 0.25,
        "cost_source": "provider_billed",
        "billing_receipt": _usd_billing_receipt(
            status="pending",
            amount_nanos=250_000_000,
            usd_nanos=None,
        ),
    }
    billed_row = {
        "role": "proposer",
        "label": "billed",
        "provider": "tokenrhythm",
        "model": "glm-5.2",
        "input_tokens": 100,
        "output_tokens": 10,
        "billed_cost": 0.25,
        "cost_source": "provider_billed",
    }
    rows = [billed_row, pending_row] if include_billed_row else [pending_row]
    provider = _ErrorUsageReceiptProvider(rows)
    sink = _RecordingUsageSink()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_id="ensemble",
            model_id="outer-unpriced",
            max_provider_retries=0,
        ),
        usage_event_sink=sink,
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].estimated_cost_nanos == 0
    breakdown_cost = sum(row["cost_usd"] for row in done.model_usage_breakdown)
    breakdown_billed = sum(
        row["billed_cost_usd"] for row in done.model_usage_breakdown
    )
    assert done.cost_usd == pytest.approx(breakdown_cost)
    assert done.billed_cost == pytest.approx(breakdown_billed)
    assert done.cost_source == (
        "mixed" if include_billed_row else "opensquilla_estimate"
    )
    assert all(row["model"] != "outer-unpriced" for row in done.model_usage_breakdown)


@pytest.mark.asyncio
async def test_agent_stops_when_turn_tool_error_budget_is_exceeded() -> None:
    provider = _LoopingToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=100,
            max_turn_input_tokens=0,
            max_turn_output_tokens=0,
            max_turn_billed_cost_usd=0,
            max_turn_tool_errors=2,
        ),
        tool_definitions=[_echo_definition()],
        tool_handler=_error_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(
        event.kind == "error" and event.code == "turn_tool_error_budget_exceeded"
        for event in events
    )
