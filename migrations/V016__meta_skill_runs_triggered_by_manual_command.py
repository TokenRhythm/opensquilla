"""V016 - allow manual /meta launches in meta_skill_runs.triggered_by.

The manual command path constructs MetaOrchestrator with
``triggered_by='manual_command'``. V011 relaxed the original CHECK
constraint for auto triggers, but did not include this explicit manual
source. SQLite requires the usual recreate-and-copy flow to widen a CHECK.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V015__meta_skill_step_usage"}


_NEW_TRIGGERED_BY_VALUES = (
    "'hard_takeover'",
    "'soft_meta_invoke'",
    "'auto_cron'",
    "'auto_dream'",
    "'manual_command'",
)
_OLD_TRIGGERED_BY_VALUES = (
    "'hard_takeover'",
    "'soft_meta_invoke'",
    "'auto_cron'",
    "'auto_dream'",
)

_STATUS_VALUES = (
    "'running'",
    "'ok'",
    "'failed'",
    "'cancelled'",
    "'awaiting_user'",
    "'expired'",
)

_RUN_COLUMNS = (
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
    "awaiting_step_id",
    "awaiting_schema_json",
    "awaiting_since",
    "awaiting_filled_json",
    "step_outputs_json",
    "parse_failure_count",
)

_INDEXES = (
    "CREATE INDEX idx_meta_runs_name_started "
    "ON meta_skill_runs(meta_skill_name, started_at_ms DESC)",
    "CREATE INDEX idx_meta_runs_status_started "
    "ON meta_skill_runs(status, started_at_ms DESC)",
    "CREATE INDEX idx_meta_runs_session "
    "ON meta_skill_runs(session_key, started_at_ms DESC)",
    "CREATE INDEX idx_meta_runs_started "
    "ON meta_skill_runs(started_at_ms DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_one_awaiting_per_session "
    "ON meta_skill_runs(session_key) "
    "WHERE status = 'awaiting_user'",
)


def _create_table_sql(
    triggered_by_values: tuple[str, ...],
    table_name: str,
) -> str:
    return f"""
    CREATE TABLE {table_name} (
        run_id                 TEXT PRIMARY KEY,
        meta_skill_name        TEXT NOT NULL,
        meta_skill_digest      TEXT NOT NULL,
        plan_snapshot_json     TEXT NOT NULL,
        triggered_by           TEXT NOT NULL
                                 CHECK(triggered_by IN (
                                     {", ".join(triggered_by_values)}
                                 )),
        session_key            TEXT,
        turn_id                TEXT,
        owner_pid              INTEGER,
        status                 TEXT NOT NULL
                                 CHECK(status IN ({", ".join(_STATUS_VALUES)})),
        started_at_ms          INTEGER NOT NULL,
        ended_at_ms            INTEGER,
        inputs_json            TEXT NOT NULL,
        final_text             TEXT,
        failed_step_id         TEXT,
        error                  TEXT,
        truncated_fields       TEXT NOT NULL DEFAULT '',
        awaiting_step_id       TEXT,
        awaiting_schema_json   TEXT,
        awaiting_since         REAL,
        awaiting_filled_json   TEXT,
        step_outputs_json      TEXT,
        parse_failure_count    INTEGER NOT NULL DEFAULT 0
    )
    """


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _table_sql(conn, table: str) -> str:
    cur = conn.cursor()
    cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else ""


def _assert_fk_enforcement_off(conn) -> None:
    """Fail loudly if foreign-key enforcement is live on this connection.

    yoyo wraps every Python step in an explicit transaction, and
    ``PRAGMA foreign_keys`` is a documented no-op inside a transaction, so
    the OFF/ON bracket in the recreate below cannot take effect there. The
    rebuild is only safe because SQLite defaults foreign_keys to OFF: with
    enforcement enabled (e.g. an SQLITE_DEFAULT_FOREIGN_KEYS=1 build),
    ``DROP TABLE meta_skill_runs`` would run an implicit DELETE that
    CASCADE-wipes every meta_skill_run_steps row — and the subsequent
    ``foreign_key_check`` would still pass. Refuse instead of losing data.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys")
    row = cur.fetchone()
    if row is not None and row[0]:
        raise RuntimeError(
            "V016: PRAGMA foreign_keys is enabled on the migration "
            "connection; rebuilding meta_skill_runs would cascade-delete "
            "its meta_skill_run_steps child rows. Refusing to proceed — "
            "run migrations on a connection with foreign-key enforcement "
            "disabled."
        )


def _manual_command_count(conn) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM meta_skill_runs WHERE triggered_by='manual_command'"
    )
    return int(cur.fetchone()[0] or 0)


def _recreate_runs_table(conn, triggered_by_values: tuple[str, ...]) -> None:
    _assert_fk_enforcement_off(conn)
    cur = conn.cursor()
    # Inert inside yoyo's step transaction (see _assert_fk_enforcement_off);
    # kept because it is harmless and correct outside a transaction.
    cur.execute("PRAGMA foreign_keys = OFF")
    try:
        cur.execute(_create_table_sql(triggered_by_values, "meta_skill_runs__new"))
        columns = ", ".join(_RUN_COLUMNS)
        cur.execute(
            f"INSERT INTO meta_skill_runs__new ({columns}) "
            f"SELECT {columns} FROM meta_skill_runs"
        )
        cur.execute("DROP TABLE meta_skill_runs")
        cur.execute("ALTER TABLE meta_skill_runs__new RENAME TO meta_skill_runs")
        for idx_sql in _INDEXES:
            cur.execute(idx_sql)
        cur.execute("PRAGMA foreign_key_check")
        bad = cur.fetchall()
        if bad:
            raise RuntimeError(
                f"V016 foreign_key_check found orphans after recreate: {bad}"
            )
    finally:
        cur.execute("PRAGMA foreign_keys = ON")


def apply_step(conn) -> None:
    if not _table_exists(conn, "meta_skill_runs"):
        return
    if "manual_command" in _table_sql(conn, "meta_skill_runs"):
        # Re-run guard (yoyo's apply-then-mark crash window, or operator
        # reapply): the relaxed CHECK is already in place. The recreate is
        # idempotent in effect today, but any future migration that adds a
        # column would turn a re-run into silent column loss — skip instead.
        return
    _recreate_runs_table(conn, _NEW_TRIGGERED_BY_VALUES)


def rollback_step(conn) -> None:
    if not _table_exists(conn, "meta_skill_runs"):
        return
    manual_count = _manual_command_count(conn)
    if manual_count:
        raise RuntimeError(
            "V016 rollback blocked: meta_skill_runs contains "
            f"{manual_count} manual_command row(s). Remove or archive those rows "
            "before rolling back to the pre-manual-command CHECK constraint."
        )
    _recreate_runs_table(conn, _OLD_TRIGGERED_BY_VALUES)


steps = [step(apply_step, rollback_step)]
