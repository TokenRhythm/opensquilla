"""Canonical transcript paging and crash-consistent usage backfill tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from opensquilla.gateway.usage_backfill import (
    UsageBackfillAnomalyError,
    normalize_usage_backfill_entry,
    run_usage_backfill,
)
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage
from opensquilla.session.usage_ledger import (
    UsageBackfillCursor,
    UsageBackfillWrite,
    UsageEventCompletion,
    UsageEventItem,
    UsageEventStart,
    UsageLedgerConflictError,
)


def _write(
    event_id: str,
    execution_id: str,
    *,
    cost_nanos: int = 10,
) -> UsageBackfillWrite:
    return UsageBackfillWrite(
        start=UsageEventStart(
            event_id=event_id,
            execution_id=execution_id,
            call_index=0,
            session_id="session-1",
            agent_id="main",
            started_at_ms=100,
            turn_id=f"turn-{event_id}",
            origin="backfilled_turn",
        ),
        completion=UsageEventCompletion(
            completed_at_ms=101,
            input_tokens=1,
            total_tokens=1,
            cost_nanos=cost_nanos,
            estimated_cost_nanos=cost_nanos,
            cost_source="estimate",
        ),
    )


async def test_backfill_batch_is_canonical_stable_and_marks_forks(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:one",
                session_id="session-1",
            )
        )
        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:fork",
                session_id="session-fork",
                forked_from_parent=True,
            )
        )
        for entry in (
            TranscriptEntry(
                session_id="session-1",
                session_key="agent:main:webchat:one",
                message_id="message-1",
                role="assistant",
                created_at=100,
                turn_usage={"cost_usd": 0.1},
                turn_context={"turn_id": "turn-1"},
            ),
            TranscriptEntry(
                session_id="session-1",
                session_key="agent:main:webchat:one",
                message_id="message-2",
                role="assistant",
                created_at=200,
                turn_usage={"cost_usd": 0.2},
                turn_context={"turn_id": "turn-2"},
            ),
            TranscriptEntry(
                session_id="session-fork",
                session_key="agent:main:webchat:fork",
                message_id="message-fork",
                role="assistant",
                created_at=300,
                turn_usage={"cost_usd": 0.3},
            ),
        ):
            await storage.append_transcript_entry(entry)

        # A stale compacted copy of message-1 must not appear a second time.
        await storage.conn.execute(
            """
            INSERT INTO compacted_transcript_entries (
                session_id, session_key, original_entry_id, message_id, role,
                turn_usage, turn_context, created_at, archived_at
            )
            SELECT session_id, session_key, id, message_id, role, turn_usage,
                   turn_context, created_at, 400
            FROM transcript_entries WHERE message_id = 'message-1'
            """
        )

        first = await storage.get_usage_backfill_batch(before_ms=1_000, limit=2)
        assert [entry.cursor.message_id for entry in first.entries] == [
            "message-1",
            "message-2",
        ]
        assert first.exhausted is False
        second = await storage.get_usage_backfill_batch(
            before_ms=1_000,
            after=first.next_cursor,
            limit=2,
        )
        assert [entry.cursor.message_id for entry in second.entries] == ["message-fork"]
        assert second.entries[0].forked_from_parent is True
        assert second.entries[0].turn_context is None
        assert second.exhausted is True
    finally:
        await storage.close()


async def test_apply_backfill_batch_is_atomic_and_replay_safe(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:one",
                session_id="session-1",
            )
        )
        await storage.initialize_usage_ledger(1_000)
        cursor = UsageBackfillCursor(101, "session-1", "message-1")
        write = _write("backfill-1", "execution-1")
        state = await storage.apply_usage_backfill_batch(
            (write,), cursor=cursor, exhausted=False, now_ms=1_100
        )
        assert state.backfill_status == "running"
        assert state.backfilled_event_count == 1
        assert state.backfilled_cost_nanos == 10

        replayed = await storage.apply_usage_backfill_batch(
            (write,), cursor=cursor, exhausted=True, now_ms=1_200
        )
        assert replayed.backfill_status == "complete"
        assert replayed.backfilled_event_count == 1
        assert replayed.backfilled_cost_nanos == 10

        inherited_copy = replace(
            write,
            start=replace(write.start, session_id="session-fork"),
        )
        deduplicated = await storage.apply_usage_backfill_batch(
            (inherited_copy,), cursor=cursor, exhausted=True, now_ms=1_250
        )
        assert deduplicated.backfilled_event_count == 1
        assert deduplicated.backfilled_cost_nanos == 10

        next_cursor = UsageBackfillCursor(201, "session-1", "message-2")
        first = _write("backfill-2", "execution-2")
        conflicting = _write("backfill-conflict", "execution-2")
        with pytest.raises(UsageLedgerConflictError):
            await storage.apply_usage_backfill_batch(
                (first, conflicting),
                cursor=next_cursor,
                exhausted=False,
                now_ms=1_300,
            )
        events = await storage.query_usage_events(None, None)
        assert [event.event_id for event in events] == ["backfill-1"]
        state_after_failure = await storage.get_usage_ledger_state()
        assert state_after_failure is not None
        assert state_after_failure.cursor_message_id == "message-1"

        partial = await storage.apply_usage_backfill_batch(
            (),
            cursor=next_cursor,
            exhausted=True,
            anomaly_delta=1,
            now_ms=1_400,
        )
        assert partial.backfill_status == "partial"
        assert partial.anomaly_count == 1
    finally:
        await storage.close()


async def test_usage_backfill_worker_attributes_valid_history_without_changing_baseline(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:one",
                session_id="session-1",
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                total_cost_usd=0.1,
                estimated_cost_usd=0.1,
                estimated_cost_component_usd=0.1,
                cost_source="opensquilla_estimate",
            )
        )
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="session-1",
                session_key="agent:main:webchat:one",
                message_id="assistant-1",
                role="assistant",
                created_at=100,
                turn_usage={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd": 0.1,
                    "estimated_cost_component_usd": 0.1,
                    "cost_source": "opensquilla_estimate",
                    "provider": "test",
                    "model": "model-a",
                },
                turn_context={"turn_id": "turn-1"},
            )
        )
        baseline_before = await storage.initialize_usage_ledger(1_000)

        await run_usage_backfill(storage, batch_size=250)

        state = await storage.get_usage_ledger_state()
        assert state is not None
        assert state.ledger_started_at_ms == baseline_before.ledger_started_at_ms
        assert state.backfill_status == "complete"
        assert state.backfilled_event_count == 1
        assert state.backfilled_cost_nanos == 100_000_000
        events = await storage.query_usage_events(None, None)
        assert len(events) == 1
        assert events[0].origin == "backfilled_turn"
        assert events[0].completed_at_ms == 100
        baselines = await storage.list_usage_legacy_baselines()
        assert len(baselines) == 1
        assert baselines[0].cost_nanos == 100_000_000
    finally:
        await storage.close()


async def test_usage_backfill_worker_marks_unrecoverable_fork_history_partial(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:fork",
                session_id="session-fork",
                forked_from_parent=True,
                total_cost_usd=0.2,
                estimated_cost_usd=0.2,
                estimated_cost_component_usd=0.2,
                cost_source="opensquilla_estimate",
            )
        )
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="session-fork",
                session_key="agent:main:webchat:fork",
                message_id="copied-assistant",
                role="assistant",
                created_at=100,
                turn_usage={
                    "cost_usd": 0.2,
                    "estimated_cost_component_usd": 0.2,
                    "cost_source": "opensquilla_estimate",
                },
            )
        )
        await storage.initialize_usage_ledger(1_000)

        await run_usage_backfill(storage)

        state = await storage.get_usage_ledger_state()
        assert state is not None
        assert state.backfill_status == "partial"
        assert state.anomaly_count == 1
        assert await storage.query_usage_events(None, None) == []
        baselines = await storage.list_usage_legacy_baselines()
        assert baselines[0].cost_nanos == 200_000_000
    finally:
        await storage.close()


async def test_historical_model_breakdown_must_match_every_envelope_component(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:one",
                session_id="session-1",
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                cache_read=2,
                total_cost_usd=0.1,
                estimated_cost_component_usd=0.1,
                cost_source="opensquilla_estimate",
            )
        )
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="session-1",
                session_key="agent:main:webchat:one",
                message_id="assistant-mismatch",
                role="assistant",
                created_at=100,
                turn_context={"turn_id": "turn-mismatch"},
                turn_usage={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_tokens": 2,
                    "cost_usd": 0.1,
                    "estimated_cost_component_usd": 0.1,
                    "cost_source": "opensquilla_estimate",
                    "model_usage_breakdown": [
                        {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            # Cost matches, but cache does not. Accepting this
                            # would make model totals disagree with the envelope.
                            "cache_read_tokens": 1,
                            "cost_usd": 0.1,
                            "estimated_cost_component_usd": 0.1,
                            "cost_source": "opensquilla_estimate",
                            "provider": "test",
                            "model": "model-a",
                        }
                    ],
                },
            )
        )
        await storage.initialize_usage_ledger(1_000)
        batch = await storage.get_usage_backfill_batch(before_ms=1_000)
        with pytest.raises(UsageBackfillAnomalyError, match="cache_read_tokens"):
            normalize_usage_backfill_entry(batch.entries[0])

        await run_usage_backfill(storage)

        state = await storage.get_usage_ledger_state()
        assert state is not None
        assert state.backfill_status == "partial"
        assert state.anomaly_count == 1
        assert await storage.query_usage_events(None, None) == []

        invalid_write = _write("invalid-items", "invalid-items")
        invalid_write = replace(
            invalid_write,
            items=(
                UsageEventItem(
                    event_id="invalid-items",
                    ordinal=0,
                    input_tokens=1,
                    cache_read_tokens=1,
                    total_tokens=1,
                    cost_nanos=10,
                    estimated_cost_nanos=10,
                    cost_source="estimate",
                ),
            ),
        )
        updated = await storage.apply_usage_backfill_batch(
            (invalid_write,),
            cursor=UsageBackfillCursor(200, "session-1", "invalid-items"),
            exhausted=True,
        )
        assert updated.anomaly_count == 2
        assert await storage.query_usage_events(None, None) == []
    finally:
        await storage.close()


async def test_backfill_paging_uses_keyset_indexes_and_stays_stable(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        async with storage.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ) as indexes_before:
            index_names_before = {str(row[0]) for row in await indexes_before.fetchall()}
        assert {
            "idx_transcript_usage_backfill",
            "idx_compacted_usage_backfill",
            "idx_sessions_id_key",
        }.isdisjoint(index_names_before)

        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:one",
                session_id="session-1",
            )
        )
        await storage.conn.executemany(
            """
            INSERT INTO transcript_entries (
                session_id, session_key, message_id, role, turn_usage,
                turn_context, created_at
            ) VALUES ('session-1', 'agent:main:webchat:one', ?, 'assistant',
                      '{"cost_usd": 0}', '{}', ?)
            """,
            [(f"message-{index:04d}", index * 2) for index in range(200)],
        )
        await storage.conn.executemany(
            """
            INSERT INTO compacted_transcript_entries (
                session_id, session_key, message_id, role, turn_usage,
                turn_context, created_at, archived_at
            ) VALUES ('session-1', 'agent:main:webchat:one', ?, 'assistant',
                      '{"cost_usd": 0}', '{}', ?, 1)
            """,
            [(f"archived-{index:04d}", index * 2 + 1) for index in range(200)],
        )

        # File-backed preparation must not borrow the shared interactive
        # operation lock. Gateway RPC reads can continue while the post-ready
        # worker builds these derived indexes on its own connection.
        await storage._operation_lock.acquire()
        try:
            await asyncio.wait_for(storage.prepare_usage_backfill_indexes(), timeout=5)
        finally:
            storage._operation_lock.release()
        async with storage.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ) as indexes_after:
            index_names_after = {str(row[0]) for row in await indexes_after.fetchall()}
        assert {
            "idx_transcript_usage_backfill",
            "idx_compacted_usage_backfill",
            "idx_sessions_id_key",
        } <= index_names_after

        cursor = None
        seen: list[UsageBackfillCursor] = []
        while True:
            batch = await storage.get_usage_backfill_batch(
                before_ms=1_000,
                after=cursor,
                limit=37,
            )
            seen.extend(entry.cursor for entry in batch.entries)
            cursor = batch.next_cursor
            if batch.exhausted:
                break
        assert len(seen) == 400
        assert seen == sorted(seen)
        assert len(set(seen)) == 400

        async with storage.conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT session_id, message_id, created_at
            FROM transcript_entries
            WHERE role = 'assistant' AND turn_usage IS NOT NULL
              AND created_at < ?
              AND (created_at, session_id, message_id) > (?, ?, ?)
            ORDER BY created_at, session_id, message_id
            LIMIT ?
            """,
            (1_000, 100, "session-1", "message-0000", 38),
        ) as query_plan:
            details = " ".join(str(row[3]) for row in await query_plan.fetchall())
        assert "idx_transcript_usage_backfill" in details
        assert "TEMP B-TREE" not in details
    finally:
        await storage.close()
