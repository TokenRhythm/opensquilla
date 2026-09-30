"""Actual Gateway/TurnRunner/SQLite continuity across repeated runtime restarts."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from opensquilla.engine.cache_break_monitor import active_compaction_ids, add_compaction_listener
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent as AgentDoneEvent
from opensquilla.engine.types import ErrorEvent as AgentErrorEvent
from opensquilla.gateway.adapters.session_maintenance import (
    build_gateway_session_maintenance_adapter,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.gateway.rpc_sessions import _task_state_summary
from opensquilla.gateway.session_streams import get_session_streams
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.session.context_view import (
    build_compaction_context_records,
    compaction_replay_is_complete,
    format_compaction_summary_context,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import CallerKind, ToolContext


async def _rows(manager, key):
    return [row.model_dump(mode="json") for row in await manager.get_canonical_transcript(key)]


async def _checkpoint_rows(storage, node):
    return [row.model_dump(mode="json") for row in await storage.get_all_summaries(node.session_id)]


async def _assert_idle(case):
    assert active_compaction_ids(case.key) == ()
    state = _task_state_summary(await case.storage.list_agent_tasks(session_key=case.key))
    assert state["run_status"] == "idle" and state["active_task"] is None
    assert get_session_streams().live_snapshot(case.key).task_id is None


async def test_twenty_persisted_window_turns_reopen_and_recover_durable_summary(
    tmp_path, monkeypatch,
):
    from opensquilla import token_estimation

    monkeypatch.setattr(token_estimation, "_encoding", token_estimation._ENCODING_UNAVAILABLE)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "profile"))
    database = tmp_path / "turns.sqlite"
    workspace = tmp_path / "w"
    workspace.mkdir()
    key = "agent:main:webchat:continuity"
    model = "synthetic-continuity"
    config = GatewayConfig(
        workspace_dir=str(workspace),
        llm={"provider": "openai", "model": model, "api_key": "synthetic-unused",
             "base_url": "https://example.invalid/v1", "context_window_tokens": 16_000,
             "max_tokens": 1024, "thinking": "off"},
        compaction={"protected_recent_messages": 0},
        prompt={"mode": "minimal"}, squilla_router={"enabled": False},
        llm_ensemble={"enabled": False},
    )
    config.attachments.media_root = str(tmp_path / "media")
    summary_available = True
    current_prompt = ""
    summary_requests = []
    ordinary_requests = []
    auxiliary_failures = 0

    async def provider_reply(self, messages, tools=None, config=None):
        nonlocal auxiliary_failures
        projection = self.project_final_request(messages, tools, config)
        assert projection.fits and projection.proof.get("fits", True)
        text = "\n".join(str(message.content) for message in messages)
        if config.candidate_output_mode == "inert_artifact":
            summary_requests.append(text)
            if not summary_available:
                auxiliary_failures += 1
                yield ErrorEvent(code="503", message="Synthetic summary service unavailable")
                return
            yield TextDeltaEvent(text=(
                "Persisted fact: VIOLET-731. Earlier completed work remains recorded. "
                "Continue the current task."
            ))
        else:
            assert current_prompt in text
            assert "VIOLET-731" in text  # The persisted checkpoint remains provider-visible.
            ordinary_requests.append(text)
            yield TextDeltaEvent(text=f"Acknowledged {current_prompt}")
        yield DoneEvent(stop_reason="stop", input_tokens=100, output_tokens=40)

    # Only transport output is substituted. Request projection, summary selection,
    # failure recovery, source ownership, persistence and replay remain production.
    monkeypatch.setattr(OpenAIProvider, "chat", provider_reply)

    @asynccontextmanager
    async def reopen():
        storage = SessionStorage(str(database))
        await storage.connect()
        manager = SessionManager(
            storage, inject_time_prefix=False, checkpoint_workspace_dir=workspace,
        )
        selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
            provider="openai", model=model, api_key="synthetic-unused",
            base_url="https://example.invalid/v1",
        )))
        locks = {}
        runner = TurnRunner(
            provider_selector=selector, session_manager=manager, config=config,
            model_catalog=ModelCatalog(),
            session_lock_provider=lambda value: locks.setdefault(value, asyncio.Lock()),
        )
        context = RpcContext(
            conn_id="offline-continuity", config=config, session_manager=manager,
            provider_selector=selector, turn_runner=runner,
        )
        try:
            yield SimpleNamespace(
                storage=storage, manager=manager, runner=runner, key=key,
                adapter=build_gateway_session_maintenance_adapter(context),
            )
        finally:
            await storage.close()

    async def run_turn(case, owner):
        events = [event async for event in case.runner.run(
            current_prompt, key,
            ToolContext(is_owner=True, caller_kind=CallerKind.WEB,
                        session_key=key, workspace_dir=str(workspace)),
            persist_input=True, history_has_persisted_user=False, no_memory_capture=True,
            max_iterations=1, max_provider_retries=0, length_capped_continuations=0,
            expected_session_id=owner[0], expected_session_epoch=owner[1],
        )]
        assert not any(isinstance(event, AgentErrorEvent) for event in events), events
        done = [event for event in events if isinstance(event, AgentDoneEvent)]
        assert len(done) == 1 and done[0].text == f"Acknowledged {current_prompt}"
        assert done[0].model == model and done[0].generation_epoch == 0

    async with reopen() as case:
        node = await case.manager.create(key)
        owner = node.session_id, node.epoch
        for index in range(8):
            await case.manager.append_message(
                key, "user" if index % 2 == 0 else "assistant",
                f"VIOLET-731 initial source {index}. " + "completed earlier work " * 100,
            )
        before = await _rows(case.manager, key)
        seeded = await case.adapter.compact({"key": key})
        assert seeded["applied"] is True and seeded["durability"] == "durable"
        assert seeded["summary_len"] > 0
        assert await _rows(case.manager, key) == before
        checkpoint_before = await _checkpoint_rows(case.storage, node)
        assert len(checkpoint_before) == 1
        for index in range(12):
            await case.manager.append_message(
                key, "user" if index % 2 == 0 else "assistant",
                f"HIDDEN_HISTORY_{index:02d}. " + "completed background details " * 240,
            )
        canonical = await _rows(case.manager, key)
        await _assert_idle(case)

    summary_available = False
    for index in range(20):
        # New storage, manager and runner each time exercise durable replay after
        # restart. The process-local failure circuit intentionally starts fresh.
        async with reopen() as case:
            node = await case.manager.get_session(key)
            assert (node.session_id, node.epoch) == owner
            assert node.compaction_count == 1
            assert await _rows(case.manager, key) == canonical
            assert await _checkpoint_rows(case.storage, node) == checkpoint_before
            if index == 0:
                failed_manual = await case.adapter.compact({"key": key})
                assert failed_manual["status"] == "skipped"
                assert failed_manual["applied"] is False
                assert failed_manual["reason"] == "summary_failed"
            compactions = []
            remove_listener = add_compaction_listener(
                lambda event_key, payload: compactions.append(payload)
                if event_key == key else None,
            )
            current_prompt = f"Continue synthetic step {index:02d}."
            try:
                await run_turn(case, owner)
            finally:
                remove_listener()
            assert any(event["status"] == "emergency_ephemeral" for event in compactions)
            assert not any(event["status"] == "completed" for event in compactions)
            assert "Temporary history window" in ordinary_requests[-1]
            after = await _rows(case.manager, key)
            assert after[:len(canonical)] == canonical
            assert len(after) == len(canonical) + 2
            assert after[-2]["role"] == "user" and after[-2]["content"] == current_prompt
            assert after[-1]["role"] == "assistant"
            assert after[-1]["content"] == f"Acknowledged {current_prompt}"
            assert len({row["message_id"] for row in after}) == len(after)
            assert await _checkpoint_rows(case.storage, node) == checkpoint_before
            current_node = await case.manager.get_session(key)
            assert (current_node.session_id, current_node.epoch) == owner
            assert current_node.compaction_count == 1
            canonical = after
            await _assert_idle(case)
    assert len(ordinary_requests) == 20
    assert auxiliary_failures >= 20
    assert any("HIDDEN_HISTORY_00" not in request for request in ordinary_requests)

    summary_available = True
    recovery_start = len(summary_requests)
    async with reopen() as case:
        node = await case.manager.get_session(key)
        assert (node.session_id, node.epoch) == owner
        assert await _rows(case.manager, key) == canonical
        response = await case.adapter.compact({"key": key, "wait": False})
        assert response["status"] == "started"
        async with asyncio.timeout(5):
            while active_compaction_ids(key):
                await asyncio.sleep(0.01)
        replay = get_session_streams().replay(key, since_stream_seq=0)
        terminals = [event.payload for event in replay.events
                     if event.event_name == "session.event.compaction"
                     and event.payload.get("compaction_id") == response["compaction_id"]
                     and event.payload.get("status") in {
                         "completed", "skipped", "failed", "cancelled",
                     }]
        assert len(terminals) == 1
        assert terminals[0]["status"] == "completed" and terminals[0]["applied"] is True
        assert terminals[0]["durability"] == "durable" and terminals[0]["summary_len"] > 0
        assert any("HIDDEN_HISTORY_00" in text for text in summary_requests[recovery_start:])
        assert await _rows(case.manager, key) == canonical
        assert (await case.manager.get_session(key)).compaction_count == 2
        recovered_checkpoint = await _checkpoint_rows(case.storage, node)
        await _assert_idle(case)

    async with reopen() as case:
        node = await case.manager.get_session(key)
        assert (node.session_id, node.epoch) == owner
        assert node.compaction_count == 2
        assert await _rows(case.manager, key) == canonical
        assert await _checkpoint_rows(case.storage, node) == recovered_checkpoint
        records = build_compaction_context_records(
            context_states=await case.manager.get_context_states(key),
            summaries=await case.storage.get_all_summaries(node.session_id),
        )
        texts = [record.text for record in records]
        rendered = format_compaction_summary_context(texts)
        assert rendered and compaction_replay_is_complete(texts, rendered)
        current_prompt = "Continue after recovered checkpoint restart."
        await run_turn(case, owner)
        assert "Temporary history window" not in ordinary_requests[-1]
        assert (await _rows(case.manager, key))[:len(canonical)] == canonical
        await _assert_idle(case)
