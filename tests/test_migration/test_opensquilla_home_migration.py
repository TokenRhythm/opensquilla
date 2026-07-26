"""OpenSquilla self-migration: legacy home import into the current home.

All homes here are synthetic (dummy values only) and built in tmp_path; the
config content reuses the golden cli-0.1 fixture so the import exercises a
real released-era config shape.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import tomli_w

import opensquilla.gateway.config_migration as config_migration_module
import opensquilla.migration.opensquilla_home as migration_module
import opensquilla.recovery as recovery_module
from opensquilla.artifacts import ArtifactStore
from opensquilla.attachment_refs import (
    make_attachment_ref,
    read_attachment_ref_bytes,
    write_transcript_material,
)
from opensquilla.migration import orchestrator
from opensquilla.migration.opensquilla_home import (
    IMPORT_MARKER_FILENAME,
    OpenSquillaHomeMigrator,
    OpenSquillaMigrationOptions,
    detect_legacy_cli_home,
    enumerate_portable_homes,
    inspect_opensquilla_home_candidate,
    is_valid_opensquilla_home,
)
from opensquilla.persistence.migrator import apply_pending
from opensquilla.recovery.locking import ProfileOperationLock
from opensquilla.recovery.restore import restore_profile
from opensquilla.recovery.transaction import recover_profile_transaction
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage

FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "homes" / "cli-0.1" / "config.toml"
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
PORTABLE_RELEASE_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "test_recovery"
    / "fixtures"
    / "portable"
    / "released-profiles.json"
)
PORTABLE_RELEASES = json.loads(
    PORTABLE_RELEASE_MANIFEST.read_text(encoding="utf-8")
)["published_releases"]

DUMMY_INLINE_KEY = "dummy-inline-key-123"

REPORT_KEYS = {
    "source",
    "source_kind",
    "target",
    "output_dir",
    "apply",
    "items",
    "candidates",
    "config_transforms",
    "secret_relocations",
    "paused_jobs",
    "preflight",
    "notes",
}


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve())))


@pytest.fixture(autouse=True)
def _isolate_profile_operation_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never leave migration test locks in the runner's real user-state tree."""

    monkeypatch.setenv("OPENSQUILLA_TEST", "1")
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))


def _probe_gateway_lock(state_dir: str, queue: multiprocessing.Queue) -> None:
    from opensquilla.gateway.pidlock import GatewayPidLock

    lock = GatewayPidLock(state_dir)
    try:
        lock.acquire()
    except SystemExit:
        queue.put("busy")
    else:
        queue.put("acquired")
        lock.release()

# Base scheduler_jobs DDL as scheduler/persistence.py creates it (the
# ``enabled`` column arrived later via a conditional column add).
_SCHEDULER_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS scheduler_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    cron_expr TEXT NOT NULL,
    handler_key TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_at TEXT,
    next_run_at TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    max_retries INTEGER NOT NULL DEFAULT 3,
    jitter_seconds REAL NOT NULL DEFAULT 0.0
)
"""


def _write_config(home: Path) -> None:
    payload = tomllib.loads(FIXTURE_CONFIG.read_text(encoding="utf-8"))
    payload["llm"]["api_key"] = DUMMY_INLINE_KEY
    (home / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")


def _write_sessions_db(home: Path, applied_ids: tuple[str, ...]) -> None:
    db_path = home / "state" / "sessions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE _yoyo_migration ("
            "migration_hash TEXT PRIMARY KEY, "
            "migration_id TEXT, "
            "applied_at_utc TIMESTAMP)"
        )
        for index, migration_id in enumerate(applied_ids):
            connection.execute(
                "INSERT INTO _yoyo_migration VALUES (?, ?, ?)",
                (f"dummy-hash-{index}", migration_id, "2026-01-01 00:00:00"),
            )
        connection.execute(
            "CREATE TABLE synthetic_sessions (session_id TEXT PRIMARY KEY, transcript TEXT)"
        )
        connection.execute(
            "INSERT INTO synthetic_sessions VALUES (?, ?)",
            ("synthetic-session", "synthetic portable conversation"),
        )
        connection.commit()
    finally:
        connection.close()


def _write_scheduler_db(home: Path, *, with_enabled_column: bool) -> None:
    db_path = home / "state" / "scheduler.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_SCHEDULER_JOBS_DDL)
        if with_enabled_column:
            connection.execute(
                "ALTER TABLE scheduler_jobs ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            "INSERT INTO scheduler_jobs "
            "(id, name, cron_expr, handler_key, payload, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job-1",
                "dummy daily job",
                "0 9 * * *",
                "agent_turn",
                "{}",
                "pending",
                "2026-01-01 00:00:00",
                "2026-01-01 00:00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _build_source_home(
    root: Path,
    *,
    with_enabled_column: bool = True,
    applied_ids: tuple[str, ...] = ("V001__initial_schema",),
) -> Path:
    home = root / "legacy-home"
    (home / "workspace" / "memory").mkdir(parents=True)
    identity_documents = {
        "USER.md": "# Synthetic user\n\nCall me Portable Tester.\n",
        "SOUL.md": "# Synthetic soul\n\nBe precise and reversible.\n",
        "IDENTITY.md": "# Synthetic identity\n\nName: Import Squilla.\n",
        "MEMORY.md": "# Memory index\n\n- dummy entry\n",
    }
    for name, content in identity_documents.items():
        (home / "workspace" / name).write_text(content, encoding="utf-8")
    (home / "workspace" / "memory" / "2026-01-01.md").write_text(
        "- dated dummy note\n", encoding="utf-8"
    )
    (home / "skills" / "dummy-skill").mkdir(parents=True)
    (home / "skills" / "dummy-skill" / "SKILL.md").write_text(
        "---\nname: dummy-skill\ndescription: a dummy skill\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("OPENROUTER_API_KEY=dummy\n", encoding="utf-8")
    (home / "profiles" / "dummy-profile").mkdir(parents=True)
    (home / "profiles" / "dummy-profile" / "config.toml").write_text(
        "port = 18791\n", encoding="utf-8"
    )
    _write_config(home)
    _write_sessions_db(home, applied_ids)
    _write_scheduler_db(home, with_enabled_column=with_enabled_column)
    return home


def _run(
    source: Path,
    target: Path,
    *,
    apply: bool = False,
    overwrite: bool = False,
    replace_target: bool = False,
    confirm_replace_target: Path | None = None,
) -> dict[str, Any]:
    options = OpenSquillaMigrationOptions(
        source=source,
        target=target,
        apply=apply,
        overwrite=overwrite,
        replace_target=replace_target,
        confirm_replace_target=confirm_replace_target,
    )
    return OpenSquillaHomeMigrator(options).migrate()


def _errors(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in report["items"] if item["status"] == "error"]


def _scheduler_enabled_values(db_path: Path) -> list[int]:
    connection = sqlite3.connect(db_path)
    try:
        return [row[0] for row in connection.execute("SELECT enabled FROM scheduler_jobs")]
    finally:
        connection.close()


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_imported_identity_chat_and_config(target: Path) -> None:
    expected_documents = {
        "USER.md": "# Synthetic user\n\nCall me Portable Tester.\n",
        "SOUL.md": "# Synthetic soul\n\nBe precise and reversible.\n",
        "IDENTITY.md": "# Synthetic identity\n\nName: Import Squilla.\n",
        "MEMORY.md": "# Memory index\n\n- dummy entry\n",
    }
    for name, content in expected_documents.items():
        assert (target / "workspace" / name).read_text(encoding="utf-8") == content

    with sqlite3.connect(
        f"file:{target / 'state' / 'sessions.db'}?mode=ro",
        uri=True,
    ) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT session_id, transcript FROM synthetic_sessions"
        ).fetchone() == ("synthetic-session", "synthetic portable conversation")

    config = tomllib.loads((target / "config.toml").read_text(encoding="utf-8"))
    assert "state_dir" not in config
    assert "workspace_dir" not in config
    assert config["port"] == 18791
    assert config["llm"]["provider"] == "openrouter"
    assert config["llm"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert "api_key" not in config["llm"]
    env_text = (target / ".env").read_text(encoding="utf-8")
    assert f"OPENROUTER_API_KEY={DUMMY_INLINE_KEY}" in env_text


# ---------------------------------------------------------------------------
# 1. Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_produces_full_report_and_writes_nothing(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=False)

    assert set(report) == REPORT_KEYS
    assert report["apply"] is False
    assert report["output_dir"] == ""
    assert not _errors(report)

    # Planned home entries, with profiles/ excluded.
    planned = {
        Path(item["source"]).name
        for item in report["items"]
        if item["kind"] == "home-entry" and item["status"] == "planned"
    }
    assert {"workspace", "skills", "state", "config.toml", ".env"} <= planned
    skipped = [
        item
        for item in report["items"]
        if item["kind"] == "home-entry" and item["status"] == "skipped"
    ]
    assert any(Path(item["source"]).name == "profiles" for item in skipped)

    # Paused-jobs preview read from the source scheduler.db.
    assert report["paused_jobs"] == [
        {"id": "job-1", "name": "dummy daily job", "cron_expr": "0 9 * * *"}
    ]

    # Transform and secret plans are present, redacted.
    assert any("port: 18790 -> 18791" in entry for entry in report["config_transforms"])
    assert {"config_path": "llm.api_key", "env_key": "OPENROUTER_API_KEY", "moved": True} in (
        report["secret_relocations"]
    )
    assert DUMMY_INLINE_KEY not in json.dumps(report)

    # Nothing was written anywhere: no target, no output dir, no staging,
    # and the source home is untouched.
    assert not target.exists()
    assert not list(tmp_path.glob(".opensquilla-import-*"))
    assert not (source / IMPORT_MARKER_FILENAME).exists()
    source_config = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    assert source_config["port"] == 18790
    assert source_config["llm"]["api_key"] == DUMMY_INLINE_KEY


def test_dry_run_reports_only_the_session_count_for_candidate_metadata(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    connection = sqlite3.connect(source / "state" / "sessions.db")
    try:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO sessions (id) VALUES (?)",
            [("synthetic-session-a",), ("synthetic-session-b",)],
        )
        connection.commit()
    finally:
        connection.close()

    report = _run(source, tmp_path / "target-home", apply=False)

    assert report["preflight"]["session_count"] == 2
    serialized = json.dumps(report)
    assert "synthetic-session-a" not in serialized
    assert "synthetic-session-b" not in serialized


def test_code_task_runs_are_left_in_source_for_preview_and_apply(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    code_task_python = (
        source
        / "code-task"
        / "codetask-task-synthetic"
        / "repo"
        / ".venv"
        / "bin"
        / "python"
    )
    code_task_python.parent.mkdir(parents=True)
    link_payload = "/synthetic/uv/python/cpython/bin/python3"
    if os.name == "nt":
        code_task_python.write_text("synthetic generated interpreter", encoding="utf-8")
    else:
        code_task_python.symlink_to(link_payload)
    target = tmp_path / "target-home"

    preview = _run(source, target, apply=False)
    assert not target.exists()
    applied = _run(source, target, apply=True)

    for report in (preview, applied):
        assert not _errors(report)
        skipped = [
            item
            for item in report["items"]
            if item["kind"] == "home-entry"
            and item["status"] == "skipped"
            and Path(item["source"]).name == "code-task"
        ]
        assert len(skipped) == 1
        assert "machine-local" in skipped[0]["reason"]

    assert not (target / "code-task").exists()
    if os.name == "nt":
        assert code_task_python.read_text(encoding="utf-8") == (
            "synthetic generated interpreter"
        )
    else:
        assert code_task_python.is_symlink()
        assert os.readlink(code_task_python) == link_payload


def test_direct_migrator_rejects_config_path_without_writing(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"

    report = OpenSquillaHomeMigrator(
        OpenSquillaMigrationOptions(
            source=source,
            target=target,
            config_path=tmp_path / "unsupported.toml",
            apply=True,
        )
    ).migrate()

    assert any(
        item["kind"] == "options" and "config_path is not supported" in item["reason"]
        for item in _errors(report)
    )
    assert not target.exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


# ---------------------------------------------------------------------------
# 2. Apply
# ---------------------------------------------------------------------------


def test_apply_imports_home_with_transforms(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path, with_enabled_column=True)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert report["apply"] is True
    _assert_imported_identity_chat_and_config(target)

    # Entries landed; profiles/ excluded; runtime pid locks excluded.
    assert (target / "workspace" / "MEMORY.md").is_file()
    assert (target / "workspace" / "memory" / "2026-01-01.md").is_file()
    assert (target / "skills" / "dummy-skill" / "SKILL.md").is_file()
    assert (target / "state" / "sessions.db").is_file()
    assert not (target / "profiles").exists()
    assert not (target / "state" / "gateway.pid").exists()
    assert not (target / "desktop-layout-v2.json").exists()

    # Config transforms: absolute path pins dropped, port coerced, secret
    # relocated to .env with the env pointer left behind.
    config = tomllib.loads((target / "config.toml").read_text(encoding="utf-8"))
    assert "state_dir" not in config
    assert "workspace_dir" not in config
    assert config["port"] == 18791
    assert "api_key" not in config["llm"]
    assert config["llm"]["api_key_env"] == "OPENROUTER_API_KEY"

    env_text = (target / ".env").read_text(encoding="utf-8")
    assert f"OPENROUTER_API_KEY={DUMMY_INLINE_KEY}" in env_text
    assert env_text.count("OPENROUTER_API_KEY=") == 1

    # Imported scheduler jobs all arrive paused.
    assert _scheduler_enabled_values(target / "state" / "scheduler.db") == [0]
    assert report["paused_jobs"] == [
        {"id": "job-1", "name": "dummy daily job", "cron_expr": "0 9 * * *"}
    ]

    # The report directory contains only narrow protocol/summary artifacts,
    # never a duplicate database or identity/memory document.
    output_dir = Path(report["output_dir"])
    assert {
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file()
    } == {"layout-receipt.json", "report.json", "summary.md"}

    # No staging dir left behind.
    assert not list(tmp_path.glob(".opensquilla-import-*"))

    # RC4 keeps the source strictly read-only; target receipts are authoritative.
    assert not (source / IMPORT_MARKER_FILENAME).exists()
    source_config = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    assert source_config["port"] == 18790
    assert source_config["llm"]["api_key"] == DUMMY_INLINE_KEY
    assert _scheduler_enabled_values(source / "state" / "scheduler.db") == [1]

    # The report never carries the secret value.
    assert DUMMY_INLINE_KEY not in json.dumps(report)


def test_profile_import_preserves_unmodified_toml_bytes_and_comments(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    source_config = (
        b"# operator header\n"
        b"port = 18791  # keep exact spacing\n"
        b"config_version = 1\n"
    )
    (source / "config.toml").write_bytes(source_config)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert (target / "config.toml").read_bytes() == source_config


def test_profile_import_losslessly_patches_legacy_paths_and_secret_comments(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    config_path = source / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    original = tomllib.loads(text)
    text = text.replace(
        f"state_dir = {json.dumps(original['state_dir'])}",
        f"state_dir = {json.dumps(original['state_dir'])} # state pin note",
    ).replace(
        f"workspace_dir = {json.dumps(original['workspace_dir'])}",
        f"workspace_dir = {json.dumps(original['workspace_dir'])} # identity pin note",
    ).replace(
        "port = 18790",
        "port = 18790 # legacy port note",
    ).replace(
        f'api_key = "{DUMMY_INLINE_KEY}"',
        f'api_key = "{DUMMY_INLINE_KEY}" # credential relocation note',
    )
    config_path.write_text("# profile comments survive\n" + text, encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    imported_text = (target / "config.toml").read_text(encoding="utf-8")
    imported = tomllib.loads(imported_text)
    assert "state_dir" not in imported
    assert "workspace_dir" not in imported
    assert imported["port"] == 18791
    assert imported["llm"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert "profile comments survive" in imported_text
    assert "state pin note" in imported_text
    assert "identity pin note" in imported_text
    assert "legacy port note" in imported_text
    assert "credential relocation note" in imported_text


def test_external_profile_pin_is_rebased_without_reformatting_other_toml(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    shutil.rmtree(source / "workspace")
    external = tmp_path / "external-workspace"
    external.mkdir()
    (external / "IDENTITY.md").write_text("external\n", encoding="utf-8")
    config_path = source / "config.toml"
    payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    payload["workspace_dir"] = str(external)
    rendered = "# external workspace must be snapshotted\n" + tomli_w.dumps(payload)
    rendered = rendered.replace(
        f'workspace_dir = {json.dumps(str(external))}',
        f'workspace_dir = {json.dumps(str(external))} # external pin note',
    )
    config_path.write_text(rendered, encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    imported_text = (target / "config.toml").read_text(encoding="utf-8")
    imported = tomllib.loads(imported_text)
    assert "workspace_dir" not in imported
    assert "external workspace must be snapshotted" in imported_text
    assert "external pin note" in imported_text
    assert (target / "workspace" / "IDENTITY.md").read_text(encoding="utf-8") == (
        "external\n"
    )


def test_desktop_import_finalizes_rc3_layout_marker_only_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "desktop-home"
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")

    report = _run(source, target, apply=True)

    assert not _errors(report)
    marker = json.loads((target / "desktop-layout-v2.json").read_text(encoding="utf-8"))
    assert marker["schema_version"] == 2
    assert marker["protectedBy"] == "rc4"
    assert not (tmp_path / ".desktop-home.profile-replace.json").exists()


def test_imported_config_and_env_are_owner_only_even_without_secret_rewrite(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["llm"].pop("api_key", None)
    payload["llm"]["api_key_env"] = "OPENROUTER_API_KEY"
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    os.chmod(source / "config.toml", 0o644)
    os.chmod(source / ".env", 0o644)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    if os.name != "nt":
        assert (target / "config.toml").stat().st_mode & 0o777 == 0o600
        assert (target / ".env").stat().st_mode & 0o777 == 0o600


def test_read_only_source_directories_produce_writable_imported_runtime(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    source_sessions = source / "state" / "sessions.db"
    source_sessions_bytes = source_sessions.read_bytes()
    os.chmod(source_sessions, 0o444)
    os.chmod(source / "state", 0o555)
    os.chmod(source / "workspace", 0o555)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert source_sessions.read_bytes() == source_sessions_bytes
    assert source_sessions.stat().st_mode & 0o200 == 0
    if os.name != "nt":
        assert (target / "state").stat().st_mode & 0o700 == 0o700
        assert (target / "workspace").stat().st_mode & 0o700 == 0o700
        assert (target / "state" / "sessions.db").stat().st_mode & 0o600 == 0o600
    connection = sqlite3.connect(target / "state" / "sessions.db")
    try:
        connection.execute("CREATE TABLE writable_after_import (id INTEGER)")
        connection.commit()
    finally:
        connection.close()


def test_apply_pauses_jobs_when_enabled_column_is_absent(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path, with_enabled_column=False)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    # The column was added pre-seeded at 0, so JobStore's later conditional
    # add (default 1) never fires and imported jobs arrive paused.
    assert _scheduler_enabled_values(target / "state" / "scheduler.db") == [0]
    assert report["paused_jobs"] == [
        {"id": "job-1", "name": "dummy daily job", "cron_expr": "0 9 * * *"}
    ]


# ---------------------------------------------------------------------------
# 3. Non-empty target gate
# ---------------------------------------------------------------------------


def test_non_empty_target_refused_without_overwrite(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    (target / "state").mkdir(parents=True)
    (target / "state" / "sessions.db").write_bytes(b"dummy existing db")

    report = _run(source, target, apply=True)

    errors = _errors(report)
    assert errors
    assert any("--replace-target" in item["reason"] for item in errors)
    # Nothing was imported.
    assert not (target / "workspace").exists()
    assert (target / "state" / "sessions.db").read_bytes() == b"dummy existing db"
    assert not (source / IMPORT_MARKER_FILENAME).exists()


def test_overwrite_takes_timestamped_backups(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    (target / "state").mkdir(parents=True)
    (target / "state" / "sessions.db").write_bytes(b"dummy existing db")
    (target / "old-only.txt").write_text("must not be merged", encoding="utf-8")

    report = _run(
        source,
        target,
        apply=True,
        overwrite=True,
        confirm_replace_target=target.resolve(),
    )

    assert not _errors(report)
    _assert_imported_identity_chat_and_config(target)
    backups = list(tmp_path.glob("target-home.backup.*"))
    assert len(backups) == 1
    assert (backups[0] / "state" / "sessions.db").read_bytes() == b"dummy existing db"
    assert (backups[0] / "old-only.txt").read_text(encoding="utf-8") == "must not be merged"
    # The imported store replaced the old one.
    assert (target / "state" / "sessions.db").read_bytes() != b"dummy existing db"
    assert not (target / "old-only.txt").exists()
    persisted = json.loads(
        (Path(report["output_dir"]) / "report.json").read_text(encoding="utf-8")
    )
    returned_backups = [item for item in report["items"] if item["kind"] == "backup"]
    assert returned_backups
    # The interactive report can explain the retained backup, but the durable
    # diagnostic is intentionally counts-only and never stores item rows.
    assert "items" not in persisted
    assert persisted["item_counts"]["migrated"] >= len(returned_backups)
    history = json.loads(
        (tmp_path / "profile-replacement-history.json").read_text(encoding="utf-8")
    )
    assert history["schema_version"] == 1
    record = history["backups"][0]
    assert record["backup"] == _normalized_path(backups[0])
    assert str(uuid.UUID(record["transaction_id"])) == record["transaction_id"]
    for identity_key in ("source_identity", "target_identity", "backup_identity"):
        assert {"device", "inode", "file_type", "mode", "size", "modified_at_ns"} <= set(
            record[identity_key]
        )


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX source-link contract; Windows source reparse points stay fail-closed",
)
def test_replacement_preserves_old_profile_links_but_excludes_source_code_task(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    source_project = source / "workspace" / "project"
    source_project.mkdir()
    (source_project / "COPYING").write_text("new license\n", encoding="utf-8")
    (source_project / "LICENSE").symlink_to("COPYING")
    source_code_task = source / "code-task" / "run" / "repo" / ".venv" / "bin"
    source_code_task.mkdir(parents=True)
    (source_code_task / "python").symlink_to("/missing/source/python")

    target = tmp_path / "target-home"
    (target / "workspace").mkdir(parents=True)
    (target / "workspace" / "old-only.txt").write_text("old\n", encoding="utf-8")
    historical = target / "state" / "workspace"
    historical.mkdir(parents=True)
    (historical / "COPYING").write_text("old license\n", encoding="utf-8")
    (historical / "LICENSE").symlink_to("COPYING")
    old_code_task = target / "code-task" / "run" / "repo" / ".venv" / "bin"
    old_code_task.mkdir(parents=True)
    (old_code_task / "python").symlink_to("/missing/old/python")

    report = _run(
        source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )

    assert not _errors(report)
    assert os.readlink(target / "workspace" / "project" / "LICENSE") == "COPYING"
    assert not (target / "code-task").exists()
    backup = next(tmp_path.glob("target-home.backup.*"))
    assert os.readlink(backup / "state" / "workspace" / "LICENSE") == "COPYING"
    assert os.readlink(
        backup / "code-task" / "run" / "repo" / ".venv" / "bin" / "python"
    ) == "/missing/old/python"
    history = json.loads(
        (tmp_path / "profile-replacement-history.json").read_text(encoding="utf-8")
    )
    assert history["backups"][0]["backup"] == _normalized_path(backup)


def test_published_target_validation_never_ignores_a_replaced_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path)
    source_before = _file_bytes(source)
    target = tmp_path / "target-home"
    journal = tmp_path / ".target-home.profile-replace.json"
    original_inspect = recovery_module.inspect_profile

    def replace_journal_before_inspection(*args: Any, **kwargs: Any) -> Any:
        unrelated = json.loads(journal.read_text(encoding="utf-8"))
        unrelated["transaction_id"] = str(uuid.uuid4())
        unrelated["source"] = str((tmp_path / "unrelated-source").resolve())
        journal.write_text(json.dumps(unrelated), encoding="utf-8")
        return original_inspect(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "inspect_profile",
        replace_journal_before_inspection,
    )

    report = _run(source, target, apply=True)

    assert _errors(report)
    assert any(
        "journal" in item["reason"] and "transaction was not completed" in item["reason"]
        for item in _errors(report)
    )
    assert not target.exists()
    assert journal.is_file(), "an unrelated journal must never be removed"
    assert _file_bytes(source) == source_before


@pytest.mark.parametrize(
    ("authority_name", "initial", "changed"),
    [
        ("gateway.pid", None, b"not-a-pid\n"),
        ("gateway.pid.lock", None, b"appeared\n"),
        ("gateway.pid", b"stale-pid\n", b"changed-pid\n"),
        ("gateway.pid.lock", b"existing\n", b"changed-lock\n"),
    ],
)
def test_excluded_source_gateway_authority_change_blocks_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority_name: str,
    initial: bytes | None,
    changed: bytes,
) -> None:
    source = _build_source_home(tmp_path)
    authority = source / "state" / authority_name
    if initial is not None:
        authority.write_bytes(initial)
    target = tmp_path / "target-home"
    original_write_env = OpenSquillaHomeMigrator._write_staged_env
    mutation_errors: list[OSError] = []

    def mutate_excluded_authority(
        migrator: OpenSquillaHomeMigrator,
        staging: Path,
    ) -> None:
        original_write_env(migrator, staging)
        try:
            if initial is None:
                authority.write_bytes(changed)
            else:
                # Avoid Path.write_bytes(): it truncates before the Windows
                # byte-range lock rejects the write, making the test fixture
                # itself mutate the source even though publication is blocked.
                with authority.open("r+b") as stream:
                    stream.seek(0)
                    stream.write(changed)
                    stream.truncate()
        except OSError as exc:
            mutation_errors.append(exc)
            raise

    monkeypatch.setattr(
        OpenSquillaHomeMigrator,
        "_write_staged_env",
        mutate_excluded_authority,
    )

    report = _run(source, target, apply=True)

    assert _errors(report)
    assert not target.exists(), _errors(report)
    if mutation_errors:
        # Windows can reject a write to the byte-range-locked authority before
        # the post-copy digest check runs. That is the stronger fail-closed
        # outcome and is valid only for a lock leaf that already existed.
        assert sys.platform == "win32"
        assert authority_name == "gateway.pid.lock"
        assert initial is not None
        assert authority.read_bytes() == initial
        assert any(
            "import failed before completion" in item["reason"]
            for item in _errors(report)
        )
    else:
        if sys.platform == "win32" and authority_name == "gateway.pid.lock":
            # A same-process Windows rewrite of the byte-range-locked leaf can
            # either persist or be refused without surfacing an OSError to the
            # Python write call. The safety contract is that the source remains
            # one of those two complete values and no target is published; the
            # exact lower-level diagnostic is not stable.
            assert authority.read_bytes() in {initial, changed}
        else:
            assert authority.read_bytes() == changed
            assert any(
                "gateway authority changed" in item["reason"]
                for item in _errors(report)
            )


@pytest.mark.parametrize("alias", [False, True])
def test_replacement_requires_exact_target_confirmation(
    tmp_path: Path, alias: bool
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    target.mkdir()
    existing = target / "existing.txt"
    existing.write_text("preserve", encoding="utf-8")

    report = _run(
        source,
        target,
        apply=True,
        overwrite=alias,
        replace_target=not alias,
        confirm_replace_target=tmp_path / "wrong-target",
    )

    assert any("exact confirmation" in item["reason"] for item in _errors(report))
    assert existing.read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob("target-home.backup.*"))


def test_existing_empty_target_is_parked_and_published_transactionally(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    target.mkdir()

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert (target / "workspace" / "MEMORY.md").is_file()
    backups = list(tmp_path.glob("target-home.backup.*"))
    assert len(backups) == 1
    assert not any(backups[0].iterdir())
    history = json.loads(
        (tmp_path / "profile-replacement-history.json").read_text(encoding="utf-8")
    )
    assert history["backups"][0]["backup"] == _normalized_path(backups[0])


def test_committed_replacement_history_can_restore_complete_previous_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    source = _build_source_home(tmp_path / "source-root")
    target = tmp_path / "target-home"
    (target / "workspace").mkdir(parents=True)
    (target / "workspace" / "SOUL.md").write_text("previous profile\n", encoding="utf-8")
    (target / "state").mkdir()
    (target / "config.toml").write_text("port = 18791\n", encoding="utf-8")

    imported = _run(
        source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )
    assert not _errors(imported)
    backup = next(tmp_path.glob("target-home.backup.*"))

    restored = restore_profile(backup)

    assert restored.outcome == "ready"
    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == (
        "previous profile\n"
    )
    history = json.loads(
        (tmp_path / "profile-replacement-history.json").read_text(encoding="utf-8")
    )
    assert history["backups"][0]["restored_to"] == _normalized_path(target)


# ---------------------------------------------------------------------------
# 4.-6. Pre-flight refusals
# ---------------------------------------------------------------------------


def test_live_source_gateway_refused(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    (source / "state" / "gateway.pid").write_text(
        json.dumps({"pid": os.getpid(), "start_ts": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert report["preflight"]["source_gateway_running"] is True
    errors = _errors(report)
    assert any("gateway is running on the source home" in item["reason"] for item in errors)
    assert not target.exists()


def test_read_only_preview_allows_the_active_target_gateway(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path / "source-root")
    target = tmp_path / "target-home"
    (target / "state").mkdir(parents=True)
    (target / "config.toml").write_text("port = 18791\n", encoding="utf-8")
    (target / "state" / "gateway.pid").write_text(
        json.dumps({"pid": os.getpid(), "start_ts": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    before = _file_bytes(target)

    report = OpenSquillaHomeMigrator(
        OpenSquillaMigrationOptions(
            source=source,
            target=target,
            apply=False,
            replace_target=True,
            allow_running_target_preview=True,
        )
    ).migrate()

    assert report["apply"] is False
    assert report["preflight"]["target_gateway_running"] is True
    assert not _errors(report)
    assert _file_bytes(target) == before


def test_running_target_preview_escape_hatch_is_rejected_for_apply(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path / "source-root")
    target = tmp_path / "target-home"

    report = OpenSquillaHomeMigrator(
        OpenSquillaMigrationOptions(
            source=source,
            target=target,
            apply=True,
            allow_running_target_preview=True,
        )
    ).migrate()

    assert any(
        item["kind"] == "options"
        and "valid for dry-run only" in item["reason"]
        for item in _errors(report)
    )
    assert not target.exists()


def test_schema_ahead_source_refused(tmp_path: Path) -> None:
    source = _build_source_home(
        tmp_path, applied_ids=("V001__initial_schema", "V999__future_thing")
    )
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert report["preflight"]["schema_ahead"] is True
    errors = _errors(report)
    assert any("newer OpenSquilla" in item["reason"] for item in errors)
    assert any("V999__future_thing" in item["reason"] for item in errors)
    assert not target.exists()


def test_insufficient_disk_space_refused(tmp_path: Path, monkeypatch) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    monkeypatch.setattr(
        "shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=990, free=10),
    )

    report = _run(source, target, apply=True)

    assert report["preflight"]["disk_free_bytes"] == 10
    assert report["preflight"]["disk_required_bytes"] > 10
    errors = _errors(report)
    assert any("not enough free disk space" in item["reason"] for item in errors)
    assert not target.exists()


# ---------------------------------------------------------------------------
# 7. CLI-home detection guard
# ---------------------------------------------------------------------------


def test_detect_legacy_cli_home_guard(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "userhome"
    legacy = fake_home / ".opensquilla"
    legacy.mkdir(parents=True)
    (legacy / "config.toml").write_text("port = 18790\n", encoding="utf-8")
    (legacy / "workspace").mkdir()
    (legacy / "state").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # The active home IS ~/.opensquilla: never offered as a source.
    assert detect_legacy_cli_home(legacy) is None
    # Symlink-equivalent spelling of the same home is also rejected.
    assert detect_legacy_cli_home(fake_home / ".opensquilla" / ".." / ".opensquilla") is None
    # A different target (desktop spawn, relocated state dir): offered.
    assert detect_legacy_cli_home(tmp_path / "electron-home") == legacy


def test_detect_legacy_cli_home_keeps_previously_imported_source_visible(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "userhome"
    legacy = fake_home / ".opensquilla"
    legacy.mkdir(parents=True)
    (legacy / "config.toml").write_text("port = 18790\n", encoding="utf-8")
    (legacy / "workspace").mkdir()
    (legacy / "state").mkdir()
    target = tmp_path / "desktop-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    report = _run(legacy, target, apply=True)
    assert not _errors(report)
    # A committed target receipt is a display hint, never authority to hide a
    # still-valid source. The user must remain able to choose it again.
    assert detect_legacy_cli_home(target) == legacy
    assert migration_module._matching_import_receipt(legacy, target) is not None
    assert detect_legacy_cli_home(tmp_path / "different-target") == legacy

    # A stale source marker also remains non-authoritative after uninstall.
    shutil.rmtree(target)
    assert detect_legacy_cli_home(target) == legacy


def test_is_valid_opensquilla_home_shapes(tmp_path: Path) -> None:
    assert not is_valid_opensquilla_home(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not is_valid_opensquilla_home(empty)
    with_state = tmp_path / "with-state"
    (with_state / "state").mkdir(parents=True)
    assert is_valid_opensquilla_home(with_state)
    with_workspace = tmp_path / "with-workspace"
    (with_workspace / "workspace").mkdir(parents=True)
    assert is_valid_opensquilla_home(with_workspace)


# ---------------------------------------------------------------------------
# 8. Portable enumeration
# ---------------------------------------------------------------------------


def test_enumerate_portable_homes_orders_and_era_hints(tmp_path: Path) -> None:
    base = tmp_path / "appdata"
    portable = base / "OpenSquilla" / "portable"
    older = portable / "dummy-release-a"
    newer = portable / "dummy-release-b"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "config.toml").write_text("port = 18790\n", encoding="utf-8")
    (older / "install-receipt.json").write_text(
        json.dumps({"version": "0.4.1"}), encoding="utf-8"
    )
    (newer / "config.toml").write_text("port = 18790\n", encoding="utf-8")
    (newer / "state").mkdir()
    (newer / "state" / "update_check.json").write_text("{}", encoding="utf-8")

    now = time.time()
    os.utime(older / "config.toml", (now - 1000, now - 1000))
    os.utime(newer / "config.toml", (now, now))
    os.utime(older, (now - 1000, now - 1000))
    os.utime(newer, (now, now))

    candidates = enumerate_portable_homes([base])

    assert [candidate.path for candidate in candidates] == [newer, older]
    assert candidates[0].era_hint == "0.5.0rc2+"
    assert candidates[1].era_hint == "0.4.1"
    assert candidates[0].last_used > candidates[1].last_used
    assert all(candidate.size_bytes > 0 for candidate in candidates)
    assert all(candidate.previously_imported is False for candidate in candidates)


def test_rc_update_cache_is_a_portable_home_era_hint(tmp_path: Path) -> None:
    home = tmp_path / "portable-home"
    (home / "state").mkdir(parents=True)
    (home / "state" / "update_check_rc.json").write_text("{}", encoding="utf-8")

    assert migration_module._era_hint(home) == "0.5.0rc2+"


def test_candidate_metadata_is_privacy_narrow_stable_and_read_only(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path / "source")
    config = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    config["state_dir"] = str(source / "state")
    (source / "config.toml").write_text(tomli_w.dumps(config), encoding="utf-8")
    (source / "install-receipt.json").write_text(
        json.dumps({"version": "0.5.0rc3"}),
        encoding="utf-8",
    )
    connection = sqlite3.connect(source / "state" / "sessions.db")
    try:
        connection.execute("CREATE TABLE sessions (session_key TEXT PRIMARY KEY, title TEXT)")
        connection.executemany(
            "INSERT INTO sessions VALUES (?, ?)",
            [("private-session-a", "private title a"), ("private-session-b", "private title b")],
        )
        connection.commit()
    finally:
        connection.close()
    before = _file_bytes(source)

    candidate = inspect_opensquilla_home_candidate(
        source,
        kind="cli-home",
        target=tmp_path / "target",
    )

    assert candidate is not None
    payload = candidate.as_payload()
    assert payload == {
        "kind": "cli-home",
        "path": str(source),
        "version": "0.5.0rc3",
        "estimated_activity_at": payload["estimated_activity_at"],
        "session_count": 2,
        "size_bytes": payload["size_bytes"],
        "previously_imported": False,
    }
    assert payload["estimated_activity_at"] is not None
    assert isinstance(payload["size_bytes"], int) and payload["size_bytes"] > 0
    serialized = json.dumps(payload)
    assert "private-session-a" not in serialized
    assert "private title a" not in serialized
    assert _file_bytes(source) == before


def test_candidate_size_does_not_collapse_when_directory_identity_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path / "source")
    monkeypatch.setattr(migration_module, "_advisory_identity", lambda _result: None)

    candidate = inspect_opensquilla_home_candidate(source, kind="cli-home")

    assert candidate is not None
    assert isinstance(candidate.size_bytes, int)
    assert candidate.size_bytes > 0


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX link metadata contract; Windows reparse points stay fail-closed",
)
def test_candidate_size_ignores_workspace_and_excluded_code_task_links(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path / "source")
    (source / "workspace" / "MEMORY.alias.md").symlink_to("MEMORY.md")
    code_task_bin = source / "code-task" / "run" / "repo" / ".venv" / "bin"
    code_task_bin.mkdir(parents=True)
    (code_task_bin / "python").symlink_to("/missing/generated/python")

    candidate = inspect_opensquilla_home_candidate(source, kind="cli-home")

    assert candidate is not None
    assert isinstance(candidate.size_bytes, int)
    assert candidate.size_bytes > 0


def test_candidate_metadata_size_is_bounded_and_never_follows_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text("version = \"0.5.0rc3\"\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("must-not-be-read", encoding="utf-8")
    (source / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(migration_module, "_CANDIDATE_METADATA_MAX_ENTRIES", 1)

    candidate = inspect_opensquilla_home_candidate(source, kind="cli-home")

    assert candidate is not None
    assert candidate.size_bytes is None
    assert candidate.version == "0.5.0rc3"


def test_portable_candidate_enumeration_has_a_hard_display_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portable = tmp_path / "base" / "OpenSquilla" / "portable"
    for name in ("one", "two", "three"):
        home = portable / name
        home.mkdir(parents=True)
        (home / "config.toml").write_text("port = 18791\n", encoding="utf-8")
    monkeypatch.setattr(migration_module, "_CANDIDATE_ENUMERATION_MAX_CANDIDATES", 1)

    candidates = enumerate_portable_homes([tmp_path / "base"])

    assert len(candidates) == 1


def test_candidate_session_count_rejects_a_source_changed_during_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path / "source")
    config = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    config["state_dir"] = str(source / "state")
    (source / "config.toml").write_text(tomli_w.dumps(config), encoding="utf-8")
    connection = sqlite3.connect(source / "state" / "sessions.db")
    try:
        connection.execute("CREATE TABLE sessions (session_key TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO sessions VALUES ('synthetic-session')")
        connection.commit()
    finally:
        connection.close()
    original_digest = migration_module._digest_regular_file
    changed = False

    def mutate_after_copy(*args: Any, **kwargs: Any) -> str:
        nonlocal changed
        digest = original_digest(*args, **kwargs)
        path = Path(args[0])
        if not changed and kwargs.get("destination") is not None and path.name == "sessions.db":
            changed = True
            with path.open("ab") as handle:
                handle.write(b"changed-after-copy")
        return digest

    monkeypatch.setattr(migration_module, "_digest_regular_file", mutate_after_copy)

    candidate = inspect_opensquilla_home_candidate(source, kind="cli-home")

    assert changed is True
    assert candidate is not None
    assert candidate.session_count is None


def test_candidate_metadata_does_not_open_unapproved_external_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    external_state = tmp_path / "external-state"
    external_state.mkdir()
    (source / "config.toml").write_text(
        f"state_dir = {json.dumps(str(external_state))}\n",
        encoding="utf-8",
    )
    calls: list[Path] = []

    def unexpected_count(path: Path) -> int:
        calls.append(path)
        return 99

    monkeypatch.setattr(migration_module, "_read_session_count", unexpected_count)

    candidate = inspect_opensquilla_home_candidate(source, kind="cli-home")

    assert candidate is not None
    assert candidate.session_count is None
    assert calls == []


def test_portable_candidate_reports_previous_import_without_hiding_source(
    tmp_path: Path,
) -> None:
    base = tmp_path / "appdata"
    seed = _build_source_home(tmp_path / "seed")
    portable = base / "OpenSquilla" / "portable" / "dummy-release"
    portable.parent.mkdir(parents=True)
    seed.rename(portable)
    target = tmp_path / "target-home"

    first = OpenSquillaHomeMigrator(
        OpenSquillaMigrationOptions(
            source=portable,
            kind="windows-portable",
            target=target,
            apply=True,
        )
    ).migrate()
    assert not _errors(first)

    candidates = enumerate_portable_homes([base], target=target)
    assert [candidate.path for candidate in candidates] == [portable]
    assert candidates[0].previously_imported is True


def test_previous_import_hint_is_bound_to_the_selected_source_kind(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path / "source")
    target = tmp_path / "target-home"
    first = OpenSquillaHomeMigrator(
        OpenSquillaMigrationOptions(
            source=source,
            kind="windows-portable",
            target=target,
            apply=True,
        )
    ).migrate()
    assert not _errors(first)

    portable = inspect_opensquilla_home_candidate(
        source,
        kind="windows-portable",
        target=target,
    )
    cli = inspect_opensquilla_home_candidate(source, kind="cli-home", target=target)

    assert portable is not None and portable.previously_imported is True
    assert cli is not None and cli.previously_imported is False


def test_single_portable_candidate_is_reported_but_never_auto_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "appdata"
    portable = base / "OpenSquilla" / "portable" / "dummy-release"
    portable.mkdir(parents=True)
    (portable / "config.toml").write_text("port = 18791\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    monkeypatch.delenv("TEMP", raising=False)

    report = OpenSquillaHomeMigrator(
        OpenSquillaMigrationOptions(
            kind="windows-portable",
            target=tmp_path / "target-home",
        )
    ).migrate()

    assert report["source"] == ""
    assert [candidate["path"] for candidate in report["candidates"]] == [str(portable)]
    assert report["candidates"][0]["previously_imported"] is False
    assert {
        "kind",
        "path",
        "version",
        "estimated_activity_at",
        "session_count",
        "size_bytes",
        "previously_imported",
    } <= set(report["candidates"][0])
    assert any("explicitly confirm" in item["reason"] for item in _errors(report))


@pytest.mark.parametrize(
    "release",
    PORTABLE_RELEASES,
    ids=lambda release: release["release_tag"],
)
def test_every_published_portable_release_completes_full_profile_apply(
    tmp_path: Path,
    release: dict[str, Any],
) -> None:
    """Historical Portable coverage must prove apply, not just enumeration."""

    seed = _build_source_home(
        tmp_path / f"seed-{release['release_id']}",
        applied_ids=(release["source"]["latest_migration_id"],),
    )
    source = (
        tmp_path
        / "LocalApplicationData"
        / "OpenSquilla"
        / "portable"
        / release["release_id"]
    )
    source.parent.mkdir(parents=True)
    seed.rename(source)
    config = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    config["version"] = release["release_tag"].removeprefix("v")
    config["state_dir"] = str(source / "state")
    config["workspace_dir"] = str(source / "workspace")
    (source / "config.toml").write_text(tomli_w.dumps(config), encoding="utf-8")
    (source / "install-receipt.json").write_text(
        json.dumps({"version": release["release_tag"]}),
        encoding="utf-8",
    )
    source_before = _file_bytes(source)
    target = tmp_path / f"imported-{release['release_id']}"

    report = OpenSquillaHomeMigrator(
        OpenSquillaMigrationOptions(
            source=source,
            kind="windows-portable",
            target=target,
            apply=True,
        )
    ).migrate()

    assert not _errors(report)
    _assert_imported_identity_chat_and_config(target)
    assert _file_bytes(source) == source_before
    assert not (source / IMPORT_MARKER_FILENAME).exists()
    receipt = json.loads(
        (Path(report["output_dir"]) / "layout-receipt.json").read_text(encoding="utf-8")
    )
    assert set(receipt) == {
        "schema_version",
        "transaction_id",
        "imported_at",
        "source",
        "source_identity",
        "source_kind",
        "source_version",
        "target",
        "candidate_identity",
        "recovery_outcome",
        "recovery_stable_code",
        "layout",
    }
    assert receipt["source_kind"] == "windows-portable"
    assert receipt["source_version"] == release["release_tag"]
    assert receipt["source"] == _normalized_path(source)
    assert receipt["target"] == _normalized_path(target)


# ---------------------------------------------------------------------------
# 9. WAL sidecars
# ---------------------------------------------------------------------------


def test_source_snapshot_ignores_shm_but_keeps_db_and_wal(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-snapshot"
    state = source / "state"
    state.mkdir(parents=True)
    (state / "sessions.db").write_bytes(b"database")
    (state / "sessions.db-wal").write_bytes(b"durable-wal")
    shm = state / "sessions.db-shm"
    shm.write_bytes(b"transient-one")

    first = migration_module._scan_source_tree(
        source,
        destination_prefix=Path(),
        role="test",
        excluded_leaf_suffixes=("-shm",),
    )
    shm.unlink()
    shm.write_bytes(b"transient-two")
    second = migration_module._scan_source_tree(
        source,
        destination_prefix=Path(),
        role="test",
        excluded_leaf_suffixes=first.excluded_leaf_suffixes,
    )

    files = {
        entry.relative.as_posix()
        for entry in first.entries
        if entry.entry_type == "file"
    }
    assert files == {"state/sessions.db", "state/sessions.db-wal"}
    assert migration_module._source_snapshots_match(second, first)


def test_sqlite_snapshot_normalizes_empty_wal_sidecar(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    (source / "state" / "sessions.db-wal").write_bytes(b"")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert not (target / "state" / "sessions.db-wal").exists()
    connection = sqlite3.connect(target / "state" / "sessions.db")
    try:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    finally:
        connection.close()


def test_dry_run_does_not_create_sidecars_for_checkpointed_wal_store(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    db_path = source / "state" / "sessions.db"
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.commit()
    finally:
        connection.close()
    assert not db_path.with_name("sessions.db-wal").exists()
    assert not db_path.with_name("sessions.db-shm").exists()
    before = _file_bytes(source)

    report = _run(source, tmp_path / "target-home", apply=False)

    assert not _errors(report)
    assert _file_bytes(source) == before


def test_dry_run_reads_committed_wal_without_mutating_source_bundle(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    db_path = source / "state" / "scheduler.db"
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute(
            "INSERT INTO scheduler_jobs "
            "(id, name, cron_expr, handler_key, payload, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job-wal",
                "committed only in WAL",
                "30 10 * * *",
                "agent_turn",
                "{}",
                "pending",
                "2026-01-02 00:00:00",
                "2026-01-02 00:00:00",
            ),
        )
        writer.commit()
        assert db_path.with_name("scheduler.db-wal").stat().st_size > 0
        before = _file_bytes(source)

        report = _run(source, tmp_path / "target-home", apply=False)

        assert not _errors(report)
        assert {job["id"] for job in report["paused_jobs"]} == {"job-1", "job-wal"}
        assert _file_bytes(source) == before
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# Safety regressions: split roots, fail-closed transforms, atomic publish
# ---------------------------------------------------------------------------


def _point_config_at_split_roots(
    source: Path,
    *,
    state: Path,
    workspace: Path,
    media: Path,
) -> None:
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["state_dir"] = str(state)
    payload["workspace_dir"] = str(workspace)
    attachments = payload.setdefault("attachments", {})
    attachments["media_root"] = str(media)
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")


def test_external_configured_roots_are_copied_to_canonical_target_paths(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    shutil.rmtree(source / "state")
    shutil.rmtree(source / "workspace")
    external_state = tmp_path / "external-state"
    external_workspace = tmp_path / "external-workspace"
    external_media = tmp_path / "external-media"
    external_state.mkdir()
    external_workspace.mkdir()
    external_media.mkdir()
    (external_state / "state-sentinel.bin").write_bytes(b"state-data")
    (external_workspace / "workspace-sentinel.txt").write_text(
        "workspace-data", encoding="utf-8"
    )
    (external_media / "media-sentinel.bin").write_bytes(b"media-data")
    _point_config_at_split_roots(
        source,
        state=external_state,
        workspace=external_workspace,
        media=external_media,
    )
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert (target / "state" / "state-sentinel.bin").read_bytes() == b"state-data"
    assert (target / "workspace" / "workspace-sentinel.txt").read_text(
        encoding="utf-8"
    ) == "workspace-data"
    assert (target / "media" / "media-sentinel.bin").read_bytes() == b"media-data"
    config = tomllib.loads((target / "config.toml").read_text(encoding="utf-8"))
    assert "state_dir" not in config
    assert "workspace_dir" not in config
    assert "media_root" not in config.get("attachments", {})


def test_existing_external_dot_opensquilla_path_is_never_mistaken_for_internal(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    shutil.rmtree(source / "workspace")
    external = tmp_path / "mounted-backup" / ".opensquilla" / "workspace"
    external.mkdir(parents=True)
    (external / "IDENTITY.md").write_text("external identity", encoding="utf-8")
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["workspace_dir"] = str(external)
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert (target / "workspace" / "IDENTITY.md").read_text(
        encoding="utf-8"
    ) == "external identity"


def test_missing_external_pin_blocks_even_when_canonical_root_exists(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    missing = tmp_path / "missing-external-workspace"
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["workspace_dir"] = str(missing)
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(
        item["kind"] == "preflight/data-root"
        and item["source"] == str(missing)
        for item in _errors(report)
    )
    assert not target.exists()


def test_external_agent_workspace_is_snapshotted_and_rebased(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    external = tmp_path / "external-agent-workspace"
    external.mkdir()
    (external / "IDENTITY.md").write_text("synthetic agent", encoding="utf-8")
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["agents"] = [{"id": "research", "workspace": str(external)}]
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    imported = target / "workspace" / "agents" / "research" / "IDENTITY.md"
    assert imported.read_text(encoding="utf-8") == "synthetic agent"
    config = tomllib.loads((target / "config.toml").read_text(encoding="utf-8"))
    assert config["agents"][0]["workspace"] == str(
        target / "workspace" / "agents" / "research"
    )


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX link contract; Windows reparse points stay fail-closed",
)
def test_in_profile_configured_workspace_links_are_classified_before_copy(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    project = source / "custom-workspace" / "project"
    project.mkdir(parents=True)
    (project / "COPYING").write_text("license\n", encoding="utf-8")
    (project / "LICENSE").symlink_to("COPYING")
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["workspace_dir"] = "custom-workspace"
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    preview = _run(source, target)
    applied = _run(source, target, apply=True)

    assert not _errors(preview)
    assert not _errors(applied)
    imported = target / "workspace" / "project" / "LICENSE"
    assert imported.is_symlink()
    assert os.readlink(imported) == "COPYING"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX link contract; Windows reparse points stay fail-closed",
)
@pytest.mark.parametrize("same_payload", [True, False], ids=["same", "conflicting"])
def test_split_workspace_link_collisions_are_checked_in_preview_and_apply(
    tmp_path: Path,
    same_payload: bool,
) -> None:
    source = _build_source_home(tmp_path)
    canonical = source / "workspace" / "project"
    configured = source / "custom-workspace" / "project"
    canonical.mkdir()
    configured.mkdir(parents=True)
    for root in (canonical, configured):
        (root / "COPYING").write_text("license\n", encoding="utf-8")
    (configured / "OTHER").write_text("other\n", encoding="utf-8")
    (canonical / "LICENSE").symlink_to("COPYING")
    (configured / "LICENSE").symlink_to("COPYING" if same_payload else "OTHER")
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["workspace_dir"] = "custom-workspace"
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    preview = _run(source, target)
    applied = _run(source, target, apply=True)

    if same_payload:
        assert not _errors(preview)
        assert not _errors(applied)
        assert os.readlink(target / "workspace" / "project" / "LICENSE") == "COPYING"
    else:
        for report in (preview, applied):
            assert any(
                item["kind"] == "preflight/data-root" for item in _errors(report)
            )
        assert not target.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX link contract; Windows reparse points stay fail-closed",
)
def test_in_profile_agent_workspace_links_are_classified_before_copy(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    workspace = source / "custom-agent-workspace"
    workspace.mkdir()
    (workspace / "IDENTITY.md").write_text("synthetic agent\n", encoding="utf-8")
    (workspace / "IDENTITY.alias.md").symlink_to("IDENTITY.md")
    config_path = source / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[[agents]]\nid = "research"\nworkspace = "custom-agent-workspace"\n',
        encoding="utf-8",
    )
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    imported = target / "workspace" / "agents" / "research" / "IDENTITY.alias.md"
    assert imported.is_symlink()
    assert os.readlink(imported) == "IDENTITY.md"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX link contract; Windows reparse points stay fail-closed",
)
def test_agent_snapshot_prefix_cannot_follow_existing_workspace_link(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    canonical_agents = source / "workspace" / "agents"
    redirected = canonical_agents / "redirected-research"
    redirected.mkdir(parents=True)
    (canonical_agents / "research").symlink_to("redirected-research")
    configured = source / "custom-agent-workspace"
    configured.mkdir()
    (configured / "IDENTITY.md").write_text("must not merge\n", encoding="utf-8")
    config_path = source / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[[agents]]\nid = "research"\nworkspace = "custom-agent-workspace"\n',
        encoding="utf-8",
    )
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(item["kind"] == "apply" for item in _errors(report))
    assert not target.exists()
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))


def test_dotenv_external_data_roots_are_snapshotted_rebased_and_never_shared(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path / "source-root")
    external_state = tmp_path / "external-state"
    external_workspace = tmp_path / "external-workspace"
    external_media = tmp_path / "external-media"
    shutil.move(source / "state", external_state)
    shutil.move(source / "workspace", external_workspace)
    external_media.mkdir()
    (external_media / "artifact.bin").write_bytes(b"synthetic-media")
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload.pop("state_dir", None)
    payload.pop("workspace_dir", None)
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    env_path = source / ".env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8")
        + f"OPENSQUILLA_GATEWAY_STATE_DIR={external_state}\n"
        + f"OPENSQUILLA_GATEWAY_WORKSPACE_DIR={external_workspace}\n"
        + f"OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT={external_media}\n",
        encoding="utf-8",
    )
    source_env_before = env_path.read_bytes()
    state_before = _file_bytes(external_state)
    workspace_before = _file_bytes(external_workspace)
    media_before = _file_bytes(external_media)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert _file_bytes(external_state) == state_before
    assert _file_bytes(external_workspace) == workspace_before
    assert _file_bytes(external_media) == media_before
    assert env_path.read_bytes() == source_env_before
    assert (target / "state" / "sessions.db").is_file()
    assert (target / "workspace" / "IDENTITY.md").is_file()
    assert (target / "media" / "artifact.bin").read_bytes() == b"synthetic-media"
    imported_env = (target / ".env").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=dummy" in imported_env
    for key in (
        "OPENSQUILLA_GATEWAY_STATE_DIR",
        "OPENSQUILLA_GATEWAY_WORKSPACE_DIR",
        "OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT",
    ):
        assert key not in imported_env
    imported_config = tomllib.loads(
        (target / "config.toml").read_text(encoding="utf-8")
    )
    assert "state_dir" not in imported_config
    assert "workspace_dir" not in imported_config


@pytest.mark.parametrize(
    "key",
    [
        "OPENSQUILLA_GATEWAY_STATE_DIR",
        "OPENSQUILLA_GATEWAY_WORKSPACE_DIR",
        "OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT",
    ],
)
def test_missing_dotenv_external_data_root_blocks_without_publication(
    tmp_path: Path,
    key: str,
) -> None:
    source = _build_source_home(tmp_path)
    missing = tmp_path / f"missing-{key.lower()}"
    env_path = source / ".env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8") + f"{key}={missing}\n",
        encoding="utf-8",
    )
    source_before = _file_bytes(source)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(item["kind"] == "preflight/data-root" for item in _errors(report))
    assert _file_bytes(source) == source_before
    assert not target.exists()
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))


def test_internal_dotenv_home_selectors_are_removed_before_cli_target_boot(
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "user-home"
    fake_home.mkdir()
    source = _build_source_home(tmp_path / "source-root")
    env_path = source / ".env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8")
        + f"OPENSQUILLA_STATE_DIR={source}\n"
        + f"OPENSQUILLA_GATEWAY_CONFIG_PATH={source / 'config.toml'}\n",
        encoding="utf-8",
    )
    target = fake_home / ".opensquilla"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    imported_env = (target / ".env").read_text(encoding="utf-8")
    assert "OPENSQUILLA_STATE_DIR" not in imported_env
    assert "OPENSQUILLA_GATEWAY_CONFIG_PATH" not in imported_env
    environment = os.environ.copy()
    environment["HOME"] = str(fake_home)
    for key in (
        "OPENSQUILLA_STATE_DIR",
        "OPENSQUILLA_HOME",
        "OPENSQUILLA_PROFILE",
        "OPENSQUILLA_GATEWAY_CONFIG_PATH",
    ):
        environment.pop(key, None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from opensquilla.env import load_env; "
                "from opensquilla.paths import default_opensquilla_home; "
                "home=default_opensquilla_home(); "
                "load_env(cwd=home.parent, home=home); "
                "print(default_opensquilla_home())"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()) == target


def test_external_dotenv_home_selector_blocks_instead_of_reusing_live_profile(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    other_home = tmp_path / "other-live-home"
    other_home.mkdir()
    env_path = source / ".env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8")
        + f"OPENSQUILLA_STATE_DIR={other_home}\n",
        encoding="utf-8",
    )
    source_before = _file_bytes(source)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(
        item["kind"] == "preflight/env"
        and "another live profile" in item["reason"]
        for item in _errors(report)
    )
    assert _file_bytes(source) == source_before
    assert not target.exists()


@pytest.mark.parametrize(
    "agent_ids",
    [
        ["{absolute}"],
        ["../../../../escape"],
        ["Agent_A", "agent_a"],
        ["Research"],
    ],
)
def test_unsafe_or_noncanonical_agent_ids_block_before_staging(
    tmp_path: Path,
    agent_ids: list[str],
) -> None:
    source = _build_source_home(tmp_path)
    outside = tmp_path / "agent-id-escape"
    outside.write_text("preserve", encoding="utf-8")
    outside.chmod(0o640)
    before = outside.stat()
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["agents"] = [
        {
            "id": str(outside) if agent_id == "{absolute}" else agent_id,
            "workspace": str(source / "workspace"),
        }
        for agent_id in agent_ids
    ]
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    preview = _run(source, target)
    applied = _run(source, target, apply=True)

    assert any(item["kind"] == "preflight/config" for item in _errors(preview))
    assert any(item["kind"] == "preflight/config" for item in _errors(applied))
    assert not target.exists()
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))
    assert outside.read_text(encoding="utf-8") == "preserve"
    after = outside.stat()
    assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)


def test_windows_absolute_path_pins_are_dropped_on_posix_import(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    (source / "media").mkdir()
    (source / "media" / "sentinel.bin").write_bytes(b"media")
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["state_dir"] = r"E:\Users\synthetic\.opensquilla\state"
    payload["workspace_dir"] = r"E:\Users\synthetic\.opensquilla\workspace"
    payload.setdefault("attachments", {})["media_root"] = (
        r"E:\Users\synthetic\.opensquilla\media"
    )
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    config = tomllib.loads((target / "config.toml").read_text(encoding="utf-8"))
    assert "state_dir" not in config
    assert "workspace_dir" not in config
    assert "media_root" not in config.get("attachments", {})
    assert (target / "state" / "sessions.db").is_file()
    assert (target / "workspace" / "MEMORY.md").is_file()
    assert (target / "media" / "sentinel.bin").read_bytes() == b"media"


def test_missing_unc_workspace_is_treated_as_external_and_blocks_import(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["workspace_dir"] = r"\\synthetic.invalid\share\.opensquilla\workspace"
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(
        item["kind"] == "preflight/data-root"
        and "does not exist" in item["reason"]
        for item in _errors(report)
    )
    assert not target.exists()


def test_configured_data_root_overlapping_target_is_rejected(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["state_dir"] = str(tmp_path)
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")

    report = _run(source, target, apply=True)

    assert any(
        item["kind"] == "preflight/data-root"
        and "overlaps the target home" in item["reason"]
        for item in _errors(report)
    )
    assert not target.exists()
    assert not list(tmp_path.glob(".opensquilla-import-*"))


def test_external_roots_are_included_in_disk_preflight(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    baseline = _run(source, tmp_path / "target-baseline")
    external_state = tmp_path / "external-state"
    external_workspace = tmp_path / "external-workspace"
    external_media = tmp_path / "external-media"
    for root in (external_state, external_workspace, external_media):
        root.mkdir()
        (root / "payload.bin").write_bytes(b"x" * 4096)
    _point_config_at_split_roots(
        source,
        state=external_state,
        workspace=external_workspace,
        media=external_media,
    )

    report = _run(source, tmp_path / "target-home")

    assert not _errors(baseline)
    assert not _errors(report)
    assert report["preflight"]["disk_required_bytes"] >= (
        baseline["preflight"]["disk_required_bytes"] + (3 * 4096)
    )


def test_code_task_bytes_are_excluded_from_disk_preflight(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)

    before = _run(source, tmp_path / "target-before")
    (source / "code-task" / "synthetic-run").mkdir(parents=True)
    (source / "code-task" / "synthetic-run" / "large-worktree.bin").write_bytes(
        b"x" * 4096
    )
    after = _run(source, tmp_path / "target-after")

    assert not _errors(before)
    assert not _errors(after)
    assert after["preflight"]["disk_required_bytes"] == before["preflight"][
        "disk_required_bytes"
    ]


@pytest.mark.parametrize("root_name", ["state", "workspace", "media"])
@pytest.mark.parametrize("descendant", [False, True], ids=["exact", "descendant"])
def test_configured_data_root_inside_source_code_task_blocks_import(
    tmp_path: Path,
    root_name: str,
    descendant: bool,
) -> None:
    source = _build_source_home(tmp_path)
    configured = source / "code-task"
    if descendant:
        configured = configured / "selected-subdir"
    configured.mkdir(parents=True)
    (configured / "must-not-migrate.txt").write_text("local run data\n", encoding="utf-8")
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    if root_name == "media":
        payload.setdefault("attachments", {})["media_root"] = str(configured)
    else:
        payload[f"{root_name}_dir"] = str(configured)
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")

    preview = _run(source, tmp_path / f"target-preview-{root_name}-{descendant}")
    applied_target = tmp_path / f"target-apply-{root_name}-{descendant}"
    applied = _run(source, applied_target, apply=True)

    for report in (preview, applied):
        data_root_errors = [
            item
            for item in _errors(report)
            if item["kind"] == "preflight/data-root"
            and item["source"] == str(configured)
        ]
        assert data_root_errors
        assert "inside source code-task" in data_root_errors[0]["reason"]
        assert data_root_errors[0]["details"]["stable_code"] == "configured_code_task_root"
    assert not applied_target.exists()


@pytest.mark.parametrize("configured_kind", ["workspace", "agent-workspace"])
def test_case_alias_root_inside_source_code_task_blocks_import(
    tmp_path: Path,
    configured_kind: str,
) -> None:
    source = _build_source_home(tmp_path)
    actual = source / "code-task" / "selected-subdir"
    actual.mkdir(parents=True)
    (actual / "must-not-migrate.txt").write_text(
        "local run data\n",
        encoding="utf-8",
    )
    configured = source / "CODE-TASK" / "selected-subdir"
    try:
        if not configured.samefile(actual):
            pytest.skip("filesystem does not expose a case-insensitive alias")
    except OSError:
        pytest.skip("filesystem is case-sensitive")

    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    if configured_kind == "workspace":
        payload["workspace_dir"] = str(configured)
    else:
        payload["agents"] = [{"id": "research", "workspace": str(configured)}]
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    data_root_errors = [
        item
        for item in _errors(report)
        if item["kind"] == "preflight/data-root"
        and item["source"] == str(configured)
    ]
    assert data_root_errors
    assert data_root_errors[0]["details"]["stable_code"] == "configured_code_task_root"
    assert not target.exists()


def test_dotenv_data_root_inside_source_code_task_blocks_import(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    configured = source / "code-task"
    configured.mkdir()
    (configured / "must-not-migrate.txt").write_text("local run data\n", encoding="utf-8")
    (source / ".env").write_text(
        "OPENROUTER_API_KEY=dummy\n"
        f"OPENSQUILLA_GATEWAY_WORKSPACE_DIR={configured}\n",
        encoding="utf-8",
    )
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    data_root_errors = [
        item
        for item in _errors(report)
        if item["kind"] == "preflight/data-root" and item["source"] == str(configured)
    ]
    assert data_root_errors
    assert "inside source code-task" in data_root_errors[0]["reason"]
    assert data_root_errors[0]["details"]["stable_code"] == "configured_code_task_root"
    assert not target.exists()


def test_agent_workspace_inside_source_code_task_blocks_import(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    configured = source / "code-task" / "agent-run"
    configured.mkdir(parents=True)
    (configured / "must-not-migrate.txt").write_text("local run data\n", encoding="utf-8")
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["agents"] = [{"id": "research", "workspace": str(configured)}]
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    data_root_errors = [
        item
        for item in _errors(report)
        if item["kind"] == "preflight/data-root" and item["source"] == str(configured)
    ]
    assert data_root_errors
    assert "inside source code-task" in data_root_errors[0]["reason"]
    assert data_root_errors[0]["details"]["stable_code"] == "configured_code_task_root"
    assert not target.exists()


def test_other_excluded_profile_trees_do_not_block_or_inflate_import(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    before = _run(source, tmp_path / "target-before")
    generated = source / "profiles" / "retired" / "code-task" / "run"
    generated.mkdir(parents=True)
    (generated / "large-worktree.bin").write_bytes(b"x" * 4096)
    if os.name != "nt":
        (generated / "python").symlink_to("/missing/generated/python")
    target = tmp_path / "target-home"

    preview = _run(source, target)
    applied = _run(source, target, apply=True)

    assert not _errors(before)
    assert not _errors(preview)
    assert not _errors(applied)
    assert preview["preflight"]["disk_required_bytes"] == before["preflight"][
        "disk_required_bytes"
    ]
    assert not (target / "profiles").exists()


@pytest.mark.parametrize("root_name", ["state", "workspace", "media"])
@pytest.mark.parametrize("invalid_shape", ["missing", "file"])
def test_invalid_configured_data_root_without_canonical_fallback_blocks_apply(
    tmp_path: Path,
    root_name: str,
    invalid_shape: str,
) -> None:
    source = _build_source_home(tmp_path)
    canonical = source / root_name
    if canonical.exists():
        shutil.rmtree(canonical)
    configured = tmp_path / f"invalid-{root_name}"
    if invalid_shape == "file":
        configured.write_text("not a directory", encoding="utf-8")
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    if root_name == "media":
        payload.setdefault("attachments", {})["media_root"] = str(configured)
    else:
        payload[f"{root_name}_dir"] = str(configured)
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    data_root_errors = [
        item
        for item in _errors(report)
        if item["kind"] == "preflight/data-root" and item["source"] == str(configured)
    ]
    assert data_root_errors
    assert "refusing to drop its config pin" in data_root_errors[0]["reason"]
    assert not target.exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


def test_malformed_config_blocks_apply_without_target_or_marker(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    (source / "config.toml").write_text("[llm\nprovider =", encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(item["kind"] == "preflight/config" for item in _errors(report))
    assert not target.exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


def test_schema_invalid_config_blocks_apply_without_target_or_marker(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["port"] = "not-a-port"
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(item["kind"] == "preflight/config" for item in _errors(report))
    assert not target.exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


@pytest.mark.parametrize("nested", [False, True])
def test_unknown_config_field_blocks_without_silently_deleting_it(
    tmp_path: Path,
    nested: bool,
) -> None:
    source = _build_source_home(tmp_path)
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    if nested:
        payload["llm"]["future_provider_option"] = "preserve-me"
    else:
        payload["future_profile_option"] = {"value": "preserve-me"}
    config_bytes = tomli_w.dumps(payload).encode()
    (source / "config.toml").write_bytes(config_bytes)
    target = tmp_path / "target-home"

    preview = _run(source, target)
    applied = _run(source, target, apply=True)

    for report in (preview, applied):
        assert any(
            item["kind"] == "preflight/config"
            and "cannot be preserved losslessly" in item["reason"]
            for item in _errors(report)
        )
    assert (source / "config.toml").read_bytes() == config_bytes
    assert not target.exists()
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))


def test_unreadable_sessions_database_blocks_apply(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    (source / "state" / "sessions.db").write_bytes(b"not a sqlite database")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(item["kind"] == "preflight/schema" for item in _errors(report))
    assert not target.exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


def test_corrupt_secondary_sqlite_store_blocks_dry_run_and_apply(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    corrupt = source / "state" / "approval_queue.sqlite"
    corrupt.write_bytes(b"not a sqlite database")
    target = tmp_path / "target-home"

    preview = _run(source, target, apply=False)
    applied = _run(source, target, apply=True)

    for report in (preview, applied):
        assert any(item["kind"] == "preflight/sqlite" for item in _errors(report))
    assert not target.exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


def test_scheduler_pause_failure_aborts_without_target_or_marker(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    connection = sqlite3.connect(source / "state" / "scheduler.db")
    try:
        connection.execute(
            "CREATE TRIGGER reject_pause BEFORE UPDATE OF enabled ON scheduler_jobs "
            "BEGIN SELECT RAISE(ABORT, 'pause rejected'); END"
        )
        connection.commit()
    finally:
        connection.close()
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(item["kind"] == "scheduler" for item in _errors(report))
    assert not target.exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX link contract; Windows reparse points stay fail-closed",
)
def test_contained_workspace_symlinks_are_preserved_without_dereferencing(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    project = source / "workspace" / "synthetic-project"
    project.mkdir()
    (project / "COPYING").write_text("synthetic license\n", encoding="utf-8")
    license_link = project / "LICENSE"
    license_payload = "COPYING"
    license_link.symlink_to(license_payload)
    bin_dir = project / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    dangling_link = bin_dir / "synthetic-tool"
    dangling_payload = "../missing-package/bin/synthetic-tool"
    dangling_link.symlink_to(dangling_payload)
    legacy_project = source / "state" / "workspace" / "legacy-project"
    legacy_project.mkdir(parents=True)
    (legacy_project / "COPYING").write_text("legacy license\n", encoding="utf-8")
    legacy_license = legacy_project / "LICENSE"
    legacy_license.symlink_to("COPYING")
    target = tmp_path / "target-home"

    preview = _run(source, target, apply=False)
    applied = _run(source, target, apply=True)

    assert not _errors(preview)
    assert not _errors(applied)
    imported_license = target / "workspace" / "synthetic-project" / "LICENSE"
    imported_dangling = (
        target
        / "workspace"
        / "synthetic-project"
        / "node_modules"
        / ".bin"
        / "synthetic-tool"
    )
    assert imported_license.is_symlink()
    assert os.readlink(imported_license) == license_payload
    assert imported_dangling.is_symlink()
    assert not imported_dangling.exists()
    assert os.readlink(imported_dangling) == dangling_payload
    imported_legacy_license = (
        target / "state" / "workspace" / "legacy-project" / "LICENSE"
    )
    assert imported_legacy_license.is_symlink()
    assert os.readlink(imported_legacy_license) == "COPYING"
    assert license_link.is_symlink()
    assert os.readlink(license_link) == license_payload
    assert dangling_link.is_symlink()
    assert os.readlink(dangling_link) == dangling_payload
    assert legacy_license.is_symlink()
    assert os.readlink(legacy_license) == "COPYING"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX link contract; Windows reparse points stay fail-closed",
)
@pytest.mark.parametrize(
    "link_kind",
    ["absolute", "relative-escape", "relative-exit-and-reenter"],
)
def test_workspace_link_outside_root_is_rejected_without_publication(
    tmp_path: Path,
    link_kind: str,
) -> None:
    source = _build_source_home(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be followed", encoding="utf-8")
    link = source / "workspace" / "linked.txt"
    if link_kind == "relative-exit-and-reenter":
        project = source / "workspace" / "project"
        project.mkdir()
        (project / "COPYING").write_text("license\n", encoding="utf-8")
        link = project / "linked.txt"
        payload = "../../workspace/project/COPYING"
    else:
        payload = str(outside) if link_kind == "absolute" else "../../outside.txt"
    link.symlink_to(payload)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(item["kind"] == "preflight/manifest" for item in _errors(report))
    assert not target.exists()
    assert link.is_symlink()
    assert os.readlink(link) == payload


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX link contract; Windows reparse points stay fail-closed",
)
def test_contained_link_outside_workspace_still_fails_final_role_manifest(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    skill = source / "skills" / "dummy-skill"
    (skill / "SKILL.alias.md").symlink_to("SKILL.md")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(item["kind"] == "preflight/manifest" for item in _errors(report))
    assert not target.exists()


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is unavailable on Windows")
def test_special_file_in_source_tree_is_rejected_without_publication(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    os.mkfifo(source / "workspace" / "synthetic.fifo")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(item["kind"] == "preflight/manifest" for item in _errors(report))
    assert not target.exists()


def test_source_change_after_copy_aborts_before_target_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    target.mkdir()
    existing = target / "existing.txt"
    existing.write_text("preserve", encoding="utf-8")
    migrator = OpenSquillaHomeMigrator(
        OpenSquillaMigrationOptions(
            source=source,
            target=target,
            apply=True,
            replace_target=True,
            confirm_replace_target=target,
        )
    )
    original_copy = migrator._copy_source_snapshots

    def copy_then_change(staging: Path) -> None:
        original_copy(staging)
        (source / "workspace" / "MEMORY.md").write_text(
            "source changed during copy", encoding="utf-8"
        )

    monkeypatch.setattr(migrator, "_copy_source_snapshots", copy_then_change)

    report = migrator.migrate()

    assert any("source changed during import" in item["reason"] for item in _errors(report))
    assert existing.read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob("target-home.backup.*"))


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX link contract; Windows reparse points stay fail-closed",
)
def test_workspace_link_change_after_copy_aborts_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path)
    project = source / "workspace" / "project"
    project.mkdir()
    (project / "COPYING").write_text("license\n", encoding="utf-8")
    (project / "OTHER").write_text("other\n", encoding="utf-8")
    link = project / "LICENSE"
    link.symlink_to("COPYING")
    target = tmp_path / "target-home"
    migrator = OpenSquillaHomeMigrator(
        OpenSquillaMigrationOptions(source=source, target=target, apply=True)
    )
    original_copy = migrator._copy_source_snapshots

    def copy_then_change_link(staging: Path) -> None:
        original_copy(staging)
        link.unlink()
        link.symlink_to("OTHER")

    monkeypatch.setattr(migrator, "_copy_source_snapshots", copy_then_change_link)

    report = migrator.migrate()

    assert any("source changed during import" in item["reason"] for item in _errors(report))
    assert not target.exists()
    assert not list(tmp_path.glob("target-home.backup.*"))
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent-component symlink race")
@pytest.mark.parametrize("replacement", ["relocated", "external"])
def test_workspace_parent_swap_after_preflight_fails_closed_without_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    source = _build_source_home(tmp_path / "source-root")
    config = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    config.pop("state_dir", None)
    config.pop("workspace_dir", None)
    (source / "config.toml").write_text(tomli_w.dumps(config), encoding="utf-8")
    target = tmp_path / "target-home"
    migrator = OpenSquillaHomeMigrator(
        OpenSquillaMigrationOptions(source=source, target=target, apply=True)
    )
    original_copy = migrator._copy_source_snapshots
    original_parent = source / "workspace" / "memory"
    relocated_parent = source / "workspace" / "memory-relocated"
    swap_performed = False

    def swap_parent_then_copy(staging: Path) -> None:
        nonlocal swap_performed
        original_parent.rename(relocated_parent)
        if replacement == "relocated":
            link_target = Path(relocated_parent.name)
        else:
            link_target = tmp_path / "outside-memory"
            shutil.copytree(relocated_parent, link_target)
        original_parent.symlink_to(link_target, target_is_directory=True)
        swap_performed = True
        original_copy(staging)

    monkeypatch.setattr(migrator, "_copy_source_snapshots", swap_parent_then_copy)

    report = migrator.migrate()

    assert swap_performed, _errors(report)
    assert any(item["kind"] == "apply" for item in _errors(report))
    assert not target.exists()
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))
    assert not list(tmp_path.glob("target-home.backup.*"))
    assert not (tmp_path / ".target-home.profile-replace.json").exists()
    assert not (tmp_path / "profile-replacement-history.json").exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent-component symlink race")
def test_sqlite_state_parent_swap_never_reopens_source_bundle_or_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path / "source-root")
    config = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    config.pop("state_dir", None)
    config.pop("workspace_dir", None)
    (source / "config.toml").write_text(tomli_w.dumps(config), encoding="utf-8")
    source_db = source / "state" / "sessions.db"
    writer = sqlite3.connect(source_db)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE parent_swap_payload (value TEXT NOT NULL)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO parent_swap_payload VALUES ('committed-in-wal')")
        writer.commit()
        assert source_db.with_name("sessions.db-wal").stat().st_size > 32

        target = tmp_path / "target-home"
        migrator = OpenSquillaHomeMigrator(
            OpenSquillaMigrationOptions(source=source, target=target, apply=True)
        )
        original_snapshot_sqlite = migrator._snapshot_sqlite_stores
        original_copy_sqlite_bundle = migration_module._copy_sqlite_bundle
        original_state = source / "state"
        relocated_state = source / "state-relocated"
        state_swapped = False
        reopened_source_bundle = False

        def track_sqlite_bundle_reopen(
            database: Path,
            destination_dir: Path,
            *,
            verify_stable_bundle: bool = False,
        ) -> Path:
            nonlocal reopened_source_bundle
            if state_swapped and database.is_relative_to(original_state):
                reopened_source_bundle = True
            return original_copy_sqlite_bundle(
                database,
                destination_dir,
                verify_stable_bundle=verify_stable_bundle,
            )

        def swap_state_then_snapshot(staging: Path) -> None:
            nonlocal state_swapped
            original_state.rename(relocated_state)
            original_state.symlink_to(relocated_state, target_is_directory=True)
            state_swapped = True
            original_snapshot_sqlite(staging)

        monkeypatch.setattr(
            migration_module,
            "_copy_sqlite_bundle",
            track_sqlite_bundle_reopen,
        )
        monkeypatch.setattr(migrator, "_snapshot_sqlite_stores", swap_state_then_snapshot)

        report = migrator.migrate()
    finally:
        writer.close()

    assert state_swapped, _errors(report)
    assert not reopened_source_bundle
    assert any(item["kind"] == "apply" for item in _errors(report))
    assert not target.exists()
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))
    assert not list(tmp_path.glob("target-home.backup.*"))
    assert not (tmp_path / ".target-home.profile-replace.json").exists()
    assert not (tmp_path / "profile-replacement-history.json").exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


def test_source_gateway_appearing_during_published_validation_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path / "source-root")
    target = tmp_path / "target-home"
    (target / "workspace").mkdir(parents=True)
    original = target / "workspace" / "SOUL.md"
    original.write_text("original target\n", encoding="utf-8")
    original_validate = OpenSquillaHomeMigrator._validate_published_target

    def validate_then_start_old_gateway(
        migrator: OpenSquillaHomeMigrator,
        journal_snapshot: Any,
        journal_payload: dict[str, Any],
    ) -> dict[str, int]:
        identity = original_validate(migrator, journal_snapshot, journal_payload)
        (source / "state" / "gateway.pid.lock").write_bytes(
            b"synthetic old gateway authority\n"
        )
        return identity

    monkeypatch.setattr(
        OpenSquillaHomeMigrator,
        "_validate_published_target",
        validate_then_start_old_gateway,
    )

    report = _run(
        source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )

    assert any(
        "source legacy gateway authority changed" in item["reason"]
        for item in _errors(report)
    )
    assert original.read_text(encoding="utf-8") == "original target\n"
    assert not list(tmp_path.glob("target-home.backup.*"))
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))
    assert not (tmp_path / ".target-home.profile-replace.json").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows lock snapshot regression")
def test_apply_error_is_not_misclassified_as_source_authority_change_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path / "source-root")
    target = tmp_path / "target-home"
    lock_path = source / "state" / "gateway.pid.lock"
    lock_path.write_bytes(b"stable legacy gateway lock\n")

    def fail_commit(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("synthetic apply failure")

    monkeypatch.setattr(OpenSquillaHomeMigrator, "_commit", fail_commit)

    report = _run(source, target, apply=True)

    reasons = [item["reason"] for item in _errors(report)]
    assert any("synthetic apply failure" in reason for reason in reasons)
    assert not any("source legacy gateway authority changed" in reason for reason in reasons)
    assert not target.exists()


def test_source_authority_files_are_never_copied_or_modified(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    legacy_marker = source / IMPORT_MARKER_FILENAME
    legacy_marker.write_text('{"legacy": true}\n', encoding="utf-8")
    (source / "desktop-layout-v2.json").write_text("{}\n", encoding="utf-8")
    old_authority = source / "migration" / "opensquilla" / "old" / "report.json"
    old_authority.parent.mkdir(parents=True)
    old_authority.write_text("{}\n", encoding="utf-8")
    before = _file_bytes(source)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert _file_bytes(source) == before
    assert not (target / IMPORT_MARKER_FILENAME).exists()
    assert not (target / "desktop-layout-v2.json").exists()
    assert not (target / "migration" / "opensquilla" / "old").exists()


def test_non_session_target_collision_requires_overwrite(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    (target / "workspace").mkdir(parents=True)
    existing = target / "workspace" / "existing.txt"
    existing.write_text("keep-me", encoding="utf-8")

    report = _run(source, target, apply=True)

    assert any(item["kind"] == "preflight/target" for item in _errors(report))
    assert existing.read_text(encoding="utf-8") == "keep-me"
    assert not (source / IMPORT_MARKER_FILENAME).exists()


@pytest.mark.parametrize("external_role", ["state", "workspace", "media", "agent"])
def test_replacement_blocks_target_external_roots_until_complete_backup_exists(
    tmp_path: Path,
    external_role: str,
) -> None:
    source = _build_source_home(tmp_path / "source-root")
    target = tmp_path / "target-home"
    (target / "state").mkdir(parents=True)
    (target / "workspace").mkdir()
    (target / "workspace" / "SOUL.md").write_text("current\n", encoding="utf-8")
    external = tmp_path / f"external-target-{external_role}"
    external.mkdir()
    (external / "sentinel.txt").write_text("do-not-touch\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "state_dir": str(external if external_role == "state" else target / "state"),
        "workspace_dir": str(
            external if external_role == "workspace" else target / "workspace"
        ),
        "port": 18791,
    }
    if external_role == "media":
        payload["attachments"] = {"media_root": str(external)}
    if external_role == "agent":
        payload["agents"] = [{"id": "research", "workspace": str(external)}]
    (target / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target_before = _file_bytes(target)
    external_before = _file_bytes(external)

    report = _run(
        source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )

    assert any(
        item["kind"] == "preflight/target"
        and "would not contain all current profile data" in item["reason"]
        for item in _errors(report)
    )
    assert _file_bytes(target) == target_before
    assert _file_bytes(external) == external_before
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))
    assert not list(tmp_path.glob("target-home.backup.*"))
    assert not (tmp_path / ".target-home.profile-replace.json").exists()


def test_replacement_guard_reads_legacy_target_state_dotenv(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path / "source-root")
    target = tmp_path / "target-home"
    (target / "state").mkdir(parents=True)
    (target / "workspace").mkdir()
    (target / "workspace" / "SOUL.md").write_text("current\n", encoding="utf-8")
    external = tmp_path / "external-legacy-state"
    external.mkdir()
    (external / "sentinel.txt").write_text("preserve\n", encoding="utf-8")
    (target / "state" / ".env").write_text(
        f"OPENSQUILLA_GATEWAY_STATE_DIR={external}\n",
        encoding="utf-8",
    )
    target_before = _file_bytes(target)

    report = _run(
        source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )

    assert any(
        "would not contain all current profile data" in item["reason"]
        for item in _errors(report)
    )
    assert _file_bytes(target) == target_before
    assert (external / "sentinel.txt").read_text(encoding="utf-8") == "preserve\n"


def test_empty_target_import_rejects_ambient_external_workspace_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path)
    external = tmp_path / "ambient-live-workspace"
    external.mkdir()
    (external / "SOUL.md").write_text("unrelated live identity\n", encoding="utf-8")
    target = tmp_path / "target-home"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_WORKSPACE_DIR", str(external))

    report = _run(source, target, apply=True)

    assert any(
        item["kind"] == "preflight/target"
        and "cannot share another live data root" in item["reason"]
        for item in _errors(report)
    )
    assert not target.exists()
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))
    assert (external / "SOUL.md").read_text(encoding="utf-8") == (
        "unrelated live identity\n"
    )


def test_overwrite_publish_failure_restores_complete_original_target(
    tmp_path: Path, monkeypatch
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    (target / "state").mkdir(parents=True)
    (target / "state" / "sessions.db").write_bytes(b"original-session-store")
    (target / "workspace").mkdir()
    (target / "workspace" / "original.txt").write_text("original", encoding="utf-8")
    original_move = recovery_module.native_move_no_replace

    def fail_staging_publish(
        src: str | Path,
        dst: str | Path,
        **move_options: object,
    ) -> None:
        source_path = Path(src)
        destination_path = Path(dst)
        if ".profile-staging." in source_path.name and destination_path == target:
            raise OSError("synthetic publish failure")
        original_move(src, dst, **move_options)

    monkeypatch.setattr(recovery_module, "native_move_no_replace", fail_staging_publish)

    report = _run(
        source,
        target,
        apply=True,
        overwrite=True,
        confirm_replace_target=target,
    )

    assert _errors(report)
    assert (target / "state" / "sessions.db").read_bytes() == b"original-session-store"
    assert (target / "workspace" / "original.txt").read_text(encoding="utf-8") == "original"
    assert not (source / IMPORT_MARKER_FILENAME).exists()


@pytest.mark.parametrize(
    "failure_phase",
    ["target_parking", "first_publication", "replacement_publication"],
)
def test_post_move_unknown_state_preserves_journal_and_all_observed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    source = _build_source_home(tmp_path)
    source_before = _file_bytes(source)
    target = tmp_path / "target-home"
    replacing = failure_phase in {"target_parking", "replacement_publication"}
    if replacing:
        (target / "workspace").mkdir(parents=True)
        (target / "workspace" / "original.txt").write_text(
            "original", encoding="utf-8"
        )
    original_move = recovery_module.native_move_no_replace

    def move_then_lose_post_state(
        src: str | Path,
        dst: str | Path,
        **move_options: object,
    ) -> None:
        source_path = Path(src)
        destination_path = Path(dst)
        is_parking = source_path == target and ".backup." in destination_path.name
        is_publication = (
            ".profile-staging." in source_path.name and destination_path == target
        )
        should_fail = (
            failure_phase == "target_parking"
            and is_parking
            or failure_phase == "first_publication"
            and not replacing
            and is_publication
            or failure_phase == "replacement_publication"
            and replacing
            and is_publication
        )
        original_move(src, dst, **move_options)
        if should_fail:
            raise recovery_module.AtomicStateUnknownError(
                "synthetic post-move state is unknown"
            )

    monkeypatch.setattr(
        recovery_module,
        "native_move_no_replace",
        move_then_lose_post_state,
    )

    report = _run(
        source,
        target,
        apply=True,
        replace_target=replacing,
        confirm_replace_target=target if replacing else None,
    )

    assert _errors(report)
    journal = tmp_path / ".target-home.profile-replace.json"
    assert journal.is_file()
    payload = json.loads(journal.read_text(encoding="utf-8"))
    staging = Path(payload["staging"])
    backup = Path(payload["backup"])
    if failure_phase == "target_parking":
        assert not target.exists()
        assert staging.is_dir()
        assert (backup / "workspace" / "original.txt").read_text(
            encoding="utf-8"
        ) == "original"
    elif failure_phase == "first_publication":
        assert target.is_dir()
        assert not staging.exists()
        assert not backup.exists()
    else:
        assert target.is_dir()
        assert not staging.exists()
        assert (backup / "workspace" / "original.txt").read_text(
            encoding="utf-8"
        ) == "original"
    assert _file_bytes(source) == source_before
    inspected = recovery_module.inspect_profile(
        target,
        profile_kind="desktop-primary",
    )
    assert inspected.outcome == "recovery_required"
    assert inspected.stable_code == "transaction_incomplete"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows reacquire failures are intentionally state-unknown",
)
def test_lock_handoff_failure_after_publish_rolls_back_complete_original_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.recovery.locking as locking_module

    source = _build_source_home(tmp_path)
    source_before = _file_bytes(source)
    target = tmp_path / "target-home"
    (target / "state").mkdir(parents=True)
    (target / "state" / "sessions.db").write_bytes(b"original-session-store")
    (target / "workspace").mkdir()
    (target / "workspace" / "original.txt").write_text("original", encoding="utf-8")

    def fail_handoff(source_state: Path, _destination_state: Path) -> None:
        if ".profile-staging." in source_state.parent.name:
            raise recovery_module.UnsafePathError("synthetic lock handoff failure")

    monkeypatch.setattr(
        locking_module,
        "rebind_legacy_gateway_lock",
        fail_handoff,
    )

    report = _run(
        source,
        target,
        apply=True,
        overwrite=True,
        confirm_replace_target=target,
    )

    assert _errors(report)
    assert (target / "state" / "sessions.db").read_bytes() == b"original-session-store"
    assert (target / "workspace" / "original.txt").read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))
    assert not (tmp_path / ".target-home.profile-replace.json").exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()
    assert _file_bytes(source) == source_before


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows lock handoff")
def test_windows_lock_reacquire_failure_preserves_profile_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.recovery.locking as locking_module

    source = _build_source_home(tmp_path)
    source_before = _file_bytes(source)
    target = tmp_path / "target-home"
    (target / "state").mkdir(parents=True)
    (target / "state" / "sessions.db").write_bytes(b"original-session-store")
    (target / "workspace").mkdir()
    (target / "workspace" / "original.txt").write_text("original", encoding="utf-8")
    original_reacquire = locking_module._reacquire_suspended_legacy_locks

    def fail_candidate_reacquire(moves, *, destination: bool) -> None:
        if destination and any(
            ".profile-staging." in item.source_state.parent.name for item in moves
        ):
            raise recovery_module.LegacyGatewayRunningError(
                "synthetic old gateway won the lock handoff"
            )
        original_reacquire(moves, destination=destination)

    monkeypatch.setattr(
        locking_module,
        "_reacquire_suspended_legacy_locks",
        fail_candidate_reacquire,
    )

    report = _run(
        source,
        target,
        apply=True,
        overwrite=True,
        confirm_replace_target=target,
    )

    assert _errors(report)
    journal = tmp_path / ".target-home.profile-replace.json"
    payload = json.loads(journal.read_text(encoding="utf-8"))
    backup = Path(payload["backup"])
    assert payload["phase"] == "target_parked"
    assert (backup / "state" / "sessions.db").read_bytes() == b"original-session-store"
    assert (target / "workspace" / "SOUL.md").is_file()
    assert _file_bytes(source) == source_before
    inspected = recovery_module.inspect_profile(
        target,
        profile_kind="desktop-primary",
    )
    assert inspected.outcome == "recovery_required"
    assert inspected.stable_code == "transaction_incomplete"


def test_published_candidate_holds_legacy_gateway_lock_during_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    observed: list[str] = []
    original_validate = OpenSquillaHomeMigrator._validate_published_target

    def validate_while_old_gateway_contends(
        migrator: OpenSquillaHomeMigrator,
        journal_snapshot: Any,
        journal_payload: dict[str, Any],
    ) -> dict[str, Any]:
        context = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
        queue = context.Queue()
        process = context.Process(
            target=_probe_gateway_lock,
            args=(str(target / "state"), queue),
        )
        process.start()
        process.join(timeout=10)
        assert process.exitcode == 0
        observed.append(queue.get(timeout=1))
        return original_validate(migrator, journal_snapshot, journal_payload)

    monkeypatch.setattr(
        OpenSquillaHomeMigrator,
        "_validate_published_target",
        validate_while_old_gateway_contends,
    )

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert observed == ["busy"]


def test_profile_import_contends_with_desktop_global_cleanup_lock(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    source_before = _file_bytes(source)
    acquired = threading.Event()
    release = threading.Event()

    def hold_cleanup_authority() -> None:
        with ProfileOperationLock(target.parent):
            acquired.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold_cleanup_authority)
    holder.start()
    assert acquired.wait(timeout=5)
    try:
        report = _run(source, target, apply=True)
    finally:
        release.set()
        holder.join(timeout=5)

    assert any(item["kind"] == "preflight/lock" for item in _errors(report))
    assert _file_bytes(source) == source_before
    assert not target.exists()
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))
    assert not (tmp_path / ".target-home.profile-replace.json").exists()
    assert not (tmp_path / "profile-replacement-history.json").exists()


@pytest.mark.parametrize(
    "failed_phase",
    ["target_parked", "candidate_published_unvalidated", "committed"],
)
def test_journal_phase_write_failure_rolls_back_complete_original_target(
    tmp_path: Path,
    monkeypatch,
    failed_phase: str,
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    (target / "state").mkdir(parents=True)
    (target / "state" / "sessions.db").write_bytes(b"original-session-store")
    (target / "workspace").mkdir()
    (target / "workspace" / "original.txt").write_text("original", encoding="utf-8")
    original_journal_write = migration_module._cas_publish_json

    def fail_phase_write(snapshot: Any, payload: dict[str, Any]) -> Any:
        if payload.get("phase") == failed_phase:
            raise OSError(f"synthetic {failed_phase} journal failure")
        return original_journal_write(snapshot, payload)

    monkeypatch.setattr(migration_module, "_cas_publish_json", fail_phase_write)

    report = _run(
        source,
        target,
        apply=True,
        overwrite=True,
        confirm_replace_target=target,
    )

    assert _errors(report)
    assert (target / "state" / "sessions.db").read_bytes() == b"original-session-store"
    assert (target / "workspace" / "original.txt").read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob("target-home.backup.*"))
    assert not (tmp_path / ".target-home.profile-replace.json").exists()
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))
    assert not (tmp_path / "profile-replacement-history.json").exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


@pytest.mark.parametrize("existing", [False, True])
def test_import_journal_post_publish_sync_failure_is_atomic_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    from opensquilla.recovery.config_patch import ConfigSnapshot

    journal = tmp_path / ".target.profile-replace.json"
    if existing:
        journal.write_text('{"phase":"prepared"}\n', encoding="utf-8")
    snapshot = ConfigSnapshot.capture(journal)
    payload = {"phase": "validated", "transaction_id": str(uuid.uuid4())}

    def fail_directory_sync(_path: Path) -> None:
        raise OSError("synthetic post-publication directory fsync failure")

    monkeypatch.setattr(migration_module, "_fsync_directory", fail_directory_sync)

    with pytest.raises(recovery_module.AtomicStateUnknownError):
        migration_module._cas_publish_json(snapshot, payload)

    assert json.loads(journal.read_text(encoding="utf-8")) == payload


def test_crash_after_prepared_journal_is_cleaned_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    original_journal_write = migration_module._cas_publish_json

    with monkeypatch.context() as scoped:
        def crash_after_prepared(snapshot: Any, payload: dict[str, Any]) -> Any:
            published = original_journal_write(snapshot, payload)
            if payload.get("phase") == "prepared":
                raise KeyboardInterrupt("synthetic crash after prepared")
            return published

        scoped.setattr(migration_module, "_cas_publish_json", crash_after_prepared)
        with pytest.raises(KeyboardInterrupt, match="synthetic crash"):
            _run(source, target, apply=True)

    journal = tmp_path / ".target-home.profile-replace.json"
    old_staging = next(tmp_path.glob(".target-home.profile-staging.*"))
    assert journal.is_file()
    assert not target.exists()

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert any(item["kind"] == "recovery" for item in report["items"])
    assert not journal.exists()
    assert not old_staging.exists()
    assert (target / "workspace" / "MEMORY.md").is_file()


def test_crash_after_empty_target_park_restores_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    target.mkdir()
    original_journal_write = migration_module._cas_publish_json

    with monkeypatch.context() as scoped:
        def crash_before_parked_phase(snapshot: Any, payload: dict[str, Any]) -> Any:
            if payload.get("phase") == "target_parked":
                raise KeyboardInterrupt("synthetic crash after empty target park")
            return original_journal_write(snapshot, payload)

        scoped.setattr(
            migration_module,
            "_cas_publish_json",
            crash_before_parked_phase,
        )
        with pytest.raises(KeyboardInterrupt, match="empty target park"):
            _run(source, target, apply=True)

    journal = tmp_path / ".target-home.profile-replace.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "prepared"
    assert not target.exists()
    assert len(list(tmp_path.glob("target-home.backup.*"))) == 1

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert any(item["kind"] == "recovery" for item in report["items"])
    assert not journal.exists()
    assert (target / "workspace" / "MEMORY.md").is_file()


def test_published_recovery_required_candidate_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    target.mkdir()
    original = target / "original.txt"
    original.write_text("preserve", encoding="utf-8")

    monkeypatch.setattr(
        recovery_module,
        "inspect_profile",
        lambda *_args, **_kwargs: SimpleNamespace(
            outcome="recovery_required",
            stable_code="synthetic_layout_failure",
            allowed_actions=(),
        ),
    )

    report = _run(
        source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )

    assert any("synthetic_layout_failure" in item["reason"] for item in _errors(report))
    assert original.read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob("target-home.backup.*"))
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))
    assert not (tmp_path / ".target-home.profile-replace.json").exists()
    assert not (tmp_path / "profile-replacement-history.json").exists()


def test_history_publication_failure_rolls_back_target_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    target.mkdir()
    original = target / "original.txt"
    original.write_text("preserve", encoding="utf-8")
    original_publish = migration_module._cas_publish_bytes

    def fail_history(snapshot: Any, data: bytes, *, mode: int) -> Any:
        if snapshot.path.name == "profile-replacement-history.json":
            raise OSError("synthetic history publication failure")
        return original_publish(snapshot, data, mode=mode)

    monkeypatch.setattr(migration_module, "_cas_publish_bytes", fail_history)

    report = _run(
        source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )

    assert any("history publication failure" in item["reason"] for item in _errors(report))
    assert original.read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob("target-home.backup.*"))
    assert not list(tmp_path.glob(".target-home.profile-staging.*"))
    assert not (tmp_path / ".target-home.profile-replace.json").exists()
    assert not (tmp_path / "profile-replacement-history.json").exists()


def test_source_change_after_history_publication_rolls_back_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path / "source-root")
    target = tmp_path / "target-home"
    target.mkdir()
    original = target / "original.txt"
    original.write_text("preserve", encoding="utf-8")
    original_history = OpenSquillaHomeMigrator._write_replacement_history

    def publish_history_then_change_source(
        migrator: OpenSquillaHomeMigrator,
        backup: Path,
        journal_payload: dict[str, Any],
        *,
        allow_existing: bool,
    ) -> Any:
        publication = original_history(
            migrator,
            backup,
            journal_payload,
            allow_existing=allow_existing,
        )
        (source / "workspace" / "MEMORY.md").write_text(
            "changed after history publication\n",
            encoding="utf-8",
        )
        return publication

    monkeypatch.setattr(
        OpenSquillaHomeMigrator,
        "_write_replacement_history",
        publish_history_then_change_source,
    )

    report = _run(
        source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )

    assert any("source changed during import" in item["reason"] for item in _errors(report))
    assert original.read_text(encoding="utf-8") == "preserve"
    assert not (tmp_path / ".target-home.profile-replace.json").exists()


@pytest.mark.parametrize("target_existed", [False, True])
def test_validated_import_crash_recovers_by_rolling_back_not_committing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_existed: bool,
) -> None:
    source = _build_source_home(tmp_path / "source-root")
    target = tmp_path / "target-home"
    if target_existed:
        (target / "workspace").mkdir(parents=True)
        (target / "state").mkdir()
        (target / "config.toml").write_text(
            'state_dir = "state"\nworkspace_dir = "workspace"\n',
            encoding="utf-8",
        )
        (target / "workspace" / "original.txt").write_text(
            "preserve\n",
            encoding="utf-8",
        )
    original_publish = migration_module._cas_publish_json

    def crash_before_committed_publish(snapshot: Any, payload: dict[str, Any]) -> Any:
        if payload.get("phase") == "committed":
            raise recovery_module.AtomicStateUnknownError(
                "synthetic process loss after validated phase"
            )
        return original_publish(snapshot, payload)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            migration_module,
            "_cas_publish_json",
            crash_before_committed_publish,
        )
        report = _run(
            source,
            target,
            apply=True,
            replace_target=target_existed,
            confirm_replace_target=target if target_existed else None,
        )

    assert _errors(report)
    journal = tmp_path / ".target-home.profile-replace.json"
    journal_payload = json.loads(journal.read_text(encoding="utf-8"))
    assert journal_payload["phase"] == "validated"
    before = recovery_module.inspect_profile(target, profile_kind="desktop-primary")
    recovered = recover_profile_transaction(
        target,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
        import_recoverer=migration_module.recover_interrupted_profile_import,
    )

    assert recovered.outcome == "ready"
    assert not journal.exists()
    if target_existed:
        assert (target / "workspace" / "original.txt").read_text(encoding="utf-8") == (
            "preserve\n"
        )
        history = json.loads(
            (tmp_path / "profile-replacement-history.json").read_text(encoding="utf-8")
        )
        assert all(
            item.get("transaction_id") != journal_payload["transaction_id"]
            for item in history["backups"]
        )
    else:
        assert not target.exists()
        assert not (tmp_path / "profile-replacement-history.json").exists()


def test_committed_journal_cannot_bypass_missing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    (target / "workspace").mkdir(parents=True)
    (target / "workspace" / "SOUL.md").write_text("previous\n", encoding="utf-8")
    (target / "config.toml").write_text("port = 18791\n", encoding="utf-8")
    transaction_id = "00000000-0000-0000-0000-000000000101"
    monkeypatch.setattr(
        migration_module.uuid,
        "uuid4",
        lambda: uuid.UUID(transaction_id),
    )
    finalized = tmp_path / (
        f".target-home.profile-replace.{transaction_id}.committed.json"
    )
    collision = b"existing recovery authority\n"
    finalized.write_bytes(collision)
    report = _run(
        source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )
    assert not _errors(report)
    journal = tmp_path / ".target-home.profile-replace.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "committed"
    assert finalized.read_bytes() == collision
    (tmp_path / "profile-replacement-history.json").unlink()

    inspected = recovery_module.inspect_profile(target, profile_kind="desktop-primary")

    assert inspected.outcome == "recovery_required"
    assert inspected.stable_code == "transaction_incomplete"


def test_desktop_marker_after_hardened_journal_finalize_keeps_transaction_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"

    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")

    report = _run(source, target, apply=True)

    assert not _errors(report)
    assert (target / "desktop-layout-v2.json").is_file()
    journal = tmp_path / ".target-home.profile-replace.json"
    assert not journal.exists()
    assert len(list(tmp_path.glob(".target-home.profile-replace.*.committed.json"))) == 1
    restarted = recovery_module.inspect_profile(
        target,
        profile_kind="desktop-primary",
    )
    assert restarted.outcome == "ready"
    assert restarted.stable_code == "canonical_workspace"


def test_reconcile_finalizes_retained_commit_after_target_metadata_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source_home(tmp_path / "first-source")
    target = tmp_path / "target-home"
    transaction_id = "00000000-0000-0000-0000-000000000102"
    finalized = tmp_path / (
        f".target-home.profile-replace.{transaction_id}.committed.json"
    )
    finalized.write_bytes(b"existing recovery authority\n")
    with monkeypatch.context() as scoped:
        scoped.setattr(
            migration_module.uuid,
            "uuid4",
            lambda: uuid.UUID(transaction_id),
        )
        first = _run(source, target, apply=True)

    assert not _errors(first)
    journal = tmp_path / ".target-home.profile-replace.json"
    assert journal.is_file()
    (target / "operator-note.txt").write_text("post-commit change\n", encoding="utf-8")
    finalized.unlink()

    reconciled = recovery_module.reconcile_profile(
        target,
        profile_kind="desktop-primary",
    )

    assert reconciled.outcome in {"ready", "attention"}
    assert not journal.exists()
    assert json.loads(finalized.read_text(encoding="utf-8"))["phase"] == "committed"

    second_source = _build_source_home(tmp_path / "second-source")
    (second_source / "workspace" / "MEMORY.md").write_text(
        "# second source\n",
        encoding="utf-8",
    )
    second = _run(
        second_source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )

    assert not _errors(second)
    assert (target / "workspace" / "MEMORY.md").read_text(encoding="utf-8") == (
        "# second source\n"
    )


def test_dry_run_config_compatibility_pass_has_no_global_side_effects(
    tmp_path: Path, monkeypatch
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    diagnostics_home = tmp_path / "diagnostics-home"
    monkeypatch.setattr(
        config_migration_module,
        "default_opensquilla_home",
        lambda: diagnostics_home,
    )
    memory_warned = config_migration_module._LEGACY_MEMORY_FIELDS_WARNED
    memory_seen = set(config_migration_module._LEGACY_MEMORY_FIELDS_SEEN)
    token_warned = config_migration_module._LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED
    token_seen = set(config_migration_module._LEGACY_AGENT_TOKEN_SAVING_FIELDS_SEEN)

    report = _run(source, target)

    assert not _errors(report)
    assert not diagnostics_home.exists()
    assert config_migration_module._LEGACY_MEMORY_FIELDS_WARNED is memory_warned
    assert config_migration_module._LEGACY_MEMORY_FIELDS_SEEN == memory_seen
    assert config_migration_module._LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED is token_warned
    assert config_migration_module._LEGACY_AGENT_TOKEN_SAVING_FIELDS_SEEN == token_seen


def _write_simple_sqlite(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE payload (value TEXT NOT NULL)")
        connection.execute("INSERT INTO payload VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def test_all_imported_sqlite_stores_are_consistent_without_report_copies(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    _write_simple_sqlite(source / "state" / "approval_queue.sqlite", "approval")
    _write_simple_sqlite(source / "state" / "sandbox_user_grants.sqlite", "sandbox")
    _write_simple_sqlite(source / "state" / "channel_delivery.sqlite", "delivery")
    _write_simple_sqlite(source / "state" / "agents" / "main" / "memory.db", "memory")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    expected = [
        Path("sessions.db"),
        Path("scheduler.db"),
        Path("approval_queue.sqlite"),
        Path("sandbox_user_grants.sqlite"),
        Path("channel_delivery.sqlite"),
        Path("agents/main/memory.db"),
    ]
    for relative in expected:
        imported = target / "state" / relative
        assert imported.is_file(), relative
        connection = sqlite3.connect(imported)
        try:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        finally:
            connection.close()
    output_dir = Path(report["output_dir"])
    assert not any(
        path.suffix in {".db", ".sqlite", ".sqlite3"}
        for path in output_dir.rglob("*")
        if path.is_file()
    )


def test_wal_only_committed_row_survives_as_normalized_sqlite_snapshot(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    db_path = source / "state" / "sessions.db"
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE wal_payload (value TEXT NOT NULL)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO wal_payload VALUES ('committed-in-wal')")
        writer.commit()
        assert db_path.with_name("sessions.db-wal").stat().st_size > 0
        source_before = _file_bytes(source)

        target = tmp_path / "target-home"
        report = _run(source, target, apply=True)
        source_after = _file_bytes(source)
    finally:
        writer.close()

    assert not _errors(report)
    assert source_after == source_before
    target_db = target / "state" / "sessions.db"
    assert not target_db.with_name("sessions.db-wal").exists()
    assert not target_db.with_name("sessions.db-shm").exists()
    connection = sqlite3.connect(target_db)
    try:
        assert connection.execute("SELECT value FROM wal_payload").fetchall() == [
            ("committed-in-wal",)
        ]
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    finally:
        connection.close()


def test_complete_wal_bundle_wins_over_identical_duplicate_base_database(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    canonical_db = source / "state" / "sessions.db"
    connection = sqlite3.connect(canonical_db)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE wal_bundle_payload (value TEXT NOT NULL)")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    external_state = tmp_path / "external-state"
    external_state.mkdir()
    external_db = external_state / "sessions.db"
    shutil.copy2(canonical_db, external_db)
    writer = sqlite3.connect(external_db)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO wal_bundle_payload VALUES ('committed-in-external-wal')")
        writer.commit()
        assert external_db.with_name("sessions.db-wal").stat().st_size > 32
        assert canonical_db.read_bytes() == external_db.read_bytes()
        payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
        payload["state_dir"] = str(external_state)
        (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
        target = tmp_path / "target-home"

        report = _run(source, target, apply=True)
    finally:
        writer.close()

    assert not _errors(report)
    target_connection = sqlite3.connect(target / "state" / "sessions.db")
    try:
        assert target_connection.execute("SELECT value FROM wal_bundle_payload").fetchall() == [
            ("committed-in-external-wal",)
        ]
    finally:
        target_connection.close()


def test_logically_conflicting_duplicate_sqlite_roots_fail_closed(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    canonical_db = source / "state" / "sessions.db"
    external_state = tmp_path / "external-state"
    external_state.mkdir()
    external_db = external_state / "sessions.db"
    shutil.copy2(canonical_db, external_db)
    connection = sqlite3.connect(external_db)
    try:
        connection.execute("CREATE TABLE divergent_payload (value TEXT NOT NULL)")
        connection.execute("INSERT INTO divergent_payload VALUES ('external-only')")
        connection.commit()
    finally:
        connection.close()
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["state_dir"] = str(external_state)
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert any(
        item["kind"] == "preflight/data-root"
        and "conflicting logical SQLite stores" in item["reason"]
        for item in _errors(report)
    )
    assert not target.exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


def test_conflicting_split_state_roots_fail_without_publishing(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    (source / "state" / "collision.bin").write_bytes(b"canonical")
    external_state = tmp_path / "external-state"
    external_state.mkdir()
    (external_state / "collision.bin").write_bytes(b"external")
    payload = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    payload["state_dir"] = str(external_state)
    (source / "config.toml").write_text(tomli_w.dumps(payload), encoding="utf-8")
    target = tmp_path / "target-home"

    preview = _run(source, target, apply=False)
    report = _run(source, target, apply=True)

    for result in (preview, report):
        assert any(item["kind"] == "preflight/data-root" for item in _errors(result))
    assert not target.exists()
    assert not (source / IMPORT_MARKER_FILENAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX staging-link cleanup contract")
def test_matching_staging_cleanup_handles_legacy_code_task_links_without_following(
    tmp_path: Path,
) -> None:
    staging = tmp_path / ".target.profile-staging.synthetic"
    code_task_bin = staging / "code-task" / "run" / "repo" / ".venv" / "bin"
    code_task_bin.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")
    (code_task_bin / "python").symlink_to(outside)
    expected = migration_module._path_identity_payload(staging)

    migration_module._remove_matching_staging(staging, expected)

    assert not staging.exists()
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is unavailable on Windows")
def test_matching_staging_cleanup_still_rejects_specials_inside_code_task(
    tmp_path: Path,
) -> None:
    staging = tmp_path / ".target.profile-staging.synthetic"
    special = staging / "code-task" / "run" / "synthetic.fifo"
    special.parent.mkdir(parents=True)
    os.mkfifo(special)
    expected = migration_module._path_identity_payload(staging)

    with pytest.raises(RuntimeError, match="special files"):
        migration_module._remove_matching_staging(staging, expected)

    assert staging.exists()
    assert special.exists()


def test_interrupted_overwrite_is_restored_before_retry(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    transaction_id = "123e4567-e89b-42d3-a456-426614174000"
    interrupted_backup = tmp_path / f"target-home.backup.{transaction_id}"
    (interrupted_backup / "workspace").mkdir(parents=True)
    (interrupted_backup / "workspace" / "original.txt").write_text(
        "original", encoding="utf-8"
    )
    interrupted_staging = tmp_path / f".target-home.profile-staging.{transaction_id}"
    interrupted_staging.mkdir()
    (interrupted_staging / "partial.txt").write_text("partial", encoding="utf-8")
    journal = tmp_path / ".target-home.profile-replace.json"
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation": "profile-import",
                "transaction_id": transaction_id,
                "source": _normalized_path(source),
                "source_kind": "cli-home",
                "target": _normalized_path(target),
                "staging": _normalized_path(interrupted_staging),
                "backup": _normalized_path(interrupted_backup),
                "phase": "target_parked",
                "target_existed": True,
                "target_had_real_data": True,
                "target_was_empty": False,
                "identities": {
                    "source": migration_module._path_identity_payload(source),
                    "original_target": migration_module._path_identity_payload(
                        interrupted_backup
                    ),
                    "staging": migration_module._path_identity_payload(
                        interrupted_staging
                    ),
                    "backup": migration_module._path_identity_payload(
                        interrupted_backup
                    ),
                    "candidate": None,
                },
            }
        ),
        encoding="utf-8",
    )

    report = _run(
        source,
        target,
        apply=True,
        overwrite=True,
        confirm_replace_target=target,
    )

    assert not _errors(report)
    assert any(item["kind"] == "recovery" for item in report["items"])
    assert not journal.exists()
    assert not interrupted_staging.exists()
    assert not interrupted_backup.exists()
    backups = list(tmp_path.glob("target-home.backup.*"))
    assert len(backups) == 1
    assert (backups[0] / "workspace" / "original.txt").read_text(
        encoding="utf-8"
    ) == "original"
    assert (target / "workspace" / "MEMORY.md").is_file()


def test_explicit_retry_never_reuses_old_receipt_and_imports_new_source_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    source = _build_source_home(fake_home)
    source.rename(fake_home / ".opensquilla")
    source = fake_home / ".opensquilla"
    target = tmp_path / "target-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    legacy_marker = source / IMPORT_MARKER_FILENAME
    legacy_marker.write_text(
        json.dumps({"target": str(tmp_path / "stale-target"), "transaction_id": "stale"}),
        encoding="utf-8",
    )
    first = _run(source, target, apply=True)
    assert not _errors(first)
    first_transaction_id = Path(first["output_dir"]).name

    assert detect_legacy_cli_home(target) == source
    (source / "workspace" / "MEMORY.md").write_text(
        "# Memory index\n\n- updated after first import\n",
        encoding="utf-8",
    )
    connection = sqlite3.connect(source / "state" / "sessions.db")
    try:
        connection.execute(
            "INSERT INTO synthetic_sessions VALUES (?, ?)",
            ("new-session", "new synthetic conversation"),
        )
        connection.commit()
    finally:
        connection.close()
    source_before_retry = _file_bytes(source)

    refused = _run(source, target, apply=True)

    assert any(item["kind"] == "preflight/target" for item in _errors(refused))
    assert Path(first["output_dir"]).is_dir()

    retried = _run(
        source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )

    assert not _errors(retried)
    assert Path(retried["output_dir"]).name != first_transaction_id
    assert (target / "workspace" / "MEMORY.md").read_text(encoding="utf-8") == (
        "# Memory index\n\n- updated after first import\n"
    )
    target_connection = sqlite3.connect(target / "state" / "sessions.db")
    try:
        imported_ids = {
            row[0]
            for row in target_connection.execute(
                "SELECT session_id FROM synthetic_sessions"
            )
        }
    finally:
        target_connection.close()
    assert imported_ids == {"synthetic-session", "new-session"}
    assert not (tmp_path / ".target-home.profile-replace.json").exists()
    assert _file_bytes(source) == source_before_retry
    assert detect_legacy_cli_home(target) == source


@pytest.mark.parametrize("tamper", ["schema-forward", "extra", "wrong-operation"])
def test_cli_retry_never_mutates_an_inexact_replacement_journal(
    tmp_path: Path,
    tamper: str,
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    transaction_id = str(uuid.uuid4())
    backup = tmp_path / f"target-home.backup.{transaction_id}"
    (backup / "workspace").mkdir(parents=True)
    (backup / "workspace" / "original.txt").write_text("original\n", encoding="utf-8")
    staging = tmp_path / f".target-home.profile-staging.{transaction_id}"
    staging.mkdir()
    (staging / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "operation": "profile-import",
        "source_kind": "cli-home",
        "transaction_id": transaction_id,
        "source": _normalized_path(source),
        "target": _normalized_path(target),
        "staging": _normalized_path(staging),
        "backup": _normalized_path(backup),
        "phase": "target_parked",
        "target_existed": True,
        "target_had_real_data": True,
        "target_was_empty": False,
        "identities": {
            "source": migration_module._path_identity_payload(source),
            "original_target": migration_module._path_identity_payload(backup),
            "staging": migration_module._path_identity_payload(staging),
            "backup": migration_module._path_identity_payload(backup),
            "candidate": None,
        },
    }
    if tamper == "schema-forward":
        payload["schema_version"] = 2
    elif tamper == "extra":
        payload["future_field"] = True
    else:
        payload["operation"] = "restore-profile"
    journal = tmp_path / ".target-home.profile-replace.json"
    journal.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    journal_before = journal.read_bytes()
    source_before = _file_bytes(source)
    backup_before = _file_bytes(backup)
    staging_before = _file_bytes(staging)

    report = _run(
        source,
        target,
        apply=True,
        replace_target=True,
        confirm_replace_target=target,
    )

    assert any(item["kind"] == "preflight/recovery" for item in _errors(report))
    assert journal.read_bytes() == journal_before
    assert _file_bytes(source) == source_before
    assert _file_bytes(backup) == backup_before
    assert _file_bytes(staging) == staging_before
    assert not target.exists()


def test_layout_receipt_is_only_completion_authority(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"

    first = _run(source, target, apply=True)
    assert not _errors(first)
    output_dir = Path(first["output_dir"])
    layout_receipt = output_dir / "layout-receipt.json"
    report_path = output_dir / "report.json"

    # Arbitrary report content cannot revoke or forge the committed import.
    report_path.write_text(
        json.dumps(
            {
                "paused_jobs": [
                    {"id": "private-id", "name": "private name", "cron_expr": "* * * * *"}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert migration_module._matching_import_receipt(source, target) is not None
    retried = _run(source, target, apply=True)
    assert any(item["kind"] == "preflight/target" for item in _errors(retried))
    assert Path(first["output_dir"]) == output_dir

    # The authority schema is exact: detailed scheduler rows are not accepted
    # even if injected into the otherwise-valid layout receipt.
    receipt = json.loads(layout_receipt.read_text(encoding="utf-8"))
    receipt["paused_jobs"] = [
        {"id": "private-id", "name": "private name", "cron_expr": "* * * * *"}
    ]
    layout_receipt.write_text(json.dumps(receipt), encoding="utf-8")
    assert migration_module._matching_import_receipt(source, target) is None

    receipt.pop("paused_jobs")
    receipt["source_identity"]["private_scheduler_name"] = "must not persist"
    layout_receipt.write_text(json.dumps(receipt), encoding="utf-8")
    assert migration_module._matching_import_receipt(source, target) is None

    # Conversely, a detailed report cannot replace a missing layout receipt.
    layout_receipt.unlink()
    assert migration_module._matching_import_receipt(source, target) is None


def test_committed_import_verifier_returns_only_locked_protocol_metadata(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    report = _run(source, target, apply=True)
    transaction_id = Path(report["output_dir"]).name

    verified = migration_module.verify_committed_profile_import(
        source,
        target,
        source_kind="cli-home",
        transaction_id=transaction_id,
    )

    assert set(verified) == {
        "schema_version",
        "outcome",
        "stable_code",
        "source",
        "source_kind",
        "target",
        "transaction_id",
        "matching_transaction_ids",
        "provider_connection",
        "report",
    }
    assert verified["outcome"] == "verified"
    assert verified["transaction_id"] == transaction_id
    assert verified["matching_transaction_ids"] == [transaction_id]
    assert verified["provider_connection"] == {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
    }
    assert _normalized_path(Path(verified["report"]["output_dir"])) == _normalized_path(
        Path(report["output_dir"])
    )
    serialized = json.dumps(verified, ensure_ascii=False)
    assert DUMMY_INLINE_KEY not in serialized
    assert "dummy session content" not in serialized

    excluded = migration_module.verify_committed_profile_import(
        source,
        target,
        source_kind="cli-home",
        excluded_transaction_ids=(transaction_id,),
    )
    assert excluded["outcome"] == "not_found"
    assert excluded["matching_transaction_ids"] == []
    assert excluded["report"] is None


def test_committed_import_verifier_treats_proven_missing_target_as_not_found(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "missing-target-home"

    verified = migration_module.verify_committed_profile_import(
        source,
        target,
        source_kind="windows-portable",
    )

    assert verified["outcome"] == "not_found"
    assert verified["stable_code"] == "profile_import_receipt_not_found"
    assert verified["matching_transaction_ids"] == []
    assert verified["report"] is None
    assert not target.exists()


@pytest.mark.parametrize("unsafe_source", ["missing", "file"])
def test_committed_import_verifier_rejects_unsafe_source_with_missing_target(
    tmp_path: Path,
    unsafe_source: str,
) -> None:
    source = tmp_path / "source-home"
    if unsafe_source == "file":
        source.write_text("not a profile directory", encoding="utf-8")
    target = tmp_path / "missing-target-home"

    verified = migration_module.verify_committed_profile_import(
        source,
        target,
        source_kind="windows-portable",
    )

    assert verified["outcome"] == "unsafe"
    assert verified["stable_code"] == "profile_import_receipt_path_unsafe"
    assert not target.exists()


def test_committed_import_verifier_rejects_existing_non_directory_target(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    target.write_text("not a profile directory", encoding="utf-8")

    verified = migration_module.verify_committed_profile_import(
        source,
        target,
        source_kind="windows-portable",
    )

    assert verified["outcome"] == "unsafe"
    assert verified["stable_code"] == "profile_import_receipt_path_unsafe"


def test_committed_import_verifier_never_emits_private_provider_url(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    report = _run(source, target, apply=True)
    config_path = target / "config.toml"
    config = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        config.replace(
            "https://openrouter.ai/api/v1",
            "https://user:secret@private.invalid/v1?token=hidden",
        ),
        encoding="utf-8",
    )

    verified = migration_module.verify_committed_profile_import(
        source,
        target,
        source_kind="cli-home",
        transaction_id=Path(report["output_dir"]).name,
    )

    serialized = json.dumps(verified, ensure_ascii=False)
    assert verified["outcome"] == "unsafe"
    assert verified["stable_code"] == "profile_import_provider_connection_unsafe"
    assert "secret" not in serialized
    assert "hidden" not in serialized


def test_committed_import_verifier_rejects_non_string_or_invalid_provider_fields(
    tmp_path: Path,
) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    report = _run(source, target, apply=True)
    transaction_id = Path(report["output_dir"]).name
    config_path = target / "config.toml"
    original = tomllib.loads(config_path.read_text(encoding="utf-8"))
    invalid_values: tuple[tuple[str, object], ...] = (
        ("provider", {"secret": "TOPSECRET"}),
        ("model", ["TOPSECRET"]),
        ("base_url", {"secret": "TOPSECRET"}),
        ("api_key_env", ["TOPSECRET"]),
        ("api_key_env", "INVALID-ENV-NAME"),
        ("api_key_env", "PATH"),
        ("api_key_env", "PYTHONPATH"),
        ("api_key_env", "NODE_OPTIONS"),
        ("api_key_env", "LD_PRELOAD"),
    )

    for field, value in invalid_values:
        candidate = json.loads(json.dumps(original))
        candidate["llm"][field] = value
        config_path.write_text(tomli_w.dumps(candidate), encoding="utf-8")

        verified = migration_module.verify_committed_profile_import(
            source,
            target,
            source_kind="cli-home",
            transaction_id=transaction_id,
        )

        serialized = json.dumps(verified, ensure_ascii=False)
        assert verified["outcome"] == "unsafe"
        assert verified["stable_code"] == "profile_import_provider_connection_unsafe"
        assert "TOPSECRET" not in serialized
        assert "INVALID-ENV-NAME" not in serialized

    for accepted_env in ("CUSTOM_LLM_KEY", "HF_TOKEN"):
        candidate = json.loads(json.dumps(original))
        candidate["llm"]["api_key_env"] = accepted_env
        config_path.write_text(tomli_w.dumps(candidate), encoding="utf-8")
        verified = migration_module.verify_committed_profile_import(
            source,
            target,
            source_kind="cli-home",
            transaction_id=transaction_id,
        )
        assert verified["outcome"] == "verified"
        assert verified["provider_connection"]["api_key_env"] == accepted_env


def test_persisted_migration_reports_do_not_store_scheduler_rows(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    # The stdout report remains detailed for the active user interaction.
    assert report["paused_jobs"] == [
        {"id": "job-1", "name": "dummy daily job", "cron_expr": "0 9 * * *"}
    ]
    output_dir = Path(report["output_dir"])
    persisted_report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    persisted_bytes = json.dumps(persisted_report, ensure_ascii=False)
    summary = (output_dir / "summary.md").read_text(encoding="utf-8")

    assert persisted_report["paused_job_count"] == 1
    assert "paused_jobs" not in persisted_report
    for private_value in ("job-1", "dummy daily job", "0 9 * * *"):
        assert private_value not in persisted_bytes
        assert private_value not in summary


def test_dry_run_reports_interrupted_commit_without_mutating_it(tmp_path: Path) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    transaction_id = "123e4567-e89b-42d3-a456-426614174000"
    backup = tmp_path / f"target-home.backup.{transaction_id}"
    backup.mkdir()
    staging = tmp_path / f".target-home.profile-staging.{transaction_id}"
    staging.mkdir()
    journal = tmp_path / ".target-home.profile-replace.json"
    payload = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "source": _normalized_path(source),
        "target": _normalized_path(target),
        "staging": _normalized_path(staging),
        "backup": _normalized_path(backup),
        "phase": "target_parked",
        "target_existed": True,
        "target_was_empty": False,
        "identities": {
            "source": migration_module._path_identity_payload(source),
            "original_target": migration_module._path_identity_payload(backup),
            "staging": migration_module._path_identity_payload(staging),
            "backup": migration_module._path_identity_payload(backup),
            "candidate": None,
        },
    }
    journal.write_text(json.dumps(payload), encoding="utf-8")

    report = _run(source, target, apply=False)

    assert any(item["kind"] == "preflight/recovery" for item in _errors(report))
    assert json.loads(journal.read_text(encoding="utf-8")) == payload
    assert backup.is_dir()
    assert staging.is_dir()
    assert not target.exists()


@pytest.mark.asyncio
async def test_real_session_transcript_workspace_and_media_survive_import(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-home"
    source.mkdir()
    _write_config(source)
    source_db = source / "state" / "sessions.db"
    source_db.parent.mkdir(parents=True)
    apply_pending(f"sqlite:///{source_db}", MIGRATIONS_DIR)
    storage = SessionStorage(str(source_db))
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False, media_root=source / "media")
        session = await manager.create("agent:main:direct:migration-e2e")
        for role, content in (("user", "synthetic user prompt"), ("assistant", "synthetic reply")):
            await storage.append_transcript_entry(
                TranscriptEntry(
                    session_id=session.session_id,
                    session_key=session.session_key,
                    role=role,
                    content=content,
                    token_count=3,
                )
            )
    finally:
        await storage.close()

    (source / "workspace").mkdir()
    (source / "workspace" / "important.txt").write_text(
        "workspace survives", encoding="utf-8"
    )
    attachment_sha, _path, _wrote = write_transcript_material(
        media_root=source / "media",
        session_id=session.session_id,
        payload=b"attachment survives",
    )
    artifact = ArtifactStore(source / "media").publish_bytes(
        b"artifact survives",
        session_id=session.session_id,
        session_key=session.session_key,
        name="result.bin",
        mime="application/octet-stream",
        source="migration-test",
    )
    _write_scheduler_db(source, with_enabled_column=True)
    target = tmp_path / "target-home"

    report = _run(source, target, apply=True)

    assert not _errors(report)
    apply_pending(f"sqlite:///{target / 'state' / 'sessions.db'}", MIGRATIONS_DIR)
    reopened = SessionStorage(str(target / "state" / "sessions.db"))
    await reopened.connect()
    try:
        restored = await reopened.get_session(session.session_key)
        assert restored is not None
        transcript = await reopened.get_transcript(session.session_id)
        assert [(entry.role, entry.content) for entry in transcript] == [
            ("user", "synthetic user prompt"),
            ("assistant", "synthetic reply"),
        ]
    finally:
        await reopened.close()

    assert (target / "workspace" / "important.txt").read_text(
        encoding="utf-8"
    ) == "workspace survives"
    attachment_ref = make_attachment_ref(
        sha256=attachment_sha,
        name="upload.bin",
        mime="application/octet-stream",
        size=len(b"attachment survives"),
        session_id=session.session_id,
        source="transcript",
    )
    assert read_attachment_ref_bytes(attachment_ref, media_root=target / "media") == (
        b"attachment survives"
    )
    _ref, artifact_path = ArtifactStore(target / "media").resolve_for_download(
        artifact.id, session_id=session.session_id
    )
    assert artifact_path.read_bytes() == b"artifact survives"
    assert _scheduler_enabled_values(target / "state" / "scheduler.db") == [0]


# ---------------------------------------------------------------------------
# Orchestrator wiring
# ---------------------------------------------------------------------------


def test_orchestrator_rejects_non_default_item_options_for_opensquilla() -> None:
    options = orchestrator.MigrationBatchOptions(preset="user-data")
    try:
        orchestrator.validate_batch_options(("opensquilla",), options)
    except orchestrator.MigrationOptionError as exc:
        assert "does not take preset/include/exclude" in str(exc)
    else:
        raise AssertionError("non-default preset must be rejected for opensquilla")


def test_orchestrator_accepts_wizard_defaults_for_opensquilla() -> None:
    options = orchestrator.MigrationBatchOptions(
        preset="full", skill_conflict="skip", persona_conflict="use-opensquilla"
    )
    orchestrator.validate_batch_options(("opensquilla",), options)


def test_orchestrator_runs_opensquilla_source(tmp_path: Path, monkeypatch) -> None:
    source = _build_source_home(tmp_path)
    target = tmp_path / "target-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(target))

    report = orchestrator.run_one_migration(
        "opensquilla", source, orchestrator.MigrationBatchOptions(apply=False)
    )

    assert report["source_kind"] == "cli-home"
    assert _normalized_path(Path(report["target"])) == _normalized_path(target)
    assert not any(item["status"] == "error" for item in report["items"])
