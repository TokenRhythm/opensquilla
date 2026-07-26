"""Turn-loop error records: catch-all writes a turn_errors row and the
ErrorEvent carries the matching error_id; record failure never masks the error.

Offline: a failing provider selector forces the catch-all; storage is a temp
sqlite file with real migrations.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ErrorEvent
from opensquilla.persistence.migrator import apply_pending
from opensquilla.persistence.turn_error_writer import open_turn_error_writer
from opensquilla.session.terminal_reply import append_error_ref
from opensquilla.tools.types import ToolContext

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class _ExplodingSelector:
    def select(self, *args, **kwargs):
        raise RuntimeError("synthetic selector explosion")

    def __getattr__(self, name):
        raise RuntimeError("synthetic selector explosion")


def _rows(db: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM turn_errors").fetchall()
    finally:
        conn.close()


async def _run_collect(runner: TurnRunner, session_key: str) -> list:
    return [
        event
        async for event in runner.run(
            "hello",
            session_key=session_key,
            tool_context=ToolContext(session_key=session_key),
        )
    ]


async def test_failed_turn_writes_error_record_and_ref(tmp_path) -> None:
    db = str(tmp_path / "sessions.sqlite")
    apply_pending(db, MIGRATIONS_DIR)
    writer = open_turn_error_writer(db)
    runner = TurnRunner(provider_selector=_ExplodingSelector(), turn_error_writer=writer)
    events = await _run_collect(runner, "agent:main:test:s1")
    error_events = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(error_events) == 1
    event = error_events[0]
    assert event.error_id
    assert len(event.error_id) == 8

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["error_id"] == event.error_id
    assert rows[0]["session_key"] == "agent:main:test:s1"
    assert rows[0]["error_class"]
    assert "synthetic selector explosion" in (rows[0]["traceback"] or "")
    writer.close()


async def test_writer_failure_does_not_mask_error(tmp_path) -> None:
    class _BrokenWriter:
        def record_error(self, record):
            raise RuntimeError("store exploded")

    runner = TurnRunner(provider_selector=_ExplodingSelector(), turn_error_writer=_BrokenWriter())
    events = await _run_collect(runner, "agent:main:test:s2")
    error_events = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].message  # original error still surfaced


async def test_locked_error_writer_does_not_block_event_loop(tmp_path) -> None:
    db = str(tmp_path / "sessions.sqlite")
    apply_pending(db, MIGRATIONS_DIR)
    writer = open_turn_error_writer(db)
    write_started = threading.Event()

    class _SignalingWriter:
        def record_error(self, record):
            write_started.set()
            return writer.record_error(record)

    blocker = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
    blocker.execute("PRAGMA journal_mode = WAL")
    blocker.execute("BEGIN IMMEDIATE")
    release_lock = threading.Timer(0.6, blocker.rollback)
    runner = TurnRunner(
        provider_selector=_ExplodingSelector(),
        turn_error_writer=_SignalingWriter(),
    )
    loop = asyncio.get_running_loop()
    record_task = asyncio.create_task(
        runner._record_turn_error(
            session_key="agent:main:test:locked",
            turn_id="turn-locked",
            session_id=None,
            surface="test",
            error_class="synthetic_error",
            message="synthetic writer contention",
            exc=RuntimeError("synthetic writer contention"),
            provider=None,
            model=None,
            fallback_hops=0,
        )
    )
    release_lock.start()
    try:
        # A synchronous record_error call would hold the event loop until the
        # timer releases SQLite's writer lock. Polling can only make progress
        # while the writer is waiting in a worker thread.
        deadline = loop.time() + 0.3
        while not write_started.is_set() and not record_task.done() and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert write_started.is_set()
        assert not record_task.done()

        error_id = await asyncio.wait_for(record_task, timeout=2)
        assert error_id
        assert len(_rows(db)) == 1
    finally:
        release_lock.cancel()
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
        writer.close()


async def test_no_writer_yields_error_without_ref(tmp_path) -> None:
    runner = TurnRunner(provider_selector=_ExplodingSelector())
    events = await _run_collect(runner, "agent:main:test:s3")
    error_events = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].error_id == ""


def test_append_error_ref_is_idempotent() -> None:
    base = "The task failed before it could finish."
    once = append_error_ref(base, "abcd1234")
    assert once == "The task failed before it could finish. (ref: abcd1234)"
    assert append_error_ref(once, "abcd1234") == once
    assert append_error_ref(base, None) == base
    assert append_error_ref(base, "") == base
