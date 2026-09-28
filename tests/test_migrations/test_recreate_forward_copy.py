"""Rows-present forward-copy coverage for the recreate migrations.

The V011–V016 migrations rebuild ``meta_skill_runs`` / ``meta_skill_run_steps``
with a drop-and-copy. Full-directory tests only ever exercise those copies
against EMPTY tables (the second ``apply_pending`` filters to nothing), so
these tests build the schema as of just-before the target migration, seed
synthetic rows, then migrate to head and assert row-for-row survival.

Also covers the re-run guards (yoyo's apply-then-mark crash window /
operator reapply, simulated by deleting the ledger row) and the fail-loud
guard against live foreign-key enforcement on the migration connection.

All data below is synthetic public-dummy content.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from tests.helpers.historical_migrations import apply_pre_retirement_migrations as apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

V010_RUNS = "V010__meta_skill_runs"
V011 = "V011__meta_skill_runs_triggered_by_auto"
V012 = "V012__meta_skill_run_steps_allow_llm_chat"
V013 = "V013__meta_skill_runs_clarify"
V014 = "V014__meta_skill_run_steps_allow_user_input"
V016 = "V016__meta_skill_runs_triggered_by_manual_command"

RUN_COLUMNS_V010 = (
    "run_id",
    "meta_skill_name",
    "meta_skill_digest",
    "plan_snapshot_json",
    "triggered_by",
    "session_key",
    "turn_id",
    "owner_pid",
    "status",
    "started_at_ms",
    "ended_at_ms",
    "inputs_json",
    "final_text",
    "failed_step_id",
    "error",
    "truncated_fields",
)

STEP_COLUMNS_V010 = (
    "run_id",
    "step_id",
    "step_kind",
    "declared_skill",
    "effective_skill",
    "status",
    "started_at_ms",
    "ended_at_ms",
    "rendered_inputs_json",
    "output_text",
    "error",
    "substitute_step_id",
    "truncated_fields",
)

CLARIFY_COLUMNS = (
    "awaiting_step_id",
    "awaiting_schema_json",
    "awaiting_since",
    "awaiting_filled_json",
    "step_outputs_json",
    "parse_failure_count",
)


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


def _unmark(db: str, migration_id: str) -> None:
    """Delete *migration_id* from the yoyo ledger, simulating the
    apply-then-mark crash window (or an operator forcing a reapply)."""
    conn = sqlite3.connect(db)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE '%yoyo_migration'"
            ).fetchall()
        ]
        assert tables, "yoyo ledger table not found"
        removed = 0
        for table in tables:
            cur = conn.execute(
                f'DELETE FROM "{table}" WHERE migration_id = ?', (migration_id,)
            )
            removed += cur.rowcount
        conn.commit()
        assert removed, f"{migration_id} was not recorded in the ledger"
    finally:
        conn.close()


def _fk_check_clean(conn: sqlite3.Connection) -> None:
    orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert orphans == [], f"foreign_key_check found orphans: {orphans}"


def _seed_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    triggered_by: str = "hard_takeover",
    status: str = "ok",
    session_key: str | None = None,
) -> dict[str, object]:
    values = {
        "run_id": run_id,
        "meta_skill_name": "synth-meta",
        "meta_skill_digest": "cafef00d",
        "plan_snapshot_json": '{"steps": ["collect", "draft"]}',
        "triggered_by": triggered_by,
        "session_key": session_key,
        "turn_id": f"turn-{run_id}",
        "owner_pid": 4242,
        "status": status,
        "started_at_ms": 1_000_000,
        "ended_at_ms": 1_000_500,
        "inputs_json": '{"topic": "demo"}',
        "final_text": f"final text for {run_id}",
        "failed_step_id": None,
        "error": None,
        "truncated_fields": "",
    }
    columns = ", ".join(RUN_COLUMNS_V010)
    marks = ", ".join("?" for _ in RUN_COLUMNS_V010)
    conn.execute(
        f"INSERT INTO meta_skill_runs ({columns}) VALUES ({marks})",
        tuple(values[column] for column in RUN_COLUMNS_V010),
    )
    conn.commit()
    return values


def _seed_step(
    conn: sqlite3.Connection,
    run_id: str,
    step_id: str,
    *,
    step_kind: str = "agent",
    status: str = "ok",
) -> dict[str, object]:
    values = {
        "run_id": run_id,
        "step_id": step_id,
        "step_kind": step_kind,
        "declared_skill": "note_taker",
        "effective_skill": "note_taker",
        "status": status,
        "started_at_ms": 1_000_100,
        "ended_at_ms": 1_000_200,
        "rendered_inputs_json": '{"prompt": "demo"}',
        "output_text": f"output for {step_id}",
        "error": None,
        "substitute_step_id": None,
        "truncated_fields": "",
    }
    columns = ", ".join(STEP_COLUMNS_V010)
    marks = ", ".join("?" for _ in STEP_COLUMNS_V010)
    conn.execute(
        f"INSERT INTO meta_skill_run_steps ({columns}) VALUES ({marks})",
        tuple(values[column] for column in STEP_COLUMNS_V010),
    )
    conn.commit()
    return values


def _fetch_row(
    conn: sqlite3.Connection, table: str, columns: tuple[str, ...], where: str, args: tuple
) -> tuple:
    return conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {where}", args
    ).fetchone()


def _assert_row(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    where: str,
    args: tuple,
    expected: dict[str, object],
) -> None:
    row = _fetch_row(conn, table, columns, where, args)
    assert row is not None, f"row {args} missing from {table}"
    for column, actual in zip(columns, row):
        assert actual == expected[column], (
            f"{table}.{column} for {args}: expected {expected[column]!r}, "
            f"got {actual!r}"
        )


# ── forward copies with rows present ─────────────────────────────────


def test_v011_forward_copy_preserves_seeded_rows(tmp_path: Path) -> None:
    db = str(tmp_path / "v011_fwd.db")
    _apply_through(db, V010_RUNS)

    conn = _open_conn(db)
    try:
        run_a = _seed_run(conn, "rA", triggered_by="hard_takeover", status="ok")
        run_b = _seed_run(conn, "rB", triggered_by="soft_meta_invoke", status="failed")
        step_a = _seed_step(conn, "rA", "s1", step_kind="agent")
        step_b = _seed_step(conn, "rA", "s2", step_kind="tool_call", status="failed")
    finally:
        conn.close()

    applied = apply_pending(db, MIGRATIONS_DIR)
    assert V011 in applied

    conn = _open_conn(db)
    try:
        _assert_row(conn, "meta_skill_runs", RUN_COLUMNS_V010, "run_id=?", ("rA",), run_a)
        _assert_row(conn, "meta_skill_runs", RUN_COLUMNS_V010, "run_id=?", ("rB",), run_b)
        _assert_row(
            conn, "meta_skill_run_steps", STEP_COLUMNS_V010, "step_id=?", ("s1",), step_a
        )
        _assert_row(
            conn, "meta_skill_run_steps", STEP_COLUMNS_V010, "step_id=?", ("s2",), step_b
        )
        # New columns picked up defaults across the chain.
        clarify = _fetch_row(
            conn, "meta_skill_runs", CLARIFY_COLUMNS, "run_id=?", ("rA",)
        )
        assert clarify == (None, None, None, None, None, 0)
        usage = conn.execute(
            "SELECT usage_json FROM meta_skill_run_steps WHERE step_id='s1'"
        ).fetchone()
        assert usage == ("{}",)
        # Relaxed CHECK accepts the new trigger values.
        _seed_run(conn, "rC", triggered_by="auto_cron")
        _fk_check_clean(conn)
    finally:
        conn.close()


def test_v012_forward_copy_preserves_seeded_steps(tmp_path: Path) -> None:
    db = str(tmp_path / "v012_fwd.db")
    _apply_through(db, V011)

    conn = _open_conn(db)
    try:
        _seed_run(conn, "r1", triggered_by="auto_dream", status="running")
        step_a = _seed_step(conn, "r1", "s1", step_kind="llm_classify")
        step_b = _seed_step(conn, "r1", "s2", step_kind="skill_exec", status="substituted")
    finally:
        conn.close()

    applied = apply_pending(db, MIGRATIONS_DIR)
    assert V012 in applied

    conn = _open_conn(db)
    try:
        _assert_row(
            conn, "meta_skill_run_steps", STEP_COLUMNS_V010, "step_id=?", ("s1",), step_a
        )
        _assert_row(
            conn, "meta_skill_run_steps", STEP_COLUMNS_V010, "step_id=?", ("s2",), step_b
        )
        # Widened CHECK accepts llm_chat after the copy.
        _seed_step(conn, "r1", "s3", step_kind="llm_chat")
        _fk_check_clean(conn)
    finally:
        conn.close()


def test_v013_forward_copy_preserves_seeded_rows(tmp_path: Path) -> None:
    db = str(tmp_path / "v013_fwd.db")
    _apply_through(db, V012)

    conn = _open_conn(db)
    try:
        run_a = _seed_run(conn, "r1", status="running", session_key="agent:demo:one")
        run_b = _seed_run(conn, "r2", status="cancelled")
        step_a = _seed_step(conn, "r1", "s1", step_kind="llm_chat")
    finally:
        conn.close()

    applied = apply_pending(db, MIGRATIONS_DIR)
    assert V013 in applied

    conn = _open_conn(db)
    try:
        _assert_row(conn, "meta_skill_runs", RUN_COLUMNS_V010, "run_id=?", ("r1",), run_a)
        _assert_row(conn, "meta_skill_runs", RUN_COLUMNS_V010, "run_id=?", ("r2",), run_b)
        _assert_row(
            conn, "meta_skill_run_steps", STEP_COLUMNS_V010, "step_id=?", ("s1",), step_a
        )
        clarify = _fetch_row(
            conn, "meta_skill_runs", CLARIFY_COLUMNS, "run_id=?", ("r1",)
        )
        assert clarify == (None, None, None, None, None, 0)
        _fk_check_clean(conn)
    finally:
        conn.close()


def test_v014_forward_preserves_awaiting_user_run(tmp_path: Path) -> None:
    """V013→head path: a parked awaiting_user run with populated clarify
    state must survive the V014 step rebuild and the V016 run rebuild."""
    db = str(tmp_path / "v014_fwd.db")
    _apply_through(db, V013)

    clarify_values = {
        "awaiting_step_id": "collect",
        "awaiting_schema_json": '{"fields": [{"name": "city"}]}',
        "awaiting_since": 1_000_000.5,
        "awaiting_filled_json": '{"city": "Exampleville"}',
        "step_outputs_json": '{"collect": {"city": "Exampleville"}}',
        "parse_failure_count": 2,
    }
    conn = _open_conn(db)
    try:
        run = _seed_run(
            conn, "r1", status="running", session_key="agent:demo:awaiting"
        )
        step = _seed_step(conn, "r1", "s1", step_kind="agent", status="running")
        assignments = ", ".join(f"{column} = ?" for column in CLARIFY_COLUMNS)
        conn.execute(
            f"UPDATE meta_skill_runs SET status='awaiting_user', {assignments} "
            "WHERE run_id='r1'",
            tuple(clarify_values[column] for column in CLARIFY_COLUMNS),
        )
        conn.commit()
    finally:
        conn.close()

    applied = apply_pending(db, MIGRATIONS_DIR)
    assert V014 in applied
    assert V016 in applied

    conn = _open_conn(db)
    try:
        expected = dict(run)
        expected["status"] = "awaiting_user"
        _assert_row(
            conn, "meta_skill_runs", RUN_COLUMNS_V010, "run_id=?", ("r1",), expected
        )
        _assert_row(
            conn, "meta_skill_runs", CLARIFY_COLUMNS, "run_id=?", ("r1",), clarify_values
        )
        _assert_row(
            conn, "meta_skill_run_steps", STEP_COLUMNS_V010, "step_id=?", ("s1",), step
        )
        # Widened CHECK accepts user_input after the copy.
        _seed_step(conn, "r1", "s2", step_kind="user_input", status="running")
        _fk_check_clean(conn)
    finally:
        conn.close()


# ── re-run guards (apply-then-mark crash window / operator reapply) ──


def test_v013_reapply_keeps_clarify_state(tmp_path: Path) -> None:
    """Re-running V013 against an already-migrated schema must not NULL a
    parked awaiting_user run's clarify columns (that would break resume)."""
    db = str(tmp_path / "v013_reapply.db")
    apply_pending(db, MIGRATIONS_DIR)

    clarify_values = {
        "awaiting_step_id": "collect",
        "awaiting_schema_json": '{"fields": []}',
        "awaiting_since": 2_000_000.25,
        "awaiting_filled_json": "{}",
        "step_outputs_json": '{"collect": {}}',
        "parse_failure_count": 1,
    }
    conn = _open_conn(db)
    try:
        _seed_run(conn, "r1", status="running", session_key="agent:demo:parked")
        assignments = ", ".join(f"{column} = ?" for column in CLARIFY_COLUMNS)
        conn.execute(
            f"UPDATE meta_skill_runs SET status='awaiting_user', {assignments} "
            "WHERE run_id='r1'",
            tuple(clarify_values[column] for column in CLARIFY_COLUMNS),
        )
        conn.commit()
    finally:
        conn.close()

    _unmark(db, V013)
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert applied == [V013]

    conn = _open_conn(db)
    try:
        _assert_row(
            conn, "meta_skill_runs", CLARIFY_COLUMNS, "run_id=?", ("r1",), clarify_values
        )
        status = conn.execute(
            "SELECT status FROM meta_skill_runs WHERE run_id='r1'"
        ).fetchone()
        assert status == ("awaiting_user",)
    finally:
        conn.close()


def test_v014_reapply_keeps_usage_json(tmp_path: Path) -> None:
    """Re-running V014 post-V015 must not rebuild the step table with the
    pre-usage column list (that would silently drop usage_json data)."""
    db = str(tmp_path / "v014_reapply.db")
    apply_pending(db, MIGRATIONS_DIR)

    conn = _open_conn(db)
    try:
        _seed_run(conn, "r1")
        _seed_step(conn, "r1", "s1")
        conn.execute(
            "UPDATE meta_skill_run_steps SET usage_json=? WHERE step_id='s1'",
            ('{"input_tokens": 5}',),
        )
        conn.commit()
    finally:
        conn.close()

    _unmark(db, V014)
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert applied == [V014]

    conn = _open_conn(db)
    try:
        usage = conn.execute(
            "SELECT usage_json FROM meta_skill_run_steps WHERE step_id='s1'"
        ).fetchone()
        assert usage == ('{"input_tokens": 5}',)
    finally:
        conn.close()


@pytest.mark.parametrize("migration_id", [V011, V012, V016])
def test_recreate_reapply_is_noop_on_head_schema(
    tmp_path: Path, migration_id: str
) -> None:
    """Re-running the CHECK-widening recreates against the head schema must
    neither fail on column-count drift nor lose any data."""
    db = str(tmp_path / "reapply.db")
    apply_pending(db, MIGRATIONS_DIR)

    conn = _open_conn(db)
    try:
        run = _seed_run(conn, "r1")
        step = _seed_step(conn, "r1", "s1")
    finally:
        conn.close()

    _unmark(db, migration_id)
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert applied == [migration_id]

    conn = _open_conn(db)
    try:
        _assert_row(conn, "meta_skill_runs", RUN_COLUMNS_V010, "run_id=?", ("r1",), run)
        _assert_row(
            conn, "meta_skill_run_steps", STEP_COLUMNS_V010, "step_id=?", ("s1",), step
        )
        _fk_check_clean(conn)
    finally:
        conn.close()


# ── fail-loud guard against live FK enforcement ──────────────────────


def _load_module(migration_id: str):
    migrations = read_migrations(str(MIGRATIONS_DIR))
    migration = next(m for m in migrations if m.id == migration_id)
    migration.load()
    return migration.module


@pytest.mark.parametrize(
    "migration_id",
    [V011, V012, V013, V014, "V015__meta_skill_step_usage", V016],
)
def test_fk_guard_raises_when_enforcement_is_live(
    tmp_path: Path, migration_id: str
) -> None:
    """`PRAGMA foreign_keys` is a no-op inside a transaction, so a recreate
    running on an enforcement-enabled connection cannot switch it off; every
    recreate migration must refuse instead of cascade-deleting child rows."""
    db = str(tmp_path / "fk_guard.db")
    apply_pending(db, MIGRATIONS_DIR)

    module = _load_module(migration_id)
    conn = sqlite3.connect(db)
    conn.isolation_level = None  # autocommit; transactions managed manually
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        with pytest.raises(RuntimeError, match="foreign_keys"):
            module._assert_fk_enforcement_off(conn)
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_v011_recreate_invokes_fk_guard(tmp_path: Path) -> None:
    """End-to-end: the V011 apply path itself refuses on a live-FK
    connection inside a transaction (pre-V011 schema, rows present)."""
    db = str(tmp_path / "fk_guard_apply.db")
    _apply_through(db, V010_RUNS)

    conn = sqlite3.connect(db)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN")
        inserted = conn.execute(
            "INSERT INTO meta_skill_runs ("
            "run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            "triggered_by, status, started_at_ms, inputs_json"
            ") VALUES ('r1', 'synth', 'cafef00d', '{}', 'hard_takeover', "
            "'ok', 1000000, '{}')"
        )
        assert inserted.rowcount == 1
        conn.execute("COMMIT")

        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        module = _load_module(V011)
        with pytest.raises(RuntimeError, match="cascade-delete"):
            module.apply_step(conn)
        conn.execute("ROLLBACK")

        # Rows untouched by the refused rebuild.
        row = conn.execute(
            "SELECT run_id FROM meta_skill_runs WHERE run_id='r1'"
        ).fetchone()
        assert row == ("r1",)
    finally:
        conn.close()
