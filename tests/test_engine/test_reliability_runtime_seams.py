from __future__ import annotations

import asyncio
import contextlib
import json
import types
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import (
    AnswerGenerationResetEvent,
    ControlTerminalReason,
    DoneEvent,
    ErrorEvent,
    RouterControlReplayEvent,
    RunHeartbeatEvent,
    TextDeltaEvent,
    ToolCall,
    ToolResultEvent,
)
from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEndEvent
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStartEvent
from opensquilla.telemetry.contracts.common import ResultOutcome
from opensquilla.telemetry.contracts.reliability import (
    ToolCategory,
    ToolErrorCode,
    ToolOutcome,
    TurnErrorCode,
    TurnFailureStage,
)
from opensquilla.telemetry.runtime_facts import (
    ToolCallReliabilityFacts,
    TurnReliabilityFacts,
    classify_control_terminal,
    mark_current_turn_failure_stage,
    tool_category_for_name,
)
from opensquilla.tools.types import ToolContext


class _SeamRunner(TurnRunner):
    def __init__(
        self,
        stream_factory: Any,
        sink: Any,
        *,
        clock: Any | None = None,
        growth_started_sink: Any | None = None,
        growth_succeeded_sink: Any | None = None,
    ) -> None:
        self._stream_factory = stream_factory
        self._turn_reliability_sink = sink
        self._turn_growth_started_sink = growth_started_sink
        self._turn_growth_succeeded_sink = growth_succeeded_sink
        if clock is not None:
            self._reliability_clock = clock

    async def _run_without_reliability(self, *_args: Any, **_kwargs: Any) -> Any:
        async with contextlib.aclosing(self._stream_factory()) as stream:
            async for event in stream:
                yield event


async def _collect_turn(runner: TurnRunner) -> list[Any]:
    return [
        event
        async for event in runner.run(
            "private prompt",
            "agent:main:test",
            ToolContext(),
        )
    ]


@pytest.mark.asyncio
async def test_turn_seam_reports_public_ttft_and_one_fifteen_second_stall(
) -> None:
    async def stream() -> AsyncIterator[Any]:
        yield TextDeltaEvent(text="private answer")
        yield RunHeartbeatEvent(idle_ms=15_050, message="still running")
        yield DoneEvent(text="private answer")

    clock = iter((100.0, 100.25, 115.30, 115.40, 115.40))
    facts: list[TurnReliabilityFacts] = []

    events = await _collect_turn(
        _SeamRunner(stream, facts.append, clock=lambda: next(clock))
    )

    assert [event.kind for event in events] == ["text_delta", "run_heartbeat", "done"]
    assert facts == [
        TurnReliabilityFacts(
            outcome=ResultOutcome.SUCCESS,
            error_code=None,
            failure_stage=None,
            duration_ms=15_400,
            ttft_ms=250,
            stall_count=1,
        )
    ]
    serialized = repr(asdict(facts[0]))
    assert "private" not in serialized


@pytest.mark.asyncio
async def test_turn_seam_classifies_error_without_retaining_error_text() -> None:
    async def stream() -> AsyncIterator[Any]:
        mark_current_turn_failure_stage(TurnFailureStage.AGENT_EXECUTION)
        yield ErrorEvent(message="SECRET provider body", code="total_timeout")

    facts: list[TurnReliabilityFacts] = []
    events = await _collect_turn(_SeamRunner(stream, facts.append))

    assert events[0].message == "SECRET provider body"
    assert len(facts) == 1
    assert facts[0].outcome is ResultOutcome.TIMEOUT
    assert facts[0].error_code is TurnErrorCode.HARD_DEADLINE
    assert facts[0].failure_stage is TurnFailureStage.AGENT_EXECUTION
    assert "SECRET" not in repr(asdict(facts[0]))


@pytest.mark.asyncio
async def test_turn_seam_aclose_is_exactly_once_cancel() -> None:
    closed = asyncio.Event()

    async def stream() -> AsyncIterator[Any]:
        try:
            yield TextDeltaEvent(text="visible")
            await asyncio.Event().wait()
        finally:
            closed.set()

    facts: list[TurnReliabilityFacts] = []
    iterator = _SeamRunner(stream, facts.append).run("prompt", "session", ToolContext())

    assert isinstance(await anext(iterator), TextDeltaEvent)
    await iterator.aclose()

    assert closed.is_set()
    assert len(facts) == 1
    assert facts[0].outcome is ResultOutcome.CANCEL
    assert facts[0].error_code is TurnErrorCode.UNKNOWN
    assert facts[0].failure_stage is TurnFailureStage.TURN_SETUP


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "expected_outcome", "expected_error_code"),
    [
        (DoneEvent(text="ok"), ResultOutcome.SUCCESS, None),
        (
            ErrorEvent(message="private", code="total_timeout"),
            ResultOutcome.TIMEOUT,
            TurnErrorCode.HARD_DEADLINE,
        ),
    ],
)
async def test_turn_seam_aclose_preserves_observed_terminal_result(
    terminal: Any,
    expected_outcome: ResultOutcome,
    expected_error_code: TurnErrorCode | None,
) -> None:
    async def stream() -> AsyncIterator[Any]:
        yield terminal
        raise AssertionError("consumer should close after terminal")

    facts: list[TurnReliabilityFacts] = []
    iterator = _SeamRunner(stream, facts.append).run(
        "private prompt",
        "agent:main:test",
        ToolContext(),
    )

    assert await anext(iterator) is terminal
    await iterator.aclose()

    assert len(facts) == 1
    assert facts[0].outcome is expected_outcome
    assert facts[0].error_code is expected_error_code


@pytest.mark.asyncio
async def test_turn_seam_classifies_terminal_generation_reset_before_aclose() -> None:
    terminal = AnswerGenerationResetEvent(
        terminal=True,
        terminal_text_snapshot="private friendly failure",
        authoritative_text_snapshot="private draft",
        terminal_error_message="SECRET provider body",
        terminal_error_code="total_timeout",
        terminal_failure_kind="transport_transient",
    )

    async def stream() -> AsyncIterator[Any]:
        yield terminal
        raise AssertionError("consumer should close after terminal")

    facts: list[TurnReliabilityFacts] = []
    iterator = _SeamRunner(stream, facts.append).run(
        "private prompt",
        "agent:main:test",
        ToolContext(),
    )

    assert await anext(iterator) is terminal
    await iterator.aclose()

    assert len(facts) == 1
    assert facts[0].outcome is ResultOutcome.TIMEOUT
    assert facts[0].error_code is TurnErrorCode.HARD_DEADLINE
    serialized = repr(asdict(facts[0]))
    assert "private" not in serialized
    assert "SECRET" not in serialized


@pytest.mark.asyncio
async def test_public_runner_aclose_synchronously_closes_internal_turn_stream() -> None:
    closed = asyncio.Event()

    async def fake_run_turn(
        _self: TurnRunner,
        *_args: Any,
        **_kwargs: Any,
    ) -> AsyncIterator[Any]:
        try:
            yield TextDeltaEvent(text="visible")
            await asyncio.Event().wait()
        finally:
            closed.set()

    runner = TurnRunner(provider_selector=None)
    runner._run_turn = types.MethodType(fake_run_turn, runner)
    iterator = runner.run("prompt", "agent:main:test", ToolContext())

    assert isinstance(await anext(iterator), TextDeltaEvent)
    await iterator.aclose()

    assert closed.is_set()


@pytest.mark.asyncio
async def test_turn_sink_failure_does_not_change_stream() -> None:
    async def stream() -> AsyncIterator[Any]:
        yield DoneEvent(text="ok")

    def broken_sink(_facts: TurnReliabilityFacts) -> None:
        raise RuntimeError("observer unavailable")

    events = await _collect_turn(_SeamRunner(stream, broken_sink))

    assert len(events) == 1
    assert isinstance(events[0], DoneEvent)


@pytest.mark.asyncio
async def test_growth_seam_publishes_only_content_free_started_and_done_boundaries() -> None:
    async def stream() -> AsyncIterator[Any]:
        yield TextDeltaEvent(text="private answer")
        yield DoneEvent(text="private answer")

    boundaries: list[str] = []
    runner = _SeamRunner(
        stream,
        lambda _facts: None,
        growth_started_sink=lambda: boundaries.append("started"),
        growth_succeeded_sink=lambda: boundaries.append("succeeded"),
    )

    events = await _collect_turn(runner)

    assert [event.kind for event in events] == ["text_delta", "done"]
    assert boundaries == ["started", "succeeded"]
    assert "private" not in repr(boundaries)


@pytest.mark.asyncio
async def test_growth_success_settles_before_consumer_closes_after_done() -> None:
    async def stream() -> AsyncIterator[Any]:
        yield DoneEvent(text="ok")
        raise AssertionError("consumer should close after terminal")

    boundaries: list[str] = []
    runner = _SeamRunner(
        stream,
        lambda _facts: None,
        growth_started_sink=lambda: boundaries.append("started"),
        growth_succeeded_sink=lambda: boundaries.append("succeeded"),
    )
    iterator = runner.run("private", "session", ToolContext())

    assert isinstance(await anext(iterator), DoneEvent)
    await iterator.aclose()

    assert boundaries == ["started", "succeeded"]


@pytest.mark.asyncio
async def test_growth_seam_excludes_non_user_and_subagent_runs() -> None:
    async def stream() -> AsyncIterator[Any]:
        yield DoneEvent(text="ok")

    boundaries: list[str] = []
    runner = _SeamRunner(
        stream,
        lambda _facts: None,
        growth_started_sink=lambda: boundaries.append("started"),
        growth_succeeded_sink=lambda: boundaries.append("succeeded"),
    )

    _ = [
        event
        async for event in runner.run(
            "internal",
            "session",
            ToolContext(),
            input_mode="system",
        )
    ]
    _ = [
        event
        async for event in runner.run(
            "internal",
            "session",
            ToolContext(),
            run_kind="subagent",
        )
    ]

    assert boundaries == []


def test_control_terminal_strenum_is_classified_by_closed_value() -> None:
    assert classify_control_terminal(ControlTerminalReason.HARD_DEADLINE) == (
        ResultOutcome.TIMEOUT,
        TurnErrorCode.HARD_DEADLINE,
    )


def test_mcp_tool_name_is_reduced_to_closed_category() -> None:
    assert tool_category_for_name("mcp_private_remote_name") is ToolCategory.MCP_EXTENSION
    assert classify_control_terminal("not-a-terminal-reason") == (
        ResultOutcome.CANCEL,
        TurnErrorCode.UNKNOWN,
    )


class _OneToolProvider:
    provider_name = "test"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderToolUseStartEvent(
            tool_use_id="opaque-call-id",
            tool_name="private_custom_tool",
        )
        yield ProviderToolUseEndEvent(
            tool_use_id="opaque-call-id",
            tool_name="private_custom_tool",
            arguments={"private_argument": "SECRET"},
        )
        yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _one_tool_agent(handler: Any) -> Agent:
    return Agent(
        provider=_OneToolProvider(),
        config=AgentConfig(max_iterations=1, tool_timeout=0.03),
        tool_definitions=[
            ToolDefinition(
                name="private_custom_tool",
                description="private tool description",
                input_schema=ToolInputSchema(
                    properties={"private_argument": {"type": "string"}},
                    required=["private_argument"],
                ),
            )
        ],
        tool_handler=handler,
    )


@pytest.mark.asyncio
async def test_agent_settles_tool_fact_once_without_private_fields() -> None:
    async def handler(tc: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="SECRET result body",
        )

    facts: list[ToolCallReliabilityFacts] = []
    agent = _one_tool_agent(handler)
    agent.set_tool_reliability_sink(facts.append)

    events = [event async for event in agent.run_turn("SECRET prompt")]

    assert any(event.kind == "tool_result" for event in events)
    assert facts == [
        ToolCallReliabilityFacts(
            tool_category=ToolCategory.OTHER,
            outcome=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=facts[0].duration_ms,
            retry_count=0,
        )
    ]
    serialized = repr(asdict(facts[0]))
    assert "SECRET" not in serialized
    assert "private" not in serialized
    assert "opaque" not in serialized


@pytest.mark.asyncio
async def test_aclose_after_terminal_tool_event_keeps_success_fact() -> None:
    async def handler(tc: ToolCall) -> ToolResult:
        return ToolResult(tc.tool_use_id, tc.tool_name, "completed")

    facts: list[ToolCallReliabilityFacts] = []
    agent = _one_tool_agent(handler)
    agent.set_tool_reliability_sink(facts.append)
    iterator = agent.run_turn("run")
    try:
        while True:
            event = await anext(iterator)
            if isinstance(event, ToolResultEvent):
                break
        await iterator.aclose()

        assert len(facts) == 1
        assert facts[0].outcome is ToolOutcome.SUCCESS
        assert facts[0].error_code is None
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
async def test_agent_tool_timeout_reports_timeout_not_inner_cancellation() -> None:
    async def handler(_tc: ToolCall) -> ToolResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    facts: list[ToolCallReliabilityFacts] = []
    agent = _one_tool_agent(handler)
    agent.set_tool_reliability_sink(facts.append)

    await asyncio.wait_for(
        _collect_agent(agent),
        timeout=1,
    )

    assert len(facts) == 1
    assert facts[0].outcome is ToolOutcome.TIMEOUT
    assert facts[0].error_code is ToolErrorCode.TOOL_TIMEOUT


def test_agent_retry_tracker_settles_once_with_retry_count() -> None:
    async def handler(_tc: ToolCall) -> ToolResult:
        raise AssertionError("not dispatched")

    facts: list[ToolCallReliabilityFacts] = []
    agent = _one_tool_agent(handler)
    agent.set_tool_reliability_sink(facts.append)

    first = agent._begin_tool_reliability_attempt(
        tool_use_id="logical-call",
        tool_name="private_custom_tool",
    )
    agent._end_tool_reliability_attempt(
        tool_use_id="logical-call",
        started_at=first,
    )
    second = agent._begin_tool_reliability_attempt(
        tool_use_id="logical-call",
        tool_name="private_custom_tool",
    )
    agent._end_tool_reliability_attempt(
        tool_use_id="logical-call",
        started_at=second,
    )
    result = ToolResult(
        tool_use_id="logical-call",
        tool_name="private_custom_tool",
        content="SECRET",
    )
    agent._settle_tool_reliability(tool_use_id="logical-call", result=result)
    agent._settle_tool_reliability(tool_use_id="logical-call", result=result)

    assert len(facts) == 1
    assert facts[0].retry_count == 1


@pytest.mark.asyncio
async def test_agent_tool_sink_failure_does_not_change_result_stream() -> None:
    async def handler(tc: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="still delivered",
        )

    def broken_sink(_facts: ToolCallReliabilityFacts) -> None:
        raise RuntimeError("observer unavailable")

    agent = _one_tool_agent(handler)
    agent.set_tool_reliability_sink(broken_sink)

    events = await _collect_agent(agent)

    assert any(
        event.kind == "tool_result" and event.result == "still delivered"
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_turn_cancellation_flushes_active_tool_once() -> None:
    started = asyncio.Event()

    async def handler(_tc: ToolCall) -> ToolResult:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    facts: list[ToolCallReliabilityFacts] = []
    agent = _one_tool_agent(handler)
    agent.set_tool_reliability_sink(facts.append)
    task = asyncio.create_task(_collect_agent(agent))
    await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(facts) == 1
    assert facts[0].outcome is ToolOutcome.CANCEL
    assert facts[0].error_code is ToolErrorCode.CANCELLED


async def _collect_agent(agent: Agent) -> list[Any]:
    return [event async for event in agent.run_turn("run")]


class _ToolBatchProvider:
    provider_name = "test"

    def __init__(self, calls: list[tuple[str, str, dict[str, Any]]]) -> None:
        self._tool_calls = calls
        self.call_count = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.call_count += 1
        return self._stream(self.call_count)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > 1:
            yield ProviderTextDeltaEvent(text="done")
            yield ProviderDoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        for tool_use_id, tool_name, arguments in self._tool_calls:
            yield ProviderToolUseStartEvent(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
            )
            yield ProviderToolUseEndEvent(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                arguments=arguments,
            )
        yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _definition(
    name: str,
    properties: dict[str, Any] | None = None,
    *,
    required: list[str] | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="test definition",
        input_schema=ToolInputSchema(
            properties=properties or {},
            required=list(properties or {}) if required is None else required,
        ),
    )


@pytest.mark.asyncio
async def test_parallel_calls_each_settle_exactly_once() -> None:
    provider = _ToolBatchProvider(
        [
            ("read-call", "read_file", {}),
            ("search-call", "web_search", {}),
        ]
    )
    both_active = asyncio.Event()
    active = 0

    async def handler(tc: ToolCall) -> ToolResult:
        nonlocal active
        active += 1
        if active == 2:
            both_active.set()
        await asyncio.wait_for(both_active.wait(), timeout=1)
        active -= 1
        return ToolResult(tc.tool_use_id, tc.tool_name, "SECRET result")

    facts: list[ToolCallReliabilityFacts] = []
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2, max_safe_tool_concurrency=2),
        tool_definitions=[_definition("read_file"), _definition("web_search")],
        tool_handler=handler,
    )
    agent.set_tool_reliability_sink(facts.append)

    await _collect_agent(agent)

    assert len(facts) == 2
    assert {fact.tool_category for fact in facts} == {
        ToolCategory.FILESYSTEM_READ,
        ToolCategory.SEARCH,
    }
    assert all(fact.outcome is ToolOutcome.SUCCESS for fact in facts)


@pytest.mark.asyncio
async def test_dispatch_boundary_trailing_call_is_not_reported() -> None:
    provider = _ToolBatchProvider(
        [
            ("checkpoint", "plan_run_checkpoint", {}),
            ("trailing-write", "write_file", {}),
        ]
    )
    dispatched: list[str] = []

    async def handler(tc: ToolCall) -> ToolResult:
        dispatched.append(tc.tool_name)
        return ToolResult(
            tc.tool_use_id,
            tc.tool_name,
            "boundary",
            terminates_turn=tc.tool_name == "plan_run_checkpoint",
        )

    facts: list[ToolCallReliabilityFacts] = []
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[
            _definition("plan_run_checkpoint"),
            _definition("write_file"),
        ],
        tool_handler=handler,
    )
    agent.set_tool_reliability_sink(facts.append)

    events = await _collect_agent(agent)

    assert dispatched == ["plan_run_checkpoint"]
    assert len(facts) == 1
    assert facts[0].outcome is ToolOutcome.SUCCESS
    trailing = next(
        event
        for event in events
        if isinstance(event, ToolResultEvent)
        and event.tool_use_id == "trailing-write"
    )
    assert json.loads(trailing.result)["status"] == "not_executed"


@pytest.mark.asyncio
async def test_iteration_batch_deadline_reports_timeouts_not_cancellations() -> None:
    provider = _ToolBatchProvider(
        [("slow-read", "read_file", {}), ("slow-search", "web_search", {})]
    )

    async def handler(_tc: ToolCall) -> ToolResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    facts: list[ToolCallReliabilityFacts] = []
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            iteration_timeout=0.03,
            tool_timeout=1,
            max_safe_tool_concurrency=1,
        ),
        tool_definitions=[_definition("read_file"), _definition("web_search")],
        tool_handler=handler,
    )
    agent.set_tool_reliability_sink(facts.append)

    await asyncio.wait_for(_collect_agent(agent), timeout=1)

    assert len(facts) == 2
    assert all(fact.outcome is ToolOutcome.TIMEOUT for fact in facts)
    assert all(fact.error_code is ToolErrorCode.TOOL_TIMEOUT for fact in facts)


@pytest.mark.asyncio
async def test_meta_invoke_special_path_settles_once() -> None:
    provider = _ToolBatchProvider(
        [("meta-call", "meta_invoke", {"name": "private-meta-name"})]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        tool_definitions=[
            _definition("meta_invoke", {"name": {"type": "string"}})
        ],
        tool_handler=None,
    )

    async def fake_meta_stream(
        _self: Agent,
        tc: ToolCall,
        _ctx: ToolContext,
    ) -> AsyncIterator[Any]:
        yield ToolResult(
            tc.tool_use_id,
            tc.tool_name,
            "SECRET meta result",
            terminates_turn=True,
        )

    agent._run_one_streaming = types.MethodType(fake_meta_stream, agent)
    facts: list[ToolCallReliabilityFacts] = []
    agent.set_tool_reliability_sink(facts.append)

    await _collect_agent(agent)

    assert facts == [
        ToolCallReliabilityFacts(
            tool_category=ToolCategory.COLLABORATION,
            outcome=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=facts[0].duration_ms,
            retry_count=0,
        )
    ]
    assert "private-meta-name" not in repr(asdict(facts[0]))


@pytest.mark.asyncio
async def test_router_replay_event_cannot_preempt_successful_tool_settlement() -> None:
    provider = _ToolBatchProvider([("router-call", "router_control", {})])

    async def handler(tc: ToolCall) -> ToolResult:
        return ToolResult(
            tc.tool_use_id,
            tc.tool_name,
            json.dumps(
                {
                    "status": "router_control",
                    "accepted": True,
                    "replay_required": True,
                    "action": "set_hold",
                    "target_tier": "private-tier",
                }
            ),
        )

    facts: list[ToolCallReliabilityFacts] = []
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        tool_definitions=[_definition("router_control")],
        tool_handler=handler,
    )
    agent.set_tool_reliability_sink(facts.append)
    iterator = agent.run_turn("run")
    try:
        while True:
            event = await anext(iterator)
            if isinstance(event, RouterControlReplayEvent):
                break
        await iterator.aclose()

        assert len(facts) == 1
        assert facts[0].outcome is ToolOutcome.SUCCESS
        assert "private-tier" not in repr(asdict(facts[0]))
    finally:
        await iterator.aclose()


class _ApprovalProvider:
    provider_name = "test"

    def __init__(self) -> None:
        self.call_count = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.call_count += 1
        return self._stream(self.call_count)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > 1:
            yield ProviderTextDeltaEvent(text="done")
            yield ProviderDoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        yield ProviderToolUseStartEvent(tool_use_id="approval-call", tool_name="exec_command")
        yield ProviderToolUseEndEvent(
            tool_use_id="approval-call",
            tool_name="exec_command",
            arguments={"command": "SECRET command"},
        )
        yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _approval_agent(handler: Any) -> Agent:
    return Agent(
        provider=_ApprovalProvider(),
        config=AgentConfig(max_iterations=2),
        tool_definitions=[
            _definition(
                "exec_command",
                {
                    "command": {"type": "string"},
                    "approval_id": {"type": "string"},
                },
                required=["command"],
            )
        ],
        tool_handler=handler,
    )


def _approval_id(call: ToolCall) -> str | None:
    if call.continuation is not None:
        return call.continuation.approval_id
    raw = call.arguments.get("approval_id")
    return raw if isinstance(raw, str) else None


@pytest.mark.parametrize(
    ("approved", "expected_outcome", "expected_error", "expected_retries"),
    [
        (True, ToolOutcome.SUCCESS, None, 1),
        (False, ToolOutcome.DENIED, ToolErrorCode.POLICY_DENIED, 0),
    ],
)
@pytest.mark.asyncio
async def test_approval_resume_and_denial_settle_one_logical_fact(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    approved: bool,
    expected_outcome: ToolOutcome,
    expected_error: ToolErrorCode | None,
    expected_retries: int,
) -> None:
    from opensquilla.application import approval_queue as approval_queue_module

    monkeypatch.setattr(
        approval_queue_module,
        "_DEFAULT_APPROVAL_QUEUE_PATH",
        tmp_path / "approval_queue.sqlite",
    )
    reset_approval_queue()

    async def handler(call: ToolCall) -> ToolResult:
        approval_id = _approval_id(call)
        if approval_id is None:
            approval_id = get_approval_queue().request(
                "exec",
                {"toolName": call.tool_name, "command": call.arguments["command"]},
            )
            return ToolResult(
                call.tool_use_id,
                call.tool_name,
                json.dumps(
                    {"status": "approval_required", "approval_id": approval_id}
                ),
            )
        return ToolResult(call.tool_use_id, call.tool_name, "completed")

    facts: list[ToolCallReliabilityFacts] = []
    agent = _approval_agent(handler)
    agent.set_tool_reliability_sink(facts.append)
    try:
        async for event in agent.run_turn("run"):
            if isinstance(event, ToolResultEvent) and "approval_required" in event.result:
                approval_id = str(json.loads(event.result)["approval_id"])
                get_approval_queue().resolve(approval_id, approved)

        assert len(facts) == 1
        assert facts[0].outcome is expected_outcome
        assert facts[0].error_code is expected_error
        assert facts[0].retry_count == expected_retries
    finally:
        reset_approval_queue()


@pytest.mark.asyncio
async def test_closing_at_pending_approval_reports_cancel_not_internal_error(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.application import approval_queue as approval_queue_module

    monkeypatch.setattr(
        approval_queue_module,
        "_DEFAULT_APPROVAL_QUEUE_PATH",
        tmp_path / "approval_queue.sqlite",
    )
    reset_approval_queue()

    async def handler(call: ToolCall) -> ToolResult:
        approval_id = get_approval_queue().request(
            "exec",
            {"toolName": call.tool_name, "command": call.arguments["command"]},
        )
        return ToolResult(
            call.tool_use_id,
            call.tool_name,
            json.dumps({"status": "approval_required", "approval_id": approval_id}),
        )

    facts: list[ToolCallReliabilityFacts] = []
    agent = _approval_agent(handler)
    agent.set_tool_reliability_sink(facts.append)
    iterator = agent.run_turn("run")
    try:
        while True:
            event = await anext(iterator)
            if isinstance(event, ToolResultEvent) and "approval_required" in event.result:
                break
        await iterator.aclose()

        assert len(facts) == 1
        assert facts[0].outcome is ToolOutcome.CANCEL
        assert facts[0].error_code is ToolErrorCode.CANCELLED
    finally:
        await iterator.aclose()
        reset_approval_queue()
