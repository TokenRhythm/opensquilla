"""V030 - goal run failure retry ledger (backoff, pause reason, last error)."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V029__goal_runs"}

ADD_FAILURE_RETRIES = """
ALTER TABLE goal_runs ADD COLUMN failure_retries INTEGER NOT NULL DEFAULT 0
"""

ADD_NEXT_RETRY_AT_MS = """
ALTER TABLE goal_runs ADD COLUMN next_retry_at_ms INTEGER
"""

ADD_PAUSE_REASON = """
ALTER TABLE goal_runs ADD COLUMN pause_reason TEXT
"""

ADD_LAST_ERROR = """
ALTER TABLE goal_runs ADD COLUMN last_error TEXT
"""


def apply_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(ADD_FAILURE_RETRIES)
    cur.execute(ADD_NEXT_RETRY_AT_MS)
    cur.execute(ADD_PAUSE_REASON)
    cur.execute(ADD_LAST_ERROR)


def rollback_step(conn) -> None:
    # SQLite supports DROP COLUMN since 3.35; the migration is additive and the
    # rollback is best-effort for fresh test databases.
    cur = conn.cursor()
    for column in ("last_error", "pause_reason", "next_retry_at_ms", "failure_retries"):
        try:
            cur.execute(f"ALTER TABLE goal_runs DROP COLUMN {column}")
        except Exception:  # noqa: BLE001 - best-effort rollback
            pass


steps = [step(apply_step, rollback_step)]
