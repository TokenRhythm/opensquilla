"""V023 - requested versus executed Router deployment telemetry.

Adds token-only columns to the existing ``router_decisions`` observability
table.  They distinguish the deployment selected by routing from the one
that actually executed after credential-resolution vetoes or provider
fallbacks.  No prompt/error free text is stored: ``fallback_reason`` is a
bounded enum-like token and the writer sanitizes every new column.

The legacy ``provider``/``model`` columns remain unchanged for existing
readers.  Rollback removes only these additive columns.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V022__telemetry_daily_usage"}

TABLE = "router_decisions"
COLUMNS: tuple[tuple[str, str], ...] = (
    ("requested_provider", "TEXT"),
    ("requested_model", "TEXT"),
    ("executed_provider", "TEXT"),
    ("executed_model", "TEXT"),
    ("fallback_reason", "TEXT"),
)


def _table_columns(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({TABLE})")
    return {str(row[1]) for row in cur.fetchall()}


def apply_step(conn) -> None:
    cur = conn.cursor()
    existing = _table_columns(conn)
    if not existing:
        return
    for column, ddl in COLUMNS:
        if column not in existing:
            cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} {ddl}")


def rollback_step(conn) -> None:
    cur = conn.cursor()
    existing = _table_columns(conn)
    for column, _ddl in reversed(COLUMNS):
        if column in existing:
            cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN {column}")


steps = [step(apply_step, rollback_step)]
