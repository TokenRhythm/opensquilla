"""Migration startup diagnostics used by Desktop recovery failure reports."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

import opensquilla.persistence.migrator as migrator
from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def test_fresh_database_migration_reports_each_blocking_phase(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.DEBUG, logger="opensquilla.persistence.migrator")

    applied = apply_pending(str(tmp_path / "sessions.db"), MIGRATIONS_DIR)

    assert applied
    messages = [record.getMessage() for record in caplog.records]
    expected = [
        "migrator.backend_open_started",
        "migrator.backend_open_ready",
        "migrator.discovery_started",
        "migrator.discovery_ready",
        "migrator.lock_wait_started",
        "migrator.lock_acquired",
        "migrator.plan_ready",
        "migrator.apply_started",
        "migrator.apply_ready",
    ]
    positions = [messages.index(message) for message in expected]
    assert positions == sorted(positions)


def test_full_fresh_migration_audit_never_uses_fqdn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "sessions.db"
    monkeypatch.setattr(
        migrator.socket,
        "getfqdn",
        lambda *_args, **_kwargs: pytest.fail("fresh migrations must remain network-free"),
    )
    monkeypatch.setattr(migrator.socket, "gethostname", lambda: "synthetic-local-host")

    applied = apply_pending(str(database), MIGRATIONS_DIR)

    with sqlite3.connect(database) as connection:
        audit_rows = connection.execute(
            "SELECT migration_id, hostname FROM _yoyo_log WHERE operation = 'apply'"
        ).fetchall()
    assert len(audit_rows) == len(applied) == 25
    assert {migration_id for migration_id, _hostname in audit_rows} == set(applied)
    assert {hostname for _migration_id, hostname in audit_rows} == {"synthetic-local-host"}
