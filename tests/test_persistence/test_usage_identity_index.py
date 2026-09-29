"""Historical-index failure must not leave live reservations doing table scans."""

import pytest

import opensquilla.session.storage as storage_module
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.session.usage_ledger import UsageEventStart


async def test_live_identity_index_survives_historical_index_failure(tmp_path, monkeypatch):
    path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(path))
    try:
        await storage.upsert_session(SessionNode(
            session_key="agent:main:webchat:live", session_id="live", agent_id="main",
        ))
        with monkeypatch.context() as patch:
            patch.setattr(storage_module, "_CREATE_IDX_TRANSCRIPT_USAGE_BACKFILL",
                          "CREATE INDEX invalid_history_index ON missing_history_table(id)")
            with pytest.raises(storage_module.aiosqlite.OperationalError):
                await storage.prepare_usage_backfill_indexes()
        assert not storage._usage_backfill_indexes_ready
    finally:
        await storage.close()

    # The completed index survives both the later failure and process reopening.
    storage = await SessionStorage.open(str(path))
    try:
        async with storage.conn.execute(
            "EXPLAIN QUERY PLAN SELECT agent_id, epoch FROM sessions "
            "WHERE session_id = ? ORDER BY session_key LIMIT 1", ("live",),
        ) as cursor:
            plan = " ".join(str(row[3]) for row in await cursor.fetchall())
        assert "idx_sessions_id_key" in plan
        assert "TEMP B-TREE" not in plan
        event = UsageEventStart(event_id="event", execution_id="execution",
                                call_index=0, session_id="live", started_at_ms=1)
        first = await storage.start_usage_event(event)
        assert await storage.start_usage_event(event) == first
        await storage.prepare_usage_backfill_indexes()
        assert storage._usage_backfill_indexes_ready
        async with storage.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'",
        ) as cursor:
            names = {row[0] for row in await cursor.fetchall()}
        assert {"idx_transcript_usage_backfill", "idx_compacted_usage_backfill"} <= names
    finally:
        await storage.close()
