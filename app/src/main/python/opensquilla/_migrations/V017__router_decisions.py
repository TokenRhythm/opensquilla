"""V017 - per-turn router decision records.

Creates ``router_decisions``: one row per routed user message, written
best-effort by ``persistence/router_decision_writer.py``. The table is the
durable source for the router's sticky/anti-downgrade history
(``RoutingHistoryStore`` is rehydrated from it at gateway boot) and for
operator audit of routing behavior.

Privacy contract: the table has **no free-text columns**. ``probs`` is a JSON
array of numbers; ``flags`` is a JSON array of enum tokens; ``trail`` is a
JSON array of stage entries containing only enum tokens, booleans, and
numbers. Prompt text never reaches this table — the writer sanitizes every
JSON column and ``tests/test_persistence/test_router_decision_writer.py``
enforces the bar.

Downgrade story (SchemaAheadError, refusal-by-design): boot only migrates
forward. An older build whose migration set does not include V017 refuses to
boot against a database that records it (``assert_schema_not_ahead`` in
``persistence/migrator.py``). Operators who must run such a build first roll
back with yoyo, which invokes this module's ``rollback_step`` (drops the
index and the table) and removes V017 from the ledger.

The ``session_key`` column is intentionally not FK-protected: the ``sessions``
table is created lazily by ``SessionStorage.connect()`` rather than by yoyo
(V001 docstring precedent). Cleanup is the explicit purge in
``SessionStorage.delete_session`` plus the writer's write-time retention
pruning.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V016__meta_skill_runs_triggered_by_manual_command"}


CREATE_ROUTER_DECISIONS = """
CREATE TABLE router_decisions (
    decision_id      TEXT PRIMARY KEY,
    session_key      TEXT NOT NULL,
    turn_index       INTEGER,
    ts_ms            INTEGER NOT NULL,
    classifier       TEXT,
    proposed_tier    TEXT,
    confidence       REAL,
    probs            TEXT,
    flags            TEXT,
    final_tier       TEXT,
    provider         TEXT,
    model            TEXT,
    thinking_level   TEXT,
    source           TEXT,
    trail            TEXT,
    baseline_model   TEXT,
    savings_pct      REAL,
    executed_kind    TEXT
                       CHECK(executed_kind IN ('single', 'ensemble')),
    ensemble_profile TEXT,
    fallback_hops    INTEGER NOT NULL DEFAULT 0
)
"""

CREATE_INDEX = (
    "CREATE INDEX idx_router_decisions_session_ts"
    " ON router_decisions(session_key, ts_ms)"
)


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def apply_step(conn) -> None:
    if _table_exists(conn, "router_decisions"):
        return
    cur = conn.cursor()
    cur.execute(CREATE_ROUTER_DECISIONS)
    cur.execute(CREATE_INDEX)


def rollback_step(conn) -> None:
    # Rollback drops the index and the table (V010 precedent). Decision
    # records are observability, not conversation state — dropping them is
    # safe; the in-process RoutingHistoryStore simply starts cold again.
    cur = conn.cursor()
    cur.execute("DROP INDEX IF EXISTS idx_router_decisions_session_ts")
    cur.execute("DROP TABLE IF EXISTS router_decisions")


steps = [step(apply_step, rollback_step)]
