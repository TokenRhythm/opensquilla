"""Unreadable checkpoints recover from proven raw archives, never older tasks."""

import json

import pytest
import pytest_asyncio

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent, ErrorEvent
from opensquilla.session import manager as manager_module
from opensquilla.session.compaction import CompactionResult
from opensquilla.session.compaction_state import StructuredCompactionSummary
from opensquilla.session.context_view import (
    CompactionCheckpointUnavailableError,
    build_compaction_context_records,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage, StaleEpochError
from tests.test_engine.test_agent_llm_budget import _FinalProofBudgetProvider


@pytest_asyncio.fixture
async def recovered_session():
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create("agent:main:canonical-recovery")
    await manager.append_message(node.session_key, "user", "Fix TASK-42. Do not deploy.")
    await manager.append_message(
        node.session_key, "assistant", "Tests passed. Deployment was not run.",
    )
    await manager.append_message(node.session_key, "user", "Continue TASK-42 with packaging only.")
    original = await manager.get_canonical_transcript(node.session_key)
    source = await manager.capture_compaction_source(node.session_key)
    result = await manager.persist_compaction_result(
        node.session_key, "Obsolete rewrite module X",
        [{"role": "user", "content": original[-1].content}], removed_count=2,
        source_entries=source.entries, source_preimage=source.preimage,
        source_context_fingerprint=source.context_fingerprint,
    )
    assert result.applied
    state = (await manager.get_context_states(node.session_key))[0]
    await storage.conn.execute(
        "UPDATE session_context_states SET payload = ? WHERE id = ?",
        (json.dumps({**state.payload, "known_failures": [{"count": 3}]}), state.id),
    )
    await storage.conn.execute("UPDATE session_summaries SET summary_text = ''")
    await storage.conn.commit()
    yield manager, node, original
    await storage.close()


async def context(manager, node):
    return await manager.resolve_compaction_context(
        node.session_key,
        context_states=await manager.get_context_states(node.session_key),
        summaries=await manager.get_summaries(node.session_key),
        expected_session_id=node.session_id, expected_session_epoch=node.epoch,
    )


@pytest.mark.asyncio
async def test_canonical_checkpoint_recovery_replays_only_archived_data(recovered_session):
    manager, node, original = recovered_session
    records, fingerprint = await context(manager, node)
    assert fingerprint.startswith(f"canonical:{original[1].id}:")
    assert len(records) == 1 and records[0].source == "canonical_recovery"
    assert "Fix TASK-42. Do not deploy." in records[0].text
    assert "Tests passed" in records[0].text
    assert "packaging only" not in records[0].text
    assert "Obsolete rewrite" not in records[0].text
    assert "not a new user instruction" in records[0].text
    runner = TurnRunner(provider_selector=None, session_manager=manager)
    visible = await runner._compaction_summary_context(
        node.session_key, [], expected_session_id=node.session_id,
        expected_session_epoch=node.epoch,
    )
    assert "TASK-42" in visible and visible.count("[Compacted Session Summaries]") == 1
    source = await manager.capture_compaction_source(node.session_key)
    assert source.previous_summary == records[0].text
    assert source.context_fingerprint == fingerprint
    assert [entry.id for entry in source.entries] == [original[-1].id]
    assert await manager.get_canonical_transcript(node.session_key) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["active", "in_turn"])
async def test_canonical_recovery_candidate_replaces_damaged_state(
    recovered_session, monkeypatch, writer,
):
    manager, node, original = recovered_session
    source = await manager.capture_compaction_source(node.session_key)
    payload = StructuredCompactionSummary(
        current_status="TASK-42 tests passed. Package only; do not deploy.",
        source_coverage={"replaces_prior_context": True},
    ).model_dump(mode="json")
    if writer == "active":
        async def compact(request):
            assert "Fix TASK-42" in request.previous_summary
            assert "Tests passed" in request.previous_summary
            assert request.entries[0]["content"] == original[-1].content
            return CompactionResult(
                summary=payload["current_status"], summary_payload=payload,
                summary_format="structured_v1", summary_source="llm",
                removed_count=0, kept_entries=request.entries, chunks_processed=1,
                replaced_previous_summary=True,
            )

        monkeypatch.setattr(manager_module, "compact_context", compact)
        result = await manager.compact_with_result(
            node.session_key, context_window_tokens=32768, trigger_reason="manual",
        )
        assert result.replaced_previous_summary and result.skip_reason is None
    else:
        result = await manager.persist_compaction_result(
            node.session_key, payload["current_status"],
            [{"role": "user", "content": original[-1].content}],
            removed_count=0, summary_payload=payload, summary_format="structured_v1",
            source_entries=source.entries, source_preimage=source.preimage,
            source_context_fingerprint=source.context_fingerprint,
        )
        assert result.applied
    records = build_compaction_context_records(
        context_states=await manager.get_context_states(node.session_key),
        summaries=await manager.get_summaries(node.session_key),
    )
    assert len(records) == 1 and "Package only" in records[0].text
    assert records[0].source == "context_state"
    assert await manager.get_canonical_transcript(node.session_key) == original
    coverage = await manager._storage.get_canonical_transcript_coverage(node.session_id)
    assert coverage.canonical_complete


@pytest.mark.asyncio
async def test_incomplete_archive_does_not_fall_back_to_active_tail(recovered_session):
    manager, node, _ = recovered_session
    await manager._storage.conn.execute(
        "DELETE FROM compacted_transcript_entries WHERE id = "
        "(SELECT MIN(id) FROM compacted_transcript_entries)"
    )
    await manager._storage.conn.commit()
    with pytest.raises(CompactionCheckpointUnavailableError, match="coverage is incomplete"):
        await context(manager, node)


@pytest.mark.asyncio
async def test_canonical_recovery_detects_reset_during_read(recovered_session, monkeypatch):
    manager, node, _ = recovered_session
    original_reader = manager._storage.get_canonical_transcript

    async def reset_during_read(session_id, *args, **kwargs):
        entries = await original_reader(session_id, *args, **kwargs)
        await manager._storage.conn.execute(
            "UPDATE sessions SET epoch = epoch + 1 WHERE session_key = ?", (node.session_key,),
        )
        await manager._storage.conn.commit()
        return entries

    monkeypatch.setattr(manager._storage, "get_canonical_transcript", reset_during_read)
    with pytest.raises(StaleEpochError):
        await context(manager, node)


@pytest.mark.asyncio
async def test_recovery_digest_rechecked_inside_writer_transaction(recovered_session, monkeypatch):
    manager, node, original = recovered_session
    source = await manager.capture_compaction_source(node.session_key)
    payload = StructuredCompactionSummary(
        current_status="TASK-42 remains; do not deploy.",
        source_coverage={"replaces_prior_context": True},
    ).model_dump(mode="json")
    rewrite = manager._storage.rewrite_compacted_session

    async def change_archive_then_rewrite(*args, **kwargs):
        await manager._storage.conn.execute(
            "UPDATE compacted_transcript_entries SET content = 'Changed after admission' "
            "WHERE original_entry_id = ?", (original[0].id,),
        )
        await manager._storage.conn.commit()
        return await rewrite(*args, **kwargs)

    monkeypatch.setattr(manager._storage, "rewrite_compacted_session", change_archive_then_rewrite)
    result = await manager.persist_compaction_result(
        node.session_key, payload["current_status"],
        [{"role": "user", "content": original[-1].content}], removed_count=0,
        summary_payload=payload, summary_format="structured_v1",
        source_entries=source.entries, source_preimage=source.preimage,
        source_context_fingerprint=source.context_fingerprint,
    )
    assert not result.applied
    assert len(await manager.get_summaries(node.session_key)) == 1
    assert len(await manager.get_transcript(node.session_key)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", [False, True])
async def test_canonical_request_window_reaches_provider_without_durable_write(
    recovered_session, oversized,
):
    manager, node, original = recovered_session
    if oversized:
        await manager._storage.conn.execute(
            "UPDATE compacted_transcript_entries SET content = ? WHERE original_entry_id = ?",
            ("unneeded historical detail " * 30000, original[1].id),
        )
        await manager._storage.conn.commit()
    before = await manager.get_canonical_transcript(node.session_key)
    runner = TurnRunner(provider_selector=None, session_manager=manager)
    window = await runner._prepare_request_window(
        node.session_key, await manager.get_transcript(node.session_key), 30000,
        compaction_id="canonical-window", phase="preflight", reason="summary_failed",
        protected_recent_messages=1, expected_session_id=node.session_id,
        expected_session_epoch=node.epoch,
    )
    assert window is not None and window.canonical_boundary == original[1].id
    assert "Fix TASK-42" in str(window.kept_entries)
    assert "packaging only" in str(window.kept_entries)
    if not oversized:
        assert len(window.kept_entries) == 3
        assert "Temporary canonical recovery" in window.summary
    provider = _FinalProofBudgetProvider()
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=32768, max_tokens=1024, max_provider_retries=0,
    ))
    visible = await runner._load_history(
        agent, node.session_key, trim_last_user=False, prepared_window=window,
        expected_session_id=node.session_id, expected_session_epoch=node.epoch,
    )
    agent.config.compaction_summary_context = visible
    events = [event async for event in agent.run_turn("Continue the packaging task.")]
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(provider.calls) == 1
    assert "Fix TASK-42. Do not deploy." in str(provider.calls[0])
    assert "packaging only" in str(provider.calls[0])
    assert "Obsolete rewrite" not in str(provider.calls[0])
    assert await manager.get_canonical_transcript(node.session_key) == before
    assert len(await manager.get_summaries(node.session_key)) == 1


@pytest.mark.asyncio
async def test_canonical_window_revalidates_archived_content(recovered_session):
    manager, node, original = recovered_session
    runner = TurnRunner(provider_selector=None, session_manager=manager)
    window = await runner._prepare_request_window(
        node.session_key, await manager.get_transcript(node.session_key), 30000,
        compaction_id="canonical-window", phase="preflight", reason="summary_failed",
        protected_recent_messages=1, expected_session_id=node.session_id,
        expected_session_epoch=node.epoch,
    )
    await manager._storage.conn.execute(
        "UPDATE compacted_transcript_entries SET content = ? WHERE original_entry_id = ?",
        ("Fix TASK-43. Do not deploy.", original[0].id),
    )
    await manager._storage.conn.commit()
    agent = Agent(provider=_FinalProofBudgetProvider(), config=AgentConfig(
        context_window_tokens=32768, max_tokens=1024,
    ))
    await runner._load_history(
        agent, node.session_key, trim_last_user=False, prepared_window=window,
        expected_session_id=node.session_id, expected_session_epoch=node.epoch,
    )
    assert "Fix TASK-43" in str(agent.history_snapshot())
    assert "Fix TASK-42" not in str(agent.history_snapshot())


@pytest.mark.asyncio
async def test_missing_state_and_empty_backup_still_recovers_archive(recovered_session):
    manager, node, _ = recovered_session
    await manager._storage.conn.execute("DELETE FROM session_context_states")
    await manager._storage.conn.commit()
    records, fingerprint = await context(manager, node)
    assert "Fix TASK-42" in records[0].text
    assert fingerprint.startswith("canonical:")


@pytest.mark.asyncio
async def test_canonical_recovery_rejects_unproven_boundary(recovered_session):
    manager, node, _ = recovered_session
    await manager._storage.conn.execute(
        "UPDATE session_context_states SET covered_through_id = 999"
    )
    await manager._storage.conn.execute(
        "UPDATE session_summaries SET covered_through_id = 999"
    )
    await manager._storage.conn.commit()
    with pytest.raises(CompactionCheckpointUnavailableError, match="source boundary is invalid"):
        await context(manager, node)


@pytest.mark.asyncio
async def test_same_generation_backup_does_not_read_archive(recovered_session, monkeypatch):
    manager, node, _ = recovered_session
    await manager._storage.conn.execute(
        "UPDATE session_summaries SET summary_text = 'TASK-42 intact backup'"
    )
    await manager._storage.conn.commit()

    async def forbidden_archive_read(*args, **kwargs):
        raise AssertionError("healthy checkpoint does not need canonical recovery")

    monkeypatch.setattr(manager._storage, "get_canonical_transcript", forbidden_archive_read)
    monkeypatch.setattr(
        manager._storage, "get_canonical_transcript_coverage", forbidden_archive_read,
    )
    records, fingerprint = await context(manager, node)
    assert records[0].source == "summary" and records[0].text == "TASK-42 intact backup"
    assert not fingerprint.startswith("canonical:")


@pytest.mark.asyncio
async def test_missing_summary_row_cannot_claim_complete_canonical_coverage(recovered_session):
    manager, node, _ = recovered_session
    await manager._storage.conn.execute("DELETE FROM session_summaries")
    await manager._storage.conn.commit()
    with pytest.raises(CompactionCheckpointUnavailableError, match="coverage is incomplete"):
        await context(manager, node)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {"current_status": ["invalid"]}])
async def test_unparseable_whole_payload_recovers_canonical_source(recovered_session, payload):
    manager, node, _ = recovered_session
    await manager._storage.conn.execute(
        "UPDATE session_context_states SET payload = ?", (json.dumps(payload),),
    )
    await manager._storage.conn.commit()
    records, fingerprint = await context(manager, node)
    assert "Fix TASK-42" in records[0].text
    assert "Obsolete rewrite" not in records[0].text
    assert fingerprint.startswith("canonical:")


@pytest.mark.asyncio
async def test_large_canonical_recovery_uses_actual_summary_builder_and_checkpoint_only_commit(
    recovered_session, monkeypatch,
):
    from opensquilla.session.compaction import CompactionConfig
    from opensquilla.session.compaction_deployment import CompactionExecutionPlan
    from tests.test_session.test_compaction_previous_projection import deployment, install_stream

    manager, node, original = recovered_session
    await manager._storage.conn.execute(
        "UPDATE compacted_transcript_entries SET content = ? WHERE original_entry_id = ?",
        ("historical intermediate detail 甲乙丙丁戊己 " * 20000
         + "\nTests passed. Deployment was not run.", original[1].id),
    )
    await manager._storage.conn.commit()
    before = await manager.get_canonical_transcript(node.session_key)
    _provider, target, request_context = deployment(32768)
    payloads = install_stream(
        monkeypatch, text="TASK-42 tests passed. Package only. Do not deploy.",
    )
    result = await manager.compact_with_result(
        node.session_key, context_window_tokens=32768, trigger_reason="manual",
        config=CompactionConfig(
            llm_plan=CompactionExecutionPlan(candidates=(target,)),
            request_context=request_context, protected_recent_messages=1,
        ),
        consumer_admission=lambda summary, kept: (
            "TASK-42" in summary and "Do not deploy" in summary
            and [entry["content"] for entry in kept] == [original[-1].content]
        ),
    )
    assert result.replaced_previous_summary and result.removed_count == 0
    assert result.skip_reason is None
    assert len(payloads) == 1
    wire = json.dumps(payloads[0], ensure_ascii=False)
    assert "<previous-summary>" in wire and "previous-summary omitted" in wire
    assert "Fix TASK-42" in wire and "Deployment was not run" in wire
    assert len(wire) < 32768 * 4
    assert await manager.get_canonical_transcript(node.session_key) == before
    records, _fingerprint = await context(manager, node)
    assert records[0].source == "context_state" and "Do not deploy" in records[0].text
    assert len(await manager.get_summaries(node.session_key)) == 2
