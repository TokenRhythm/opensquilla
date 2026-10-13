"""Opt-in timing must preserve transaction ordering and failure behaviour."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing

import pytest

from opensquilla.compat import aiosqlite
from opensquilla.observability.log_privacy import PrivateLogFormatter
from opensquilla.session.storage import SessionStorage, StorageBusyError


def timing_records(caplog):
    return [
        json.loads(PrivateLogFormatter().format(record).split(": ", 1)[1])
        for record in caplog.records
        if getattr(record, "_opensquilla_log_metadata", {}).get("event")
        == "session_storage.transaction_timing"
    ]


@pytest.mark.parametrize("fallback", [False, True])
async def test_timing_preserves_sql_and_is_opt_in(tmp_path, monkeypatch, caplog, fallback):
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", fallback)
    caplog.set_level(logging.INFO, logger="opensquilla.session.storage")
    traces = []
    for enabled in (False, True):
        if enabled:
            monkeypatch.setenv("OPENSQUILLA_STORAGE_DIAGNOSTICS", "1")
        else:
            monkeypatch.delenv("OPENSQUILLA_STORAGE_DIAGNOSTICS", raising=False)
        storage = await SessionStorage.open(str(tmp_path / f"timing-{enabled}.db"))
        statements = []
        try:
            await storage.conn.execute("CREATE TABLE timing_fixture(value TEXT)")
            await storage.conn.set_trace_callback(statements.append)
            caplog.clear()
            async with storage._write_transaction("timing.write") as conn:
                await conn.execute("INSERT INTO timing_fixture VALUES (?)", ("private body",))
            async with storage.read_transaction("timing.read") as conn:
                async with conn.execute("SELECT value FROM timing_fixture") as cursor:
                    assert (await cursor.fetchone())[0] == "private body"
            assert not storage.conn.in_transaction
            assert not storage._operation_lock.locked()
            records = timing_records(caplog)
            if enabled:
                assert len(records) == 2
                write, read = records
                assert write["status"] == read["status"] == "ok"
                assert set(write["timings"]) == {
                    "queue_ms", "begin_await_ms", "body_ms", "commit_await_ms",
                }
                assert set(read["timings"]) == {
                    "queue_ms", "begin_await_ms", "body_ms", "rollback_await_ms",
                }
                for record in records:
                    assert sum(record["timings"].values()) == pytest.approx(record["duration_ms"])
                    assert all(value >= 0 for value in record["timings"].values())
                    assert record["connection_in_transaction"] is False
                    assert "private body" not in json.dumps(record)
                assert write["operation_id"] != read["operation_id"]
            else:
                assert not records
            traces.append(statements.copy())
        finally:
            await storage.conn.set_trace_callback(None)
            await storage.close()
    assert traces[0] == traces[1]


async def test_queue_timeout_reports_only_queue_and_keeps_holder(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OPENSQUILLA_STORAGE_DIAGNOSTICS", "1")
    storage = await SessionStorage.open(str(tmp_path / "queue.db"))
    caplog.set_level(logging.INFO, logger="opensquilla.session.storage")
    try:
        async with storage.read_transaction("holding"):
            caplog.clear()
            with pytest.raises(StorageBusyError):
                async with storage._write_transaction("queued", budget_seconds=0):
                    pytest.fail("queued write acquired an occupied gate")
            [record] = timing_records(caplog)
            assert record["status"] == "error"
            assert record["exception_type"] == "StorageBusyError"
            assert set(record["timings"]) == {"queue_ms"}
            assert storage._operation_holder[0] == "read.holding"
    finally:
        await storage.close()


async def test_independent_writer_is_reported_as_begin_wait(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OPENSQUILLA_STORAGE_DIAGNOSTICS", "1")
    path = tmp_path / "sqlite-writer.db"
    storage = await SessionStorage.open(str(path))
    caplog.set_level(logging.INFO, logger="opensquilla.session.storage")
    try:
        with closing(sqlite3.connect(path, isolation_level=None)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            caplog.clear()
            with pytest.raises(StorageBusyError) as error:
                async with storage._write_transaction("contending", budget_seconds=0):
                    pytest.fail("write began while another connection owned the writer slot")
            assert error.value.stage == "begin"
            [record] = timing_records(caplog)
            assert record["status"] == "error"
            assert record["phase"] == "begin_await_ms"
            assert set(record["timings"]) == {"queue_ms", "begin_await_ms"}
            assert not storage._operation_lock.locked()
            assert not storage.conn.in_transaction
            writer.rollback()
        async with storage._write_transaction("after_contention"):
            pass
    finally:
        await storage.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_timing_failure_does_not_change_rollback(tmp_path, monkeypatch, caplog, cancel):
    monkeypatch.setenv("OPENSQUILLA_STORAGE_DIAGNOSTICS", "1")
    storage = await SessionStorage.open(str(tmp_path / "rollback.db"))
    caplog.set_level(logging.INFO, logger="opensquilla.session.storage")
    error = asyncio.CancelledError if cancel else ValueError
    try:
        await storage.conn.execute("CREATE TABLE timing_fixture(value TEXT)")
        caplog.clear()
        with pytest.raises(error):
            async with storage._write_transaction("failure") as conn:
                await conn.execute("INSERT INTO timing_fixture VALUES ('synthetic')")
                raise error("private exception")
        [record] = timing_records(caplog)
        assert record["status"] == ("cancelled" if cancel else "error")
        assert "rollback_await_ms" in record["timings"]
        assert "commit_await_ms" not in record["timings"]
        assert "private exception" not in json.dumps(record)
        assert not storage.conn.in_transaction
        assert not storage._operation_lock.locked()
        assert storage._operation_holder is None
        async with storage.conn.execute("SELECT count(*) FROM timing_fixture") as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await storage.close()


def test_capture_has_a_fixed_upper_bound(monkeypatch, caplog):
    monkeypatch.setenv("OPENSQUILLA_STORAGE_DIAGNOSTICS", "1")
    storage = SessionStorage()
    caplog.set_level(logging.INFO, logger="opensquilla.session.storage")
    for _ in range(300):
        with storage._transaction_diagnostics("bounded", "read"):
            pass
    records = timing_records(caplog)
    assert len(records) == 256
    assert records[-1]["capture_limit_reached"] is True
