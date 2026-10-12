"""V018 - standalone ts_ms index on router_decisions.

The retention prune (``DELETE ... WHERE ts_ms < ?``, run every 64th
insert), the boot rehydration window scan (``WHERE ts_ms >= ?``), and the
unfiltered operator listing (``ORDER BY ts_ms DESC``) in
``persistence/router_decision_writer.py`` all filter or order on bare
``ts_ms``. The only index V017 shipped is ``(session_key, ts_ms)``, whose
leading column makes it useless for those queries, so each of them runs
as a full table scan. A standalone ``ts_ms`` index turns all three into
index scans.

This migration also deliberately depends on ``V010__transcript_turn_usage``:
that migration was a leaf nothing else depended on, so dependency-aware
partial-apply tooling could skip it while still reaching the head of the
chain. Naming it here reconnects it to the dependency graph head.

Downgrade story: rollback drops the index only (the table and its data are
untouched); queries fall back to full scans, which is slower but correct.
An older build whose migration set does not include V018 refuses to boot
against a database that records it (``assert_schema_not_ahead`` in
``persistence/migrator.py``) — operators roll back with yoyo first, which
invokes this module's ``rollback_step`` and removes V018 from the ledger.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V017__router_decisions", "V010__transcript_turn_usage"}


CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_router_decisions_ts"
    " ON router_decisions(ts_ms)"
)


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def apply_step(conn) -> None:
    if not _table_exists(conn, "router_decisions"):
        # V017 creates the table; be defensive against manual rollbacks
        # that removed it out from under the ledger.
        return
    cur = conn.cursor()
    cur.execute(CREATE_INDEX)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute("DROP INDEX IF EXISTS idx_router_decisions_ts")


steps = [step(apply_step, rollback_step)]
