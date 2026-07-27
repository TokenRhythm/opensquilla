"""Unit tests for the additive V020 turn ingress receipt migration."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import (
    SchemaAheadError,
    apply_pending,
    assert_schema_not_ahead,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
V020_ID = "V020__turn_ingress_receipts"

EXPECTED_COLUMNS = {
    "receipt_id",
    "source_scope",
    "request_session_key",
    "client_request_id",
    "request_fingerprint",
    "accepted_session_key",
    "session_id",
    "message_id",
    "task_id",
    "accepted_at",
    "schema_version",
}


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(conn: sqlite3.Connection, table: str) -> dict[str, bool]:
    return {
        row[1]: bool(row[2])
        for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
        if not row[1].startswith("sqlite_")
    }


def _insert_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    source_scope: str = "gateway:chat.send",
    request_session_key: str = "agent:main:webchat:request",
    client_request_id: str = "client-request-1",
    task_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO turn_ingress_receipts (
            receipt_id,
            source_scope,
            request_session_key,
            client_request_id,
            request_fingerprint,
            accepted_session_key,
            session_id,
            message_id,
            task_id,
            accepted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt_id,
            source_scope,
            request_session_key,
            client_request_id,
            "sha256:synthetic-fingerprint",
            "agent:main:webchat:accepted",
            "session-1",
            "message-1",
            task_id,
            1_700_000_000_000,
        ),
    )


def test_v020_apply_creates_receipt_table_and_unique_request_index(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "test.sqlite")
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert V020_ID in applied

    conn = _open_conn(db)
    try:
        assert _table_columns(conn, "turn_ingress_receipts") == EXPECTED_COLUMNS
        assert _indexes(conn, "turn_ingress_receipts") == {
            "idx_turn_ingress_receipts_accepted_session": False,
            "uq_turn_ingress_receipts_request": True,
        }
        assert _indexes(conn, "turn_errors")["idx_turn_errors_ts_error"] is False

        # A task is optional, and schema_version defaults for old/new writers
        # that omit the additive version marker.
        _insert_receipt(conn, receipt_id="receipt-1")
        row = conn.execute(
            "SELECT task_id, schema_version FROM turn_ingress_receipts "
            "WHERE receipt_id = 'receipt-1'"
        ).fetchone()
        assert row == (None, 1)

        # Reusing the scoped client request identity is rejected even when a
        # different receipt id is supplied.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_receipt(conn, receipt_id="receipt-2")

        # The same client-generated id remains valid in another source scope.
        _insert_receipt(
            conn,
            receipt_id="receipt-3",
            source_scope="channel:telegram",
        )
    finally:
        conn.close()


def test_v020_required_acceptance_identifiers_are_not_nullable(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "test.sqlite")
    apply_pending(db, MIGRATIONS_DIR)

    conn = _open_conn(db)
    try:
        required_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(turn_ingress_receipts)"
            ).fetchall()
            if row[3]
        }
        assert {
            "source_scope",
            "request_session_key",
            "client_request_id",
            "request_fingerprint",
            "accepted_session_key",
            "session_id",
            "message_id",
            "accepted_at",
            "schema_version",
        } <= required_columns
        assert "task_id" not in required_columns
    finally:
        conn.close()


def test_v020_rollback_drops_receipt_objects_and_turn_error_retention_index(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "test.sqlite")
    apply_pending(db, MIGRATIONS_DIR)

    conn = _open_conn(db)
    try:
        conn.execute("CREATE TABLE rollback_sentinel (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    backend = get_backend("sqlite:///" + db)
    try:
        v020 = read_migrations(str(MIGRATIONS_DIR)).filter(lambda m: m.id == V020_ID)
        with backend.lock():
            backend.rollback_migrations(v020)
    finally:
        backend.connection.close()

    conn = _open_conn(db)
    try:
        objects = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name IN (?, ?, ?, ?)",
                (
                    "turn_ingress_receipts",
                    "uq_turn_ingress_receipts_request",
                    "idx_turn_errors_ts_error",
                    "rollback_sentinel",
                ),
            ).fetchall()
        }
        assert ("table", "turn_ingress_receipts") not in objects
        assert ("index", "uq_turn_ingress_receipts_request") not in objects
        assert ("index", "idx_turn_errors_ts_error") not in objects
        assert ("table", "rollback_sentinel") in objects
    finally:
        conn.close()


def test_v020_version_prefix_and_dependency_are_explicit() -> None:
    v020_files = sorted(p.name for p in MIGRATIONS_DIR.glob("V020__*.py"))
    assert v020_files == ["V020__turn_ingress_receipts.py"]
    source = (MIGRATIONS_DIR / v020_files[0]).read_text(encoding="utf-8")
    assert "V019__turn_errors" in source


def test_schema_ahead_refuses_boot_without_v020(tmp_path: Path) -> None:
    db = str(tmp_path / "test.sqlite")
    apply_pending(db, MIGRATIONS_DIR)

    older_build_dir = tmp_path / "migrations_without_v020"
    older_build_dir.mkdir()
    unavailable = {
        "V020__turn_ingress_receipts.py",
        "V021__usage_ledger.py",
        "V022__telemetry_daily_usage.py",
        "V023__router_deployment_telemetry.py",
        "V024__usage_native_billing_receipts.py",
    }
    for migration in MIGRATIONS_DIR.glob("V*.py"):
        if migration.name not in unavailable:
            shutil.copy2(migration, older_build_dir / migration.name)

    with pytest.raises(SchemaAheadError, match=V020_ID):
        assert_schema_not_ahead(db, older_build_dir)
    assert_schema_not_ahead(db, MIGRATIONS_DIR)
