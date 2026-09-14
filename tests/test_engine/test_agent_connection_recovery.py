from __future__ import annotations

import asyncio
import copy

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.provider.types import (
    DoneEvent,
    ErrorEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


def _connection():
    return ErrorEvent(message="temporary connection failure", code="connection_failed")


def _success():
    return [TextDeltaEvent(text="done"), DoneEvent(stop_reason="stop")]


class _Provider:
    provider_name = "openai"
    retry_failed_call_safe = True

    def __init__(self, streams, *, creation_failure=False):
        self.streams = streams
        self.creation_failure = creation_failure
        self.calls = []

    def chat(self, messages, tools=None, config=None):
        index = len(self.calls)
        self.calls.append((copy.deepcopy(messages), config))
        stream = self.streams[min(index, len(self.streams) - 1)]
        if self.creation_failure and isinstance(stream[0], Exception):
            raise stream[0]
        return self._stream(stream)

    async def _stream(self, stream):
        for event in stream:
            if isinstance(event, Exception):
                raise event
            yield event


def _config(**overrides):
    return AgentConfig(
        retry_base_backoff_ms=0,
        retry_max_backoff_ms=0,
        **overrides,
    )


async def _run(provider, **config):
    agent = Agent(provider=provider, config=_config(**config))
    return [event async for event in agent.run_turn("complete the task")]


@pytest.fixture
async def fast_wait(monkeypatch):
    delays = []
    original_sleep = asyncio.sleep
    loop = asyncio.get_running_loop()
    now = [loop.time()]

    async def sleep(delay):
        delays.append(delay)
        now[0] += delay
        await original_sleep(0)

    monkeypatch.setattr(loop, "time", lambda: now[0])
    monkeypatch.setattr("opensquilla.engine.agent.asyncio.sleep", sleep)
    return delays


@pytest.mark.parametrize("creation_failure", [False, True])
async def test_typed_connection_wait_is_independent_of_finite_retry_budget(
    fast_wait, creation_failure
) -> None:
    provider = _Provider(
        [[httpx.ConnectError("untrusted error prose")]] * 7 + [_success()],
        creation_failure=creation_failure,
    )
    events = await _run(provider, max_provider_retries=0)
    waits = [
        event
        for event in events
        if event.kind == "provider_activity" and event.phase == "retry_wait"
    ]
    assert fast_wait == [5, 10, 20, 40, 60, 60, 60]
    assert [event.retry_limit for event in waits] == [0] * 7
    assert [event.retry_attempt for event in waits] == list(range(1, 8))
    assert all(event.reason == "transport_transient" for event in waits)
    assert len(provider.calls) == 8
    assert any(event.kind == "done" and event.text == "done" for event in events)


async def test_connection_wait_does_not_exhaust_subsequent_rate_retries(fast_wait) -> None:
    rate = ErrorEvent(message="rate limit exceeded", code="429")
    provider = _Provider([[_connection()]] * 5 + [[rate]] * 3 + [_success()])
    events = await _run(provider)
    assert len(provider.calls) == 9
    assert any(event.kind == "done" and event.text == "done" for event in events)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("connection timeout"),
        RuntimeError("connection failed"),
        ErrorEvent(message="insufficient_quota", code="429"),
    ],
)
async def test_only_typed_connection_failures_enter_persistent_wait(fast_wait, error):
    provider = _Provider([[error]])
    events = await _run(provider)
    assert len(provider.calls) <= 4
    assert not any(
        event.kind == "provider_activity" and event.phase == "retry_wait" and event.retry_limit == 0
        for event in events
    )


@pytest.mark.parametrize("prefix", [TextDeltaEvent(text="partial"), ReasoningDeltaEvent(text="r")])
async def test_visible_output_prevents_connection_replay(fast_wait, prefix):
    provider = _Provider([[prefix, _connection()]])
    events = await _run(provider)
    assert len(provider.calls) == 1
    assert fast_wait == []
    assert any(event.kind == "error" for event in events)


async def test_composite_provider_retains_its_retry_owner(fast_wait):
    provider = _Provider([[_connection()]])
    provider.retry_failed_call_safe = False
    await _run(provider)
    assert len(provider.calls) == 1
    assert fast_wait == []


async def test_connection_wait_is_cancellable(monkeypatch):
    waiting = asyncio.Event()

    async def sleep(_delay):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("opensquilla.engine.agent.asyncio.sleep", sleep)
    provider = _Provider([[_connection()]])
    task = asyncio.create_task(_run(provider))
    await asyncio.wait_for(waiting.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(provider.calls) == 1


async def test_connection_wait_respects_existing_task_deadline():
    provider = _Provider([[_connection()]])
    events = await asyncio.wait_for(_run(provider, timeout=0.05), timeout=1)

    assert len(provider.calls) == 1
    assert any(
        event.kind == "provider_activity" and event.phase == "retry_wait"
        for event in events
    )
    errors = [event for event in events if event.kind == "error"]
    assert len(errors) == 1
    assert errors[0].code == "agent_runtime_timeout"


async def test_rate_retry_wait_respects_existing_task_deadline(monkeypatch):
    cancelled = asyncio.Event()

    async def delayed_wait(_delay):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("opensquilla.engine.agent.sleep_before_retry", delayed_wait)
    provider = _Provider([
        [ErrorEvent(message="rate limit exceeded", code="429", retry_after_s=0.01)]
    ])
    events = await asyncio.wait_for(_run(provider, timeout=0.05), timeout=1)

    assert cancelled.is_set()
    assert len(provider.calls) == 1
    errors = [event for event in events if event.kind == "error"]
    assert len(errors) == 1
    assert errors[0].code == "agent_runtime_timeout"


async def test_connection_wait_recovers_after_long_network_outage(monkeypatch):
    loop = asyncio.get_running_loop()
    now = [loop.time()]
    original_sleep = asyncio.sleep
    delays = []

    async def sleep(delay):
        delays.append(delay)
        now[0] += delay
        await original_sleep(0)

    monkeypatch.setattr(loop, "time", lambda: now[0])
    monkeypatch.setattr("opensquilla.engine.agent.asyncio.sleep", sleep)
    provider = _Provider([[_connection()]] * 35 + [_success()])
    events = await _run(
        provider, timeout=0, iteration_timeout=1800
    )
    assert sum(delays) > 1800
    assert len(provider.calls) == 36
    assert any(event.kind == "done" and event.text == "done" for event in events)
    assert not any(event.kind == "error" for event in events)


async def test_connection_retry_keeps_completed_tool_results_without_reexecution(fast_wait):
    provider = _Provider(
        [
            [
                ToolUseStartEvent(tool_use_id="write-1", tool_name="write_file"),
                ToolUseEndEvent(tool_use_id="write-1", tool_name="write_file", arguments={}),
                DoneEvent(stop_reason="tool_use"),
            ],
            [_connection()],
            [_connection()],
            _success(),
        ]
    )
    executions = []

    async def tool(call):
        executions.append(call.tool_use_id)
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="saved")

    agent = Agent(
        provider=provider,
        config=_config(),
        tool_handler=tool,
        tool_definitions=[
            ToolDefinition(
                name="write_file", description="Write", input_schema=ToolInputSchema(properties={})
            )
        ],
    )
    events = [event async for event in agent.run_turn("write the file")]
    assert executions == ["write-1"]
    assert len(provider.calls) == 4
    assert provider.calls[1][0] == provider.calls[2][0] == provider.calls[3][0]
    assert any(event.kind == "done" and event.text == "done" for event in events)
