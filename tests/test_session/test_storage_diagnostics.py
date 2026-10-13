"""Storage diagnostics must preserve privacy and transaction cleanup."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import pytest

from opensquilla.observability.log_privacy import PrivateLogFormatter
from opensquilla.session.models import AgentTaskRecord
from opensquilla.session.storage import (
    SessionStorage,
    StorageBusyError,
    StorageConnectionPoisonedError,
)


@pytest.mark.parametrize(
    ("reason", "journal_mode", "expected_reason", "expected_mode"),
    [
        ("memory_database", "memory", "memory_database", "memory"),
        ("private-reason", "private-journal-mode", "unknown", "unknown"),
    ],
)
def test_transcript_reader_fallback_keeps_only_known_classifications(
    caplog, reason, journal_mode, expected_reason, expected_mode,
):
    storage = SessionStorage(":memory:")
    with caplog.at_level(logging.WARNING):
        storage._warn_transcript_reader_fallback_once(reason, journal_mode)
        storage._warn_transcript_reader_fallback_once("open_failed", "delete")
    records = [
        record for record in caplog.records
        if getattr(record, "_opensquilla_log_metadata", {}).get("event")
        == "session_storage.transcript_reader_fallback"
    ]
    assert len(records) == 1
    formatted = PrivateLogFormatter().format(records[0])
    assert '"event": "session_storage.transcript_reader_fallback"' in formatted
    assert f'"reason_code": "{expected_reason}"' in formatted
    assert f'"journal_mode": "{expected_mode}"' in formatted
    assert "private-reason" not in formatted
    assert "private-journal-mode" not in formatted


@pytest.mark.parametrize("failure_type", [None, ValueError, asyncio.CancelledError])
async def test_shared_read_holder_is_reported_and_cleared_after_exit(tmp_path, failure_type):
    storage = await SessionStorage.open(str(tmp_path / "read-holder.db"))

    async def blocked_writer():
        async with storage._write_transaction("synthetic_write", budget_seconds=0):
            pytest.fail("writer entered while the read snapshot held the shared gate")

    async def read_snapshot():
        async with storage.read_transaction("synthetic_snapshot") as conn:
            assert conn is storage.conn
            with pytest.raises(StorageBusyError) as busy:
                await asyncio.create_task(blocked_writer())
            assert busy.value.stage == "lock_acquire"
            assert busy.value.holder_operation == "read.synthetic_snapshot"
            assert busy.value.hold_ms is not None
            if failure_type is not None:
                raise failure_type("synthetic snapshot interrupted")

    try:
        if failure_type is None:
            await read_snapshot()
        else:
            with pytest.raises(failure_type):
                await read_snapshot()
        assert storage._operation_holder is None
        assert storage.conn.in_transaction is False
        await storage.create_agent_task(AgentTaskRecord(
            task_id="after-read", session_key="synthetic",
        ))
        assert await storage.get_agent_task("after-read") is not None
    finally:
        await storage.close()


@pytest.mark.parametrize("failure_type", [ValueError, asyncio.CancelledError])
async def test_failed_rollback_logs_safely_and_retires_storage(caplog, failure_type):
    storage = SessionStorage(":memory:")

    class FailedRollback:
        in_transaction = True

        async def rollback(self):
            raise failure_type("private rollback detail")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(StorageConnectionPoisonedError) as failure:
            await storage._rollback_transaction(FailedRollback(), "synthetic_rollback")
    assert isinstance(failure.value.__cause__, failure_type)
    assert storage._poisoned is True
    with pytest.raises(StorageConnectionPoisonedError):
        storage._raise_if_poisoned()
    formatted = PrivateLogFormatter().format(caplog.records[-1])
    assert '"event": "session_storage.rollback_failed"' in formatted
    assert '"operation": "synthetic_rollback"' in formatted
    assert f'"exception_type": "{failure_type.__name__}"' in formatted
    assert '"phase": "rollback"' in formatted
    assert "private rollback detail" not in formatted


async def test_cancelled_commit_can_already_be_persisted(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OPENSQUILLA_STORAGE_DIAGNOSTICS", "1")
    caplog.set_level(logging.INFO, logger="opensquilla.session.storage")
    path = tmp_path / "committed-before-cancellation.db"
    storage = await SessionStorage.open(str(path))
    connection = storage.conn

    class CommitThenCancel:
        cancelled_once = False

        def __getattr__(self, name):
            return getattr(connection, name)

        async def commit(self):
            await connection.commit()
            if not self.cancelled_once:
                self.cancelled_once = True
                raise asyncio.CancelledError("synthetic cancellation after commit")

    try:
        await connection.execute("CREATE TABLE timing_fixture(value TEXT)")
        monkeypatch.setattr(storage, "_conn", CommitThenCancel())
        caplog.clear()
        with pytest.raises(asyncio.CancelledError):
            async with storage._write_transaction("committed_before_cancellation") as conn:
                await conn.execute("INSERT INTO timing_fixture VALUES ('committed')")

        [record] = [
            json.loads(PrivateLogFormatter().format(record).split(": ", 1)[1])
            for record in caplog.records
            if getattr(record, "_opensquilla_log_metadata", {}).get("event")
            == "session_storage.transaction_timing"
        ]
        assert record["status"] == "cancelled"
        assert record["phase"] == "rollback_await_ms"
        assert record["connection_in_transaction"] is False
        assert "commit_await_ms" in record["timings"]
        assert storage._operation_holder is None
        assert not storage._operation_lock.locked()
        assert not storage._poisoned

        # An independent connection proves the first write was committed even
        # though the transaction context propagated cancellation to its caller.
        reader = sqlite3.connect(path)
        try:
            assert reader.execute("SELECT value FROM timing_fixture").fetchall() == [
                ("committed",),
            ]
            async with storage._write_transaction("after_cancelled_commit") as conn:
                await conn.execute("INSERT INTO timing_fixture VALUES ('next')")
            assert reader.execute("SELECT value FROM timing_fixture ORDER BY rowid").fetchall() == [
                ("committed",), ("next",),
            ]
        finally:
            reader.close()
    finally:
        await storage.close()
