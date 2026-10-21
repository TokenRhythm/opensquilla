"""Runtime capabilities survive compaction request snapshots without cloning hosts."""

from __future__ import annotations

import json
import sqlite3
from contextvars import ContextVar

import httpx
import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.registry import ToolRegistry, ToolSpec
from opensquilla.tools.types import ToolContext
from scripts.live_reasoning_replay_e2e import _config


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["active_read", "backup_io", "backup_receipt"])
async def test_preflight_busy_read_recovers_on_wire_and_next_turn_can_commit(
    tmp_path, monkeypatch, fault,
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
    from unittest.mock import AsyncMock

    class NoTools:
        async def inspect_fixture(self):
            raise AssertionError("The continuation must not repeat tools")

    owner = NoTools()
    registry = ToolRegistry()
    registry.register(ToolSpec(name="inspect_fixture", description="Inspect local fixture.",
                               parameters={}),
                      owner.inspect_fixture)
    requests = []
    # Observe the operation, not wording that a prompt revision can remove.
    import opensquilla.session.manager as manager_module

    in_summary = ContextVar("precommit_test_in_summary", default=False)
    real_compact = manager_module.compact_context

    async def observed_compact(*args, **kwargs):
        token = in_summary.set(True)
        try:
            return await real_compact(*args, **kwargs)
        finally:
            in_summary.reset(token)

    monkeypatch.setattr(manager_module, "compact_context", observed_compact)

    def respond(request):
        payload = json.loads(request.content)
        is_summary = in_summary.get()
        requests.append((is_summary, payload))
        assert len(requests) <= 20  # Both turns share the observer.
        answer = "TASK-42 remains active. Ready to continue."
        if is_summary:
            answer = "TASK-42 remains active. Historical logs are disposable."
        chunks = [{"choices": [{"index": 0, "delta": {"content": answer},
                                "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 100, "completion_tokens": 20}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        ).encode())

    real_read = storage.get_all_summaries
    failed_reads = 0

    async def fail_once_after_candidate(*args, **kwargs):
        nonlocal failed_reads
        if any(summary for summary, _ in requests) and not failed_reads:
            failed_reads += 1
            raise sqlite3.OperationalError("database is locked")
        return await real_read(*args, **kwargs)

    if fault == "active_read":
        monkeypatch.setattr(storage, "get_all_summaries", fail_once_after_candidate)
    else:
        checkpoint_method = manager.record_memory_checkpoint

        async def fail_backup_once(*args, **kwargs):
            nonlocal failed_reads
            if not failed_reads:
                failed_reads += 1
                if fault == "backup_io":
                    raise PermissionError("backup directory is temporarily read-only")
                from types import SimpleNamespace
                return SimpleNamespace(scope="checkpoint", status="checkpoint_failed")
            return await checkpoint_method(*args, **kwargs)

        monkeypatch.setattr(manager, "record_memory_checkpoint", fail_backup_once)
    rewrite = AsyncMock(wraps=storage.rewrite_compacted_session)
    monkeypatch.setattr(storage, "rewrite_compacted_session", rewrite)
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
        assert any(summary for summary, _payload in requests) == (fault == "active_read")
        assert any(not summary for summary, _payload in requests)
        assert not [event for event in events if event.kind == "error"]
        assert any(event.kind == "done" for event in events)
        summaries = await manager.get_summaries(key)
        assert failed_reads == 1
        assert not summaries and rewrite.await_count == 0
        # Local storage contention must not bench a healthy summary provider.
        assert key not in runner._compaction_failures
        # A fresh turn must load durable S0/raw, not the refused candidate.
        next_prompt = "Continue TASK-42 after the temporary recovery. Do not call tools."
        next_user = await manager.append_message(key, "user", next_prompt)
        second = [event async for event in runner.run(
            next_prompt, session_key=key, bound_user_message_id=next_user.message_id,
            tool_context=ToolContext(is_owner=True, workspace_dir=config.workspace_dir),
        )]
        assert not [event for event in second if event.kind == "error"]
        assert any(event.kind == "done" for event in second)
        assert rewrite.await_count > 0
        assert await manager.get_summaries(key)
        after = {
            entry.message_id: entry.content
            for entry in await manager.get_canonical_transcript(key)
        }
        assert all(after.get(entry.message_id) == entry.content for entry in before)
    finally:
        await storage.close()
