"""Retirement upgrades preserve checkpoint safety and unrelated durable data."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from opensquilla.persistence.memory_flush_retirement import retire_memory_flush_metadata
from opensquilla.persistence.migrator import apply_pending
from opensquilla.session.models import MemoryDurableReceipt
from opensquilla.session.storage import SessionStorage

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

LEGACY_MEMORY_SCHEMA = """
CREATE TABLE session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    compaction_index INTEGER NOT NULL DEFAULT 0,
    summary_text TEXT NOT NULL,
    flush_receipt_status TEXT NOT NULL DEFAULT 'unknown',
    covered_through_id INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_summaries_session_id ON session_summaries(session_id);
CREATE TABLE memory_durable_receipts (
    receipt_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    scope TEXT NOT NULL,
    source_path TEXT,
    target_path TEXT,
    content_hash TEXT,
    coverage_turn_id TEXT,
    coverage_hash TEXT,
    coverage_entry_count INTEGER,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    reason TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at_ms INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_memory_durable_receipts_session
ON memory_durable_receipts(session_key, status, created_at);
CREATE INDEX idx_memory_durable_receipts_coverage
ON memory_durable_receipts(session_key, session_id, scope, status,
                         coverage_turn_id, coverage_hash, coverage_entry_count);
"""


def _legacy_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_MEMORY_SCHEMA)
        conn.execute(
            "INSERT INTO session_summaries "
            "(id, session_id, session_key, summary_text, flush_receipt_status, created_at) "
            "VALUES (7, 'synthetic-session', 'agent:main:webchat:demo', "
            "'Keep this compaction summary', 'degraded_forensic', 10)"
        )
        # Retain AUTOINCREMENT's high-water mark even after historical deletions.
        conn.execute("UPDATE sqlite_sequence SET seq = 42 WHERE name = 'session_summaries'")
        for scope, status in (
            ("checkpoint", "checkpoint_saved"),
            ("checkpoint", "checkpoint_failed"),
            ("flush", "flush_appended"),
            ("preimage", "preimage_saved"),
            ("repair", "repair_pending"),
            ("extension", "unrelated"),
        ):
            conn.execute(
                "INSERT INTO memory_durable_receipts "
                "(receipt_id, session_key, session_id, scope, source_path, content_hash, "
                "coverage_turn_id, coverage_hash, coverage_entry_count, idempotency_key, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (status, "agent:main:webchat:demo", "synthetic-session", scope,
                 "memory/.checkpoints/synthetic.jsonl", "checkpoint-hash",
                 "turn-1", "coverage-hash",
                 2, status, status, 10, 11),
            )


def _assert_retired(conn: sqlite3.Connection) -> None:
    assert "flush_receipt_status" not in {
        row[1] for row in conn.execute("PRAGMA table_info(session_summaries)")
    }
    assert not {"target_path", "attempt_count", "next_retry_at_ms"}.intersection(
        row[1] for row in conn.execute("PRAGMA table_info(memory_durable_receipts)")
    )
    assert conn.execute(
        "SELECT receipt_id FROM memory_durable_receipts ORDER BY receipt_id"
    ).fetchall() == [("checkpoint_failed",), ("checkpoint_saved",), ("unrelated",)]
    assert conn.execute(
        "SELECT content_hash, coverage_turn_id, coverage_hash, coverage_entry_count "
        "FROM memory_durable_receipts WHERE receipt_id = 'checkpoint_saved'"
    ).fetchone() == ("checkpoint-hash", "turn-1", "coverage-hash", 2)
    assert conn.execute("SELECT id, summary_text FROM session_summaries").fetchall() == [
        (7, "Keep this compaction summary")
    ]
    assert conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'session_summaries'"
    ).fetchone() == (42,)
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_summaries_session_id", "idx_memory_durable_receipts_session",
            "idx_memory_durable_receipts_coverage"} <= indexes
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("versioned", [False, True])
async def test_legacy_flush_cleanup_preserves_checkpoint_data_and_reopens(
    tmp_path: Path, versioned: bool,
) -> None:
    path = tmp_path / "sessions.db"
    _legacy_db(path)
    memory = tmp_path / "workspace" / "MEMORY.md"
    archive = tmp_path / "workspace" / "memory" / ".raw_fallbacks" / "legacy.md"
    archive.parent.mkdir(parents=True)
    memory.write_text("Synthetic explicit memory", encoding="utf-8")
    archive.write_text("Synthetic historical archive", encoding="utf-8")
    if versioned:
        assert "V044__retire_legacy_memory_flush" in apply_pending(str(path), MIGRATIONS_DIR)
    for _ in range(2):
        storage = await SessionStorage.open(path)
        receipts = await storage.list_memory_durable_receipts(scope="checkpoint")
        assert len(receipts) == 2
        await storage.upsert_memory_durable_receipt(receipts[0])
        await storage.close()
        with sqlite3.connect(path) as conn:
            _assert_retired(conn)
    assert memory.read_text() == "Synthetic explicit memory"
    assert archive.read_text() == "Synthetic historical archive"
    if versioned:
        assert apply_pending(str(path), MIGRATIONS_DIR) == []


def test_retirement_rolls_back_all_changes_after_mid_migration_failure(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    _legacy_db(path)
    with sqlite3.connect(path) as conn:
        # Reject the second table's replacement after summary retirement ran.
        def deny_receipt_create(action, name, _arg2, _db, _trigger):
            if action == sqlite3.SQLITE_CREATE_TABLE and name == (
                "memory_durable_receipts_without_legacy_flush"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_receipt_create)
        with pytest.raises(sqlite3.DatabaseError):
            retire_memory_flush_metadata(conn)
        conn.set_authorizer(None)
        assert "flush_receipt_status" in {
            row[1] for row in conn.execute("PRAGMA table_info(session_summaries)")
        }
        assert conn.execute("SELECT count(*) FROM memory_durable_receipts").fetchone() == (6,)
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%without_legacy_flush%'"
        ).fetchall()
        retire_memory_flush_metadata(conn)
        _assert_retired(conn)


def test_concurrent_retirement_serializes_before_reading_schema(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    _legacy_db(path)

    def upgrade() -> None:
        with sqlite3.connect(path, timeout=10) as conn:
            retire_memory_flush_metadata(conn)
            _assert_retired(conn)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: upgrade(), range(4)))


async def test_parallel_direct_storage_opens_converge(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    _legacy_db(path)
    # Populate ordinary additive metadata first while leaving the retirement
    # columns present, so the concurrent path isolates this upgrade boundary.
    from unittest.mock import patch

    with patch.object(SessionStorage, "_retire_legacy_memory_flush_metadata", return_value=None):
        storage = await SessionStorage.open(path)
        await storage.close()
    stores = await asyncio.gather(*(SessionStorage.open(path) for _ in range(3)))
    try:
        with sqlite3.connect(path) as conn:
            _assert_retired(conn)
        fresh = await stores[0].upsert_memory_durable_receipt(MemoryDurableReceipt(
            session_key="agent:main:webchat:fresh", session_id="fresh-session",
            scope="checkpoint", status="checkpoint_saved", idempotency_key="new-checkpoint",
            source_path="memory/.checkpoints/fresh.jsonl", content_hash="fresh-hash",
        ))
        assert fresh.status == "checkpoint_saved"
    finally:
        for storage in stores:
            await storage.close()


async def test_current_schema_retirement_does_not_acquire_writer_lock(tmp_path: Path) -> None:
    from unittest.mock import patch

    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        with patch.object(
            storage, "_write_transaction", side_effect=AssertionError("unexpected write"),
        ):
            await storage._retire_legacy_memory_flush_metadata()
    finally:
        await storage.close()


def test_failed_versioned_upgrade_keeps_backup_and_can_retry(tmp_path, monkeypatch) -> None:
    import shutil

    from opensquilla.persistence import memory_flush_retirement

    path = tmp_path / "sessions.db"
    _legacy_db(path)
    previous_migrations = tmp_path / "previous_migrations"
    previous_migrations.mkdir()
    for migration in MIGRATIONS_DIR.glob("*.py"):
        if not migration.name.startswith("V044__"):
            shutil.copyfile(migration, previous_migrations / migration.name)
    apply_pending(str(path), previous_migrations)
    original_plan = memory_flush_retirement.memory_flush_retirement_statements

    def fail_after_rebuild(**kwargs):
        return [*original_plan(**kwargs), "INSERT INTO missing_retirement_test_table VALUES (1)"]

    with monkeypatch.context() as patch:
        patch.setattr(
            memory_flush_retirement, "memory_flush_retirement_statements", fail_after_rebuild,
        )
        with pytest.raises(sqlite3.OperationalError, match="missing_retirement_test_table"):
            apply_pending(str(path), MIGRATIONS_DIR)
    backup = tmp_path / "sessions.db.pre-V044__retire_legacy_memory_flush.bak"
    assert backup.is_file()
    for inspected in (path, backup):
        with sqlite3.connect(inspected) as conn:
            assert "flush_receipt_status" in {
                row[1] for row in conn.execute("PRAGMA table_info(session_summaries)")
            }
            assert conn.execute("SELECT count(*) FROM memory_durable_receipts").fetchone() == (6,)
    assert apply_pending(str(path), MIGRATIONS_DIR) == ["V044__retire_legacy_memory_flush"]
    with sqlite3.connect(path) as conn:
        _assert_retired(conn)
