"""TurnErrorWriter: inserts, scrubbing, pruning, purge, listing.

Free-text columns (message/traceback) are the point of this table, unlike the
V017 no-free-text bar — but both pass scrub_text so secret-shaped values and
home paths never persist. All data below is synthetic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from opensquilla.persistence.migrator import apply_pending
from opensquilla.persistence.turn_error_writer import (
    TurnErrorWriter,
    new_error_id,
    open_turn_error_writer,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

FAKE_KEY = "sk-FAKE1234567890abcdef"


def _make_writer(tmp_path: Path, **kwargs) -> tuple[TurnErrorWriter, str]:
    db = str(tmp_path / "sessions.sqlite")
    apply_pending(db, MIGRATIONS_DIR)
    conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return TurnErrorWriter(conn, **kwargs), db


def _base_record(**overrides) -> dict:
    record = {
        "error_id": "abcd1234",
        "turn_id": "t" * 32,
        "session_key": "agent:main:webchat:s1",
        "session_id": "sess-1",
        "ts_ms": 1_000_000,
        "surface": "webui",
        "error_class": "agent_error",
        "message": "The provider rejected the request.",
        "traceback": f"Traceback ...\n  api_key={FAKE_KEY}\nValueError: boom",
        "provider": "FakeProvider",
        "model": "fake/model",
        "fallback_hops": 1,
    }
    record.update(overrides)
    return record


def _rows(db: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM turn_errors ORDER BY ts_ms").fetchall()
    finally:
        conn.close()


def test_new_error_id_is_short_hex() -> None:
    error_id = new_error_id()
    assert len(error_id) == 8
    assert all(char in "0123456789abcdef" for char in error_id)


def test_record_error_inserts_scrubbed_row(tmp_path: Path) -> None:
    writer, db = _make_writer(tmp_path)
    assert writer.record_error(_base_record()) is True
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["error_id"] == "abcd1234"
    assert row["session_key"] == "agent:main:webchat:s1"
    assert row["error_class"] == "agent_error"
    assert FAKE_KEY not in row["traceback"]
    assert "[redacted]" in row["traceback"]
    writer.close()


def test_record_error_requires_error_id_and_session_key(tmp_path: Path) -> None:
    writer, db = _make_writer(tmp_path)
    assert writer.record_error(_base_record(error_id=None)) is False
    assert writer.record_error(_base_record(session_key="")) is False
    assert _rows(db) == []
    writer.close()


def test_record_error_fail_open_on_missing_table(tmp_path: Path) -> None:
    db = str(tmp_path / "no-migrations.sqlite")
    conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    writer = TurnErrorWriter(conn)
    assert writer.record_error(_base_record()) is False  # no raise
    writer.close()


def test_write_time_pruning(tmp_path: Path) -> None:
    now_ms = 10_000_000_000_000
    writer, db = _make_writer(tmp_path, retention_days=30, prune_every=3, clock=lambda: now_ms)
    stale_ts = now_ms - 31 * 24 * 60 * 60 * 1000
    writer.record_error(_base_record(error_id="old00001", ts_ms=stale_ts))
    writer.record_error(_base_record(error_id="new00001", ts_ms=now_ms - 1000))
    assert len(_rows(db)) == 2
    writer.record_error(_base_record(error_id="new00002", ts_ms=now_ms - 500))
    remaining = {row["error_id"] for row in _rows(db)}
    assert remaining == {"new00001", "new00002"}
    writer.close()


def test_write_time_pruning_is_bounded(tmp_path: Path) -> None:
    now_ms = 10_000_000_000_000
    writer, db = _make_writer(
        tmp_path,
        retention_days=30,
        prune_every=6,
        prune_batch=2,
        clock=lambda: now_ms,
    )
    stale_ts = now_ms - 31 * 24 * 60 * 60 * 1000
    for index in range(5):
        writer.record_error(
            _base_record(error_id=f"old{index:05d}", ts_ms=stale_ts + index)
        )

    # The sixth insert triggers retention, but one foreground write may delete
    # only the configured batch instead of monopolizing SQLite for the backlog.
    writer.record_error(_base_record(error_id="new00001", ts_ms=now_ms))

    remaining = {row["error_id"] for row in _rows(db)}
    assert remaining == {"old00002", "old00003", "old00004", "new00001"}
    writer.close()


def test_purge_for_session(tmp_path: Path) -> None:
    writer, db = _make_writer(tmp_path)
    writer.record_error(_base_record(error_id="aaaa0001", session_key="agent:a"))
    writer.record_error(_base_record(error_id="bbbb0001", session_key="agent:b"))
    assert writer.purge_for_session("agent:a") == 1
    assert {row["session_key"] for row in _rows(db)} == {"agent:b"}
    writer.close()


def test_list_errors_newest_first_with_days_window(tmp_path: Path) -> None:
    now_ms = 10_000_000_000_000
    writer, db = _make_writer(tmp_path, clock=lambda: now_ms)
    writer.record_error(_base_record(error_id="aaaa0001", ts_ms=now_ms - 5 * 24 * 60 * 60 * 1000))
    writer.record_error(_base_record(error_id="bbbb0001", ts_ms=now_ms - 1000))
    listed = writer.list_errors(days=3)
    assert [entry["error_id"] for entry in listed] == ["bbbb0001"]
    listed_all = writer.list_errors()
    assert [entry["error_id"] for entry in listed_all] == ["bbbb0001", "aaaa0001"]
    writer.close()


def test_open_turn_error_writer_boot_constructor(tmp_path: Path) -> None:
    db = str(tmp_path / "sessions.sqlite")
    apply_pending(db, MIGRATIONS_DIR)
    writer = open_turn_error_writer(db, retention_days=7)
    try:
        assert writer.record_error(_base_record()) is True
        assert len(_rows(db)) == 1
    finally:
        writer.close()
