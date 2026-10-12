"""V012 - allow llm_chat meta-skill steps in the audit ledger.

V010 created ``meta_skill_run_steps.step_kind`` before the runtime added
``llm_chat`` as a first-class step kind. FULL_GATED meta-skill-creator runs
now use ``llm_chat`` for the single-model baseline and acceptance comparison,
so the writer must be able to persist those steps.

SQLite cannot alter a CHECK constraint in place, so recreate the step table
with the expanded allowed-value set.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V011__meta_skill_runs_triggered_by_auto"}


_NEW_STEP_KIND_VALUES = (
    "'agent'",
    "'llm_classify'",
    "'llm_chat'",
    "'tool_call'",
    "'skill_exec'",
)
_OLD_STEP_KIND_VALUES = (
    "'agent'",
    "'llm_classify'",
    "'tool_call'",
    "'skill_exec'",
)


def _create_steps_table_sql(
    step_kind_values: tuple[str, ...],
    table_name: str = "meta_skill_run_steps",
) -> str:
    return f"""
    CREATE TABLE {table_name} (
        run_id              TEXT NOT NULL
                              REFERENCES meta_skill_runs(run_id) ON DELETE CASCADE,
        step_id             TEXT NOT NULL,
        step_kind           TEXT NOT NULL
                              CHECK(step_kind IN ({", ".join(step_kind_values)})),
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
    rebuild has only ever been validated with SQLite's default
    foreign_keys=OFF; with enforcement live the drop-and-copy would run
    under FK semantics this migration was not designed for. Refuse loudly.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys")
    row = cur.fetchone()
    if row is not None and row[0]:
        raise RuntimeError(
            "V012: PRAGMA foreign_keys is enabled on the migration "
            "connection; the meta_skill_run_steps rebuild would run with "
            "foreign-key enforcement live (the OFF pragma below is a no-op "
            "inside yoyo's step transaction). Refusing to proceed — run "
            "migrations on a connection with foreign-key enforcement "
            "disabled."
        )


def _recreate_steps_table(conn, step_kind_values: tuple[str, ...]) -> None:
    _assert_fk_enforcement_off(conn)
    cur = conn.cursor()
    # Inert inside yoyo's step transaction (see _assert_fk_enforcement_off);
    # kept because it is harmless and correct outside a transaction.
    cur.execute("PRAGMA foreign_keys = OFF")
    try:
        cur.execute(_create_steps_table_sql(step_kind_values, "meta_skill_run_steps__new"))
        cur.execute(
            "INSERT INTO meta_skill_run_steps__new SELECT * FROM meta_skill_run_steps"
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
                f"V012 foreign_key_check found orphans after recreate: {bad}"
            )
    finally:
        cur.execute("PRAGMA foreign_keys = ON")


def apply_step(conn) -> None:
    if not _table_exists(conn, "meta_skill_run_steps"):
        return
    if "llm_chat" in _table_sql(conn, "meta_skill_run_steps"):
        # Re-run guard (yoyo's apply-then-mark crash window, or operator
        # reapply): the relaxed CHECK is already in place. Re-running the
        # recreate against a later schema (V015 added usage_json) would
        # fail on the column-count mismatch — skip instead.
        return
    _recreate_steps_table(conn, _NEW_STEP_KIND_VALUES)


def rollback_step(conn) -> None:
    if not _table_exists(conn, "meta_skill_run_steps"):
        return
    _recreate_steps_table(conn, _OLD_STEP_KIND_VALUES)


steps = [step(apply_step, rollback_step)]
