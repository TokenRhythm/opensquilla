"""Concurrent tool dispatch tests.

Verifies that same-turn safe tools run concurrently via asyncio.gather
and mutex tools remain serial.  All tests use mock LLM and mock tool
handlers; no real network or provider calls are made.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.runtime import _SAFE_TOOL_NAMES, _get_tool_concurrency_policy
from opensquilla.engine.types import ToolCall
from opensquilla.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderTextDelta
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TOOL_SLEEP_S = 0.2
_SCHEDULER_TOLERANCE_S = 0.05


def _tool_def(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Mock tool {name}",
        input_schema=ToolInputSchema(properties={}, required=[]),
    )


class _FixedToolCallProvider:
    """Fake LLM that returns a fixed list of tool calls on the first turn,
    then a plain text done on the second turn."""

    provider_name = "fake"

    def __init__(self, tool_names: list[str]) -> None:
        self._tool_names = tool_names
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
        if call_number > 1:
            # Second call: return a non-empty text response so the agent
            # does not trigger the empty-response retry loop.
            yield ProviderTextDelta(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        # First call: emit all tool uses then done
        for i, name in enumerate(self._tool_names):
            tid = f"tool-{i}"
            yield ProviderToolUseStart(tool_use_id=tid, tool_name=name)
            yield ProviderToolUseEnd(
                tool_use_id=tid,
                tool_name=name,
                arguments={},
            )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _FixedToolCallArgsProvider:
    """Fake LLM that returns fixed tool calls with explicit arguments."""

    provider_name = "fake"

    def __init__(self, tool_calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._tool_calls = tool_calls
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
        if call_number > 1:
            yield ProviderTextDelta(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        for i, (name, arguments) in enumerate(self._tool_calls):
            tid = f"tool-{i}"
            yield ProviderToolUseStart(tool_use_id=tid, tool_name=name)
            yield ProviderToolUseEnd(
                tool_use_id=tid,
                tool_name=name,
                arguments=arguments,
            )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


async def _collect(agent: Agent, message: str = "go") -> list[Any]:
    return [e async for e in agent.run_turn(message)]


# ---------------------------------------------------------------------------
# Pick two safe tool names that are guaranteed to be in _SAFE_TOOL_NAMES
# ---------------------------------------------------------------------------

_SAFE_SAMPLE = sorted(_SAFE_TOOL_NAMES)[:6]  # pick 6 safe names for the concurrent test


def test_web_search_is_safe_for_same_turn_concurrency() -> None:
    """web_search is read-only network I/O and should batch with safe tools."""
    assert "web_search" in _SAFE_TOOL_NAMES


def test_sessions_spawn_policy_keys_by_parent_session() -> None:
    """Spawn policy is scoped to the parent session rather than global state."""
    policy = _get_tool_concurrency_policy(
        "sessions_spawn",
        {},
        parent_session_key="agent:main:parent-a",
    )

    assert policy.mode == "keyed"
    assert policy.key == ("sessions_spawn", "agent:main:parent-a")


@pytest.mark.asyncio
async def test_image_analysis_calls_have_dedicated_inflight_cap() -> None:
    """Vision requests should not fan out at the generic safe-tool limit."""
    tool_calls = [("image", {"path": f"slide-{i}.png"}) for i in range(6)]
    in_flight = 0
    max_in_flight = 0

    async def _handler(tc: ToolCall) -> ToolResult:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(_TOOL_SLEEP_S)
        in_flight -= 1
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="ok",
        )

    provider = _FixedToolCallArgsProvider(tool_calls)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2, max_safe_tool_concurrency=6),
        tool_definitions=[_tool_def("image")],
        tool_handler=_handler,
    )

    t0 = time.monotonic()
    await _collect(agent)
    elapsed = time.monotonic() - t0

    assert max_in_flight == 2
    assert 3 * _TOOL_SLEEP_S - _SCHEDULER_TOLERANCE_S <= elapsed < 4 * _TOOL_SLEEP_S


@pytest.mark.asyncio
async def test_feishu_mutating_tools_remain_serial() -> None:
    """Feishu writes must remain mutex even when read-only Feishu tools batch."""
    tool_calls = [
        ("feishu_chat_send", {"chat_id": "chat-a", "text": "one"}),
        ("feishu_chat_edit", {"message_id": "msg-a", "text": "two"}),
    ]
    intervals: list[tuple[str, float, float]] = []

    async def _handler(tc: ToolCall) -> ToolResult:
        start = time.monotonic()
        await asyncio.sleep(_TOOL_SLEEP_S)
        intervals.append((tc.tool_name, start, time.monotonic()))
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="ok",
        )

    provider = _FixedToolCallArgsProvider(tool_calls)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[_tool_def(n) for n, _ in tool_calls],
        tool_handler=_handler,
    )

    await _collect(agent)

    assert len(intervals) == len(tool_calls)
    (_, _, first_end), (_, second_start, _) = intervals
    assert second_start >= first_end - 0.01


@pytest.mark.asyncio
async def test_sessions_send_different_targets_run_concurrent() -> None:
    """session sends to different target sessions should not globally serialize."""
    tool_calls = [
        ("sessions_send", {"session_key": "agent:main:child-a", "message": "one"}),
        ("sessions_send", {"session_key": "agent:main:child-b", "message": "two"}),
    ]
    intervals: list[tuple[str, str, float, float]] = []

    async def _handler(tc: ToolCall) -> ToolResult:
        target = str(tc.arguments["session_key"])
        start = time.monotonic()
        await asyncio.sleep(_TOOL_SLEEP_S)
        intervals.append((tc.tool_name, target, start, time.monotonic()))
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="ok",
        )

    provider = _FixedToolCallArgsProvider(tool_calls)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[_tool_def(n) for n, _ in tool_calls],
        tool_handler=_handler,
    )

    t0 = time.monotonic()
    await _collect(agent)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.35, (
        f"Expected sends to different sessions to overlap, got {elapsed:.3f} s."
    )
    assert len(intervals) == len(tool_calls)


@pytest.mark.asyncio
async def test_sessions_send_same_target_serializes() -> None:
    """session sends to one target session must preserve per-target order."""
    tool_calls = [
        ("sessions_send", {"session_key": "agent:main:child-a", "message": "one"}),
        ("sessions_send", {"session_key": "agent:main:child-a", "message": "two"}),
    ]
    intervals: list[tuple[str, float, float]] = []

    async def _handler(tc: ToolCall) -> ToolResult:
        start = time.monotonic()
        await asyncio.sleep(_TOOL_SLEEP_S)
        intervals.append((str(tc.arguments["message"]), start, time.monotonic()))
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="ok",
        )

    provider = _FixedToolCallArgsProvider(tool_calls)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[_tool_def(n) for n, _ in tool_calls],
        tool_handler=_handler,
    )

    await _collect(agent)

    assert len(intervals) == len(tool_calls)
    (_, _, first_end), (_, second_start, _) = intervals
    assert second_start >= first_end - 0.01


# ---------------------------------------------------------------------------
# Test 1: 6 safe tools run concurrently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_six_safe_tools_run_concurrent() -> None:
    """Six safe tools should complete in roughly one sleep period, not six."""
    assert len(_SAFE_SAMPLE) == 6, "Need at least 6 safe tools in _SAFE_TOOL_NAMES"

    call_order: list[str] = []

    async def _handler(tc: ToolCall) -> ToolResult:
        call_order.append(tc.tool_name)
        await asyncio.sleep(_TOOL_SLEEP_S)
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="ok",
        )

    provider = _FixedToolCallProvider(_SAFE_SAMPLE)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[_tool_def(n) for n in _SAFE_SAMPLE],
        tool_handler=_handler,
    )

    t0 = time.monotonic()
    await _collect(agent)
    elapsed = time.monotonic() - t0

    # Concurrent: should be ~0.2 s; serial would be ~1.2 s
    assert elapsed < 0.60, (
        f"Expected concurrent execution (<0.60 s), got {elapsed:.3f} s. "
        "Safe tools may still be running serially."
    )
    # Speed-up vs serial lower bound
    serial_estimate = len(_SAFE_SAMPLE) * _TOOL_SLEEP_S
    assert elapsed * 3 < serial_estimate, (
        f"Expected at least 3x speedup over serial ({serial_estimate:.1f} s), "
        f"got {elapsed:.3f} s"
    )
    # All 6 tools were called
    assert sorted(call_order) == sorted(_SAFE_SAMPLE)


@pytest.mark.asyncio
async def test_safe_tool_concurrency_limit_caps_in_flight_tasks() -> None:
    """Safe tools should run concurrently, but not without an upper bound."""
    tool_names = _SAFE_SAMPLE
    max_in_flight = 0
    in_flight = 0

    async def _handler(tc: ToolCall) -> ToolResult:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(_TOOL_SLEEP_S)
        in_flight -= 1
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="ok",
        )

    provider = _FixedToolCallProvider(tool_names)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2, max_safe_tool_concurrency=2),
        tool_definitions=[_tool_def(n) for n in tool_names],
        tool_handler=_handler,
    )

    t0 = time.monotonic()
    await _collect(agent)
    elapsed = time.monotonic() - t0

    assert max_in_flight == 2
    assert 2 * _TOOL_SLEEP_S <= elapsed < 4 * _TOOL_SLEEP_S


# ---------------------------------------------------------------------------
# Test 2: 2 mutex tools stay serial
# ---------------------------------------------------------------------------

# Pick two tool names that are NOT in _SAFE_TOOL_NAMES
_MUTEX_NAMES = ["write_file", "exec_command"]
assert all(n not in _SAFE_TOOL_NAMES for n in _MUTEX_NAMES), (
    "Test assumes write_file and exec_command are mutex (not in _SAFE_TOOL_NAMES)"
)


@pytest.mark.asyncio
async def test_two_mutex_tools_serialize() -> None:
    """Two mutex tools must not run concurrently."""
    execution_intervals: list[tuple[float, float]] = []

    async def _handler(tc: ToolCall) -> ToolResult:
        start = time.monotonic()
        await asyncio.sleep(_TOOL_SLEEP_S)
        execution_intervals.append((start, time.monotonic()))
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="ok",
        )

    provider = _FixedToolCallProvider(_MUTEX_NAMES)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[_tool_def(n) for n in _MUTEX_NAMES],
        tool_handler=_handler,
    )

    t0 = time.monotonic()
    await _collect(agent)
    elapsed = time.monotonic() - t0

    # Serial: must take at least 2 * sleep
    assert elapsed >= 2 * _TOOL_SLEEP_S * 0.9, (
        f"Expected serial execution (>= {2 * _TOOL_SLEEP_S * 0.9:.2f} s), "
        f"got {elapsed:.3f} s. Mutex tools may be running concurrently."
    )

    # Verify non-overlapping: second start >= first end
    assert len(execution_intervals) == 2
    (s1, e1), (s2, e2) = execution_intervals
    assert s2 >= e1 - 0.01, (
        f"Execution intervals overlap: [{s1:.3f}, {e1:.3f}] and [{s2:.3f}, {e2:.3f}]. "
        "Mutex tools ran concurrently."
    )


# ---------------------------------------------------------------------------
# Test 3: one failing safe tool does not block the others
# ---------------------------------------------------------------------------

_SAFE_TRIO = sorted(_SAFE_TOOL_NAMES)[:3]  # three safe tools (also reused for cancellation test)


@pytest.mark.asyncio
async def test_one_failing_tool_does_not_block_others() -> None:
    """Tool A raises; tools B and C must still complete and results stay ordered."""
    assert len(_SAFE_TRIO) == 3, "Need at least 3 safe tools in _SAFE_TOOL_NAMES"

    tool_a, tool_b, tool_c = _SAFE_TRIO
    completed: list[str] = []

    async def _handler(tc: ToolCall) -> ToolResult:
        if tc.tool_name == tool_a:
            raise RuntimeError("simulated failure")
        await asyncio.sleep(0.05)
        completed.append(tc.tool_name)
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content=f"{tc.tool_name} ok",
        )

    provider = _FixedToolCallProvider([tool_a, tool_b, tool_c])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[_tool_def(n) for n in _SAFE_TRIO],
        tool_handler=_handler,
    )

    events = await _collect(agent)

    # B and C completed despite A failing
    assert tool_b in completed, "tool_b did not complete"
    assert tool_c in completed, "tool_c did not complete"

    # All three tool result events were yielded (A as error, B+C as success)
    from opensquilla.engine.types import ToolResultEvent

    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(result_events) == 3, (
        f"Expected 3 ToolResultEvents, got {len(result_events)}. "
        "A failing tool may have blocked sibling results."
    )

    # Ordering: results appear in original tool_calls order (A, B, C)
    names_in_order = [e.tool_name for e in result_events]
    assert names_in_order == [tool_a, tool_b, tool_c], (
        f"Results out of original order: {names_in_order}"
    )

    # A's result is an error
    assert result_events[0].is_error, "Expected tool_a result to be an error"


# ---------------------------------------------------------------------------
# Test 4: cancellation propagates to in-flight safe tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_propagates() -> None:
    """Cancelling the outer Task must reach all in-flight safe tool coroutines."""
    cancel_received: list[str] = []
    started: list[str] = []

    async def _handler(tc: ToolCall) -> ToolResult:
        started.append(tc.tool_name)
        try:
            await asyncio.sleep(2.0)  # long sleep — will be cancelled
        except asyncio.CancelledError:
            cancel_received.append(tc.tool_name)
            raise
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="ok",
        )

    provider = _FixedToolCallProvider(_SAFE_TRIO)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[_tool_def(n) for n in _SAFE_TRIO],
        tool_handler=_handler,
    )

    async def _run() -> None:
        await _collect(agent)

    task = asyncio.create_task(_run())

    # Let tools start
    await asyncio.sleep(0.05)
    task.cancel()

    t0 = time.monotonic()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await task
    elapsed = time.monotonic() - t0

    # All in-flight tools should have received CancelledError
    assert len(cancel_received) > 0, (
        "No tool received CancelledError — cancellation did not propagate."
    )
    # Should exit well under 2 s (the tool sleep duration)
    assert elapsed < 1.0, (
        f"Cancellation took {elapsed:.3f} s — tools may not be cancelling promptly."
    )


# ---------------------------------------------------------------------------
# Test 5: http_request is not in the safe set
# ---------------------------------------------------------------------------


def test_http_request_not_in_safe_set() -> None:
    """http_request writes to .fetch/ directory and must not be dispatched concurrently."""
    assert "http_request" not in _SAFE_TOOL_NAMES, (
        "http_request has write side-effects (_save_http_response_body) and must not "
        "be in _SAFE_TOOL_NAMES."
    )


# ---------------------------------------------------------------------------
# Test 6: safe tool after mutex must wait for mutex to finish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_after_mutex_waits_for_mutex() -> None:
    """A safe tool that comes AFTER a mutex must not start until the mutex finishes."""
    # call sequence: [write_file (mutex), read_file (safe)]
    mutex_first = "write_file"
    safe_second = "read_file"
    assert mutex_first not in _SAFE_TOOL_NAMES
    assert safe_second in _SAFE_TOOL_NAMES

    intervals: dict[str, tuple[float, float]] = {}

    async def _handler(tc: ToolCall) -> ToolResult:
        start = time.monotonic()
        await asyncio.sleep(_TOOL_SLEEP_S)
        end = time.monotonic()
        intervals[tc.tool_name] = (start, end)
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="ok",
        )

    tool_names = [mutex_first, safe_second]
    provider = _FixedToolCallProvider(tool_names)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[_tool_def(n) for n in tool_names],
        tool_handler=_handler,
    )

    await _collect(agent)

    assert mutex_first in intervals, "mutex tool did not run"
    assert safe_second in intervals, "safe tool did not run"

    mutex_end = intervals[mutex_first][1]
    safe_start = intervals[safe_second][0]

    assert safe_start >= mutex_end - 0.01, (
        f"Safe tool started at {safe_start:.3f} before mutex finished at {mutex_end:.3f}. "
        "Ordering barrier was not respected."
    )


# ---------------------------------------------------------------------------
# Test 7: mutex after safe batch waits for all safe tools, safe tools overlap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutex_after_safe_waits_for_safe_batch() -> None:
    """A mutex tool after safe tools must not start until all safe tools finish,
    and the safe tools must run concurrently (overlapping intervals)."""
    safe_a = "read_file"
    safe_b = "glob_search"
    mutex_c = "write_file"
    assert safe_a in _SAFE_TOOL_NAMES
    assert safe_b in _SAFE_TOOL_NAMES
    assert mutex_c not in _SAFE_TOOL_NAMES

    intervals: dict[str, tuple[float, float]] = {}

    async def _handler(tc: ToolCall) -> ToolResult:
        start = time.monotonic()
        await asyncio.sleep(_TOOL_SLEEP_S)
        end = time.monotonic()
        intervals[tc.tool_name] = (start, end)
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="ok",
        )

    tool_names = [safe_a, safe_b, mutex_c]
    provider = _FixedToolCallProvider(tool_names)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[_tool_def(n) for n in tool_names],
        tool_handler=_handler,
    )

    await _collect(agent)

    assert safe_a in intervals
    assert safe_b in intervals
    assert mutex_c in intervals

    # safe tools must have overlapping execution intervals
    (sa_s, sa_e) = intervals[safe_a]
    (sb_s, sb_e) = intervals[safe_b]
    assert sa_s < sb_e and sb_s < sa_e, (
        f"safe_a [{sa_s:.3f}, {sa_e:.3f}] and safe_b [{sb_s:.3f}, {sb_e:.3f}] "
        "did not overlap — they may have run serially."
    )

    # mutex must start after both safe tools finish
    mutex_start = intervals[mutex_c][0]
    safe_batch_end = max(sa_e, sb_e)
    assert mutex_start >= safe_batch_end - 0.01, (
        f"Mutex started at {mutex_start:.3f} before safe batch ended at {safe_batch_end:.3f}."
    )
