"""Pin the subagent → parent turn usage rollup contract.

A turn that delegates work to subagents must report the total spend of
the turn: the parent agent's own provider calls plus every completed
child run it spawned. Before this contract existed the child DoneEvent's
usage fields were discarded in ``SubagentManager.spawn``'s consumer
loop, so per-turn cost systematically under-reported delegated work.

Boundary notes:

- Only child runs completed before the parent terminal event are rolled
  into that event.
- The parent delta is captured first, then completed child rows are added
  to the in-memory UsageTracker without writing another durable ledger
  event. This preserves cumulative session snapshots without counting
  the child twice in the current turn.
- A late child remains ledger-only. Production creates a new manager for
  the next turn, so late usage must never drift into a different turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.subagent import SubagentManager, SubagentSpec
from opensquilla.engine.turn_runner.turn_finalizer_stage import _turn_usage_payload
from opensquilla.engine.types import AgentEvent, ToolCall
from opensquilla.engine.types import DoneEvent as EngineDoneEvent
from opensquilla.engine.types import ErrorEvent as EngineErrorEvent
from opensquilla.engine.types import TextDeltaEvent as EngineTextDeltaEvent
from opensquilla.engine.usage import UsageTracker
from opensquilla.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEndEvent
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStartEvent


class _ScriptedChildAgent:
    """Fake child agent that replays a fixed engine-event stream."""

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events

    async def run_turn(self, _task: str) -> AsyncIterator[AgentEvent]:
        for event in self._events:
            yield event


class _HangingChildAgent:
    """Fake child that never reaches a terminal event (abort target)."""

    async def run_turn(self, _task: str) -> AsyncIterator[AgentEvent]:
        yield EngineTextDeltaEvent(text="partial")
        await asyncio.Event().wait()


class _GatedChildAgent:
    """Child that finishes only after the parent turn has already ended."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def run_turn(self, _task: str) -> AsyncIterator[AgentEvent]:
        await self._gate.wait()
        yield _child_done()


def _child_done(
    *,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    reasoning_tokens: int = 7,
    cached_tokens: int = 50,
    cache_write_tokens: int = 25,
    cost_usd: float = 0.5,
    billed_cost: float = 0.5,
    cost_source: str = "provider_billed",
    estimate_basis: str | None = None,
    missing_cost_entries: int = 0,
    model: str = "deepseek/deepseek-v4-pro",
    provider: str = "openai",
    model_usage_breakdown: list[dict[str, Any]] | None = None,
) -> EngineDoneEvent:
    return EngineDoneEvent(
        text="child result",
        text_snapshot="child result",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_usd=cost_usd,
        billed_cost=billed_cost,
        cost_source=cost_source,
        estimate_basis=estimate_basis,
        missing_cost_entries=missing_cost_entries,
        model=model,
        provider=provider,
        model_usage_breakdown=model_usage_breakdown or [],
    )


class _SpawningToolProvider:
    """Two-step parent provider: one tool call, then the final answer.

    The parent's own billed spend across the turn is 0.03 + 0.04 = 0.07
    with 70 input / 7 output tokens, mirroring the ensemble breakdown
    fixture in test_agent_usage_tracker_billed_propagation.py.
    """

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
        return self._stream(self.calls)

    async def _stream(self, call: int) -> AsyncIterator[Any]:
        if call == 1:
            yield ProviderToolUseStartEvent(tool_use_id="spawn-1", tool_name="spawn_helper")
            yield ProviderToolUseEndEvent(
                tool_use_id="spawn-1",
                tool_name="spawn_helper",
                arguments={},
            )
            yield ProviderDoneEvent(
                stop_reason="tool_use",
                input_tokens=30,
                output_tokens=3,
                billed_cost=0.03,
                cost_source="provider_billed",
                model="fake/parent-model",
            )
            return
        yield ProviderTextDeltaEvent(text="parent answer")
        yield ProviderDoneEvent(
            stop_reason="end_turn",
            input_tokens=40,
            output_tokens=4,
            billed_cost=0.04,
            cost_source="provider_billed",
            model="fake/parent-model",
        )

    async def list_models(self) -> list[Any]:
        return []


_SPAWN_TOOL = ToolDefinition(
    name="spawn_helper",
    description="spawn a helper subagent",
    input_schema=ToolInputSchema(properties={}, required=[]),
)


async def _run_parent_turn(
    manager: SubagentManager,
    spawn_action,
    *,
    usage_tracker: UsageTracker | None = None,
    session_key: str | None = None,
) -> EngineDoneEvent:
    """Run one parent turn whose tool handler performs *spawn_action*."""

    async def tool_handler(call: ToolCall) -> ToolResult:
        await spawn_action(manager)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="helper finished",
        )

    agent = Agent(
        provider=_SpawningToolProvider(),
        config=AgentConfig(max_iterations=3),
        tool_definitions=[_SPAWN_TOOL],
        tool_handler=tool_handler,
        subagent_manager=manager,
        usage_tracker=usage_tracker,
        session_key=session_key,
    )
    events = [event async for event in agent.run_turn("delegate this")]
    done_events = [event for event in events if isinstance(event, EngineDoneEvent)]
    assert done_events
    return done_events[-1]


async def _spawn_and_wait(manager: SubagentManager, events: list[AgentEvent]) -> None:
    handle = await manager.spawn(
        SubagentSpec(task="child task", timeout=0),
        lambda _spec, _depth: _ScriptedChildAgent(events),
    )
    await handle.task


async def test_single_subagent_usage_rolls_into_parent_turn() -> None:
    """Issue #266 baseline: displayed turn cost = parent + spawned child."""

    async def spawn_action(manager: SubagentManager) -> None:
        await _spawn_and_wait(manager, [EngineTextDeltaEvent(text="child"), _child_done()])

    done = await _run_parent_turn(SubagentManager(), spawn_action)

    assert done.input_tokens == 70 + 1000
    assert done.output_tokens == 7 + 200
    assert done.message_output_tokens == 7
    assert done.reasoning_tokens == 7
    assert done.cached_tokens == 50
    assert done.cache_write_tokens == 25
    assert done.billed_cost == pytest.approx(0.07 + 0.5)
    assert done.cost_usd == pytest.approx(0.07 + 0.5)
    assert done.cost_source == "provider_billed"
    assert {row["model"] for row in done.model_usage_breakdown} == {
        "fake/parent-model",
        "deepseek/deepseek-v4-pro",
    }
    assert sum(row["cost_usd"] for row in done.model_usage_breakdown) == pytest.approx(
        0.57
    )

    payload = _turn_usage_payload(done, resolved_model="fake/parent-model")
    assert payload is not None
    assert payload["input_tokens"] == 1070
    assert payload["output_tokens"] == 207
    assert payload["cached_tokens"] == 50
    assert payload["cache_write_tokens"] == 25
    assert payload["cost_usd"] == pytest.approx(0.57)
    assert payload["billed_cost"] == pytest.approx(0.57)
    assert payload["cost_source"] == "provider_billed"


async def test_multiple_concurrent_subagents_are_summed() -> None:
    async def spawn_action(manager: SubagentManager) -> None:
        handles = []
        for index in range(3):
            handle = await manager.spawn(
                SubagentSpec(task=f"child {index}", timeout=0),
                lambda _spec, _depth: _ScriptedChildAgent(
                    [
                        _child_done(
                            input_tokens=100,
                            output_tokens=10,
                            reasoning_tokens=1,
                            cached_tokens=5,
                            cache_write_tokens=2,
                            cost_usd=0.1,
                            billed_cost=0.1,
                        )
                    ]
                ),
            )
            handles.append(handle)
        await asyncio.gather(*(h.task for h in handles))

    done = await _run_parent_turn(SubagentManager(), spawn_action)

    assert done.input_tokens == 70 + 300
    assert done.output_tokens == 7 + 30
    assert done.reasoning_tokens == 3
    assert done.cached_tokens == 15
    assert done.cache_write_tokens == 6
    assert done.billed_cost == pytest.approx(0.07 + 0.3)
    assert done.cost_usd == pytest.approx(0.07 + 0.3)
    assert done.cost_source == "provider_billed"


async def test_errored_subagent_terminal_usage_still_rolls_up() -> None:
    """An errored child still emits its terminal usage snapshot; the spend
    happened, so the parent turn must report it."""

    async def spawn_action(manager: SubagentManager) -> None:
        await _spawn_and_wait(
            manager,
            [
                EngineErrorEvent(message="child exploded", code="agent_error"),
                _child_done(
                    input_tokens=500,
                    output_tokens=50,
                    reasoning_tokens=0,
                    cached_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=0.2,
                    billed_cost=0.2,
                ),
            ],
        )

    done = await _run_parent_turn(SubagentManager(), spawn_action)

    assert done.input_tokens == 70 + 500
    assert done.output_tokens == 7 + 50
    assert done.billed_cost == pytest.approx(0.07 + 0.2)
    assert done.cost_usd == pytest.approx(0.07 + 0.2)


async def test_aborted_subagent_without_terminal_usage_contributes_nothing() -> None:
    async def spawn_action(manager: SubagentManager) -> None:
        handle = await manager.spawn(
            SubagentSpec(task="doomed child", timeout=0),
            lambda _spec, _depth: _HangingChildAgent(),
        )
        await asyncio.sleep(0)
        assert manager.registry.abort(handle.run_id)
        await asyncio.wait([handle.task])

    done = await _run_parent_turn(SubagentManager(), spawn_action)

    assert done.input_tokens == 70
    assert done.output_tokens == 7
    assert done.billed_cost == pytest.approx(0.07)
    assert done.cost_usd == pytest.approx(0.07)


async def test_new_manager_next_turn_keeps_child_only_in_cumulative_snapshot() -> None:
    """Production creates a new manager per turn while reusing the tracker."""

    tracker = UsageTracker()
    session_key = "agent:test:webchat:subagent-rollup"

    async def spawn_action(manager: SubagentManager) -> None:
        await _spawn_and_wait(manager, [_child_done()])

    first = await _run_parent_turn(
        SubagentManager(),
        spawn_action,
        usage_tracker=tracker,
        session_key=session_key,
    )

    assert first.input_tokens == 1070
    assert first.output_tokens == 207
    assert first.billed_cost == pytest.approx(0.57)
    assert first.session_totals is not None
    assert first.session_totals.input_tokens == 1070
    assert first.session_totals.output_tokens == 207
    assert first.session_totals.cost_usd == pytest.approx(0.57)
    session_usage = tracker.get(session_key)
    assert session_usage is not None
    assert session_usage.model_id == "fake/parent-model"

    async def no_spawn(_manager: SubagentManager) -> None:
        return None

    second = await _run_parent_turn(
        SubagentManager(),
        no_spawn,
        usage_tracker=tracker,
        session_key=session_key,
    )

    assert second.input_tokens == 70
    assert second.output_tokens == 7
    assert second.billed_cost == pytest.approx(0.07)
    assert second.session_totals is not None
    assert second.session_totals.input_tokens == 1140
    assert second.session_totals.output_tokens == 214
    assert second.session_totals.cost_usd == pytest.approx(0.64)


async def test_child_estimate_mixes_with_parent_billed_cost_source() -> None:
    async def spawn_action(manager: SubagentManager) -> None:
        await _spawn_and_wait(
            manager,
            [
                _child_done(
                    cost_usd=0.5,
                    billed_cost=0.0,
                    cost_source="opensquilla_static_estimate",
                )
            ],
        )

    done = await _run_parent_turn(SubagentManager(), spawn_action)

    assert done.billed_cost == pytest.approx(0.07)
    assert done.cost_usd == pytest.approx(0.07 + 0.5)
    assert done.cost_source == "mixed"
    assert done.missing_cost_entries == 0


@pytest.mark.parametrize(
    ("children", "expected_cost", "expected_billed", "expected_source", "expected_missing"),
    [
        ([_child_done()], 0.57, 0.57, "provider_billed", 0),
        (
            [
                _child_done(
                    cost_usd=0.5,
                    billed_cost=0.0,
                    cost_source="opensquilla_static_estimate",
                    estimate_basis="cache_aware",
                )
            ],
            0.57,
            0.07,
            "mixed",
            0,
        ),
        (
            [
                _child_done(
                    cost_usd=0.0,
                    billed_cost=0.0,
                    cost_source="unavailable",
                    missing_cost_entries=1,
                )
            ],
            0.07,
            0.07,
            "mixed",
            1,
        ),
        (
            [
                _child_done(
                    cost_usd=0.0,
                    billed_cost=0.0,
                    cost_source="unavailable",
                    estimate_basis="free",
                )
            ],
            0.07,
            0.07,
            "provider_billed",
            0,
        ),
        (
            [
                _child_done(
                    cost_usd=0.5,
                    billed_cost=0.0,
                    cost_source="opensquilla_static_estimate",
                    estimate_basis="cache_aware",
                ),
                _child_done(
                    cost_usd=0.0,
                    billed_cost=0.0,
                    cost_source="unavailable",
                    missing_cost_entries=1,
                ),
            ],
            0.57,
            0.07,
            "mixed",
            1,
        ),
    ],
)
async def test_cost_provenance_matrix(
    children: list[EngineDoneEvent],
    expected_cost: float,
    expected_billed: float,
    expected_source: str,
    expected_missing: int,
) -> None:
    async def spawn_action(manager: SubagentManager) -> None:
        for child in children:
            await _spawn_and_wait(manager, [child])

    done = await _run_parent_turn(SubagentManager(), spawn_action)

    assert done.cost_usd == pytest.approx(expected_cost)
    assert done.billed_cost == pytest.approx(expected_billed)
    assert done.cost_source == expected_source
    assert done.missing_cost_entries == expected_missing
    if any(child.estimate_basis not in {None, "free"} for child in children):
        assert done.estimate_basis != "free"


async def test_cancel_before_terminal_delivery_does_not_consume_child_usage() -> None:
    manager = SubagentManager()

    async def spawn_action(inner: SubagentManager) -> None:
        await _spawn_and_wait(inner, [_child_done()])

    async def tool_handler(call: ToolCall) -> ToolResult:
        await spawn_action(manager)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="helper finished",
        )

    agent = Agent(
        provider=_SpawningToolProvider(),
        config=AgentConfig(max_iterations=3),
        tool_definitions=[_SPAWN_TOOL],
        tool_handler=tool_handler,
        subagent_manager=manager,
    )
    stream = agent.run_turn("delegate this")
    async for event in stream:
        if event.kind == "state_change" and event.to_state.value == "done":
            break
    await stream.aclose()

    handle = manager.registry.all_handles()[0]
    assert handle.usage is not None
    assert handle.usage_rolled_up is False
    assert manager.drain_completed_usage() == [handle.usage]
    assert manager.drain_completed_usage() == []


async def test_successful_done_delivery_consumes_child_usage_once() -> None:
    manager = SubagentManager()

    async def spawn_action(inner: SubagentManager) -> None:
        await _spawn_and_wait(inner, [_child_done()])

    done = await _run_parent_turn(manager, spawn_action)

    assert done.cost_usd == pytest.approx(0.57)
    handle = manager.registry.all_handles()[0]
    assert handle.usage_rolled_up is True
    assert manager.drain_completed_usage() == []


async def test_late_child_stays_out_of_parent_and_next_turn() -> None:
    tracker = UsageTracker()
    session_key = "agent:test:webchat:late-child"
    gate = asyncio.Event()
    late_handle = None

    async def spawn_late(manager: SubagentManager) -> None:
        nonlocal late_handle
        late_handle = await manager.spawn(
            SubagentSpec(task="late child", timeout=0),
            lambda _spec, _depth: _GatedChildAgent(gate),
        )

    first = await _run_parent_turn(
        SubagentManager(),
        spawn_late,
        usage_tracker=tracker,
        session_key=session_key,
    )
    assert first.cost_usd == pytest.approx(0.07)
    assert first.input_tokens == 70
    assert first.session_totals is not None
    assert first.session_totals.cost_usd == pytest.approx(0.07)

    gate.set()
    assert late_handle is not None
    await late_handle.task

    async def no_spawn(_manager: SubagentManager) -> None:
        return None

    second = await _run_parent_turn(
        SubagentManager(),
        no_spawn,
        usage_tracker=tracker,
        session_key=session_key,
    )
    assert second.cost_usd == pytest.approx(0.07)
    assert second.input_tokens == 70
    assert second.session_totals is not None
    assert second.session_totals.cost_usd == pytest.approx(0.14)


async def test_handle_captures_child_done_usage_snapshot() -> None:
    manager = SubagentManager()
    handle = await manager.spawn(
        SubagentSpec(task="synthetic task", timeout=0),
        lambda _spec, _depth: _ScriptedChildAgent(
            [EngineTextDeltaEvent(text="partial"), _child_done()]
        ),
    )
    assert await handle.task == "child result"

    assert handle.usage is not None
    assert handle.usage.input_tokens == 1000
    assert handle.usage.output_tokens == 200
    assert handle.usage.reasoning_tokens == 7
    assert handle.usage.cached_tokens == 50
    assert handle.usage.cache_write_tokens == 25
    assert handle.usage.cost_usd == pytest.approx(0.5)
    assert handle.usage.billed_cost == pytest.approx(0.5)
    assert handle.usage.cost_source == "provider_billed"
    assert handle.usage.model == "deepseek/deepseek-v4-pro"
