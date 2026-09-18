"""V045 - attribute physical usage to Goals without rewriting historical bills."""

from yoyo import step

__depends__ = {"V044__retire_legacy_memory_flush"}


def apply_step(conn) -> None:
    additions = {
        "usage_events": {
            "root_turn_id": "TEXT",
            "goal_id": "TEXT",
        },
        "session_goals": {
            "token_budget": "INTEGER CHECK (token_budget IS NULL OR token_budget > 0)",
            "budget_tokens_used": "INTEGER NOT NULL DEFAULT 0",
            # Existing totals only covered the owner's finalized calls at settlement.
            # They cannot be used to promise a complete lifetime token budget.
            "usage_accounting_version": "INTEGER NOT NULL DEFAULT 0",
            "usage_coverage": "TEXT NOT NULL DEFAULT 'partial_history'",
            "usage_accounting_started_at_ms": "INTEGER",
            "background": "INTEGER NOT NULL DEFAULT 0",
        },
    }
    for table, columns in additions.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_events_goal "
        "ON usage_events(goal_id) WHERE goal_id IS NOT NULL"
    )


def rollback_step(conn) -> None:
    # Forward-only: attribution is audit data and must survive an app rollback.
    pass


steps = [step(apply_step, rollback_step)]
