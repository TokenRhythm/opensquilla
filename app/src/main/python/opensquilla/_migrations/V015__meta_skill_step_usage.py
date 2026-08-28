"""V015 - persist per-step meta-skill usage summaries."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V014__meta_skill_run_steps_allow_user_input"}

_STEP_COLUMNS_WITHOUT_USAGE = (
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


def _create_steps_table_without_usage_sql(table_name: str) -> str:
    return f"""
    CREATE TABLE {table_name} (
        run_id              TEXT NOT NULL
                              REFERENCES meta_skill_runs(run_id) ON DELETE CASCADE,
        step_id             TEXT NOT NULL,
        step_kind           TEXT NOT NULL
                              CHECK(step_kind IN (
                                  'agent', 'llm_classify', 'llm_chat',
                                  'tool_call', 'skill_exec', 'user_input'
                              )),
        declared_skill      TEXT NOT NULL,
        effective_skill     TEXT NOT NULL,
        status              TEXT NOT NULL
                              CHECK(status IN ('running','ok','failed','substituted')),
        started_at_ms       INTEGER NOT NULL,
        ended_at_ms         INTEGER,
        rendered_inputs_json TEXT NOT NULL,
        output_text         TEXT,
        error               TEXT,
        substitute_step_id  TEXT,
        truncated_fields    TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (run_id, step_id)
    )
    """


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _column_exists(conn, table: str, column: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _assert_fk_enforcement_off(conn) -> None:
    """Fail loudly if foreign-key enforcement is live on this connection.

    yoyo wraps every Python step in an explicit transaction, and
    ``PRAGMA foreign_keys`` is a documented no-op inside a transaction, so
    the OFF/ON bracket in the rollback recreate below cannot take effect
    there. The rebuild has only ever been validated with SQLite's default
    foreign_keys=OFF; with enforcement live the drop-and-copy would run
    under FK semantics this migration was not designed for. Refuse loudly.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys")
    row = cur.fetchone()
    if row is not None and row[0]:
        raise RuntimeError(
            "V015: PRAGMA foreign_keys is enabled on the migration "
            "connection; the meta_skill_run_steps rebuild would run with "
            "foreign-key enforcement live (the OFF pragma below is a no-op "
            "inside yoyo's step transaction). Refusing to proceed — run "
            "migrations on a connection with foreign-key enforcement "
            "disabled."
        )


def apply_step(conn) -> None:
    if not _table_exists(conn, "meta_skill_run_steps"):
        return
    if _column_exists(conn, "meta_skill_run_steps", "usage_json"):
        return
    conn.cursor().execute(
        "ALTER TABLE meta_skill_run_steps "
        "ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}'"
    )


def rollback_step(conn) -> None:
    if not _table_exists(conn, "meta_skill_run_steps"):
        return
    if not _column_exists(conn, "meta_skill_run_steps", "usage_json"):
        return
    _assert_fk_enforcement_off(conn)
    cur = conn.cursor()
    # Inert inside yoyo's step transaction (see _assert_fk_enforcement_off);
    # kept because it is harmless and correct outside a transaction.
    cur.execute("PRAGMA foreign_keys = OFF")
    try:
        cur.execute(
            _create_steps_table_without_usage_sql("meta_skill_run_steps__new")
        )
        columns = ", ".join(_STEP_COLUMNS_WITHOUT_USAGE)
        cur.execute(
            f"INSERT INTO meta_skill_run_steps__new ({columns}) "
            f"SELECT {columns} FROM meta_skill_run_steps"
        )
        cur.execute("DROP TABLE meta_skill_run_steps")
        cur.execute(
            "ALTER TABLE meta_skill_run_steps__new RENAME TO meta_skill_run_steps"
        )
        cur.execute(
            "CREATE INDEX idx_meta_run_steps_status"
            " ON meta_skill_run_steps(status)"
        )
        cur.execute("PRAGMA foreign_key_check")
        bad = cur.fetchall()
        if bad:
            raise RuntimeError(
                f"V015 foreign_key_check found orphans after rollback: {bad}"
            )
    finally:
        cur.execute("PRAGMA foreign_keys = ON")


steps = [step(apply_step, rollback_step)]
