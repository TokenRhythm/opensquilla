"""A blocked counter database must not delay conversation storage or collection."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.compat import aiosqlite
from opensquilla.engine.types import DoneEvent
from opensquilla.observability import install_telemetry, usage_telemetry
from opensquilla.observability.daily_usage_store import DailyUsageStore
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage


@pytest.mark.parametrize("fallback", [False, True], ids=["aiosqlite", "sqlite3-fallback"])
async def test_locked_sidecar_does_not_block_session_database_or_turn_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool,
) -> None:
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", fallback)
    monkeypatch.setattr(install_telemetry, "_telemetry_skip_reason", lambda **_kw: None)
    config = SimpleNamespace(state_dir=str(tmp_path / "state"))
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    runtime = usage_telemetry.StandaloneUsageTelemetry(
        config=config,
        legacy_storage=storage,
        flush_interval_seconds=3600,
        upload_on_close=False,
    )
    path = usage_telemetry.standalone_daily_usage_path(config)
    runtime._storage = await DailyUsageStore.open(path)
    key = "agent:main:telemetry-isolation"
    await storage.upsert_session(
        SessionNode(session_key=key, session_id="sid", agent_id="main"),
    )
    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    attempted = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        if Path(args[0]) == path:
            def trace(sql: str) -> None:
                if sql == "BEGIN IMMEDIATE":
                    loop.call_soon_threadsafe(attempted.set)

            connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    turn = DoneEvent(input_tokens=10, output_tokens=3, cached_tokens=4, cache_write_tokens=2)
    flush = None
    try:
        await runtime.record_turn(run_kind="default", done_event=turn)
        flush = asyncio.create_task(runtime.flush())
        await asyncio.wait_for(attempted.wait(), timeout=2)
        # SQLite is actively waiting on the real sidecar writer lock. Complete
        # normal transcript writes/reads and collection before releasing it.
        async with asyncio.timeout(2):
            for _ in range(1000):
                await runtime.record_turn(run_kind="default", done_event=turn)
            await storage.append_transcript_entry(
                TranscriptEntry(
                    session_key=key,
                    session_id="sid",
                    message_id="mid",
                    role="assistant",
                    content="Synthetic completed answer.",
                    created_at=100,
                ),
                expected_epoch=0,
            )
            transcript = await storage.get_transcript("sid")
            assert len(transcript) == 1
            assert transcript[0].content == "Synthetic completed answer."
            assert await storage.list_pending_daily_usage(before_day="9999-12-31") == []
        assert not flush.done()

        holder.rollback()
        await flush
        await runtime.flush()
        rows = await runtime._storage.list_pending_daily_usage(before_day="9999-12-31")
        assert len(rows) == 1
        assert rows[0]["conversation_turns"] == 1001
        assert rows[0]["input_tokens"] == 10010
        assert rows[0]["output_tokens"] == 3003
        assert rows[0]["cached_tokens"] == 4004
        assert rows[0]["cache_write_tokens"] == 2002
    finally:
        holder.close()
        if flush is not None:
            await asyncio.gather(flush, return_exceptions=True)
        await runtime.close()
        await storage.close()
