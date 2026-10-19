"""Only proven pretransaction refusals may continue without installing a checkpoint."""
from __future__ import annotations

import asyncio
import inspect
import sqlite3
from dataclasses import replace

import pytest

from opensquilla.engine.types import CompactionEvent, DoneEvent, TextDeltaEvent
from opensquilla.session.compaction import CompactionConfig, CompactionResult
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage, StorageBusyError
from tests.test_engine.test_compaction_rolling_source import _agent, _handler, _payload, _seed
from tests.test_engine.turn_runner.test_stream_consumer_stage_unit import _make_input, _make_stage


def _candidate(request):
    return CompactionResult(
        summary="S1", summary_payload=_payload("S1"), summary_format="structured_v1",
        kept_entries=request.entries[2:], kept_start_index=2, removed_count=2,
        chunks_processed=1, replaced_previous_summary=True, tokens_before=24000,
        tokens_after=4000,
    )


async def _write(manager, node, source, writer):
    if writer == "active":
        return await manager.compact_with_result(
            node.session_key, 32768, CompactionConfig(), trigger_reason="manual",
            expected_session_id=node.session_id, expected_session_epoch=node.epoch,
        )
    return await manager.persist_compaction_result(
        node.session_key, "S1", [entry.model_dump() for entry in source.entries[2:]],
        summary_payload=_payload("S1"), summary_format="structured_v1",
        source_snapshot=source, removed_count=2, expected_session_id=node.session_id,
        expected_session_epoch=node.epoch,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pretransaction_read", "begin_lock"])
@pytest.mark.parametrize("writer", ["active", "inline_before_send", "inline_inflight"])
async def test_writer_refusal_continues_without_durable_loss_and_next_turn_commits(
    tmp_path, monkeypatch, failure, writer,
):
    path = tmp_path / "sessions.db"
    storage = SessionStorage(path)
    await storage.connect()
    blocker = None
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        node, raw, source = await _seed(manager)
        await manager.append_message(node.session_key, "user", "FUTURE_QUEUED_USER")
        before = await manager.get_canonical_transcript(node.session_key)
        summaries = await manager.get_summaries(node.session_key)
        states = await manager.get_context_states(node.session_key)
        count = (await manager.get_session(node.session_key)).compaction_count
        generated = []

        async def summarize(request):
            generated.append(request)
            return _candidate(request)

        monkeypatch.setattr("opensquilla.session.manager.compact_context", summarize)
        started_transactions = []
        original_begin = storage._begin_immediate

        async def begin(*args, **kwargs):
            result = await original_begin(*args, **kwargs)
            if args[1] == "rewrite_compacted_session":
                started_transactions.append(args[1])
            return result

        monkeypatch.setattr(storage, "_begin_immediate", begin)
        if failure == "pretransaction_read":
            original_read = storage._read_transcript_rows

            async def read(*args, **kwargs):
                # Fail only the writer's inner read, after all manager preflight reads.
                if inspect.currentframe().f_back.f_code.co_name == "rewrite_compacted_session":
                    monkeypatch.setattr(storage, "_read_transcript_rows", original_read)
                    exc = sqlite3.OperationalError("database is locked")
                    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise exc
                return await original_read(*args, **kwargs)

            monkeypatch.setattr(storage, "_read_transcript_rows", read)
        else:
            # The real external writer lock uses the unchanged production retry budget.
            assert storage._busy_budget_seconds == 2.0
            blocker = sqlite3.connect(str(path), timeout=0)
            blocker.execute("BEGIN IMMEDIATE")

        if writer == "active":
            result = await _write(manager, node, source, writer)
            assert len(generated) == 1
            assert result.skip_reason == "precommit_write_busy"
            assert result.failure_kind == "storage_error"
            assert not result.summary and result.summary_payload is None
            assert result.removed_count == result.kept_start_index == result.chunks_processed == 0
            assert not result.replaced_previous_summary
            assert result.tokens_after == result.tokens_before == 24000
            assert len(result.kept_entries) == len(before)
        else:
            agent = _agent()
            agent.bind_compaction_source(source)
            handler, adapter = _handler(manager)
            event = CompactionEvent(
                summary="S1", summary_payload=_payload("S1"), summary_format="structured_v1",
                kept_entries=[entry.model_dump() for entry in raw[2:]], removed_count=2,
                source_snapshot=source,
            )
            trace = []

            class Stream:
                async def run_turn(self, *_args, **_kwargs):
                    if writer == "inline_inflight":
                        trace.append("upstream_first_chunk_received")
                    yield event
                    if writer == "inline_before_send":
                        trace.append("provider_send")
                    trace.append("continued")
                    yield TextDeltaEvent(text="CONTINUED_ONCE")
                    yield DoneEvent(text="CONTINUED_ONCE")

            stage, _ = _make_stage(agent_run=Stream(), compaction_persist=adapter)
            stage._compaction_handler = handler
            inp = replace(
                _make_input(private_memory_allowed=False), agent=agent,
                session_key=node.session_key, agent_id="main",
                expected_session_id=node.session_id, expected_session_epoch=node.epoch,
                compaction_source_entries=source.entries,
                compaction_source_preimage=source.preimage,
                compaction_source_context_fingerprint=source.context_fingerprint,
                compaction_source_boundary_message_id=source.boundary_message_id,
                compaction_source_boundary_entry_id=source.boundary_entry_id,
            )
            inp.state.compaction_source_snapshot = source
            yielded = [item async for item in stage.run(inp)]
            assert event.persist_result.reason == "precommit_write_busy"
            assert not event.persist_result.applied
            assert agent._durable_compaction_source is None
            assert inp.state.compaction_source_snapshot is None
            assert trace == [
                "upstream_first_chunk_received" if writer == "inline_inflight" else "provider_send",
                "continued",
            ]
            assert [e.text for e in yielded if isinstance(e, TextDeltaEvent)] == ["CONTINUED_ONCE"]
            assert sum(isinstance(e, DoneEvent) for e in yielded) == 1

        if blocker is not None:
            blocker.rollback()
            blocker.close()
            blocker = None
        assert not started_transactions
        assert not storage.conn.in_transaction and not storage._operation_lock.locked()
        assert await manager.get_canonical_transcript(node.session_key) == before
        assert await manager.get_summaries(node.session_key) == summaries
        assert await manager.get_context_states(node.session_key) == states
        assert (await manager.get_session(node.session_key)).compaction_count == count

        # A transient writer refusal must not prevent ordinary persistence or fresh capture.
        await manager.append_message(node.session_key, "assistant", "FOLLOWUP_WRITE_OK")
        await storage.close()
        storage = SessionStorage(path)
        await storage.connect()
        manager = SessionManager(storage, inject_time_prefix=False)
        recovered = await manager.get_canonical_transcript(node.session_key)
        assert recovered[:len(before)] == before
        assert recovered[-1].content == "FOLLOWUP_WRITE_OK"
        assert await manager.get_summaries(node.session_key) == summaries
        fresh_source = await manager.capture_compaction_source(
            node.session_key, boundary_message_id=source.boundary_message_id,
        )
        assert "CHECKPOINT_ZERO" in fresh_source.previous_summary
        assert "S1" not in fresh_source.previous_summary
        applied = await _write(manager, node, fresh_source, writer)
        if writer == "active":
            assert applied.removed_count == 2
        else:
            assert applied.applied
        assert len(await manager.get_summaries(node.session_key)) == len(summaries) + 1
        assert await manager.get_canonical_transcript(node.session_key) == recovered
        assert any(
            e.content == "FUTURE_QUEUED_USER"
            for e in await manager.get_transcript(node.session_key)
        )
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["active", "inline"])
@pytest.mark.parametrize("operation,stage", [
    ("rewrite_compacted_session", "commit"), ("unrelated_writer", "begin"),
])
async def test_unproven_writer_busy_remains_terminal(monkeypatch, writer, operation, stage):
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        node, _, source = await _seed(manager)

        async def summarize(request):
            return _candidate(request)

        async def write(**_kwargs):
            raise StorageBusyError(operation, stage=stage, waited_ms=2000, retry_after_ms=100)

        monkeypatch.setattr("opensquilla.session.manager.compact_context", summarize)
        monkeypatch.setattr(storage, "rewrite_compacted_session", write)
        with pytest.raises(StorageBusyError) as caught:
            await _write(manager, node, source, writer)
        assert caught.value.operation == operation and caught.value.stage == stage
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["active", "inline"])
async def test_cancellation_wins_over_settled_known_unapplied_writer(monkeypatch, writer):
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        node, _, source = await _seed(manager)
        summaries = await manager.get_summaries(node.session_key)
        entered, release = asyncio.Event(), asyncio.Event()

        async def summarize(request):
            return _candidate(request)

        async def write(**_kwargs):
            entered.set()
            await release.wait()
            raise StorageBusyError(
                "rewrite_compacted_session", stage="begin", waited_ms=2000, retry_after_ms=100,
            )

        monkeypatch.setattr("opensquilla.session.manager.compact_context", summarize)
        monkeypatch.setattr(storage, "rewrite_compacted_session", write)
        task = asyncio.create_task(_write(manager, node, source, writer))
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await manager.get_summaries(node.session_key) == summaries
    finally:
        await storage.close()
