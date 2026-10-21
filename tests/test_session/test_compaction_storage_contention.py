"""Compaction preparation must leave the writer available without weakening CAS."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from opensquilla.observability.log_privacy import PrivateLogFormatter
from opensquilla.session import storage as storage_module
from opensquilla.session.models import AgentTaskRecord, SessionNode, SessionSummary, TranscriptEntry
from opensquilla.session.storage import SessionStorage, _transcript_preimage


async def seed(storage: SessionStorage):
    node = SessionNode(session_key="agent:main:webchat:synthetic-storage", session_id="synthetic")
    await storage.upsert_session(node)
    for index in range(4):
        await storage.append_transcript_entry(TranscriptEntry(
            session_id=node.session_id, session_key=node.session_key,
            message_id=f"synthetic-{index}", role="user", content="sample " * 2000,
            created_at=100 + index, turn_context={"revision": 1},
        ), expected_epoch=0)
    return node, await storage.get_transcript(node.session_id)


@pytest.mark.parametrize("memory", [False, True])
async def test_canonical_decode_releases_all_storage_gates(tmp_path, monkeypatch, memory):
    storage = await SessionStorage.open(":memory:" if memory else str(tmp_path / "sessions.db"))
    entered, release = threading.Event(), threading.Event()
    read = None
    try:
        node, original = await seed(storage)
        decode = storage_module._decode_transcript_rows

        def paused_decode(rows):
            entered.set()
            assert release.wait(5)
            return decode(rows)

        monkeypatch.setattr(storage_module, "_decode_transcript_rows", paused_decode)
        read = asyncio.create_task(storage.get_canonical_transcript(node.session_id))
        assert await asyncio.to_thread(entered.wait, 5)
        await asyncio.wait_for(storage.create_agent_task(AgentTaskRecord(
            task_id="unrelated-task", session_key=node.session_key,
        )), timeout=2)
        assert not read.done()
        release.set()
        assert await read == original
    finally:
        release.set()
        if read:
            await asyncio.gather(read, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("change", ["content", "turn_context", "append"])
async def test_rewrite_rechecks_raw_source_after_preparation(tmp_path, monkeypatch, change):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    entered, release = threading.Event(), threading.Event()
    rewrite = None
    try:
        node, entries = await seed(storage)
        check = storage_module._compaction_source_matches

        def paused_check(*args):
            entered.set()
            assert release.wait(5)
            return check(*args)

        monkeypatch.setattr(storage_module, "_compaction_source_matches", paused_check)
        node.compaction_count = 1
        rewrite = asyncio.create_task(storage.rewrite_compacted_session(
            node=node,
            summary=SessionSummary(session_id=node.session_id, session_key=node.session_key,
                                   summary_text="synthetic checkpoint"),
            entries=entries[2:], archived_entries=entries[:2],
            expected_source_entries=entries,
            expected_source_preimage=_transcript_preimage(entries),
            expected_session_id=node.session_id, expected_session_epoch=0,
        ))
        assert await asyncio.to_thread(entered.wait, 5)
        if change == "append":
            await asyncio.wait_for(storage.append_transcript_entry(TranscriptEntry(
                session_id=node.session_id, session_key=node.session_key,
                message_id="later-suffix", role="user", content="later", created_at=200,
            ), expected_epoch=0), timeout=2)
        else:
            async with storage._write_transaction("synthetic_edit") as conn:
                value = "edited" if change == "content" else '{"revision": 2}'
                await conn.execute(
                    f"UPDATE transcript_entries SET {change} = ? WHERE id = ?",
                    (value, entries[0].id),
                )
        release.set()
        installed = await rewrite
        canonical = await storage.get_canonical_transcript(node.session_id)
        if change == "append":
            assert installed
            assert canonical[:4] == entries
            assert canonical[-1].message_id == "later-suffix"
            active = await storage.get_transcript(node.session_id)
            assert [item.id for item in active[:2]] == [item.id for item in entries[2:]]
        else:
            assert not installed
            assert len(canonical) == 4
            assert await storage.get_all_summaries(node.session_id) == []
    finally:
        release.set()
        if rewrite:
            await asyncio.gather(rewrite, return_exceptions=True)
        await storage.close()


@pytest.mark.parametrize("projection", ["canonical", "page", "title", "preview", "coverage"])
async def test_canonical_reader_observes_atomic_archive_snapshot(tmp_path, projection):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        node, entries = await seed(storage)
        async def read():
            if projection == "page":
                return await storage.get_canonical_transcript_page(node.session_id, limit=10)
            if projection == "title":
                return await storage.list_user_transcript_content_batch([node.session_id])
            if projection == "preview":
                return await storage.list_last_transcript_content_batch([node.session_id])
            if projection == "coverage":
                return await storage.get_canonical_transcript_coverage(node.session_id)
            return await storage.get_canonical_transcript(node.session_id)

        before = await read()
        async with storage._write_transaction("synthetic_uncommitted_delete") as conn:
            await conn.execute(
                "DELETE FROM transcript_entries WHERE session_id = ?", (node.session_id,),
            )
            assert await asyncio.wait_for(read(), timeout=2) == before
        assert await storage.get_canonical_transcript(node.session_id) == []
    finally:
        await storage.close()


@pytest.mark.parametrize("projection", ["title", "preview"])
@pytest.mark.parametrize("backend", ["native", "sqlite3"])
async def test_directory_projection_work_is_bounded_by_requested_rows(
    tmp_path, monkeypatch, projection, backend,
):
    connect = (
        storage_module.aiosqlite._native_aiosqlite.connect
        if backend == "native" else storage_module.aiosqlite._connect_sqlite3
    )
    monkeypatch.setattr(storage_module.aiosqlite, "connect", connect)
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        node, _ = await seed(storage)
        steps = 0

        async def count_reader(reader):
            set_progress_handler = reader.set_progress_handler

            async def install_progress_handler(callback, interval):
                if callback is None:
                    return await set_progress_handler(None, 0)
                ticks = 0

                def progress():
                    nonlocal steps, ticks
                    steps += 1
                    ticks += 1
                    # Keep the real lease's deadline/cancellation callback.
                    return callback() if ticks % interval == 0 else 0

                await set_progress_handler(progress, 1)

            monkeypatch.setattr(reader, "set_progress_handler", install_progress_handler)
            return reader

        open_reader = storage._open_recovery_reader

        async def open_counted_reader():
            return await count_reader(await open_reader())

        monkeypatch.setattr(storage, "_open_recovery_reader", open_counted_reader)

        async def measure():
            nonlocal steps
            steps = 0

            def progress():
                nonlocal steps
                steps += 1
                return 0

            await storage._transcript_reader.set_progress_handler(progress, 1)
            try:
                if projection == "title":
                    result = await storage.list_user_transcript_content_batch(
                        [node.session_id, node.session_id, "missing"], limit_per_session=3,
                    )
                    assert len(result[node.session_id]) == 3
                    assert result["missing"] == []
                else:
                    result = await storage.list_last_transcript_content_batch(
                        [node.session_id, "missing"], max_chars=10,
                    )
                    assert len(result[node.session_id]) == 10
                    assert result["missing"] == ""
            finally:
                await storage._transcript_reader.set_progress_handler(None, 0)
            return steps

        small = await measure()
        assert small > 0
        async with storage._write_transaction("synthetic_history") as conn:
            await conn.executemany(
                "INSERT INTO transcript_entries "
                "(session_id, session_key, message_id, role, content, created_at) "
                "VALUES (?, ?, ?, 'user', ?, ?)",
                [(node.session_id, node.session_key, f"bulk-{i}", "synthetic " * 100, 200 + i)
                 for i in range(4000)],
            )
        large = await measure()
        # SQLite VM work must not grow with the unrequested body of the history.
        assert large <= small * 3 + 1000
    finally:
        await storage.close()


def test_lock_diagnostics_survive_private_log_formatting(caplog):
    storage = SessionStorage(":memory:")
    storage._operation_holder = ("synthetic_read", storage._monotonic() - 1)
    with caplog.at_level(logging.WARNING):
        error = storage._operation_busy_error("synthetic_write", storage._monotonic() - 2)
    formatted = PrivateLogFormatter().format(caplog.records[-1])
    assert '"event": "session_storage.operation_busy"' in formatted
    assert '"operation": "synthetic_read"' in formatted
    assert error.holder_operation == "synthetic_read"


async def test_wal_metadata_reads_committed_task_while_writer_is_active(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.create_agent_task(AgentTaskRecord(task_id="task", session_key="synthetic"))
        async with storage._write_transaction("synthetic_uncommitted_status") as conn:
            await conn.execute("UPDATE agent_tasks SET status = 'running' WHERE task_id = 'task'")
            task = await asyncio.wait_for(storage.get_agent_task("task"), 1)
            assert task.status == "queued"
        assert (await storage.get_agent_task("task")).status == "running"
    finally:
        await storage.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_wal_read_does_not_hold_writer_and_close_drains_cursor(tmp_path, monkeypatch, cancel):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    entered, release = asyncio.Event(), asyncio.Event()
    reader = storage._transcript_reader
    original_execute = reader.execute

    class PausedCursor:
        async def __aenter__(self):
            self.delegate = original_execute("SELECT * FROM agent_tasks WHERE task_id = 'task'")
            self.cursor = await self.delegate.__aenter__()
            return self

        async def fetchone(self):
            entered.set()
            await release.wait()
            return await self.cursor.fetchone()

        async def __aexit__(self, *args):
            return await self.delegate.__aexit__(*args)

    def execute(sql, params=()):
        if "FROM agent_tasks WHERE task_id" in sql:
            return PausedCursor()
        return original_execute(sql, params)

    await storage.create_agent_task(AgentTaskRecord(task_id="task", session_key="synthetic"))
    monkeypatch.setattr(reader, "execute", execute)
    read = asyncio.create_task(storage.get_agent_task("task"))
    close = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(storage.create_agent_task(
            AgentTaskRecord(task_id="other", session_key="synthetic-other"),
        ), 1)
        assert await asyncio.wait_for(storage.get_session("absent"), 1) is None
        if cancel:
            read.cancel()
        close = asyncio.create_task(storage.close())
        await asyncio.sleep(0)
        assert not close.done()
    finally:
        release.set()
        result, = await asyncio.gather(read, return_exceptions=True)
        if close is not None:
            await close
        else:
            await storage.close()
    if cancel:
        assert isinstance(result, asyncio.CancelledError)
    else:
        assert result.task_id == "task"
