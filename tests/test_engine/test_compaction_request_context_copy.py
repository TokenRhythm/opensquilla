"""Runtime capabilities survive compaction request snapshots without cloning hosts."""

from __future__ import annotations

import copy
import json
import sqlite3

import httpx
import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ChatConfig, ToolDefinition, ToolInputSchema
from opensquilla.session.compaction import CompactionRequestContext
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.registry import ToolRegistry, ToolSpec
from opensquilla.tools.types import ToolContext
from scripts.live_reasoning_replay_e2e import _config


class DatabaseOwner:
    def __init__(self, connection):
        self.connection = connection

    def resolve(self, messages):
        assert self.connection.execute("SELECT 1").fetchone() == (1,)
        return frozenset()

    async def inspect_fixture(self):
        raise AssertionError("The summary and this text-only answer must not execute tools")


def test_request_context_copy_isolates_values_but_preserves_live_resolver():
    connection = sqlite3.connect(":memory:")
    try:
        owner = DatabaseOwner(connection)
        context = CompactionRequestContext(
            chat_config=ChatConfig(context_window_tokens=32768, max_tokens=2048),
            tools=(ToolDefinition(
                name="inspect_fixture", description="Read fixture state.",
                input_schema=ToolInputSchema(properties={"value": {"type": "string"}}),
            ),),
            recoverable_reference_resolver=owner.resolve,
        )
        snapshot = copy.deepcopy(context)
        assert snapshot is not context
        assert snapshot.chat_config is not context.chat_config
        assert snapshot.tools[0] is not context.tools[0]
        assert snapshot.recoverable_reference_resolver is context.recoverable_reference_resolver
        assert snapshot.recoverable_reference_resolver.__self__ is owner
        assert snapshot.recoverable_reference_resolver([]) == frozenset()
        snapshot.chat_config.max_tokens = 17
        snapshot.tools[0].input_schema.properties["value"]["type"] = "integer"
        assert context.chat_config.max_tokens == 2048
        assert context.tools[0].input_schema.properties["value"]["type"] == "string"
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_fault", [False, True], ids=["summary", "empty-summary"])
async def test_turnrunner_preflight_copies_agent_capability_and_reaches_real_wire(
    tmp_path, monkeypatch, summary_fault,
):
    model = "qwen/qwen-2.5-72b-instruct"
    config = _config(tmp_path, "openrouter", model, "https://offline.invalid/v1", thinking="off")
    config.llm.api_key = "offline-only"
    config.llm.context_window_tokens = 32768
    config.llm.max_tokens = 2048
    config.compaction.enabled = True
    config.preflight_compact_ratio = 0.8
    config.tools.allow = ["inspect_fixture"]
    storage = SessionStorage(str(tmp_path / "sessions.sqlite"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path)
    owner_connection = sqlite3.connect(":memory:")
    owner = DatabaseOwner(owner_connection)
    registry = ToolRegistry()
    registry.register(ToolSpec(name="inspect_fixture", description="Inspect local fixture.",
                               parameters={}),
                      owner.inspect_fixture)
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        is_summary = any(
            message.get("role") == "system"
            and "conversation compactor" in str(message.get("content", ""))
            for message in payload["messages"]
        )
        requests.append((is_summary, payload))
        assert len(requests) <= 12
        answer = "TASK-42 remains active. Ready to continue."
        if is_summary:
            answer = "" if summary_fault else (
                "TASK-42 remains active. Historical logs are disposable."
            )
        chunks = [{"choices": [{"index": 0, "delta": {"content": answer},
                                "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 100, "completion_tokens": 20}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        ).encode())

    client = httpx.AsyncClient
    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", lambda **kwargs: client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    try:
        key = "agent:main:context-copy"
        await manager.create(session_key=key, agent_id="main")
        for index in range(7):
            await manager.append_message(key, "user", "TASK-42 remains active. "
                                         + "Historical disposable log entry. " * 1500)
            await manager.append_message(key, "assistant", f"Acknowledged history {index}.")
        before = await manager.get_canonical_transcript(key)
        prompt = "Continue TASK-42 and confirm that you are ready. Do not call tools."
        user = await manager.append_message(key, "user", prompt)
        runner = TurnRunner(provider_selector=ModelSelector(SelectorConfig(primary=ProviderConfig(
            provider="openrouter", model=model, api_key="offline-only",
            base_url="https://offline.invalid/v1",
        ))), session_manager=manager, tool_registry=registry, config=config)
        events = [event async for event in runner.run(
            prompt, session_key=key, bound_user_message_id=user.message_id,
            tool_context=ToolContext(is_owner=True, workspace_dir=config.workspace_dir),
        )]
        assert any(summary for summary, _payload in requests), [event.kind for event in events]
        assert any(not summary for summary, _payload in requests)
        assert not [event for event in events if event.kind == "error"]
        assert any(event.kind == "done" for event in events)
        summaries = await manager.get_summaries(key)
        assert bool(summaries) is not summary_fault
        after = {
            entry.message_id: entry.content
            for entry in await manager.get_canonical_transcript(key)
        }
        assert all(after.get(entry.message_id) == entry.content for entry in before)
        assert owner_connection.execute("SELECT 1").fetchone() == (1,)
    finally:
        owner_connection.close()
        await storage.close()
