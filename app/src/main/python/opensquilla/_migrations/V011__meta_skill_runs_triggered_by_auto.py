"""V011 - relax meta_skill_runs.triggered_by CHECK to accept auto_* values.

V010 originally restricted ``triggered_by`` to
``('hard_takeover','soft_meta_invoke')``. Auto-trigger paths (cron
+ dream-loop hook) need to record runs as ``'auto_cron'`` or
``'auto_dream'`` so the WebUI proposals panel and ``opensquilla skills
meta runs`` CLI can distinguish unattended synthesis from user-driven
invocations.

SQLite cannot ``ALTER`` a CHECK constraint in place. We follow the
SQLite-recommended recipe
(https://www.sqlite.org/lang_altertable.html section 7) which avoids
breaking the child-table foreign-key references that point at
``meta_skill_runs``:

1. CREATE TABLE meta_skill_runs__new with the relaxed CHECK
2. INSERT INTO meta_skill_runs__new SELECT * FROM meta_skill_runs
3. DROP TABLE meta_skill_runs
4. ALTER TABLE meta_skill_runs__new RENAME TO meta_skill_runs
5. recreate indexes
6. PRAGMA foreign_key_check (verify no orphans introduced)

Renaming the NEW table (step 4) is safe; renaming the OLD table is
what corrupts child FK refs and was the original bug.

Note on ``PRAGMA foreign_keys``: yoyo wraps every Python step in an
explicit transaction, and ``PRAGMA foreign_keys`` is a documented
no-op inside a transaction — so the OFF/ON bracket around the rebuild
does NOT take effect here. Child rows survive the parent DROP only
because SQLite ships with foreign-key enforcement off by default on
the migration connection. The recreate helper therefore checks
``PRAGMA foreign_keys`` first and refuses to run if enforcement is
live (the DROP would otherwise cascade-delete every step row while
``foreign_key_check`` still passed). The OFF/ON pragmas are kept:
they are harmless, and correct if the step ever runs outside a
transaction.

Rollback restores the original (stricter) CHECK; rows whose
``triggered_by`` already contains ``auto_*`` would block the rollback
copy step, which is the correct safety behavior — operators who
roll back must first purge auto-triggered runs they no longer want.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V010__meta_skill_runs"}


_NEW_TRIGGERED_BY_VALUES = (
    "'hard_takeover'",
    "'soft_meta_invoke'",
    "'auto_cron'",
    "'auto_dream'",
)
_OLD_TRIGGERED_BY_VALUES = (
    "'hard_takeover'",
    "'soft_meta_invoke'",
)


def _create_table_sql(
    triggered_by_values: tuple[str, ...],
    table_name: str = "meta_skill_runs",
) -> str:
    return f"""
    CREATE TABLE {table_name} (
        run_id              TEXT PRIMARY KEY,
        meta_skill_name     TEXT NOT NULL,
        meta_skill_digest   TEXT NOT NULL,
        plan_snapshot_json  TEXT NOT NULL,
        triggered_by        TEXT NOT NULL
                              CHECK(triggered_by IN ({", ".join(triggered_by_values)})),
        session_key         TEXT,
        turn_id             TEXT,
        owner_pid           INTEGER,
        status              TEXT NOT NULL
                              CHECK(status IN ('running','ok','failed','cancelled')),
        started_at_ms       INTEGER NOT NULL,
        ended_at_ms         INTEGER,
        inputs_json         TEXT NOT NULL,
        final_text          TEXT,
        failed_step_id      TEXT,
        error               TEXT,
        truncated_fields    TEXT NOT NULL DEFAULT ''
    )
    """


_INDEXES = [
    "CREATE INDEX idx_meta_runs_name_started"
    " ON meta_skill_runs(meta_skill_name, started_at_ms DESC)",
    "CREATE INDEX idx_meta_runs_status_started"
    " ON meta_skill_runs(status, started_at_ms DESC)",
    "CREATE INDEX idx_meta_runs_session"
    " ON meta_skill_runs(session_key, started_at_ms DESC)",
    "CREATE INDEX idx_meta_runs_started"
    " ON meta_skill_runs(started_at_ms DESC)",
]


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
            "V011: PRAGMA foreign_keys is enabled on the migration "
            "connection; rebuilding meta_skill_runs would cascade-delete "
            "its meta_skill_run_steps child rows. Refusing to proceed — "
            "run migrations on a connection with foreign-key enforcement "
            "disabled."
        )


def _recreate_runs_table(conn, triggered_by_values: tuple[str, ...]) -> None:
    """Follow SQLite's recommended recreate procedure (section 7 of
    lang_altertable.html). Build the NEW table under a temporary name,
    copy rows, drop the OLD table, then rename NEW to the canonical
    name. This order keeps child-table FK references intact — renaming
    the OLD table first would orphan them."""
    _assert_fk_enforcement_off(conn)
    cur = conn.cursor()
    # Inert inside yoyo's step transaction (see _assert_fk_enforcement_off);
    # kept because it is harmless and correct outside a transaction.
    cur.execute("PRAGMA foreign_keys = OFF")
    try:
        cur.execute(_create_table_sql(triggered_by_values, "meta_skill_runs__new"))
        cur.execute(
            "INSERT INTO meta_skill_runs__new SELECT * FROM meta_skill_runs"
        )
        cur.execute("DROP TABLE meta_skill_runs")
        cur.execute("ALTER TABLE meta_skill_runs__new RENAME TO meta_skill_runs")
        for ddl in _INDEXES:
            cur.execute(ddl)
        # Defensive: catch any orphan child rows the recreate would have
        # introduced. If this fails, the surrounding transaction rolls
        # back and the migration aborts cleanly.
        cur.execute("PRAGMA foreign_key_check")
        bad = cur.fetchall()
        if bad:
            raise RuntimeError(
                f"V011 foreign_key_check found orphans after recreate: {bad}"
            )
    finally:
        cur.execute("PRAGMA foreign_keys = ON")


def apply_step(conn) -> None:
    if not _table_exists(conn, "meta_skill_runs"):
        # V010 hasn't been applied — nothing to relax. yoyo's __depends__
        # should prevent this, but be defensive against manual rollbacks.
        return
    if "auto_cron" in _table_sql(conn, "meta_skill_runs"):
        # Re-run guard (yoyo's apply-then-mark crash window, or operator
        # reapply): the relaxed CHECK is already in place. Re-running the
        # recreate against a later schema (V013 added six clarify columns)
        # would fail on the column-count mismatch — skip instead.
        return
    _recreate_runs_table(conn, _NEW_TRIGGERED_BY_VALUES)


def rollback_step(conn) -> None:
    if not _table_exists(conn, "meta_skill_runs"):
        return
    _recreate_runs_table(conn, _OLD_TRIGGERED_BY_VALUES)


steps = [step(apply_step, rollback_step)]
