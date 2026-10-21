"""Original archive recall uses the real registered tool, never checkpoint prose."""

from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest
import pytest_asyncio

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.types import DoneEvent, ErrorEvent, ToolCall
from opensquilla.provider import ModelCapabilities, OpenAIProvider
from opensquilla.session.compaction_state import StructuredCompactionSummary
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin import sessions as sessions_tools
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry, get_default_registry
from opensquilla.tools.types import ToolContext, ToolError, current_tool_context


@pytest_asyncio.fixture
async def archive(tmp_path, monkeypatch):
    storage = SessionStorage(str(tmp_path / "recall.sqlite"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create("agent:main:archive-recall")
    await manager.append_message(node.session_key, "user", "Original goal: inspect west.")
    await manager.append_message(
        node.session_key, "assistant", "Original action.", tool_calls=[
            {"type": "tool_use", "id": "original-call-id", "name": "lookup",
             "input": {"target": "west", "path": "源文件🐟"}},
        ],
    )
    await manager.append_message(node.session_key, "tool", "WEST_ORIGINAL_EVIDENCE",
                                 tool_call_id="original-call-id")
    await manager.append_message(node.session_key, "assistant", "Read complete.")
    await manager.append_message(node.session_key, "user", "Retrieve the original evidence.")
    raw = await manager.get_transcript(node.session_key)
    source = await manager.capture_compaction_source(node.session_key)
    result = await manager.persist_compaction_result(
        node.session_key, "CHECKPOINT_ONLY_NOT_ORIGINAL", [],
        summary_payload=StructuredCompactionSummary(
            current_status="CHECKPOINT_ONLY_NOT_ORIGINAL",
            source_coverage={"replaces_prior_context": False},
        ).model_dump(mode="json"), summary_format="structured_v1",
        source_snapshot=source, removed_count=4,
    )
    assert result.applied
    monkeypatch.setattr(sessions_tools, "_session_manager", manager)
    ctx = ToolContext(is_owner=True, session_key=node.session_key,
                      session_id=node.session_id, session_epoch=node.epoch)
    token = current_tool_context.set(ctx)
    try:
        yield manager, node, raw, ctx
    finally:
        current_tool_context.reset(token)
        await storage.close()


async def read(**kwargs):
    text = await sessions_tools.sessions_history("current", canonical=True, **kwargs)
    assert len(text.encode("utf-8")) <= 16384
    return json.loads(text)


@pytest.mark.asyncio
async def test_page_and_fragment_recall_preserve_archive_and_default_contract(archive):
    manager, node, raw, _ctx = archive
    legacy = json.loads(await sessions_tools.sessions_history(node.session_key))
    assert set(legacy) == {"session_key", "message_count", "messages"}
    assert "WEST_ORIGINAL_EVIDENCE" not in str(legacy)
    recent = json.loads(await sessions_tools.sessions_history(node.session_key, recent=True))
    assert recent["messages"][-1]["content"] == raw[-1].content
    assert "WEST_ORIGINAL_EVIDENCE" not in str(recent)
    pages = []
    before = None
    while True:
        page = await read(limit=2, before=before)
        assert page["canonical_complete"] is True
        pages.extend(page["entries"])
        before = page["next_before"]
        if before is None:
            break
    assert {entry["entry_id"] for entry in pages} == {entry.id for entry in raw}
    assert all("content" not in entry and "tool_calls" not in entry for entry in pages)
    fragment = await read(entry_id=raw[1].id)
    original = json.loads(fragment["text"])
    assert original["tool_calls"] == raw[1].tool_calls
    assert fragment["source"] == "compacted" and fragment["next_offset"] is None
    result = await read(message_id=raw[2].message_id)
    assert json.loads(result["text"])["tool_call_id"] == "original-call-id"
    assert "WEST_ORIGINAL_EVIDENCE" in result["text"]
    assert "CHECKPOINT_ONLY_NOT_ORIGINAL" not in str(pages) + result["text"]
    assert [entry.model_dump() for entry in await manager.get_canonical_transcript(
        node.session_key,
    )] == [entry.model_dump() for entry in raw]


@pytest.mark.asyncio
@pytest.mark.parametrize("args, message", [
    ({"recent": True, "canonical": True}, "not both"),
    ({"recent": True, "entry_id": 1}, "require canonical=true"),
    ({"canonical": "true"}, "canonical must be a boolean"),
])
async def test_recent_preview_and_canonical_locators_cannot_be_mixed(archive, args, message):
    _manager, node, _raw, _ctx = archive
    with pytest.raises(ToolError, match=message):
        await sessions_tools.sessions_history(node.session_key, **args)


@pytest.mark.asyncio
async def test_fragment_bounds_include_huge_arguments_unicode_and_envelope(archive, monkeypatch):
    manager, node, _raw, _ctx = archive
    arguments = {"payload": "源文件🐟\"\\\n" * 6000}
    entry = await manager.append_message(
        node.session_key, "assistant", "bounded", tool_calls=[
            {"type": "tool_use", "id": "huge-call", "name": "lookup", "input": arguments},
        ],
    )
    entry = (await manager.get_transcript(node.session_key))[-1]
    original = entry.model_dump()
    original_query = manager._storage._read_history_query

    async def bounded_query(*args, **kwargs):
        rows = await original_query(*args, **kwargs)
        if kwargs.get("operation") == "canonical_page":
            assert all(row["tool_calls"] is None and row["content"] is None for row in rows)
        if kwargs.get("operation") == "canonical_fragment":
            assert all(len(row["text"]) <= 4096 for row in rows)
        return rows

    monkeypatch.setattr(manager._storage, "_read_history_query", bounded_query)
    page = await read(limit=1)
    assert "payload" not in str(page)
    parts, offset, revision = [], 0, None
    while True:
        fragment = await read(entry_id=entry.id, offset=offset, revision=revision)
        assert fragment["offset"] == offset
        assert len(fragment["text"]) <= 4096
        parts.append(fragment["text"])
        revision = fragment["revision"]
        next_offset = fragment["next_offset"]
        if next_offset is None:
            break
        assert next_offset == offset + len(fragment["text"])
        assert next_offset > offset
        offset = next_offset
    rebuilt = json.loads("".join(parts))
    assert rebuilt["tool_calls"] == original["tool_calls"]
    assert (await manager.get_transcript(node.session_key))[-1].model_dump() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["foreign_key", "foreign_message", "foreign_entry",
                                  "foreign_cursor", "missing_owner", "stale_epoch", "guest"])
async def test_canonical_recall_requires_current_owner_and_scoped_identity(archive, guard):
    manager, node, _raw, ctx = archive
    foreign = await manager.create("agent:main:foreign-secret")
    secret = await manager.append_message(foreign.session_key, "user", "FOREIGN_SECRET")
    secret = (await manager.get_transcript(foreign.session_key))[-1]
    args = {"session_key": "current", "canonical": True}
    changed = deepcopy(ctx)
    if guard == "foreign_key":
        args["session_key"] = foreign.session_key
    elif guard == "foreign_message":
        args["message_id"] = secret.message_id
    elif guard == "foreign_entry":
        args["entry_id"] = secret.id
    elif guard == "foreign_cursor":
        args["before"] = f"{secret.created_at}:{secret.id}"
    elif guard == "missing_owner":
        changed.session_id = None
    elif guard == "stale_epoch":
        await manager._storage.advance_reset_epoch(node.session_key)
    else:
        changed.guest_safe = True
    token = current_tool_context.set(changed)
    try:
        with pytest.raises(ToolError) as error:
            await sessions_tools.sessions_history(**args)
        assert "FOREIGN_SECRET" not in str(error.value)
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["page", "fragment"])
async def test_owner_reset_during_canonical_read_never_returns_stale_data(
    archive, monkeypatch, mode,
):
    manager, node, raw, _ctx = archive
    method = ("get_canonical_transcript_page" if mode == "page"
              else "get_canonical_transcript_fragment")
    original = getattr(manager._storage, method)

    async def reset_during_read(*args, **kwargs):
        result = await original(*args, **kwargs)
        await manager._storage.advance_reset_epoch(node.session_key)
        return result

    monkeypatch.setattr(manager._storage, method, reset_during_read)
    with pytest.raises(ToolError):
        await read(**({"entry_id": raw[2].id} if mode == "fragment" else {}))


@pytest.mark.asyncio
async def test_archive_gap_is_reported_and_fragments_reject_same_length_updates(archive):
    manager, node, raw, _ctx = archive
    first = await read(entry_id=raw[1].id)
    await manager._storage.conn.execute(
        "UPDATE compacted_transcript_entries SET tool_calls = ? WHERE original_entry_id = ?",
        (json.dumps(raw[1].tool_calls).replace("west", "east"), raw[1].id),
    )
    await manager._storage.conn.commit()
    with pytest.raises(ToolError, match="changed"):
        await read(entry_id=raw[1].id, revision=first["revision"])
    await manager._storage.conn.execute(
        "DELETE FROM compacted_transcript_entries WHERE original_entry_id = ?", (raw[0].id,),
    )
    await manager._storage.conn.commit()
    assert (await read())["canonical_complete"] is False


@pytest.mark.asyncio
async def test_real_agent_32k_uses_registered_recall_tool_across_physical_requests(
    archive, monkeypatch,
):
    manager, node, raw, ctx = archive
    registered = get_default_registry().get("sessions_history")
    assert registered is not None and "canonical" in registered.spec.parameters
    registry = ToolRegistry()
    registry.register(registered.spec, registered.handler)
    search = get_default_registry().get("tool_search")
    registry.register(search.spec, search.handler)
    definitions = registry.to_model_tool_definitions(registry.to_tool_definitions(ctx), ctx)
    assert [definition.name for definition in definitions] == ["tool_search"]
    physical = []

    def response(request):
        payload = json.loads(request.content)
        physical.append(payload)
        tools = [message for message in payload["messages"] if message["role"] == "tool"]
        if not tools:
            assert {item["function"]["name"] for item in payload["tools"]} == {"tool_search"}
            name = "tool_search"
            arguments = {"query": "pre-compaction original archive", "limit": 1}
        elif len(tools) == 1:
            assert json.loads(tools[-1]["content"])["matches"][0]["name"] == "sessions_history"
            assert "sessions_history" in {item["function"]["name"] for item in payload["tools"]}
            name = "sessions_history"
            arguments = {"session_key": "current", "canonical": True, "limit": 2}
        elif len(tools) == 2:
            name = "sessions_history"
            page = json.loads(tools[-1]["content"])
            assert page["next_before"]
            arguments = {"session_key": "current", "canonical": True, "limit": 2,
                         "before": page["next_before"]}
        elif len(tools) == 3:
            name = "sessions_history"
            page = json.loads(tools[-1]["content"])
            target = next(entry for entry in page["entries"] if entry["role"] == "tool")
            arguments = {"session_key": "current", "canonical": True,
                         "message_id": target["message_id"]}
        else:
            original = json.loads(json.loads(tools[-1]["content"])["text"])
            assert original["tool_call_id"] == "original-call-id"
            assert original["content"] == "WEST_ORIGINAL_EVIDENCE"
            body = {"choices": [{"delta": {"content": "Recovered WEST_ORIGINAL_EVIDENCE."},
                                  "finish_reason": "stop"}]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n")
        body = {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": f"recall-{len(tools)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]}, "finish_reason": "tool_calls"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n")

    client = httpx.AsyncClient
    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", lambda **kwargs: client(
        **{**kwargs, "transport": httpx.MockTransport(response)},
    ))
    agent = Agent(
        provider=OpenAIProvider(api_key="synthetic-unused", model="synthetic-recall-32k"),
        config=AgentConfig(context_window_tokens=32768, context_window_known=True,
                           max_tokens=2048, max_provider_retries=0,
                           model_capabilities=ModelCapabilities(supports_tools=True),
                           tool_result_compression_enabled=False,
                           tool_result_projection_max_inline_chars=32768),
        tool_definitions=definitions, tool_registry=registry, tool_context=ctx,
        tool_handler=build_tool_handler(registry, ctx),
    )
    events = [event async for event in agent.run_turn("Recall the original west tool evidence.")]
    assert len(physical) == 5
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert [entry.model_dump() for entry in await manager.get_canonical_transcript(
        node.session_key,
    )] == [entry.model_dump() for entry in raw]


@pytest.mark.asyncio
async def test_guest_registry_dispatch_denies_archive_tool_before_read(archive, monkeypatch):
    _manager, _node, _raw, ctx = archive
    guest = deepcopy(ctx)
    guest.guest_safe = True
    registered = get_default_registry().get("sessions_history")
    registry = ToolRegistry()
    registry.register(registered.spec, registered.handler)
    token = current_tool_context.set(guest)
    try:
        result = await build_tool_handler(registry, guest)(ToolCall(
            tool_use_id="guest-recall", tool_name="sessions_history",
            arguments={"session_key": "current", "canonical": True},
        ))
        assert result.is_error
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_page_budget_never_skips_entries_when_long_unicode_ids_fill_envelope(archive):
    manager, node, raw, _ctx = archive
    for index in range(30):
        await manager.append_message(node.session_key, "user", "small body",
                                     message_id=f"{index}-" + "中" * 200)
    expected = {entry.id for entry in await manager.get_canonical_transcript(node.session_key)}
    seen, before, pages = [], None, 0
    while True:
        result = await read(limit=100, before=before)
        seen.extend(entry["entry_id"] for entry in result["entries"])
        before = result["next_before"]
        pages += 1
        if before is None:
            break
    assert pages > 1 and set(seen) == expected
    assert len(seen) == len(expected) == len(raw) + 30


@pytest.mark.asyncio
async def test_legacy_missing_entry_identity_reports_unavailable_continuation(archive):
    manager, _node, _raw, _ctx = archive
    await manager._storage.conn.execute(
        "UPDATE compacted_transcript_entries SET original_entry_id = NULL",
    )
    await manager._storage.conn.commit()
    result = await read(limit=2)
    assert result["canonical_complete"] is False
    assert result["continuation_unavailable"] is True
    assert result["has_more"] is True and result["next_before"] is None
