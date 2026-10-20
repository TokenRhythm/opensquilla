"""P4: full checkpoint bodies survive reads, forks, exports and reopen."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from opensquilla.cli.gateway_client import session_history_all
from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent, ErrorEvent
from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.gateway.rpc_chat import _handle_chat_history
from opensquilla.gateway.rpc_sessions import _handle_sessions_history_page_v2
from opensquilla.session.compaction_state import (
    StructuredCompactionSummary,
    render_structured_summary,
)
from opensquilla.session.context_view import build_compaction_context_records
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.session.tokenizer import estimate_tokens
from tests.test_engine.test_agent_llm_budget import _FinalProofBudgetProvider


@asynccontextmanager
async def _reopen(database):
    storage = SessionStorage(str(database))
    await storage.connect()
    try:
        yield SessionManager(storage, inject_time_prefix=False)
    finally:
        await storage.close()


def _payload(body, *, replacement):
    return StructuredCompactionSummary(
        current_status=body, constraints_and_preferences=["Do not deploy without approval."],
        source_coverage={"replaces_prior_context": replacement},
    ).model_dump(mode="json")


async def _seed(database):
    async with _reopen(database) as manager:
        node = await manager.create("agent:main:webchat:p4-contracts")
        for role, text in [
            ("user", "Task Alpha: inspect the report; do not deploy."),
            ("assistant", "Alpha inspection completed."),
            ("user", "Alpha is done. Continue Task Beta: package the result."),
            ("assistant", "Beta package is prepared; tests remain."),
            ("user", "Run tests for Beta; keep deployment disabled."),
        ]:
            await manager.append_message(node.session_key, role, text, token_count=20)
        canonical = await manager.get_canonical_transcript(node.session_key)
        initial = await manager.capture_compaction_source(node.session_key)
        payload0 = _payload("S0_OLD_PLAN: inspect Alpha.", replacement=False)
        assert (await manager.persist_compaction_result(
            node.session_key, payload0["current_status"], [],
            summary_payload=payload0, summary_format="structured_v1", removed_count=2,
            source_snapshot=initial, compaction_id="p4-S0",
        )).applied
        source1 = await manager.capture_compaction_source(node.session_key)
        payload1 = _payload(
            "S1_CURRENT: Alpha completed; Beta package prepared; run tests next.",
            replacement=True,
        )
        assert (await manager.persist_compaction_result(
            node.session_key, payload1["current_status"], [],
            summary_payload=payload1, summary_format="structured_v1", removed_count=2,
            source_snapshot=source1, compaction_id="p4-S1",
        )).applied
        assert await manager.get_canonical_transcript(node.session_key) == canonical
        return node, canonical, initial, source1, payload1


@pytest.mark.asyncio
async def test_checkpoint_body_history_endpoints_fork_and_export(tmp_path, monkeypatch):
    from opensquilla.cli import sessions_cmd

    database = tmp_path / "contracts.sqlite"
    node, canonical, _initial, _source1, _payload1 = await _seed(database)
    async with _reopen(database) as manager:
        context = RpcContext(
            conn_id="p4-contracts", principal=SimpleNamespace(role="operator"),
            session_manager=manager,
        )
        chat = await _handle_chat_history({
            "sessionKey": node.session_key, "includeCanonical": True,
            "includeSummaries": True, "limit": 20,
        }, context)
        page = await _handle_sessions_history_page_v2({
            "key": node.session_key, "direction": "before", "target_items": 20,
            "session_id": node.session_id, "session_epoch": node.epoch,
        }, context)
        summaries = await manager.get_summaries(node.session_key)
        bodies = [render_structured_summary(summary.summary_payload) for summary in summaries]
        assert [summary.summary_text for summary in summaries] == bodies
        for response in (chat, page):
            assert response["canonical_available"] and response["canonical_complete"]
            assert [
                summary["summary_text"] for summary in response["compaction_summaries"]
            ] == bodies
            assert all(body.count("[Structured Compaction Summary]") == 1 for body in bodies)
            assert all("[Compacted Session Summaries]" not in body for body in bodies)
        assert len(chat["messages"]) == len(page["items"]) == len(canonical)

        active = await manager.get_transcript(node.session_key)
        budget = sum(entry.token_count or 0 for entry in active) + sum(
            estimate_tokens(body) for body in bodies
        )
        prose_only_budget = sum(entry.token_count or 0 for entry in active) + sum(
            estimate_tokens(summary.summary_payload["current_status"]) for summary in summaries
        )
        assert budget > prose_only_budget
        denied = await manager.branch(
            node.session_key, "agent:main:webchat:p4-fork-small",
            fork_transcript=True, max_fork_tokens=budget - 1,
        )
        allowed = await manager.branch(
            node.session_key, "agent:main:webchat:p4-fork-fit",
            fork_transcript=True, max_fork_tokens=budget,
        )
        assert not denied.forked_from_parent
        assert allowed.forked_from_parent
        assert [row.content for row in await manager.get_canonical_transcript(
            allowed.session_key,
        )] == [row.content for row in canonical]
        assert (await manager._storage.get_canonical_transcript_coverage(
            allowed.session_id,
        )).canonical_complete
        assert [summary.summary_text for summary in await manager.get_summaries(
            allowed.session_key,
        )] == bodies

        page_requests = []

        async def read_export_page(key, **kwargs):
            page_requests.append(kwargs)
            return await _handle_chat_history({
                "sessionKey": key, "limit": kwargs["limit"], "before": kwargs["before"],
                "includeCanonical": kwargs["include_canonical"],
                "includeSummaries": kwargs["include_summaries"],
            }, context)

        history = await session_history_all(read_export_page, node.session_key, page_size=2)
        assert len(page_requests) == 3 and history["canonical_complete"]
        assert history["compaction_summaries"] == []
        assert [message["message_id"] for message in history["messages"]] == [
            entry.message_id for entry in canonical
        ]
        # The reader above is real SQLite -> gateway -> CLI pagination. Feed
        # that result to the actual CLI file renderers without starting a server.
        async def captured_client_result(_action):
            return {
                "resolved": {"session_key": node.session_key}, "preview": {"previews": []},
                "history": history,
            }

        monkeypatch.setattr(sessions_cmd, "_with_client", captured_client_result)
        for extension in ("json", "md"):
            output = tmp_path / f"export.{extension}"
            await asyncio.to_thread(
                sessions_cmd.sessions_export, node.session_key, output, extension,
            )
            exported = output.read_text(encoding="utf-8")
            assert all(entry.content in exported for entry in canonical)
            assert "S0_OLD_PLAN" not in exported and "S1_CURRENT" not in exported
            if extension == "json":
                assert len(json.loads(exported)["history"]["messages"]) == len(canonical)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_kind", ["old_generation", "duplicate_current"])
async def test_reopened_replacement_rejects_late_writer_and_replays_once(tmp_path, late_kind):
    database = tmp_path / "restart.sqlite"
    node, canonical, initial, source1, payload1 = await _seed(database)
    async with _reopen(database) as manager:
        before = [summary.model_dump() for summary in await manager.get_summaries(node.session_key)]
        late_source = initial if late_kind == "old_generation" else source1
        late_payload = _payload("LATE_OLD_TASK: restart Alpha", replacement=True)
        rejected = await manager.persist_compaction_result(
            node.session_key, late_payload["current_status"], [],
            summary_payload=late_payload, summary_format="structured_v1", removed_count=2,
            source_snapshot=late_source,
            compaction_id="p4-S0" if late_kind == "old_generation" else "p4-S1",
            expected_session_id=node.session_id, expected_session_epoch=node.epoch,
        )
        assert not rejected.applied and rejected.reason == "stale_context_state"
        assert [summary.model_dump() for summary in await manager.get_summaries(
            node.session_key,
        )] == before
        assert await manager.get_canonical_transcript(node.session_key) == canonical
        assert (await manager.get_session(node.session_key)).compaction_count == 2
        records = build_compaction_context_records(
            context_states=await manager.get_context_states(node.session_key),
            summaries=await manager.get_summaries(node.session_key),
        )
        assert len(records) == 1 and records[0].text == render_structured_summary(payload1)
        provider = _FinalProofBudgetProvider()
        agent = Agent(provider=provider, config=AgentConfig(
            context_window_tokens=32768, max_tokens=1024, max_provider_retries=0,
        ))
        runner = TurnRunner(provider_selector=None, session_manager=manager)
        visible = await runner._load_history(
            agent, node.session_key, trim_last_user=False,
            expected_session_id=node.session_id, expected_session_epoch=node.epoch,
        )
        agent.config.compaction_summary_context = visible
        agent.bind_compaction_source(await manager.capture_compaction_source(node.session_key))
        events = [event async for event in agent.run_turn("Continue Beta tests.")]
        assert any(isinstance(event, DoneEvent) for event in events)
        assert not any(isinstance(event, ErrorEvent) for event in events)
        assert len(provider.calls) == 1
        wire = str(provider.calls[0])
        assert wire.count("[Compacted Session Summaries]") == 1
        assert wire.count("[Structured Compaction Summary]") == 1
        assert wire.count("S1_CURRENT") == 1
        assert "S0_OLD_PLAN" not in wire and "LATE_OLD_TASK" not in wire
