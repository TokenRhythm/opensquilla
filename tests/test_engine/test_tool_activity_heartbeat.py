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

    async def _handler(tc: ToolCall) -> ToolResult:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return ToolResult(tc.tool_use_id, tc.tool_name, "late-success")

    agent = Agent(
        provider=_OneToolProvider(),
        config=AgentConfig(max_iterations=1, tool_timeout=60.0),
        tool_definitions=[_tool_def()],
        tool_handler=_handler,
    )
    turn = asyncio.create_task(_collect_events(agent))
    await asyncio.wait_for(started.wait(), timeout=0.2)

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, timeout=0.2)
    assert cancelled.is_set()

    release.set()


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
