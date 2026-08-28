"""V010 - meta-skill execution audit ledger.

Creates two tables to support G4 traceable and auditable meta-skill runs:

* ``meta_skill_runs`` — one row per orchestrator invocation. Captures the
  plan snapshot (so historical replays reproduce the original plan even
  after the meta-skill spec is updated), digest, trigger source, owner pid,
  redacted inputs, final outcome.
* ``meta_skill_run_steps`` — one row per step. Step status carries
  ``substituted`` to record on_failure invocations distinctly from
  ``failed``/``ok``.

Cross-row CASCADE on ``run_id`` requires ``PRAGMA foreign_keys=ON`` on each
connection (see ``MetaRunWriter``). The cross-table ``session_key`` is not
FK-protected because the ``sessions`` table is created lazily by
``SessionStorage.connect()`` rather than by yoyo (V001 docstring).
Privacy cleanup is via the writer's ``purge_for_session(session_key)``.

Rollback drops both tables and indexes.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V009__transcript_reasoning_content"}


CREATE_RUNS = """
CREATE TABLE meta_skill_runs (
    run_id              TEXT PRIMARY KEY,
    meta_skill_name     TEXT NOT NULL,
    meta_skill_digest   TEXT NOT NULL,
    plan_snapshot_json  TEXT NOT NULL,
    triggered_by        TEXT NOT NULL
                          CHECK(triggered_by IN ('hard_takeover','soft_meta_invoke')),
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

CREATE_STEPS = """
CREATE TABLE meta_skill_run_steps (
    run_id              TEXT NOT NULL
                          REFERENCES meta_skill_runs(run_id) ON DELETE CASCADE,
    step_id             TEXT NOT NULL,
    step_kind           TEXT NOT NULL
                          CHECK(step_kind IN ('agent','llm_classify','tool_call','skill_exec')),
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

INDEXES = [
    "CREATE INDEX idx_meta_runs_name_started"
    " ON meta_skill_runs(meta_skill_name, started_at_ms DESC)",
    "CREATE INDEX idx_meta_runs_status_started"
    " ON meta_skill_runs(status, started_at_ms DESC)",
    "CREATE INDEX idx_meta_runs_session"
    " ON meta_skill_runs(session_key, started_at_ms DESC)",
    "CREATE INDEX idx_meta_runs_started"
    " ON meta_skill_runs(started_at_ms DESC)",
    "CREATE INDEX idx_meta_run_steps_status"
    " ON meta_skill_run_steps(status)",
]


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def apply_step(conn) -> None:
    if _table_exists(conn, "meta_skill_runs"):
        return
    cur = conn.cursor()
    cur.execute(CREATE_RUNS)
    cur.execute(CREATE_STEPS)
    for ddl in INDEXES:
        cur.execute(ddl)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS meta_skill_run_steps")
    cur.execute("DROP TABLE IF EXISTS meta_skill_runs")


steps = [step(apply_step, rollback_step)]
