"""Unit tests for the V017 router_decisions migration.

Covers the additive apply, the rollback step (V010 precedent), the
duplicate-version-prefix guard (the V010 double-file trap), and the
refusal-by-design downgrade contract: a database that records V017 must
make ``assert_schema_not_ahead`` raise when the running code's migration
set does not include it.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from opensquilla.persistence.migrator import (
    SchemaAheadError,
    apply_pending,
    assert_schema_not_ahead,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

EXPECTED_COLUMNS = {
    "decision_id",
    "session_key",
    "turn_index",
    "ts_ms",
    "classifier",
    "proposed_tier",
    "confidence",
    "probs",
    "flags",
    "final_tier",
    "requested_provider",
    "requested_model",
    "provider",
    "model",
    "executed_provider",
    "executed_model",
    "fallback_reason",
    "thinking_level",
    "source",
    "trail",
    "baseline_model",
    "savings_pct",
    "executed_kind",
    "ensemble_profile",
    "fallback_hops",
}


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,),
        ).fetchall()
        if not row[0].startswith("sqlite_")
    }


def test_v017_apply_creates_table_and_index(tmp_path: Path) -> None:
    db = str(tmp_path / "test.sqlite")
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert "V017__router_decisions" in applied

    conn = _open_conn(db)
    try:
        assert _table_columns(conn, "router_decisions") == EXPECTED_COLUMNS
        assert "idx_router_decisions_session_ts" in _indexes(conn, "router_decisions")
        # executed_kind CHECK enforces the enum.
        conn.execute(
            "INSERT INTO router_decisions (decision_id, session_key, ts_ms, executed_kind) "
            "VALUES ('d1', 'agent:main:main', 1, 'single')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO router_decisions (decision_id, session_key, ts_ms, executed_kind) "
                "VALUES ('d2', 'agent:main:main', 1, 'other')"
            )
        # fallback_hops defaults to 0.
        row = conn.execute(
            "SELECT fallback_hops FROM router_decisions WHERE decision_id='d1'"
        ).fetchone()
        assert row[0] == 0
    finally:
        conn.close()


def test_v017_rollback_drops_table_and_index(tmp_path: Path) -> None:
    db = str(tmp_path / "test.sqlite")
    apply_pending(db, MIGRATIONS_DIR)

    from yoyo import get_backend, read_migrations

    backend = get_backend("sqlite:///" + db)
    try:
        v017 = read_migrations(str(MIGRATIONS_DIR)).filter(
            lambda m: m.id == "V017__router_decisions"
        )
        with backend.lock():
            backend.rollback_migrations(v017)
    finally:
        backend.connection.close()

    conn = _open_conn(db)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "router_decisions" not in tables
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_router_decisions_session_ts" not in indexes
    finally:
        conn.close()

    # After rollback, the ledger no longer records V017 — the older build's
    # assert_schema_not_ahead accepts the database again (SchemaAheadError
    # rollback story documented in the migration docstring).
    older_build_dir = tmp_path / "migrations_without_v017"
    older_build_dir.mkdir()
    for migration in MIGRATIONS_DIR.glob("V*.py"):
        if migration.name != "V017__router_decisions.py":
            shutil.copy2(migration, older_build_dir / migration.name)
    assert_schema_not_ahead(db, older_build_dir)


def test_v017_version_prefix_is_unique() -> None:
    """Guard against the duplicate-V010 trap: exactly one V017 file."""
    v017_files = sorted(p.name for p in MIGRATIONS_DIR.glob("V017__*.py"))
    assert v017_files == ["V017__router_decisions.py"]


def test_v017_depends_on_v016() -> None:
    source = (MIGRATIONS_DIR / "V017__router_decisions.py").read_text(encoding="utf-8")
    assert "V016__meta_skill_runs_triggered_by_manual_command" in source


def test_schema_ahead_refuses_boot_without_v017(tmp_path: Path) -> None:
    """Downgrade contract: DB with V017 + code without it -> SchemaAheadError."""
    db = str(tmp_path / "test.sqlite")
    apply_pending(db, MIGRATIONS_DIR)

    older_build_dir = tmp_path / "migrations_without_v017"
    older_build_dir.mkdir()
    for migration in MIGRATIONS_DIR.glob("V*.py"):
        if migration.name == "V017__router_decisions.py":
            continue
        shutil.copy2(migration, older_build_dir / migration.name)

    with pytest.raises(SchemaAheadError, match="V017__router_decisions"):
        assert_schema_not_ahead(db, older_build_dir)

    # The full migration set accepts the same database.
    assert_schema_not_ahead(db, MIGRATIONS_DIR)
