"""V019 - durable per-turn error records.

Creates ``turn_errors``: one row per failed turn, written best-effort by
``persistence/turn_error_writer.py`` from the turn-loop catch-all. Each row
carries the short ``error_id`` shown to users as ``(ref: <error_id>)`` in
error messages, so a bug report quoting a ref joins directly to the sanitized
message and redacted traceback here.

Privacy contract: unlike V017's no-free-text bar, ``message`` and
``traceback`` are deliberately free-text — that is the diagnostic point of
the table. Both are sanitized before insert: ``message`` is the
``sanitize_agent_error`` output and ``traceback`` passes
``observability.redact.scrub_text`` (secret-shaped values masked, home
directory normalized to ``~``). ``tests/test_persistence/test_turn_error_writer.py``
enforces the scrub bar. Retention is write-time pruning at 30 days.

Downgrade story (SchemaAheadError, refusal-by-design): boot only migrates
forward. An older build refuses to boot against a database recording V019
(``assert_schema_not_ahead``); operators roll back with yoyo, which invokes
``rollback_step`` (drops the index and the table) — error records are
observability, not conversation state, so dropping them is safe.

The ``session_key`` column is intentionally not FK-protected: the ``sessions``
table is created lazily by ``SessionStorage.connect()`` rather than by yoyo
(V001/V017 precedent). Cleanup is the explicit purge in
``SessionStorage.delete_session`` plus write-time retention pruning.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V018__router_decisions_ts_index"}


CREATE_TURN_ERRORS = """
CREATE TABLE turn_errors (
    error_id      TEXT PRIMARY KEY,
    turn_id       TEXT,
    session_key   TEXT NOT NULL,
    session_id    TEXT,
    ts_ms         INTEGER NOT NULL,
    surface       TEXT,
    error_class   TEXT,
    message       TEXT,
    traceback     TEXT,
    provider      TEXT,
    model         TEXT,
    fallback_hops INTEGER NOT NULL DEFAULT 0
)
"""

CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_turn_errors_session_ts"
    " ON turn_errors(session_key, ts_ms)"
)


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def apply_step(conn) -> None:
    cur = conn.cursor()
    if not _table_exists(conn, "turn_errors"):
        cur.execute(CREATE_TURN_ERRORS)
    # Outside the table guard so a pre-existing table (created out-of-band)
    # still gains the index; IF NOT EXISTS keeps the step idempotent.
    cur.execute(CREATE_INDEX)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute("DROP INDEX IF EXISTS idx_turn_errors_session_ts")
    cur.execute("DROP TABLE IF EXISTS turn_errors")


steps = [step(apply_step, rollback_step)]
