"""Retain immutable usage owners and task grouping after session deletion."""

from yoyo import step

__depends__: set[str] = {"V049__usage_metering_settlements"}

# Frozen DDL; never import runtime schema into an already released migration.
USAGE_ATTRIBUTION_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS usage_attribution_owners (
        session_id TEXT NOT NULL, session_epoch INTEGER NOT NULL,
        task_group_id TEXT NOT NULL, root_session_id TEXT NOT NULL,
        root_session_epoch INTEGER NOT NULL,
        parent_session_id TEXT, parent_session_epoch INTEGER,
        session_kind TEXT NOT NULL, branch_kind TEXT NOT NULL,
        title TEXT NOT NULL, attribution_status TEXT NOT NULL,
        PRIMARY KEY (session_id, session_epoch)
    )""",
    """CREATE TABLE IF NOT EXISTS usage_attribution_runs (
        execution_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        session_epoch INTEGER NOT NULL, task_group_id TEXT NOT NULL,
        parent_execution_id TEXT, relation TEXT NOT NULL,
        attribution_status TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_usage_attribution_runs_group
        ON usage_attribution_runs(task_group_id, execution_id)""",
) + tuple(
    f"""CREATE TRIGGER IF NOT EXISTS usage_revision_{table}_{operation.lower()}
        AFTER {operation} ON {table} BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END"""
    for table in ("usage_attribution_owners", "usage_attribution_runs")
    for operation in ("INSERT", "UPDATE", "DELETE")
)


def apply_step(conn) -> None:
    for statement in USAGE_ATTRIBUTION_SCHEMA:
        conn.execute(statement)


def rollback_step(conn) -> None:
    if any(
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        for table in ("usage_attribution_runs", "usage_attribution_owners")
    ):
        raise RuntimeError("Cannot discard durable usage attribution during rollback")
    conn.execute("DROP TABLE IF EXISTS usage_attribution_runs")
    conn.execute("DROP TABLE IF EXISTS usage_attribution_owners")


steps = [step(apply_step, rollback_step)]
