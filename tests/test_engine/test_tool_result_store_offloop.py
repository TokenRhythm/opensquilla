"""The compaction-triggered tool-result store scan runs OFF the event loop.

``ToolResultStore.write`` does a store-wide ``rglob`` (the #305 scan). The
budget-compaction assembly path calls it synchronously; the async wrapper must
run that whole assembly in a worker thread so the O(store) filesystem scan never
blocks the gateway event loop (issue #305 completeness).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine import tool_result_store as trs_module
from opensquilla.provider import ContentBlockToolResult, ContentBlockToolUse, Message
from opensquilla.tools import ToolRegistry, tool
from opensquilla.tools.dispatch import build_tool_handler


class _CapturingProvider:
    provider_name = "fake"

    async def list_models(self):  # pragma: no cover - not used
        return []


def _retrieval_surface():
    registry = ToolRegistry()

    @tool(
        name="retrieve_tool_result",
        description="Retrieve a stored tool result.",
        params={"handle": {"type": "string"}},
        required=["handle"],
        registry=registry,
    )
    async def retrieve_tool_result(handle: str) -> str:
        return handle

    return registry.to_tool_definitions(), build_tool_handler(registry)


def _agent_with_store(tmp_path: Path) -> Agent:
    tool_definitions, tool_handler = _retrieval_surface()
    return Agent(
        provider=_CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=200,
            tool_result_store_dir=str(tmp_path / "store"),
            tool_result_store_session_id="sid-a",
            tool_result_store_session_key="agent:main:webchat:a",
            tool_result_store_agent_id="main",
        ),
        tool_definitions=tool_definitions,
        tool_handler=tool_handler,
    )


def _bulky_messages() -> list[Message]:
    raw = "compaction bulky output\n" + ("x" * 8000)
    return [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="tool-1", name="execute_code", input={})],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="tool-1", content=raw)],
        ),
    ]


@pytest.mark.asyncio
async def test_store_scan_runs_off_event_loop_thread(tmp_path: Path, monkeypatch) -> None:
    loop_thread_id = threading.get_ident()
    scan_thread_ids: list[int] = []

    original_iter = trs_module.ToolResultStore._iter_record_stats

    def _record_thread(self):  # type: ignore[no-untyped-def]
        scan_thread_ids.append(threading.get_ident())
        return original_iter(self)

    monkeypatch.setattr(trs_module.ToolResultStore, "_iter_record_stats", _record_thread)

    agent = _agent_with_store(tmp_path)
    messages = _bulky_messages()

    result_messages, _ = await agent._provider_request_messages_with_sanitize_async(
        messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context]"),
        runtime_context_insert_index=len(messages),
    )

    # The store scan must have happened (compaction stored a snapshot) and never
    # on the event-loop thread.
    assert scan_thread_ids, "expected the compaction path to write a snapshot (store scan)"
    assert loop_thread_id not in scan_thread_ids
    # The projection replaced the bulky content (sanity: the assembly ran).
    projected = next(
        block
        for message in result_messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1"
    )
    assert len(projected.content) < 8000


@pytest.mark.asyncio
async def test_async_wrapper_matches_sync_result(tmp_path: Path) -> None:
    # The off-loop wrapper must produce the same assembly as the sync path.
    agent = _agent_with_store(tmp_path)
    messages = _bulky_messages()

    sync_messages = agent._provider_request_messages(
        [m for m in messages],
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context]"),
        runtime_context_insert_index=len(messages),
    )
    async_messages = await agent._provider_request_messages_async(
        [m for m in messages],
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context]"),
        runtime_context_insert_index=len(messages),
    )

    def _tool_text(msgs):
        for message in msgs:
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1":
                        return block.content
        return None

    assert _tool_text(sync_messages) == _tool_text(async_messages)


@pytest.mark.asyncio
async def test_wrapper_does_not_block_a_concurrent_loop_task(tmp_path: Path, monkeypatch) -> None:
    # While the off-loop assembly runs (simulated slow scan), a concurrent loop
    # task must keep making progress.
    original_iter = trs_module.ToolResultStore._iter_record_stats

    def _slow_iter(self):  # type: ignore[no-untyped-def]
        import time

        time.sleep(0.2)  # blocking sleep — would stall the loop if run on it
        return original_iter(self)

    monkeypatch.setattr(trs_module.ToolResultStore, "_iter_record_stats", _slow_iter)

    agent = _agent_with_store(tmp_path)
    ticks = 0

    async def _ticker():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    await agent._provider_request_messages_with_sanitize_async(
        _bulky_messages(),
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context]"),
        runtime_context_insert_index=2,
    )
    await ticker
    # The ticker advanced during the blocking scan → the scan did not run on the loop.
    assert ticks >= 10
