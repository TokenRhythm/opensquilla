"""The compaction-triggered tool-result store scan runs OFF the event loop.

``ToolResultStore.write`` does a store-wide ``rglob`` (the #305 scan). The
budget-compaction assembly path calls it synchronously; the async wrapper must
run that whole assembly in a worker thread so the O(store) filesystem scan never
blocks the gateway event loop (issue #305 completeness).
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine import tool_result_store as trs_module
from opensquilla.provider import ContentBlockToolResult, ContentBlockToolUse, Message
from opensquilla.tools import ToolRegistry, tool
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.types import ToolContext, current_tool_context


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
        tool_context=ToolContext(session_key="agent:main:webchat:a", agent_id="main"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_turn_bound_body_writer_is_offloop_and_restored(tmp_path, monkeypatch, cancel):
    agent = _agent_with_store(tmp_path)
    loop_thread = threading.get_ident()
    write_threads = []
    original_write = trs_module.ToolResultStore.write

    def write(self, *args, **kwargs):
        write_threads.append(threading.get_ident())
        return original_write(self, *args, **kwargs)

    monkeypatch.setattr(trs_module.ToolResultStore, "write", write)
    context = agent._tool_context
    previous = context.tool_result_snapshot_writer
    references = []

    async def turn(*args, **kwargs):
        assert context.tool_result_snapshot_writer is not None
        token = current_tool_context.set(context)
        try:
            reference = await context.tool_result_snapshot_writer(
                "<external-content>first\r\n正文🙂\r\nlast</external-content>",
                "web_fetch", "fetch-1",
            )
            references.append(reference)
            if cancel:
                raise asyncio.CancelledError
        finally:
            current_tool_context.reset(token)
        if False:
            yield

    monkeypatch.setattr(agent, "_turn_generator", turn)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            _ = [event async for event in agent.run_turn("synthetic fetch")]
    else:
        _ = [event async for event in agent.run_turn("synthetic fetch")]
    assert references[0] is not None
    assert write_threads and all(thread != loop_thread for thread in write_threads)
    assert context.tool_result_snapshot_writer is previous
    record = trs_module.ToolResultStore(str(tmp_path / "store")).read(
        references[0]["handle"], session_id="sid-a",
    )
    assert "\r\n正文🙂\r\n" in record.content


@pytest.mark.asyncio
async def test_body_writer_refuses_missing_scope_or_retrieval(tmp_path):
    agent = _agent_with_store(tmp_path)
    context = agent._tool_context
    token = current_tool_context.set(context)
    try:
        assert await agent._write_tool_body_snapshot("body", "web_fetch", "") is None
        context.tool_result_retrieval_available = False
        assert await agent._write_tool_body_snapshot("body", "web_fetch", "fetch-1") is None
        context.tool_result_retrieval_available = True
        agent._provider_call_tool_result_retrieval_available = False
        assert await agent._write_tool_body_snapshot("body", "web_fetch", "fetch-1") is None
    finally:
        current_tool_context.reset(token)


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


@asynccontextmanager
async def _hold_store_budget(store: trs_module.ToolResultStore):
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with store._budget_lock():
            entered.set()
            release.wait(timeout=3)

    holder = asyncio.create_task(asyncio.to_thread(hold))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        yield
    finally:
        release.set()
        await holder


def _tool_content(messages: list[Message]) -> str:
    return next(
        block.content
        for message in messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1"
    )


async def test_sync_projection_keeps_content_when_output_writer_owns_store(tmp_path: Path) -> None:
    agent = _agent_with_store(tmp_path)
    store = trs_module.ToolResultStore(tmp_path / "store")
    messages = _bulky_messages()
    ticked = asyncio.Event()

    async def heartbeat():
        await asyncio.sleep(0)
        ticked.set()

    async with _hold_store_budget(store):
        heartbeat_task = asyncio.create_task(heartbeat())
        started = time.monotonic()
        projected = agent._provider_request_messages(
            messages,
            request_context_message=None,
            request_context_insert_index=0,
            runtime_context_message=Message(role="user", content="[Runtime context]"),
            runtime_context_insert_index=len(messages),
        )
        assert time.monotonic() - started < 0.5
        await heartbeat_task
        assert ticked.is_set()
        assert _tool_content(projected) == _tool_content(messages)
        assert agent.config.metadata.get("tool_result_store_writes", 0) == 0


async def test_async_projection_waits_for_store_then_publishes_readable_handle(
    tmp_path: Path,
) -> None:
    agent = _agent_with_store(tmp_path)
    store = trs_module.ToolResultStore(tmp_path / "store")
    messages = _bulky_messages()
    async with _hold_store_budget(store):
        projection = asyncio.create_task(agent._provider_request_messages_async(
            messages,
            request_context_message=None,
            request_context_insert_index=0,
            runtime_context_message=Message(role="user", content="[Runtime context]"),
            runtime_context_insert_index=len(messages),
        ))
        await asyncio.sleep(0.05)
        assert not projection.done()

    projected = await asyncio.wait_for(projection, timeout=2)
    text = _tool_content(projected)
    assert len(text) < len(_tool_content(messages))
    handle = text.split("tool_result_handle: ", 1)[1].splitlines()[0]
    record = store.read(handle, session_id="sid-a")
    assert record.content == _tool_content(messages)
