from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from opensquilla.recovery import engine
from opensquilla.recovery.config_patch import (
    STATE_OVERRIDE_ENV_VARS,
    WORKSPACE_OVERRIDE_ENV_VARS,
    workspace_patch_journal,
)
from opensquilla.recovery.settings_transaction import settings_transaction_journal


@pytest.fixture(autouse=True)
def isolated_data_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*STATE_OVERRIDE_ENV_VARS, *WORKSPACE_OVERRIDE_ENV_VARS):
        monkeypatch.delenv(name, raising=False)


def _profile(tmp_path: Path, *, external: bool = False) -> tuple[Path, Path, Path]:
    home = tmp_path / "profile"
    state = tmp_path / "external-state" if external else home / "state"
    workspace = tmp_path / "external-workspace" if external else home / "workspace"
    home.mkdir()
    state.mkdir(parents=True)
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text("synthetic identity\n", encoding="utf-8")
    (home / "config.toml").write_text(
        f"config_version = 2\nstate_dir = {json.dumps(str(state))}\n"
        f"workspace_dir = {json.dumps(str(workspace))}\n",
        encoding="utf-8",
    )
    for name in ("sessions.db", "scheduler.db"):
        with contextlib.closing(sqlite3.connect(state / name)) as connection, connection:
            connection.execute("CREATE TABLE synthetic_data (value TEXT)")
            connection.execute("INSERT INTO synthetic_data VALUES ('preserved')")
    return home, state, workspace


@pytest.mark.parametrize("external", [False, True])
def test_healthy_startup_uses_no_sqlite_snapshot_or_full_database_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external: bool
) -> None:
    home, state, workspace = _profile(tmp_path, external=external)
    expected = engine.inspect_profile(home, profile_kind="desktop-primary")
    before = {path.name: path.read_bytes() for path in state.iterdir()}

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("healthy startup must not open SQLite or inspect a database snapshot")

    monkeypatch.setattr(engine, "_database_safety_code", unexpected)
    monkeypatch.setattr(engine, "_copy_source_file_no_follow", unexpected)
    monkeypatch.setattr(sqlite3, "connect", unexpected)
    report = engine.inspect_startup_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "ready"
    assert report == expected
    assert report.effective_workspace == workspace
    assert {path.name: path.read_bytes() for path in state.iterdir()} == before


def test_database_metadata_check_reads_only_fixed_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, state, _workspace = _profile(tmp_path)
    reads: list[int] = []
    original_read = os.read

    def read(descriptor: int, size: int) -> bytes:
        reads.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", read)
    assert engine._database_path_safety_code(state / "sessions.db") is None
    assert reads == [100]


@pytest.mark.parametrize("changed_name", ["sessions.db", "sessions.db-wal"])
def test_database_metadata_check_detects_bundle_change_during_header_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_name: str
) -> None:
    _home, state, _workspace = _profile(tmp_path)
    changed = state / changed_name
    if changed_name.endswith("-wal"):
        changed.write_bytes(b"synthetic sidecar")
    original_read = os.read

    def read(descriptor: int, size: int) -> bytes:
        result = original_read(descriptor, size)
        with changed.open("ab") as stream:
            stream.write(b"changed")
        return result

    monkeypatch.setattr(os, "read", read)
    assert engine._database_path_safety_code(state / "sessions.db") == "state_database_changed"


def test_database_metadata_check_rejects_hardlinks_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, state, _workspace = _profile(tmp_path)
    os.link(state / "sessions.db", tmp_path / "other-name.db")

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("hardlinked database must be rejected before reading")

    monkeypatch.setattr(os, "read", unexpected)
    assert engine._database_path_safety_code(state / "sessions.db") == "state_database_unsafe_path"


@pytest.mark.parametrize("kind", ["desktop-primary", "desktop-recovery"])
def test_fresh_startup_does_not_create_profile_paths(tmp_path: Path, kind: str) -> None:
    home = tmp_path / "not-created"
    report = engine.inspect_startup_profile(home, profile_kind=kind)
    assert report == engine.inspect_profile(home, profile_kind=kind)
    assert report.stable_code in {"fresh_profile", "fresh_recovery_profile"}
    assert not home.exists()


def test_existing_recovery_profile_retains_canonical_data_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _state, workspace = _profile(tmp_path)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("healthy existing recovery profile must use the metadata path")

    monkeypatch.setattr(engine, "inspect_profile", unexpected)
    report = engine.inspect_startup_profile(home, profile_kind="desktop-recovery")
    assert report.outcome == "recovery_profile"
    assert report.effective_workspace == workspace


@pytest.mark.parametrize("source", ["environment", "dotenv"])
def test_startup_resolves_external_overrides_like_full_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    home, _state, _workspace = _profile(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    _other_home, state, workspace = _profile(other_root, external=True)
    overrides = {
        "OPENSQUILLA_GATEWAY_STATE_DIR": str(state),
        "OPENSQUILLA_GATEWAY_WORKSPACE_DIR": str(workspace),
    }
    if source == "environment":
        for name, value in overrides.items():
            monkeypatch.setenv(name, value)
    else:
        (home / ".env").write_text(
            "".join(f"{name}={value}\n" for name, value in overrides.items()), encoding="utf-8"
        )
    expected = engine.inspect_profile(home, profile_kind="desktop-primary")
    assert engine.inspect_startup_profile(home, profile_kind="desktop-primary") == expected
    assert expected.effective_workspace == workspace
    effective_state = next(
        candidate.path for candidate in expected.candidates if candidate.kind == "state"
    )
    assert effective_state == state


@pytest.mark.parametrize("contents", [b"not a database", b"", b"SQLite format 3\x00"])
def test_unrecognized_database_header_falls_back_to_full_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: bytes
) -> None:
    home, state, _workspace = _profile(tmp_path)
    (state / "sessions.db").write_bytes(contents)
    full_inspect = engine.inspect_profile
    calls: list[Path] = []

    def inspect(path: Path, **kwargs: object) -> engine.RecoveryReport:
        calls.append(path)
        return full_inspect(path, **kwargs)

    monkeypatch.setattr(engine, "inspect_profile", inspect)
    report = engine.inspect_startup_profile(home, profile_kind="desktop-primary")
    assert calls == [home]
    assert report == full_inspect(home, profile_kind="desktop-primary")
    assert (state / "sessions.db").read_bytes() == contents


def test_orphan_database_sidecar_is_not_mistaken_for_fresh_database(tmp_path: Path) -> None:
    home, state, _workspace = _profile(tmp_path)
    (state / "sessions.db").unlink()
    (state / "sessions.db-wal").write_bytes(b"synthetic orphan")
    report = engine.inspect_startup_profile(home, profile_kind="desktop-primary")
    assert report.stable_code == "state_database_unsafe_path"
    assert not (state / "sessions.db").exists()


@pytest.mark.parametrize(
    ("journal_kind", "stable_code", "action"),
    [
        ("replace", "transaction_incomplete", None),
        ("legacy-import", "legacy_import_transaction_incomplete", None),
        ("settings", "settings_transaction_incomplete", "recover-settings"),
        ("workspace", "workspace_patch_incomplete", "reconcile"),
    ],
)
def test_pending_transactions_keep_full_inspection_classification_without_repair(
    tmp_path: Path, journal_kind: str, stable_code: str, action: str | None
) -> None:
    home, _state, _workspace = _profile(tmp_path)
    journals = {
        "replace": home.parent / f".{home.name}.profile-replace.json",
        "legacy-import": home.parent / f".{home.name}.import-commit.json",
        "settings": settings_transaction_journal(home),
        "workspace": workspace_patch_journal(home),
    }
    journal = journals[journal_kind]
    journal.write_text('{"phase":"prepared"}', encoding="utf-8")
    before = journal.read_bytes()
    report = engine.inspect_startup_profile(home, profile_kind="desktop-primary")
    assert report == engine.inspect_profile(home, profile_kind="desktop-primary")
    assert report.outcome == "attention"
    assert report.stable_code == stable_code
    if action is not None:
        assert action in report.allowed_actions
    assert journal.read_bytes() == before


@pytest.mark.parametrize(
    ("config", "outcome", "stable_code"),
    [
        ("not valid TOML [", "attention", "config_invalid"),
        ("config_version = 999\n", "recovery_required", "config_schema_too_new"),
    ],
)
def test_invalid_and_newer_configs_keep_existing_startup_gates(
    tmp_path: Path, config: str, outcome: str, stable_code: str
) -> None:
    home, _state, _workspace = _profile(tmp_path)
    (home / "config.toml").write_text(config, encoding="utf-8")
    report = engine.inspect_startup_profile(home, profile_kind="desktop-primary")
    assert report == engine.inspect_profile(home, profile_kind="desktop-primary")
    assert report.outcome == outcome
    assert report.stable_code == stable_code
    assert (home / "config.toml").read_text(encoding="utf-8") == config


def test_newer_database_schema_is_rejected_by_runtime_before_database_writes(
    tmp_path: Path,
) -> None:
    from opensquilla.persistence import migrator

    home, state, _workspace = _profile(tmp_path)
    database = state / "sessions.db"
    with contextlib.closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE _yoyo_migration "
            "(migration_hash TEXT PRIMARY KEY, migration_id TEXT, applied_at_utc TEXT)"
        )
        connection.execute(
            "INSERT INTO _yoyo_migration VALUES ('synthetic', 'V999__future', CURRENT_TIMESTAMP)"
        )
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "V001__demo.py").write_text(
        "from yoyo import step\nsteps = [step('CREATE TABLE demo (id INTEGER)')]\n",
        encoding="utf-8",
    )
    before = database.read_bytes()

    assert engine.inspect_startup_profile(home, profile_kind="desktop-primary").outcome == "ready"
    full_report = engine.inspect_profile(home, profile_kind="desktop-primary")
    assert full_report.stable_code == "state_schema_too_new"
    with pytest.raises(migrator.SchemaAheadError, match="V999__future"):
        migrator.apply_pending(str(database), migrations)

    assert database.read_bytes() == before
    with contextlib.closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT value FROM synthetic_data").fetchall() == [("preserved",)]
