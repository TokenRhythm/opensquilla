"""Fresh-install vs upgraded-legacy schema convergence for the session DB.

The sessions/transcript schema has two sources of truth: SessionStorage's
``CREATE TABLE IF NOT EXISTS`` DDL (fresh databases,
``src/opensquilla/session/storage.py``) and the yoyo ALTER-based migrations
under ``migrations/`` (upgraded databases). Nothing else pins that the two
paths converge, which is exactly how upgrade-only breakage ships: a column
added to the fresh DDL but not to a migration (or a connect-time shim)
works on every developer machine and fails only on real users' aged
databases. This test boots both paths against the real migrations directory
and asserts the resulting shapes are identical.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from opensquilla.persistence.migrator import apply_pending
from opensquilla.session.storage import SessionStorage

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

TABLES = (
    "sessions",
    "transcript_entries",
    "session_summaries",
    "fixed_four_tier_states",
    "fixed_four_tier_request_claims",
    "fixed_four_tier_decisions",
    "usage_events",
    "usage_event_items",
    "usage_item_billing_receipts",
    "usage_billing_receipt_state",
    "usage_ledger_state",
    "usage_legacy_baselines",
)

FIXED_FOUR_TIER_TABLES = (
    "fixed_four_tier_states",
    "fixed_four_tier_request_claims",
    "fixed_four_tier_decisions",
)

# Synthetic approximation of the oldest supported on-disk shape. It is the
# current storage.py DDL with the columns REMOVED that yoyo migrations add
# on the upgrade path:
#   - V007__session_cost_source_rollup: total_cost_usd, billed_cost_usd,
#     estimated_cost_component_usd, cost_source, missing_cost_entries
#     (sessions)
#   - V009__transcript_reasoning_content: reasoning_content
#     (transcript_entries)
#   - V010__transcript_turn_usage: turn_usage (transcript_entries)
# session_summaries is not mutated by any yoyo migration (its later columns
# arrive via SessionStorage connect-time shims), so its legacy DDL matches
# the current one; it is still compared below to catch a future one-sided
# edit. Synthetic fixture only — no real session data.
LEGACY_SCHEMA = """
CREATE TABLE sessions (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    ended_at INTEGER,
    runtime_ms INTEGER,
    last_channel TEXT,
    last_to TEXT,
    last_account_id TEXT,
    last_thread_id TEXT,
    delivery_context TEXT,
    model TEXT,
    model_provider TEXT,
    provider_override TEXT,
    model_override TEXT,
    auth_profile_override TEXT,
    auth_profile_override_source TEXT,
    context_tokens INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens_fresh INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    compaction_count INTEGER NOT NULL DEFAULT 0,
    session_file TEXT,
    spawned_by TEXT,
    parent_session_key TEXT,
    forked_from_parent INTEGER NOT NULL DEFAULT 0,
    spawn_depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    chat_type TEXT NOT NULL DEFAULT 'unknown',
    thinking_level TEXT,
    fast_mode INTEGER NOT NULL DEFAULT 0,
    verbose_level TEXT,
    reasoning_level TEXT,
    send_policy TEXT NOT NULL DEFAULT 'allow',
    queue_mode TEXT NOT NULL DEFAULT 'steer',
    label TEXT,
    display_name TEXT,
    derived_title TEXT,
    channel TEXT,
    group_id TEXT,
    subject TEXT,
    origin TEXT,
    agent_id TEXT NOT NULL DEFAULT 'main',
    schema_version INTEGER NOT NULL DEFAULT 1,
    epoch INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE transcript_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    created_at INTEGER NOT NULL,
    token_count INTEGER,
    provenance_kind TEXT,
    provenance_origin_session_id TEXT,
    provenance_source_session_key TEXT,
    provenance_source_channel TEXT,
    provenance_source_tool TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    compaction_index INTEGER NOT NULL DEFAULT 0,
    compaction_id TEXT,
    trigger_reason TEXT,
    summary_text TEXT NOT NULL,
    summary_payload TEXT,
    summary_format TEXT NOT NULL DEFAULT 'text',
    summary_source TEXT NOT NULL DEFAULT 'unknown',
    coverage_status TEXT NOT NULL DEFAULT 'unknown',
    missing_obligations TEXT,
    critical_carry_forward TEXT,
    tokens_before INTEGER,
    tokens_after INTEGER,
    removed_count INTEGER NOT NULL DEFAULT 0,
    kept_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    flush_receipt_status TEXT NOT NULL DEFAULT 'unknown',
    covered_through_id INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);
"""


def _table_shape(db_path: Path, table: str) -> dict[str, str | None]:
    """{column name: default} from PRAGMA table_info (order-independent)."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    # Row layout: (cid, name, type, notnull, dflt_value, pk).
    return {row[1]: row[4] for row in rows}


def _table_contract(db_path: Path, table: str) -> dict[str, object]:
    """Full table/index contract, including checks and uniqueness."""

    conn = sqlite3.connect(db_path)
    try:
        table_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        assert table_sql_row is not None
        table_sql = " ".join(str(table_sql_row[0]).split()).casefold()
        columns = [tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")]
        indexes: dict[str, object] = {}
        for row in conn.execute(f"PRAGMA index_list({table})"):
            index_name = str(row[1])
            indexes[index_name] = {
                "unique": int(row[2]),
                "origin": str(row[3]),
                "partial": int(row[4]),
                "columns": [
                    (int(info[0]), int(info[1]), str(info[2]))
                    for info in conn.execute(f'PRAGMA index_info("{index_name}")')
                ],
            }
        return {
            "table_sql": table_sql,
            "columns": columns,
            "indexes": indexes,
        }
    finally:
        conn.close()


async def _boot_db(db_path: Path) -> None:
    """The production boot order: apply_pending, then SessionStorage.connect."""
    applied = apply_pending(str(db_path), MIGRATIONS_DIR)
    assert applied, "expected the real migrations dir to have pending migrations"
    storage = await SessionStorage.open(str(db_path))
    await storage.close()


async def test_fresh_and_upgraded_legacy_session_schemas_converge(tmp_path: Path) -> None:
    # DB-A: fresh install — migrations against an empty file, then connect.
    fresh_db = tmp_path / "fresh" / "sessions.db"
    fresh_db.parent.mkdir(parents=True)
    await _boot_db(fresh_db)

    # DB-B: upgraded legacy install — the synthetic pre-V007 shape above,
    # then the same migrations + connect the gateway boot path runs.
    legacy_db = tmp_path / "legacy" / "sessions.db"
    legacy_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(legacy_db)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    await _boot_db(legacy_db)

    for table in TABLES:
        fresh_shape = _table_shape(fresh_db, table)
        legacy_shape = _table_shape(legacy_db, table)
        assert set(fresh_shape) == set(legacy_shape), (
            f"{table}: column sets diverge between fresh and upgraded DBs "
            f"(only-fresh={sorted(set(fresh_shape) - set(legacy_shape))}, "
            f"only-legacy={sorted(set(legacy_shape) - set(fresh_shape))})"
        )
        # Defaults are deterministic text in both paths (the ALTER TABLE and
        # CREATE TABLE statements spell them identically), so pin them too.
        assert fresh_shape == legacy_shape, (
            f"{table}: column defaults diverge between fresh and upgraded DBs"
        )


async def test_fixed_four_tier_storage_only_and_migration_only_contracts_match(
    tmp_path: Path,
) -> None:
    storage_db = tmp_path / "storage-only.db"
    storage = await SessionStorage.open(str(storage_db))
    await storage.close()

    migration_db = tmp_path / "migration-only.db"
    assert apply_pending(str(migration_db), MIGRATIONS_DIR)

    for table in FIXED_FOUR_TIER_TABLES:
        assert _table_contract(storage_db, table) == _table_contract(
            migration_db,
            table,
        ), f"{table}: SessionStorage and V025 contracts diverge"
