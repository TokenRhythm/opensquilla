"""Upgrade/rollback coverage for Router deployment telemetry columns."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V023__router_deployment_telemetry"
NEW_COLUMNS = {
    "requested_provider",
    "requested_model",
    "executed_provider",
    "executed_model",
    "fallback_reason",
}


def _columns(db: str) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(router_decisions)").fetchall()
        }


def test_v023_adds_deployment_columns_without_losing_rows(tmp_path: Path) -> None:
    db = str(tmp_path / "sessions.sqlite")
    applied = apply_pending(db, MIGRATIONS_DIR)

    assert MIGRATION_ID in applied
    assert NEW_COLUMNS <= _columns(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO router_decisions "
            "(decision_id, session_key, ts_ms, requested_provider, requested_model, "
            " executed_provider, executed_model, fallback_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "deployment-test",
                "agent:main:test",
                1,
                "openai",
                "shared-model",
                "deepseek",
                "shared-model",
                "missing_credential",
            ),
        )
        row = conn.execute(
            "SELECT requested_provider, executed_provider, fallback_reason "
            "FROM router_decisions WHERE decision_id='deployment-test'"
        ).fetchone()
    assert row == ("openai", "deepseek", "missing_credential")


def test_v023_rollback_removes_only_additive_columns(tmp_path: Path) -> None:
    db = str(tmp_path / "sessions.sqlite")
    apply_pending(db, MIGRATIONS_DIR)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO router_decisions (decision_id, session_key, ts_ms) VALUES (?, ?, ?)",
            ("keep-row", "agent:main:test", 1),
        )

    backend = get_backend("sqlite:///" + db)
    try:
        migration = read_migrations(str(MIGRATIONS_DIR)).filter(
            lambda item: item.id == MIGRATION_ID
        )
        with backend.lock():
            backend.rollback_migrations(migration)
    finally:
        backend.connection.close()

    assert not (NEW_COLUMNS & _columns(db))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT decision_id FROM router_decisions WHERE decision_id='keep-row'"
        ).fetchone() == ("keep-row",)


def test_v023_version_prefix_is_unique() -> None:
    assert sorted(path.name for path in MIGRATIONS_DIR.glob("V023__*.py")) == [
        "V023__router_deployment_telemetry.py"
    ]
