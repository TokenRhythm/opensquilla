from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import opensquilla.engine.agent as agent_module
from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.types import DoneEvent, RunHeartbeatEvent, ToolCall, ToolResultEvent
from opensquilla.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import ToolContext


class _OneToolProvider:
    provider_name = "fake"

    def __init__(
        self,
        tool_name: str = "slow_tool",
        arguments: dict[str, Any] | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.arguments = arguments or {}
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
        yield ProviderToolUseStart(tool_use_id="tool-1", tool_name=self.tool_name)
        yield ProviderToolUseEnd(
            tool_use_id="tool-1",
            tool_name=self.tool_name,
            arguments=self.arguments,
        )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _tool_def(name: str = "slow_tool") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Mock tool {name}",
        input_schema=ToolInputSchema(properties={}, required=[]),
    )


async def _collect_events(agent: Agent) -> list[Any]:
    return [event async for event in agent.run_turn("run")]


@pytest.mark.asyncio
async def test_long_active_tool_emits_run_heartbeat_before_tool_result() -> None:
    async def _handler(tc: ToolCall) -> ToolResult:
        await asyncio.sleep(0.08)
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="ok",
        )

    agent = Agent(
        provider=_OneToolProvider(),
        config=AgentConfig(
            max_iterations=1,
            tool_timeout=1.0,
            metadata={"tool_activity_heartbeat_interval": 0.02},
        ),
        tool_definitions=[_tool_def()],
        tool_handler=_handler,
    )

    events = [event async for event in agent.run_turn("run")]
    heartbeat_index = next(
        index for index, event in enumerate(events) if isinstance(event, RunHeartbeatEvent)
    )
    result_index = next(
        index for index, event in enumerate(events) if isinstance(event, ToolResultEvent)
    )

    heartbeat = events[heartbeat_index]
    assert isinstance(heartbeat, RunHeartbeatEvent)
    assert heartbeat.phase == "tool"
    assert heartbeat_index < result_index
    result = events[result_index]
    assert isinstance(result, ToolResultEvent)
    assert result.result == "ok"


@pytest.mark.asyncio
async def test_tool_activity_heartbeat_does_not_extend_tool_timeout() -> None:
    cancelled = asyncio.Event()

    async def _handler(tc: ToolCall) -> ToolResult:
        try:
            await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="late",
        )

    agent = Agent(
        provider=_OneToolProvider(),
        config=AgentConfig(
            max_iterations=1,
            tool_timeout=0.06,
            metadata={"tool_activity_heartbeat_interval": 0.02},
        ),
        tool_definitions=[_tool_def()],
        tool_handler=_handler,
    )

    events = [event async for event in agent.run_turn("run")]
    result = next(event for event in events if isinstance(event, ToolResultEvent))

    assert any(isinstance(event, RunHeartbeatEvent) for event in events)
    assert cancelled.is_set()
    assert result.is_error
    assert result.execution_status is not None
    assert result.execution_status["status"] == "timeout"
    assert result.execution_status["reason"] == "runtime_timeout"
    assert result.execution_status["timed_out"] is True
    assert result.result.startswith("Tool 'slow_tool' timed out after ")


@pytest.mark.asyncio
async def test_stubborn_tool_late_success_cannot_replace_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "TIMEOUT_CANCEL_GRACE_SECONDS", 0.02)
    cancelled = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def _handler(tc: ToolCall) -> ToolResult:
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        finished.set()
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content="late-success",
        )

    agent = Agent(
        provider=_OneToolProvider(),
        config=AgentConfig(max_iterations=1, tool_timeout=0.01),
        tool_definitions=[_tool_def()],
        tool_handler=_handler,
    )

    events = await asyncio.wait_for(
        _collect_events(agent),
        timeout=0.2,
    )
    result = next(event for event in events if isinstance(event, ToolResultEvent))

    assert cancelled.is_set()
    assert result.is_error
    assert result.execution_status is not None
    assert result.execution_status["status"] == "timeout"
    assert "late-success" not in result.result

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_stubborn_tool_stop_uses_short_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "STOP_CANCEL_GRACE_SECONDS", 0.02)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    observed_batches: list[tuple[float, tuple[str, ...]]] = []
    real_cancel_tasks = agent_module.cancel_tasks

    async def observe_cancel_tasks(tasks: Any, **kwargs: Any) -> None:
        if kwargs["operation"] == "agent_tool_batch":
            observed_batches.append((kwargs["grace_seconds"], tuple(tasks.values())))
        await real_cancel_tasks(tasks, **kwargs)

    monkeypatch.setattr(agent_module, "cancel_tasks", observe_cancel_tasks)

    async def _handler(tc: ToolCall) -> ToolResult:
        started.set()
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return ToolResult(tc.tool_use_id, tc.tool_name, "late-success")
        finally:
            finished.set()

    agent = Agent(
        provider=_OneToolProvider(),
        config=AgentConfig(max_iterations=1, tool_timeout=60.0),
        tool_definitions=[_tool_def()],
        tool_handler=_handler,
    )
    turn = asyncio.create_task(_collect_events(agent))
    try:
        # Provider/agent startup is not part of the tool cancellation grace.
        await asyncio.wait_for(started.wait(), timeout=5.0)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, timeout=5.0)
        assert cancelled.is_set()
        assert observed_batches == [(0.02, ("bounded",))]
        assert not finished.is_set(), "Stop waited for the stubborn tool to finish"
    finally:
        release.set()
        if not turn.done():
            turn.cancel()
        await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=5.0)
        if started.is_set():
            await asyncio.wait_for(finished.wait(), timeout=5.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
async def test_bounded_cancellation_parks_only_when_controlled_grace_expires(
    monkeypatch: pytest.MonkeyPatch,
    batch: bool,
) -> None:
    from opensquilla.engine import cancellation

    count = 2 if batch else 1
    started = [asyncio.Event() for _ in range(count)]
    cancelled = [asyncio.Event() for _ in range(count)]
    release = asyncio.Event()
    timers: list[asyncio.Timeout] = []
    budgets: list[float] = []

    class ControlledAsyncio:
        def timeout(self, delay: float) -> asyncio.Timeout:
            budgets.append(delay)
            timer = asyncio.timeout(None)
            timers.append(timer)
            return timer

        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

    # Only the cancellation primitive sees the controlled timer. Test
    # watchdogs retain the real event loop and tolerate slow CI scheduling.
    monkeypatch.setattr(cancellation, "asyncio", ControlledAsyncio())

    async def stubborn_worker(index: int) -> None:
        started[index].set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled[index].set()
            await release.wait()

    workers = [asyncio.create_task(stubborn_worker(index)) for index in range(count)]
    operation = None
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 5.0)
        if batch:
            operation = asyncio.create_task(cancellation.cancel_tasks(
                dict.fromkeys(workers, "bounded"),
                operation="synthetic-batch",
                grace_seconds=0.02,
            ))
        else:
            operation = asyncio.create_task(cancellation.cancel_task(
                workers[0], policy="bounded", operation="synthetic-single", grace_seconds=0.02,
            ))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in cancelled)), 5.0)
        assert budgets == [0.02], "bounded workers must share one requested grace timer"
        assert not operation.done(), "cancellation returned before its grace expired"
        assert all(not worker.done() for worker in workers)

        timers[0].reschedule(asyncio.get_running_loop().time())
        result = await asyncio.wait_for(asyncio.shield(operation), 5.0)
        assert result is (None if batch else False)
        assert all(not worker.done() for worker in workers)
        assert all(worker in cancellation._BACKGROUND_TASKS for worker in workers)
    finally:
        release.set()
        if operation is not None and not operation.done():
            operation.cancel()
        pending = workers + ([operation] if operation is not None else [])
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5.0)


@pytest.mark.asyncio
async def test_write_file_timeout_waits_for_disk_and_receipt_before_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "settled.txt"
    worker_started = threading.Event()
    release_worker = threading.Event()
    timeout_cleanup_started = asyncio.Event()
    order: list[str] = []
    real_write_text = Path.write_text
    real_cancel_task = agent_module.cancel_task

    async def observe_cancel_task(*args: Any, **kwargs: Any) -> bool:
        if kwargs.get("grace_seconds") == agent_module.TIMEOUT_CANCEL_GRACE_SECONDS:
            timeout_cleanup_started.set()
        return await real_cancel_task(*args, **kwargs)

    def gated_write_text(
        path: Path,
        data: str,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        if path == target:
            worker_started.set()
            assert release_worker.wait(timeout=2.0)
        written = real_write_text(path, data, *args, **kwargs)
        if path == target:
            order.append("disk")
        return written

    monkeypatch.setattr(agent_module, "cancel_task", observe_cancel_task)
    monkeypatch.setattr(Path, "write_text", gated_write_text)
    ctx = ToolContext(
        workspace_dir=str(tmp_path),
        session_key="agent:main:mutation-settlement-test",
    )

    def record_event(event: dict[str, Any]) -> None:
        if event.get("name") == "workspace.semantic_mutation_receipt":
            order.append("receipt")

    ctx.on_runtime_event = record_event
    registry = get_default_registry()
    definitions = registry.to_tool_definitions(ctx)
    write_definition = next(item for item in definitions if item.name == "write_file")
    assert write_definition.cancellation_policy == "must_settle"
    assert "cancellation_policy" not in write_definition.model_dump()
    agent = Agent(
        provider=_OneToolProvider(
            "write_file",
            {"path": str(target), "content": "committed\n"},
        ),
        config=AgentConfig(
            max_iterations=1,
            iteration_timeout=1.0,
            tool_timeout=0.02,
        ),
        tool_definitions=[write_definition],
        tool_handler=build_tool_handler(registry, ctx),
        tool_context=ctx,
    )

    turn = asyncio.create_task(_collect_events(agent))
    assert await asyncio.to_thread(worker_started.wait, 0.5)
    await asyncio.wait_for(timeout_cleanup_started.wait(), timeout=0.5)
    assert not turn.done()
    assert not target.exists()

    release_worker.set()
    events = await asyncio.wait_for(turn, timeout=1.0)
    order.append("terminal")

    result_index = next(
        index for index, event in enumerate(events) if isinstance(event, ToolResultEvent)
    )
    done_index = next(index for index, event in enumerate(events) if isinstance(event, DoneEvent))
    result = events[result_index]
    assert isinstance(result, ToolResultEvent)
    assert result.execution_status is not None
    assert result.execution_status["status"] == "timeout"
    assert "effects settled and were recorded" in result.result
    assert result_index < done_index
    assert target.read_text(encoding="utf-8") == "committed\n"
    assert len(ctx.workspace_mutation_receipts) == 1
    assert ctx.workspace_mutation_receipts[0]["changed"] is True
    assert len(ctx.workspace_file_writes) == 1
    assert order == ["disk", "receipt", "terminal"]


@pytest.mark.asyncio
async def test_tool_task_cancellation_becomes_tool_error_without_cancelling_turn() -> None:
    async def _handler(tc: ToolCall) -> ToolResult:
        raise asyncio.CancelledError

    agent = Agent(
        provider=_OneToolProvider(),
        config=AgentConfig(max_iterations=1),
        tool_definitions=[_tool_def()],
        tool_handler=_handler,
    )

    events = [event async for event in agent.run_turn("run")]
    result = next(event for event in events if isinstance(event, ToolResultEvent))

    assert result.is_error
    assert result.execution_status is not None
    assert result.execution_status["status"] == "cancelled"
    assert result.execution_status["reason"] == "cancelled"
    assert result.result == "Tool 'slow_tool' was cancelled"
