"""V011 migration: relax meta_skill_runs.triggered_by CHECK.

Verifies the recreate-and-copy migration preserves existing rows,
opens the constraint to accept ``auto_cron`` + ``auto_dream``, and
keeps all five V010 indexes alive.

The preservation tests build the schema as of V010 only, seed rows,
then migrate to head — applying the full directory first would leave
the second ``apply_pending`` with nothing to do, so the recreate copy
would only ever run against empty tables.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from tests.helpers.historical_migrations import apply_pre_retirement_migrations as apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

V010_ID = "V010__meta_skill_runs"
V011_ID = "V011__meta_skill_runs_triggered_by_auto"


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _apply_through(db: str, last_id: str) -> None:
    """Apply *last_id* and its transitive __depends__ closure only."""
    # Mirror the migrator's Python 3.12 datetime adapter registration; the
    # yoyo ledger writes applied_at datetimes on this direct-backend path.
    sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))
    backend = get_backend("sqlite:///" + db)
    try:
        migrations = read_migrations(str(MIGRATIONS_DIR))
        by_id = {migration.id: migration for migration in migrations}
        wanted: set[str] = set()
        todo = [last_id]
        while todo:
            migration_id = todo.pop()
            if migration_id in wanted:
                continue
            wanted.add(migration_id)
            todo.extend(dep.id for dep in by_id[migration_id].depends)
        subset = migrations.filter(lambda m: m.id in wanted)
        with backend.lock():
            backend.apply_migrations(backend.to_apply(subset))
    finally:
        backend.connection.close()


def _insert_run(conn: sqlite3.Connection, run_id: str, triggered_by: str) -> None:
    conn.execute(
        """
        INSERT INTO meta_skill_runs (
            run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json,
            triggered_by, status, started_at_ms, inputs_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, "synth", "deadbeef", "{}", triggered_by, "ok",
            1_000_000, "{}",
        ),
    )
    conn.commit()


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,),
        ).fetchall()
        if not row[0].startswith("sqlite_")
    }


def test_v011_relaxes_triggered_by_check_to_accept_auto_values(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "v011.db")
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert "V011__meta_skill_runs_triggered_by_auto" in applied

    conn = _open_conn(db)
    try:
        # Existing values still accepted
        _insert_run(conn, "r1", "hard_takeover")
        _insert_run(conn, "r2", "soft_meta_invoke")
        # New values accepted post-V011
        _insert_run(conn, "r3", "auto_cron")
        _insert_run(conn, "r4", "auto_dream")
        # Bad values still rejected
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(conn, "r5", "made_up_value")
        rows = conn.execute(
            "SELECT triggered_by FROM meta_skill_runs ORDER BY run_id"
        ).fetchall()
        # Ordered by run_id (r1..r4), so the values appear in insertion order
        assert [r[0] for r in rows] == [
            "hard_takeover", "soft_meta_invoke", "auto_cron", "auto_dream",
        ]
    finally:
        conn.close()


def test_v011_preserves_pre_existing_rows(tmp_path: Path) -> None:
    """Rows inserted under the V010 schema must survive the V011
    recreate-and-copy with every column value intact."""
    db = str(tmp_path / "v011_preserve.db")
    _apply_through(db, V010_ID)

    conn = _open_conn(db)
    try:
        # Insert one row with each original value BEFORE V011 exists.
        _insert_run(conn, "rA", "hard_takeover")
        _insert_run(conn, "rB", "soft_meta_invoke")
    finally:
        conn.close()

    # Migrate to head — V011's copy now runs against a populated table.
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert "V011__meta_skill_runs_triggered_by_auto" in applied

    conn = _open_conn(db)
    try:
        rows = conn.execute(
            "SELECT run_id, triggered_by, meta_skill_name, meta_skill_digest,"
            " plan_snapshot_json, status, started_at_ms, inputs_json"
            " FROM meta_skill_runs ORDER BY run_id"
        ).fetchall()
        assert rows == [
            ("rA", "hard_takeover", "synth", "deadbeef", "{}", "ok", 1_000_000, "{}"),
            ("rB", "soft_meta_invoke", "synth", "deadbeef", "{}", "ok", 1_000_000, "{}"),
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_v011_keeps_all_five_v010_indexes(tmp_path: Path) -> None:
    db = str(tmp_path / "v011_idx.db")
    apply_pending(db, MIGRATIONS_DIR)
    conn = _open_conn(db)
    try:
        ix = _indexes(conn, "meta_skill_runs")
        # Four idx_meta_runs_* indexes are recreated by V011.
        assert "idx_meta_runs_name_started" in ix
        assert "idx_meta_runs_status_started" in ix
        assert "idx_meta_runs_session" in ix
        assert "idx_meta_runs_started" in ix
    finally:
        conn.close()


def test_v011_child_table_rows_survive_recreate(tmp_path: Path) -> None:
    """meta_skill_run_steps.run_id FKs into meta_skill_runs.run_id;
    recreating the parent table must not orphan or drop child rows that
    existed BEFORE the recreate ran."""
    db = str(tmp_path / "v011_child.db")
    _apply_through(db, V010_ID)

    conn = _open_conn(db)
    try:
        _insert_run(conn, "rx", "soft_meta_invoke")
        conn.execute(
            """
            INSERT INTO meta_skill_run_steps (
                run_id, step_id, step_kind, declared_skill, effective_skill,
                status, started_at_ms, rendered_inputs_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("rx", "step1", "agent", "memory", "memory", "ok", 1_000_001, "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    # Migrate to head — V011 drops and recreates the parent table with the
    # child rows in place.
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert V011_ID in applied

    conn = _open_conn(db)
    try:
        row = conn.execute(
            "SELECT meta_skill_runs.run_id, meta_skill_run_steps.step_id"
            " FROM meta_skill_run_steps"
            " JOIN meta_skill_runs USING (run_id)"
            " WHERE meta_skill_runs.run_id = ?",
            ("rx",),
        ).fetchone()
        assert row == ("rx", "step1")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
