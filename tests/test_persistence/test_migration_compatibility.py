"""Shipped migration inventories and unsupported lineages stay non-destructive."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from opensquilla.migration_compatibility import (
    LEGACY_MIGRATION_ALIASES,
    classify_migration_ledger,
    frozen_migration_registry,
    known_migration_ids,
)
from opensquilla.persistence import migrator
from opensquilla.recovery.engine import _database_safety_code
from scripts.freeze_migration_registry import freeze_registry

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
_LEGACY_GOALS = Path(__file__).resolve().parents[1] / "fixtures" / "legacy-goal-lineage"


def test_frozen_registry_covers_each_distinct_source_migration(tmp_path: Path) -> None:
    output = tmp_path / "registry.json"
    freeze_registry(_MIGRATIONS, output)
    registry = frozen_migration_registry(tmp_path)
    assert registry is not None
    assert set(registry) == {path.stem for path in _MIGRATIONS.glob("V*.py")}
    assert registry["V010__meta_skill_runs"] != registry["V010__transcript_turn_usage"]
    assert "V029__goal_runs" not in registry
    assert "V033__goal_runs" in registry


@pytest.mark.parametrize("with_retry", [False, True], ids=["v029", "v029-v030"])
@pytest.mark.parametrize("with_unknown", [False, True], ids=["legacy", "mixed-unknown"])
def test_unsupported_goal_lineage_preserves_source_database_and_wal(
    tmp_path: Path, with_retry: bool, with_unknown: bool,
) -> None:
    database = tmp_path / "sessions.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "CREATE TABLE _yoyo_migration (migration_id TEXT, migration_hash TEXT)"
        )
        migration_ids = ["V029__goal_runs"]
        if with_retry:
            migration_ids.append("V030__goal_run_retry")
        for migration_id in migration_ids:
            connection.executescript(
                (_LEGACY_GOALS / f"{migration_id}.sql").read_text(encoding="utf-8")
            )
        if with_unknown:
            migration_ids.append("V999__synthetic_future")
        connection.executemany(
            "INSERT INTO _yoyo_migration VALUES (?, ?)",
            [(item, hashlib.sha256(item.encode()).hexdigest()) for item in migration_ids],
        )
        connection.commit()
        goal_rows = connection.execute("SELECT * FROM goal_runs ORDER BY goal_id").fetchall()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(goal_runs)")]
        assert len(goal_rows) == 3
        assert {"goal_text", "turns", "idle_turns", "blocked_retries", "plan_run_id"} <= set(
            columns
        )
        assert ("failure_retries" in columns) is with_retry
        if with_retry:
            assert connection.execute(
                "SELECT failure_retries,next_retry_at_ms,pause_reason,last_error "
                "FROM goal_runs WHERE goal_id='synthetic-goal-paused'"
            ).fetchone() == (2, 7000, "retry_backoff", "Synthetic transport timeout.")
        schema = connection.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        assert any(row[1] == "idx_goal_runs_active" and "WHERE status IN" in row[2]
                   for row in schema)
        ledger = connection.execute(
            "SELECT * FROM _yoyo_migration ORDER BY migration_id"
        ).fetchall()
        wal = database.with_name(database.name + "-wal")
        before = (database.read_bytes(), wal.read_bytes())
        assert len(before[1]) > 32
        with pytest.raises(migrator.SchemaAheadError, match="unsupported development Goal"):
            migrator.assert_schema_not_ahead(str(database), _MIGRATIONS)
        assert (database.read_bytes(), wal.read_bytes()) == before
        with pytest.raises(migrator.SchemaAheadError, match="unsupported development Goal"):
            migrator.apply_pending(str(database), _MIGRATIONS)
        assert (database.read_bytes(), wal.read_bytes()) == before
        assert _database_safety_code(database) == "state_unsupported_goal_lineage"
        assert (database.read_bytes(), wal.read_bytes()) == before
        assert connection.execute(
            "SELECT * FROM goal_runs ORDER BY goal_id"
        ).fetchall() == goal_rows
        assert connection.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY name"
        ).fetchall() == schema
        assert connection.execute(
            "SELECT * FROM _yoyo_migration ORDER BY migration_id"
        ).fetchall() == ledger
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='session_goals'"
        ).fetchone()
        assert "V033__goal_runs" not in {row[0] for row in ledger}
        assert not list(tmp_path.glob("*.bak"))
    finally:
        connection.close()


def test_only_verified_aliases_are_compatible_across_inspection_surfaces(tmp_path: Path) -> None:
    known = known_migration_ids(_MIGRATIONS)
    legacy_id, (replacement, expected_hash) = next(iter(LEGACY_MIGRATION_ALIASES.items()))
    assert replacement in known
    assert classify_migration_ledger({legacy_id: expected_hash}, known).code is None
    assert classify_migration_ledger({legacy_id: "different"}, known).code == (
        "state_migration_alias_mismatch"
    )
    assert classify_migration_ledger({legacy_id: None}, known).code == (
        "state_migration_alias_mismatch"
    )
    database = tmp_path / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE _yoyo_migration (migration_id TEXT, migration_hash TEXT)"
        )
        connection.execute("INSERT INTO _yoyo_migration VALUES (?, ?)", (legacy_id, expected_hash))
    migrator.assert_schema_not_ahead(str(database), _MIGRATIONS)
    assert _database_safety_code(database) is None
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE _yoyo_migration SET migration_hash = 'different'")
    with pytest.raises(migrator.SchemaAheadError, match="exact historical"):
        migrator.assert_schema_not_ahead(str(database), _MIGRATIONS)
    assert _database_safety_code(database) == "state_migration_alias_mismatch"


def test_current_packaged_profile_skips_discovery_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "_migrations"
    directory.mkdir()
    migration = directory / "V001__demo.py"
    migration.write_text(
        "from yoyo import step\nsteps = [step('CREATE TABLE demo (value TEXT)')]\n",
        encoding="utf-8",
    )
    freeze_registry(directory, directory / "registry.json")
    database = tmp_path / "sessions.db"
    assert migrator.apply_pending(str(database), directory) == ["V001__demo"]

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("A current packaged profile must not discover or back up migrations")

    monkeypatch.setattr(migrator, "_discover_migrations", forbidden)
    monkeypatch.setattr(migrator, "_snapshot_before_apply", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    assert migrator.apply_pending(str(database), directory) == []


def test_frozen_registry_does_not_skip_new_migrations(tmp_path: Path) -> None:
    directory = tmp_path / "_migrations"
    directory.mkdir()
    (directory / "V001__demo.py").write_text(
        "from yoyo import step\nsteps = [step('CREATE TABLE demo (value TEXT)')]\n",
        encoding="utf-8",
    )
    database = tmp_path / "sessions.db"
    migrator.apply_pending(str(database), directory)
    (directory / "V002__extra.py").write_text(
        "from yoyo import step\n__depends__ = {'V001__demo'}\n"
        "steps = [step('ALTER TABLE demo ADD COLUMN extra TEXT')]\n",
        encoding="utf-8",
    )
    freeze_registry(directory, directory / "registry.json")
    assert migrator.apply_pending(str(database), directory) == ["V002__extra"]
    assert migrator.apply_pending(str(database), directory) == []
