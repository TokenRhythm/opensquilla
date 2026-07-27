from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tomllib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from opensquilla.cli.recovery_cmd import recovery_app
from opensquilla.cli.session_schema import prepare_session_schema
from opensquilla.recovery.atomic import _native_io_path
from opensquilla.recovery.consolidate import (
    ConsolidationResult,
)
from opensquilla.recovery.consolidate import (
    consolidate_recovery_profiles as _consolidate_recovery_profiles,
)
from opensquilla.recovery.errors import UnsafePathError


def consolidate_recovery_profiles(
    user_data: str | Path,
    primary_home: str | Path,
) -> ConsolidationResult:
    return _consolidate_recovery_profiles(
        user_data,
        primary_home,
        prepare_session_schema=prepare_session_schema,
    )


def _is_file(path: Path) -> bool:
    return os.path.isfile(_native_io_path(path))


def _link_assertion_target(target: str) -> str:
    """Normalize only equivalent Win32 spellings used by test assertions."""

    if os.name != "nt":
        return target
    lowered = target.lower()
    if lowered.startswith("\\\\?\\unc\\"):
        return "\\\\" + target[8:]
    if (
        lowered.startswith("\\\\?\\")
        and len(target) >= 7
        and target[4].isalpha()
        and target[5:7] == ":\\"
    ):
        return target[4:]
    return target


def _read_text(path: Path) -> str:
    with open(_native_io_path(path), encoding="utf-8") as handle:
        return handle.read()


def _read_bytes(path: Path) -> bytes:
    with open(_native_io_path(path), "rb") as handle:
        return handle.read()


def _write_text(path: Path, value: str) -> None:
    with open(_native_io_path(path), "w", encoding="utf-8") as handle:
        handle.write(value)


def _make_dangling_directory_link(link: Path) -> None:
    target = link.with_name(f"{link.name}-target")
    target.mkdir(parents=True)
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            pytest.skip(f"junction creation is unavailable: {completed.stderr}")
    else:
        link.symlink_to(target, target_is_directory=True)
    target.rmdir()
    assert os.path.lexists(_native_io_path(link))


def _remove_dangling_directory_link(link: Path) -> None:
    if not os.path.lexists(_native_io_path(link)):
        return
    if os.name == "nt":
        os.rmdir(_native_io_path(link))
    else:
        link.unlink()


@pytest.mark.parametrize(
    ("phase", "destination_name"),
    [
        ("external_roots_merged", "parked"),
        ("primary_parked", "primary"),
    ],
)
def test_commit_primary_rejects_dangling_move_destination(
    tmp_path: Path,
    phase: str,
    destination_name: str,
) -> None:
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    primary = tmp_path / "primary"
    staging = tmp_path / "staging"
    backup = tmp_path / "backup"
    backup.mkdir()
    destination = backup / "primary" if destination_name == "parked" else primary
    _make_dangling_directory_link(destination)
    payload = {
        "phase": phase,
        "primary_home": str(primary),
        "staging": str(staging),
        "backup_path": str(backup),
        "primary_existed": True,
        "primary_config": {
            "config": {"exists": False},
            "dotenv_path": None,
            "dotenv": {"exists": False},
        },
        "staging_merged": "synthetic-token",
    }
    try:
        with pytest.raises(consolidate_module.RecoveryError):
            consolidate_module._commit_primary(tmp_path / "journal.json", payload)
    finally:
        _remove_dangling_directory_link(destination)


def test_archive_finish_rejects_dangling_archive_destination(tmp_path: Path) -> None:
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    recovery_id = str(uuid.uuid4())
    primary = tmp_path / "primary"
    backup = tmp_path / "backup"
    backup.mkdir()
    archived = backup / "recovery-profiles"
    _make_dangling_directory_link(archived)
    payload = {
        "phase": "primary_published",
        "transaction_id": str(uuid.uuid4()),
        "recovery_root": str(tmp_path / "missing-recovery-profiles"),
        "backup_path": str(backup),
        "source_snapshots": {recovery_id: "synthetic-token"},
        "result": {
            "stable_code": "profile_consolidation_complete",
            "primary_home": str(primary),
            "configuration_source_recovery_id": None,
            "configuration_source_credential_path": None,
            "configuration_source_credential_sha256": None,
            "configuration_source_credential_size": None,
            "consumed_recovery_ids": [recovery_id],
            "backup_path": str(backup),
            "receipt_path": str(backup / "receipt.json"),
            "credential_adoption_status": "not_required",
            "revision": 1,
            "errors": [],
        },
    }
    try:
        with pytest.raises(consolidate_module.RecoveryError):
            consolidate_module._archive_and_finish(
                tmp_path,
                tmp_path / "journal.json",
                payload,
            )
    finally:
        _remove_dangling_directory_link(archived)


def _session_database(path: Path, key: str, session_id: str, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                session_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                label TEXT,
                estimated_cost_usd REAL NOT NULL DEFAULT 0.0
            );
            CREATE TABLE transcript_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                message_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                created_at INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO sessions(session_key, session_id, updated_at, label)
            VALUES (?, ?, 1, ?)
            """,
            (key, session_id, content),
        )
        connection.execute(
            """
            INSERT INTO transcript_entries(
                session_id, session_key, message_id, role, content, created_at
            ) VALUES (?, ?, ?, 'user', ?, 1)
            """,
            (session_id, key, f"message-{session_id}", content),
        )


def _recovery(
    user_data: Path,
    recovery_id: str,
    *,
    config: str,
    credential: str,
    memory: str,
    conflict: str,
    extra_name: str,
    session_key: str,
) -> Path:
    root = user_data / "recovery-profiles" / recovery_id
    home = root / "opensquilla"
    workspace = home / "workspace"
    workspace.mkdir(parents=True)
    (home / "config.toml").write_text(
        config.replace("<RECOVERY_HOME>", str(home)),
        encoding="utf-8",
    )
    (home / ".env").write_text(f"SOURCE={recovery_id}\n", encoding="utf-8")
    (root / "desktop-credential.json").write_text(credential, encoding="utf-8")
    (workspace / "MEMORY.md").write_text(memory, encoding="utf-8")
    (workspace / "conflict.txt").write_text(conflict, encoding="utf-8")
    (workspace / extra_name).write_text(recovery_id, encoding="utf-8")
    (home / "media").mkdir()
    (home / "media" / f"{recovery_id}.txt").write_text("media", encoding="utf-8")
    (home / "skills" / "custom").mkdir(parents=True)
    (home / "skills" / "custom" / "SKILL.md").write_text(
        f"skill {recovery_id}",
        encoding="utf-8",
    )
    (home / "ordinary.txt").write_text(conflict, encoding="utf-8")
    (home / "state" / "session-archive").mkdir(parents=True)
    (home / "state" / "session-archive" / f"{recovery_id}.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    agent_state = home / "state" / "agents" / "main"
    (agent_state / "memory").mkdir(parents=True)
    (agent_state / "memory" / f"{recovery_id}.md").write_text(
        f"memory {recovery_id}",
        encoding="utf-8",
    )
    (agent_state / "memory.db").write_bytes(b"derived")
    (home / "state" / "approvals.json").write_text(
        '{"pending":true}\n',
        encoding="utf-8",
    )
    (home / "state" / ".env").write_text(
        "OPENSQUILLA_HOME=D:\\External\\profile\n",
        encoding="utf-8",
    )
    (home / "state" / "sessions.db-journal").write_bytes(b"stale journal")
    (home / "state" / "auto_propose_settings.json").write_text(
        '{"enabled":true}\n',
        encoding="utf-8",
    )
    (home / "state" / "update_check.json").write_text("{}\n", encoding="utf-8")
    matrix_state = workspace / "state" / "matrix" / "account"
    matrix_state.mkdir(parents=True)
    (matrix_state / "session.json").write_text(
        '{"token":"secret"}\n',
        encoding="utf-8",
    )
    teams_state = workspace / "state" / "msteams"
    teams_state.mkdir(parents=True)
    (teams_state / "conversations.json").write_text(
        '{"conversation":"runtime"}\n',
        encoding="utf-8",
    )
    (home / "desktop-recovery-v1.json").write_text("{}\n", encoding="utf-8")
    _session_database(
        home / "state" / "sessions.db",
        session_key,
        f"session-{recovery_id}",
        recovery_id,
    )
    return root


def test_consolidates_all_recoveries_into_primary_and_archives_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    workspace = primary / "workspace"
    workspace.mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    (user_data / "desktop-credential.json").write_text(
        '{"source":"primary"}\n',
        encoding="utf-8",
    )
    (workspace / "MEMORY.md").write_text("# Memory\n\nprimary fact\n", encoding="utf-8")
    (workspace / "conflict.txt").write_text("primary", encoding="utf-8")
    _session_database(
        primary / "state" / "sessions.db",
        "agent:main:main",
        "primary-session",
        "primary",
    )
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    _recovery(
        user_data,
        first_id,
        config="first = true\n",
        credential='{"source":"first"}\n',
        memory="# Memory\n\nprimary fact\n\nfirst fact\n",
        conflict="first",
        extra_name="first-only.txt",
        session_key="agent:main:first",
    )
    _recovery(
        user_data,
        second_id,
        config="second = true\n",
        credential='{"source":"second"}\n',
        memory="# Memory\n\nsecond fact\n",
        conflict="second",
        extra_name="second-only.txt",
        session_key="agent:main:second",
    )
    (user_data / "desktop-profile-context.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_profile_kind": "recovery",
                "active_recovery_id": second_id,
                "attention_acknowledgement": None,
                "updated_at": "2026-07-25T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert 0 <= result.revision <= (2**53 - 1)
    assert result.configuration_source_recovery_id is None
    assert result.configuration_source_credential_path is None
    assert set(result.consumed_recovery_ids) == {first_id, second_id}
    assert not (user_data / "recovery-profiles").exists()
    assert result.backup_path is not None
    archived = result.backup_path / "recovery-profiles"
    assert _is_file(archived / first_id / "opensquilla" / "config.toml")
    assert _is_file(archived / second_id / "desktop-credential.json")
    assert _is_file(
        archived
        / first_id
        / "opensquilla"
        / "workspace"
        / "state"
        / "matrix"
        / "account"
        / "session.json"
    )
    assert (primary / "config.toml").read_text(encoding="utf-8") == "primary = true\n"
    assert (user_data / "desktop-credential.json").read_text(encoding="utf-8") == (
        '{"source":"primary"}\n'
    )
    assert (workspace / "first-only.txt").read_text(encoding="utf-8") == first_id
    assert (workspace / "second-only.txt").read_text(encoding="utf-8") == second_id
    assert (primary / "media" / f"{first_id}.txt").read_text(encoding="utf-8") == "media"
    assert _is_file(primary / "skills" / "custom" / "SKILL.md")
    assert _is_file(primary / "state" / "session-archive" / f"{second_id}.json")
    assert _is_file(primary / "state" / "agents" / "main" / "memory" / f"{first_id}.md")
    assert not (primary / "state" / "agents" / "main" / "memory.db").exists()
    assert not (primary / "state" / "approvals.json").exists()
    assert not (primary / "state" / ".env").exists()
    assert not (primary / "state" / "sessions.db-journal").exists()
    assert not (primary / "state" / "auto_propose_settings.json").exists()
    assert not (primary / "state" / "update_check.json").exists()
    assert not (workspace / "state" / "matrix").exists()
    assert not (workspace / "state" / "msteams").exists()
    assert _is_file(
        primary / "recovered-data" / first_id / "profile" / "state" / "auto_propose_settings.json"
    )
    assert _is_file(
        primary
        / "recovered-data"
        / first_id
        / "workspace"
        / "state"
        / "matrix"
        / "account"
        / "session.json"
    )
    assert _is_file(
        primary
        / "recovered-data"
        / first_id
        / "workspace"
        / "state"
        / "msteams"
        / "conversations.json"
    )
    assert not (primary / "desktop-recovery-v1.json").exists()
    memory = (workspace / "MEMORY.md").read_text(encoding="utf-8")
    assert memory.count("primary fact") == 1
    assert "first fact" in memory
    assert "second fact" in memory
    assert (workspace / "conflict.txt").read_text(encoding="utf-8") == "primary"
    assert (primary / "recovered-data" / first_id / "workspace" / "conflict.txt").read_text(
        encoding="utf-8"
    ) == "first"
    assert (primary / "recovered-data" / second_id / "workspace" / "conflict.txt").read_text(
        encoding="utf-8"
    ) == "second"
    ordered_ids = sorted((first_id, second_id))
    ordered_values = {first_id: "first", second_id: "second"}
    assert (primary / "ordinary.txt").read_text(encoding="utf-8") == ordered_values[ordered_ids[0]]
    assert (primary / "recovered-data" / ordered_ids[1] / "profile" / "ordinary.txt").read_text(
        encoding="utf-8"
    ) == ordered_values[ordered_ids[1]]
    with (
        contextlib.closing(sqlite3.connect(primary / "state" / "sessions.db")) as connection,
        connection,
    ):
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone() == (3,)
    context = json.loads((user_data / "desktop-profile-context.json").read_text(encoding="utf-8"))
    assert context["active_profile_kind"] == "primary"
    assert context["active_recovery_id"] is None

    repeated = consolidate_recovery_profiles(user_data, primary)

    assert repeated.outcome == "noop"
    assert repeated.stable_code == "profile_consolidation_already_complete"
    assert repeated.receipt_path == result.receipt_path
    assert repeated.backup_path == result.backup_path
    assert result.receipt_path is not None
    receipt_text = result.receipt_path.read_text(encoding="utf-8")
    assert "agent:main:first" not in receipt_text
    assert f"session-{first_id}" not in receipt_text
    with (
        contextlib.closing(sqlite3.connect(primary / "state" / "sessions.db")) as connection,
        connection,
    ):
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone() == (3,)


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_consolidates_extended_length_recovery_leaf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Long-path support must not depend on the machine-wide registry policy."""

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="long-path.txt",
        session_key="agent:main:long-path",
    )
    relative = Path("deep") / ("a" * 100) / ("b" * 100) / "preserved-session-metadata.bin"
    source_leaf = recovery / "opensquilla" / "state" / relative
    os.makedirs(_native_io_path(source_leaf.parent), exist_ok=True)
    with open(_native_io_path(source_leaf), "wb") as handle:
        handle.write(b"long path recovery data")
    assert len(str(source_leaf)) > 260
    workspace_relative = Path("shared-" + ("w" * 90)) / ("x" * 100) / "MEMORY.md"
    source_workspace_leaf = recovery / "opensquilla" / "workspace" / workspace_relative
    os.makedirs(_native_io_path(source_workspace_leaf.parent), exist_ok=True)
    with open(_native_io_path(source_workspace_leaf), "wb") as handle:
        handle.write(b"long path workspace data")
    primary_workspace_leaf = primary / "workspace" / workspace_relative
    os.makedirs(_native_io_path(primary_workspace_leaf.parent), exist_ok=True)
    with open(_native_io_path(primary_workspace_leaf), "w", encoding="utf-8") as handle:
        handle.write("primary memory\n")
    media_relative = Path("shared-" + ("m" * 90)) / ("n" * 100) / "deep-transcript-attachment.bin"
    source_media_leaf = recovery / "opensquilla" / "media" / media_relative
    os.makedirs(_native_io_path(source_media_leaf.parent), exist_ok=True)
    with open(_native_io_path(source_media_leaf), "wb") as handle:
        handle.write(b"long path media data")
    os.makedirs(
        _native_io_path(primary / "media" / media_relative.parts[0]),
        exist_ok=True,
    )
    assert len(str(source_workspace_leaf)) > 260
    assert len(str(source_media_leaf)) > 260

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result.errors
    assert result.backup_path is not None
    assert result.receipt_path is not None
    assert not str(result.backup_path).startswith("\\\\?\\")
    assert not str(result.receipt_path).startswith("\\\\?\\")
    archived_leaf = (
        result.backup_path / "recovery-profiles" / recovery_id / "opensquilla" / "state" / relative
    )
    preserved_leaf = primary / "recovered-data" / recovery_id / "profile" / "state" / relative
    assert _read_bytes(archived_leaf) == b"long path recovery data"
    assert _read_bytes(preserved_leaf) == b"long path recovery data"
    assert _read_text(primary_workspace_leaf) == ("primary memory\n\nlong path workspace data\n")
    assert _read_bytes(primary / "media" / media_relative) == b"long path media data"
    with (
        contextlib.closing(sqlite3.connect(primary / "state" / "sessions.db")) as connection,
        connection,
    ):
        assert connection.execute(
            "SELECT session_id FROM sessions WHERE session_key=?",
            ("agent:main:long-path",),
        ).fetchone() == (f"session-{recovery_id}",)


def test_empty_primary_uses_newest_recovery_configuration_bundle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    older_id = str(uuid.uuid4())
    newer_id = str(uuid.uuid4())
    older = _recovery(
        user_data,
        older_id,
        config="state_dir = '<RECOVERY_HOME>/state'\nselected = 'older'\n",
        credential='{"source":"older","updatedAt":"2026-07-25T01:00:00Z"}\n',
        memory="older memory\n",
        conflict="older",
        extra_name="older.txt",
        session_key="agent:main:older",
    )
    newer = _recovery(
        user_data,
        newer_id,
        config=(
            "state_dir = 'C:\\Recovery\\state'\n"
            "workspace_dir = 'D:\\External\\workspace'\n"
            "selected = 'newer'\n"
            "[attachments]\n"
            "media_root = 'E:\\External\\media'\n"
            "[[agents]]\n"
            "id = 'main'\n"
            "workspace = 'F:\\External\\main'\n"
            "[[agents]]\n"
            "id = 'ops'\n"
            "workspace = 'G:\\External\\ops'\n"
        ),
        credential='{"source":"newer","updatedAt":"2026-07-25T02:00:00Z"}\n',
        memory="newer memory\n",
        conflict="newer",
        extra_name="newer.txt",
        session_key="agent:main:newer",
    )
    (newer / "opensquilla" / ".env").write_text(
        "OPENSQUILLA_HOME=C:\\Recovery\\profiles\n"
        "OPENSQUILLA_GATEWAY_CONFIG_PATH=D:\\External\\config.toml\n"
        "OPENSQUILLA_GATEWAY_STATE_DIR=E:\\External\\state\n"
        "OPENSQUILLA_SCHEDULER_DB=F:\\External\\scheduler.db\n"
        "OPENSQUILLA_MEMORY_DB=G:\\External\\memory.db\n"
        "OPENSQUILLA_MEMORY_DIR=H:\\External\\memory\n"
        "OPENSQUILLA_META_RUNS_DB=I:\\External\\meta.db\n"
        "OPENSQUILLA_ROUTER_DECISIONS_DB=J:\\External\\router.db\n"
        "OPENSQUILLA_SESSION_ARCHIVE_DIR=K:\\External\\archive\n"
        "OPENSQUILLA_LOG_DIR=L:\\External\\logs\n"
        "OPENSQUILLA_LLM_TRACE_PATH=M:\\External\\trace.jsonl\n"
        "OPENSQUILLA_RUNTIME_EVENTS_PATH=N:\\External\\events.jsonl\n"
        "OPENSQUILLA_USER_STATE_DIR=O:\\External\\user-state\n"
        "OPENSQUILLA_CODETASK_RUNS_DIR=P:\\External\\code-task\n"
        "PROVIDER_SECRET=keep-me\n"
        "OPENSQUILLA_LOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.configuration_source_recovery_id == newer_id
    assert result.credential_adoption_status == "pending"
    selected_config = (primary / "config.toml").read_text(encoding="utf-8")
    assert "selected = 'newer'" in selected_config
    assert str(newer / "opensquilla") not in selected_config
    assert "state_dir" not in selected_config
    assert "workspace_dir" not in selected_config
    assert "media_root" not in selected_config
    selected_payload = tomllib.loads(selected_config)
    assert selected_payload["agents"][0]["workspace"] == str(primary / "workspace")
    assert selected_payload["agents"][1]["workspace"] == str(
        primary / "workspace" / "agents" / "ops"
    )
    selected_env = (primary / ".env").read_text(encoding="utf-8")
    assert selected_env == "PROVIDER_SECRET=keep-me\nOPENSQUILLA_LOG_LEVEL=DEBUG\n"
    assert not (user_data / "desktop-credential.json").exists()
    assert result.backup_path is not None
    expected_credential = (
        result.backup_path / "recovery-profiles" / newer_id / "desktop-credential.json"
    )
    assert result.configuration_source_credential_path == expected_credential
    assert json.loads(_read_text(expected_credential))["source"] == "newer"

    repeated = consolidate_recovery_profiles(user_data, primary)

    assert repeated.outcome == "noop"
    assert repeated.configuration_source_recovery_id == newer_id
    assert repeated.configuration_source_credential_path == expected_credential
    assert repeated.credential_adoption_status == "pending"


def test_recovery_root_mtime_breaks_configuration_recency_tie(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    older_id = str(uuid.uuid4())
    newer_id = str(uuid.uuid4())
    older = _recovery(
        user_data,
        older_id,
        config="selected = 'older-root'\n",
        credential="{}\n",
        memory="older memory\n",
        conflict="older",
        extra_name="older.txt",
        session_key="agent:main:older-root",
    )
    newer = _recovery(
        user_data,
        newer_id,
        config="selected = 'newer-root'\n",
        credential="{}\n",
        memory="newer memory\n",
        conflict="newer",
        extra_name="newer.txt",
        session_key="agent:main:newer-root",
    )
    equal_file_mtime = 1_000_000_000
    for recovery in (older, newer):
        for candidate in (
            recovery / "opensquilla" / "config.toml",
            recovery / "desktop-credential.json",
            recovery / "opensquilla" / "state" / "sessions.db",
        ):
            os.utime(candidate, ns=(equal_file_mtime, equal_file_mtime))
    os.utime(older, ns=(2_000_000_000, 2_000_000_000))
    os.utime(newer, ns=(3_000_000_000, 3_000_000_000))

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated"
    assert result.configuration_source_recovery_id == newer_id
    assert "selected = 'newer-root'" in (primary / "config.toml").read_text(encoding="utf-8")


def test_newer_config_only_profile_beats_older_credential_timestamp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    older_id = str(uuid.uuid4())
    older = _recovery(
        user_data,
        older_id,
        config="selected = 'older-credential'\n",
        credential='{"updatedAt":1700000000}\n',
        memory="older memory\n",
        conflict="older",
        extra_name="older-credential.txt",
        session_key="agent:main:older-credential",
    )
    newer_id = str(uuid.uuid4())
    newer = _recovery(
        user_data,
        newer_id,
        config="selected = 'newer-config-only'\n",
        credential="{}\n",
        memory="newer memory\n",
        conflict="newer",
        extra_name="newer-config-only.txt",
        session_key="agent:main:newer-config-only",
    )
    (newer / "desktop-credential.json").unlink()
    (newer / "opensquilla" / ".env").unlink()
    (newer / "opensquilla" / "state" / ".env").unlink()
    older_mtime = 1_600_000_000_000_000_000
    newer_mtime = 1_800_000_000_000_000_000
    for candidate in (
        older,
        older / "opensquilla" / "config.toml",
        older / "desktop-credential.json",
        older / "opensquilla" / ".env",
        older / "opensquilla" / "state" / "sessions.db",
    ):
        os.utime(candidate, ns=(older_mtime, older_mtime))
    for candidate in (
        newer,
        newer / "opensquilla" / "config.toml",
        newer / "opensquilla" / "state" / "sessions.db",
    ):
        os.utime(candidate, ns=(newer_mtime, newer_mtime))

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.configuration_source_recovery_id == newer_id
    assert (primary / "config.toml").read_text(encoding="utf-8") == (
        "selected = 'newer-config-only'\n"
    )


@pytest.mark.parametrize("updated_at", ["1e1000", "NaN"])
def test_non_finite_credential_timestamp_does_not_block_valid_config(
    tmp_path: Path,
    monkeypatch,
    updated_at: str,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = 'valid-config'\n",
        credential=f'{{"updatedAt":{updated_at}}}\n',
        memory="memory\n",
        conflict="recovery",
        extra_name="non-finite-timestamp.txt",
        session_key="agent:main:non-finite-timestamp",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.configuration_source_recovery_id == recovery_id
    assert (primary / "config.toml").read_text(encoding="utf-8") == ("selected = 'valid-config'\n")


@pytest.mark.parametrize(
    "primary_env",
    [
        "",
        "OPENSQUILLA_HOME=C:\\Recovery\\profile\n",
    ],
)
def test_empty_or_path_only_primary_dotenv_does_not_override_recovery_config(
    tmp_path: Path,
    monkeypatch,
    primary_env: str,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / ".env").write_text(primary_env, encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = 'recovery'\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:recovery-config",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.configuration_source_recovery_id == recovery_id
    assert (primary / "config.toml").read_text(encoding="utf-8") == ("selected = 'recovery'\n")
    assert (primary / ".env").read_text(encoding="utf-8") == (f"SOURCE={recovery_id}\n")


def test_profile_scoped_dotenv_keys_are_classified_case_insensitively() -> None:
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    raw = (
        "opensquilla_gateway_workspace_dir=C:\\Old\\workspace\n"
        "OpenSquilla_Gateway_State_Dir=C:\\Old\\state\n"
        "OPENSQUILLA_GATEWAY_ATTACHMENTS__media_root=C:\\Old\\media\n"
        "PROVIDER_SECRET=keep-me\n"
    )

    assert consolidate_module._dotenv_text_has_user_configuration(raw)
    assert consolidate_module._sanitized_dotenv(raw) == "PROVIDER_SECRET=keep-me\n"
    assert not consolidate_module._dotenv_text_has_user_configuration(
        "openSquilla_gateway_state_dir=C:\\Old\\state\n"
    )


def test_primary_dotenv_data_routes_survive_recovery_configuration_selection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    external = tmp_path / "external"
    workspace = external / "workspace"
    state = external / "state"
    media = external / "media"
    for path in (workspace, state, media):
        path.mkdir(parents=True)
    dotenv = (
        f"OPENSQUILLA_GATEWAY_WORKSPACE_DIR={workspace}\n"
        f"OPENSQUILLA_GATEWAY_STATE_DIR={state}\n"
        f"OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT={media}\n"
    )
    (primary / ".env").write_text(dotenv, encoding="utf-8")
    (workspace / "primary-existing.txt").write_text("primary\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = 'recovery'\n",
        credential='{"provider":"openai"}\n',
        memory="recovery memory\n",
        conflict="recovery",
        extra_name="dotenv-routed.txt",
        session_key="agent:main:dotenv-routed",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.configuration_source_recovery_id == recovery_id
    assert (primary / "config.toml").read_text(encoding="utf-8") == ("selected = 'recovery'\n")
    primary_dotenv = (primary / ".env").read_text(encoding="utf-8")
    assert "OPENSQUILLA_GATEWAY_WORKSPACE_DIR=" + str(workspace) in primary_dotenv
    assert "OPENSQUILLA_GATEWAY_STATE_DIR=" + str(state) in primary_dotenv
    assert "OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT=" + str(media) in primary_dotenv
    assert f"SOURCE={recovery_id}" in primary_dotenv
    assert (workspace / "primary-existing.txt").read_text(encoding="utf-8") == "primary\n"
    assert (workspace / "dotenv-routed.txt").read_text(encoding="utf-8") == recovery_id
    assert (state / "sessions.db").is_file()
    assert (media / f"{recovery_id}.txt").is_file()
    assert not (primary / "workspace" / "dotenv-routed.txt").exists()
    assert not (primary / "state" / "sessions.db").exists()
    assert not (primary / "media" / f"{recovery_id}.txt").exists()


@pytest.mark.parametrize("primary_config", ["", "# no user configuration\n"])
def test_empty_primary_config_and_path_env_use_config_only_recovery(
    tmp_path: Path,
    monkeypatch,
    primary_config: str,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "state").mkdir(parents=True)
    (primary / "config.toml").write_text(primary_config, encoding="utf-8")
    (primary / ".env").write_text(
        "OPENSQUILLA_HOME=C:\\Old\\primary\n",
        encoding="utf-8",
    )
    (primary / "state" / ".env").write_text(
        "OPENSQUILLA_STATE_DIR=C:\\Old\\primary\\state\n",
        encoding="utf-8",
    )
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = 'config-only'\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:config-only",
    )
    (recovery / "desktop-credential.json").unlink()
    (recovery / "opensquilla" / ".env").unlink()
    (recovery / "opensquilla" / "state" / ".env").unlink()

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated"
    assert result.configuration_source_recovery_id == recovery_id
    assert result.configuration_source_credential_path is None
    assert result.credential_adoption_status == "not_required"
    assert (primary / "config.toml").read_text(encoding="utf-8") == ("selected = 'config-only'\n")
    assert not (primary / ".env").exists()
    assert not (primary / "state" / ".env").exists()


def test_user_dotenv_only_recovery_is_a_configuration_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:dotenv-only",
    )
    (recovery / "opensquilla" / "config.toml").unlink()
    (recovery / "desktop-credential.json").unlink()
    (recovery / "opensquilla" / ".env").write_text(
        "OPENSQUILLA_HOME=C:\\Old\\recovery\nPROVIDER_SECRET=dotenv-only-secret\n",
        encoding="utf-8",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated"
    assert result.configuration_source_recovery_id == recovery_id
    assert result.configuration_source_credential_path is None
    assert not (primary / "config.toml").exists()
    assert (primary / ".env").read_text(encoding="utf-8") == (
        "PROVIDER_SECRET=dotenv-only-secret\n"
    )


def test_credential_only_recovery_is_a_configuration_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / ".env").write_text(
        "OPENSQUILLA_HOME=C:\\Old\\primary\n",
        encoding="utf-8",
    )
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential='{"provider":"openai","updatedAt":"2026-07-25T05:00:00Z"}\n',
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:credential-only",
    )
    (recovery / "opensquilla" / "config.toml").unlink()
    (recovery / "opensquilla" / ".env").unlink()
    credential_bytes = (recovery / "desktop-credential.json").read_bytes()

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated"
    assert result.configuration_source_recovery_id == recovery_id
    assert result.configuration_source_credential_path is not None
    assert (
        result.configuration_source_credential_sha256
        == hashlib.sha256(credential_bytes).hexdigest()
    )
    assert result.configuration_source_credential_size == len(credential_bytes)
    assert result.credential_adoption_status == "pending"
    assert not (primary / "config.toml").exists()
    assert not (primary / ".env").exists()
    assert not (primary / "state" / ".env").exists()


def test_invalid_credential_does_not_override_valid_recovery_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = 'valid-config'\n",
        credential="{invalid-json\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:invalid-credential-valid-config",
    )
    (recovery / "opensquilla" / ".env").unlink()
    (recovery / "opensquilla" / "state" / ".env").unlink()

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated"
    assert result.configuration_source_recovery_id == recovery_id
    assert result.configuration_source_credential_path is not None
    assert result.credential_adoption_status == "pending"
    assert (primary / "config.toml").read_text(encoding="utf-8") == ("selected = 'valid-config'\n")


def test_invalid_credential_only_recovery_is_not_a_configuration_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential="{invalid-json\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:invalid-credential-only",
    )
    (recovery / "opensquilla" / "config.toml").unlink()
    (recovery / "opensquilla" / ".env").unlink()
    (recovery / "opensquilla" / "state" / ".env").unlink()

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated"
    assert result.configuration_source_recovery_id is None
    assert result.configuration_source_credential_path is None
    assert result.credential_adoption_status == "not_required"
    assert not (primary / "config.toml").exists()


def test_invalid_newest_recovery_dotenv_falls_back_without_losing_its_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    older_id = str(uuid.uuid4())
    newer_id = str(uuid.uuid4())
    older = _recovery(
        user_data,
        older_id,
        config="selected = 'older-valid'\n",
        credential='{"updatedAt":"2026-07-25T01:00:00Z"}\n',
        memory="older memory\n",
        conflict="older",
        extra_name="older.txt",
        session_key="agent:main:older-valid",
    )
    newer = _recovery(
        user_data,
        newer_id,
        config="selected = 'newer-invalid-env'\n",
        credential='{"updatedAt":"2026-07-25T06:00:00Z"}\n',
        memory="newer memory\n",
        conflict="newer",
        extra_name="newer-data.txt",
        session_key="agent:main:newer-invalid-env",
    )
    (newer / "opensquilla" / ".env").write_bytes(b"\xff\xfe")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.configuration_source_recovery_id == older_id
    assert "selected = 'older-valid'" in (primary / "config.toml").read_text(encoding="utf-8")
    assert (primary / "workspace" / "newer-data.txt").read_text(encoding="utf-8") == newer_id
    assert result.backup_path is not None
    assert (
        result.backup_path / "recovery-profiles" / newer_id / "opensquilla" / ".env"
    ).read_bytes() == b"\xff\xfe"
    assert older.is_dir() is False


def test_corrupt_primary_config_remains_authoritative(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    (primary / "config.toml").write_bytes(b"\xff\xfe")
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = 'recovery'\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:corrupt-primary",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated"
    assert result.configuration_source_recovery_id is None
    assert (primary / "config.toml").read_bytes() == b"\xff\xfe"


def test_consolidate_cli_emits_fixed_json_and_blocks_unsafe_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    recovery_root = user_data / "recovery-profiles"
    recovery_root.mkdir(parents=True)
    (recovery_root / "not-a-recovery-uuid").mkdir()

    result = CliRunner().invoke(
        recovery_app,
        [
            "consolidate-profiles",
            "--user-data",
            str(user_data),
            "--primary-home",
            str(primary),
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "schema_version",
        "outcome",
        "stable_code",
        "primary_home",
        "configuration_source_recovery_id",
        "configuration_source_credential_path",
        "configuration_source_credential_sha256",
        "configuration_source_credential_size",
        "consumed_recovery_ids",
        "backup_path",
        "receipt_path",
        "credential_adoption_status",
        "revision",
        "errors",
        "primary_home_intact",
    }
    assert payload["outcome"] == "blocked"
    assert payload["stable_code"] == "profile_consolidation_unsafe_recovery_root"
    assert (recovery_root / "not-a-recovery-uuid").is_dir()
    # This primary is an empty shell, so booting it would show an empty app while
    # the real conversations sit in the legacy container.
    assert payload["primary_home_intact"] is False


def test_consolidate_allows_regular_finder_metadata_in_recovery_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = 'recovery'\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:finder-metadata",
    )
    finder_metadata = user_data / "recovery-profiles" / ".DS_Store"
    finder_metadata.write_bytes(b"finder metadata")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.backup_path is not None
    assert (
        result.backup_path / "recovery-profiles" / ".DS_Store"
    ).read_bytes() == b"finder metadata"


def test_consolidate_rejects_directory_named_like_finder_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    recovery_root = user_data / "recovery-profiles"
    recovery_root.mkdir()
    (recovery_root / ".DS_Store").mkdir()

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "profile_consolidation_unsafe_recovery_root"
    assert (recovery_root / ".DS_Store").is_dir()


def test_consolidate_rejects_finder_metadata_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    recovery_root = user_data / "recovery-profiles"
    recovery_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    finder_metadata = recovery_root / ".DS_Store"
    try:
        finder_metadata.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "profile_consolidation_unsafe_recovery_root"
    assert finder_metadata.is_symlink()
    assert outside.is_dir()


def test_consolidate_preserves_finder_metadata_in_empty_recovery_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    recovery_root = user_data / "recovery-profiles"
    recovery_root.mkdir()
    (recovery_root / ".DS_Store").write_bytes(b"finder metadata")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "noop"
    assert result.stable_code == "no_recovery_profiles"
    assert (recovery_root / ".DS_Store").read_bytes() == b"finder metadata"


def test_fresh_noop_does_not_create_context_or_follow_backup_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    receipts = user_data / "backups" / "profile-consolidation"
    receipts.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "receipt.json").write_text("{}\n", encoding="utf-8")
    try:
        (receipts / str(uuid.uuid4())).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "noop"
    assert result.stable_code == "no_recovery_profiles"
    assert not (user_data / "desktop-profile-context.json").exists()


def test_primary_configuration_still_validates_unsafe_credential_leaf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    real_credential = user_data / "real-credential.json"
    real_credential.write_text("{}\n", encoding="utf-8")
    try:
        (user_data / "desktop-credential.json").symlink_to(real_credential)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:recovery",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "unsafe_path"
    assert (user_data / "recovery-profiles" / recovery_id).is_dir()


def test_config_source_without_credential_keeps_source_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:recovery",
    )
    (recovery / "desktop-credential.json").unlink()

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated"
    assert result.configuration_source_recovery_id == recovery_id
    assert result.configuration_source_credential_path is None
    assert (primary / "config.toml").read_text(encoding="utf-8") == "selected = true\n"


def test_consolidation_publishes_when_primary_home_does_not_exist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential='{"updatedAt":"2026-07-25T03:00:00Z"}\n',
        memory="fresh memory\n",
        conflict="fresh",
        extra_name="fresh.txt",
        session_key="agent:main:fresh",
    )
    assert not primary.exists()

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated"
    assert primary.is_dir()
    assert (primary / "state").is_dir()
    assert (primary / "workspace" / "fresh.txt").read_text(encoding="utf-8") == recovery_id
    assert not (user_data / "recovery-profiles").exists()


def test_tampered_journal_paths_are_rejected_before_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    transaction_id = str(uuid.uuid4())
    backup = user_data / "backups" / "profile-consolidation" / transaction_id
    receipt = backup / "receipt.json"
    (user_data / ".opensquilla-profile-consolidation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "transaction_id": transaction_id,
                "phase": "prepared",
                "user_data": str(user_data),
                "primary_home": str(primary),
                "staging": str(tmp_path / "escape"),
                "recovery_root": str(user_data / "recovery-profiles"),
                "backup_path": str(backup),
                "primary_existed": True,
                "result": {
                    "schema_version": 1,
                    "outcome": "consolidated",
                    "stable_code": "profile_consolidation_complete",
                    "primary_home": str(primary),
                    "configuration_source_recovery_id": None,
                    "configuration_source_credential_path": None,
                    "consumed_recovery_ids": [],
                    "backup_path": str(backup),
                    "receipt_path": str(receipt),
                    "credential_adoption_status": "not_required",
                    "revision": 1,
                    "errors": [],
                },
                "session_merges": [],
            }
        ),
        encoding="utf-8",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "unsafe_path"
    assert primary.is_dir()
    assert not (tmp_path / "escape").exists()


def test_resume_rejects_recovery_home_symlink_before_acquiring_legacy_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:resume-symlink",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_finish = consolidate_module._archive_and_finish

    def interrupt_after_publish(*_args, **_kwargs):
        raise OSError("simulated interruption")

    monkeypatch.setattr(
        consolidate_module,
        "_archive_and_finish",
        interrupt_after_publish,
    )
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(consolidate_module, "_archive_and_finish", original_finish)
    assert interrupted.outcome == "blocked"
    journal = json.loads(
        (user_data / ".opensquilla-profile-consolidation.json").read_text(encoding="utf-8")
    )
    assert journal["phase"] == "primary_published"

    recovery_home = recovery / "opensquilla"
    recovery_home.rename(recovery / "opensquilla-original")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        recovery_home.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "profile_consolidation_unsafe_recovery_root"
    assert not (outside / "state").exists()


def test_resume_rejects_recovery_id_set_that_differs_from_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:resume-id",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_finish = consolidate_module._archive_and_finish

    def interrupt_after_publish(*_args, **_kwargs):
        raise OSError("simulated interruption")

    monkeypatch.setattr(
        consolidate_module,
        "_archive_and_finish",
        interrupt_after_publish,
    )
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(consolidate_module, "_archive_and_finish", original_finish)
    assert interrupted.outcome == "blocked"

    recovery.rename(recovery.parent / str(uuid.uuid4()))

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "unsafe_path"
    assert "do not match" in result.errors[0]


def test_non_utf8_recovery_config_is_skipped_with_fixed_json_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:invalid-config",
    )
    (recovery / "opensquilla" / "config.toml").write_bytes(b"\xff\xfe")

    completed = CliRunner().invoke(
        recovery_app,
        [
            "consolidate-profiles",
            "--user-data",
            str(user_data),
            "--primary-home",
            str(primary),
            "--json",
        ],
    )

    assert completed.exit_code == 0
    payload = json.loads(completed.stdout)
    assert payload["outcome"] == "consolidated"
    assert payload["stable_code"] == "profile_consolidation_complete"
    assert payload["configuration_source_recovery_id"] is None
    assert not (primary / "config.toml").exists()


def test_incompatible_sessions_schema_returns_fixed_blocked_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    _session_database(
        primary / "state" / "sessions.db",
        "agent:main:primary",
        "primary-session",
        "primary",
    )
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:invalid-schema",
    )
    source_db = recovery / "opensquilla" / "state" / "sessions.db"
    source_db.unlink()
    with contextlib.closing(sqlite3.connect(source_db)) as connection, connection:
        connection.execute("CREATE TABLE sessions (unexpected TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('value')")

    completed = CliRunner().invoke(
        recovery_app,
        [
            "consolidate-profiles",
            "--user-data",
            str(user_data),
            "--primary-home",
            str(primary),
            "--json",
        ],
    )

    assert completed.exit_code == 2
    payload = json.loads(completed.stdout)
    assert payload["outcome"] == "blocked"
    assert payload["stable_code"] == "profile_consolidation_failed"


def test_credential_adoption_ack_is_atomic_and_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential='{"updatedAt":"2026-07-25T04:00:00Z"}\n',
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:credential-ack",
    )

    consolidated = consolidate_recovery_profiles(user_data, primary)

    assert consolidated.outcome == "consolidated"
    assert consolidated.credential_adoption_status == "pending"
    assert consolidated.backup_path is not None
    transaction_id = consolidated.backup_path.name
    arguments = [
        "acknowledge-profile-credential",
        "--user-data",
        str(user_data),
        "--primary-home",
        str(primary),
        "--transaction-id",
        transaction_id,
        "--json",
    ]

    acknowledged = CliRunner().invoke(recovery_app, arguments)

    assert acknowledged.exit_code == 0, acknowledged.output
    payload = json.loads(acknowledged.stdout)
    assert payload["outcome"] == "noop"
    assert payload["stable_code"] == "profile_credential_adoption_acknowledged"
    assert payload["credential_adoption_status"] == "complete"
    assert payload["backup_path"] == str(consolidated.backup_path)
    assert payload["receipt_path"] == str(consolidated.receipt_path)
    assert payload["configuration_source_credential_path"] == str(
        consolidated.configuration_source_credential_path
    )
    assert (
        json.loads(consolidated.receipt_path.read_text(encoding="utf-8"))[
            "credential_adoption_status"
        ]
        == "complete"
    )

    repeated_ack = CliRunner().invoke(recovery_app, arguments)
    repeated_consolidation = consolidate_recovery_profiles(user_data, primary)

    assert repeated_ack.exit_code == 0, repeated_ack.output
    assert json.loads(repeated_ack.stdout)["credential_adoption_status"] == "complete"
    assert repeated_consolidation.outcome == "noop"
    assert repeated_consolidation.credential_adoption_status == "complete"

    credential_path = consolidated.configuration_source_credential_path
    assert credential_path is not None
    os.unlink(_native_io_path(credential_path))
    after_archive_cleanup = consolidate_recovery_profiles(user_data, primary)
    repeated_after_cleanup = CliRunner().invoke(recovery_app, arguments)

    assert after_archive_cleanup.outcome == "noop"
    assert after_archive_cleanup.credential_adoption_status == "complete"
    assert repeated_after_cleanup.exit_code == 0, repeated_after_cleanup.output
    assert json.loads(repeated_after_cleanup.stdout)["credential_adoption_status"] == "complete"


def test_credential_ack_rejects_archived_credential_tampering(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential='{"provider":"openai","apiKey":"original"}\n',
        memory="memory\n",
        conflict="recovery",
        extra_name="credential-tamper.txt",
        session_key="agent:main:credential-tamper",
    )
    consolidated = consolidate_recovery_profiles(user_data, primary)
    assert consolidated.outcome == "consolidated"
    credential_path = consolidated.configuration_source_credential_path
    assert credential_path is not None
    _write_text(credential_path, '{"provider":"openai","apiKey":"replaced"}\n')
    assert consolidated.backup_path is not None

    acknowledged = CliRunner().invoke(
        recovery_app,
        [
            "acknowledge-profile-credential",
            "--user-data",
            str(user_data),
            "--primary-home",
            str(primary),
            "--transaction-id",
            consolidated.backup_path.name,
            "--json",
        ],
    )

    assert acknowledged.exit_code == 2, acknowledged.output
    payload = json.loads(acknowledged.stdout)
    assert payload["outcome"] == "blocked"
    assert payload["stable_code"] == "profile_consolidation_source_changed"
    assert (
        json.loads(consolidated.receipt_path.read_text(encoding="utf-8"))[
            "credential_adoption_status"
        ]
        == "pending"
    )


@pytest.mark.parametrize(
    ("legacy_journal", "missing_transaction_path"),
    [
        (True, None),
        (True, "staging"),
        (True, "backup"),
        (False, None),
    ],
)
def test_only_legacy_prepared_journal_changed_by_sqlite_sidecars_restarts(
    tmp_path: Path,
    monkeypatch,
    legacy_journal: bool,
    missing_transaction_path: str | None,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="sqlite-sidecar.txt",
        session_key="agent:main:sqlite-sidecar",
    )
    source_database = recovery / "opensquilla" / "state" / "sessions.db"
    connection = sqlite3.connect(source_database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    assert not source_database.with_name("sessions.db-wal").exists()
    assert not source_database.with_name("sessions.db-shm").exists()

    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_merge = consolidate_module._merge_prepared_profiles

    def simulate_old_sqlite_read(**kwargs):
        for profile in kwargs["profiles"]:
            database = profile.home / "state" / "sessions.db"
            with (
                contextlib.closing(
                    sqlite3.connect(
                        f"{database.absolute().as_uri()}?mode=ro",
                        uri=True,
                    )
                ) as connection,
                connection,
            ):
                assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        raise OSError("simulated old build stop after direct SQLite source read")

    monkeypatch.setattr(
        consolidate_module,
        "_merge_prepared_profiles",
        simulate_old_sqlite_read,
    )
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(
        consolidate_module,
        "_merge_prepared_profiles",
        original_merge,
    )

    assert interrupted.outcome == "blocked"
    journal_path = user_data / ".opensquilla-profile-consolidation.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "prepared"
    assert journal["source_read_protocol"] == "private-sqlite-v1"
    old_staging = Path(journal["staging"])
    old_backup = Path(journal["backup_path"])
    assert source_database.with_name("sessions.db-wal").is_file()
    assert source_database.with_name("sessions.db-shm").is_file()
    if legacy_journal:
        journal.pop("source_read_protocol")
        journal_path.write_text(
            json.dumps(journal, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if missing_transaction_path == "staging":
            shutil.rmtree(old_staging)
        elif missing_transaction_path == "backup":
            old_backup.rmdir()

    resumed = consolidate_recovery_profiles(user_data, primary)

    if legacy_journal:
        assert resumed.outcome == "consolidated", resumed
        assert not journal_path.exists()
        assert not old_staging.exists()
        if missing_transaction_path == "backup":
            assert not old_backup.exists()
        else:
            assert old_backup.is_dir()
            assert not any(old_backup.iterdir())
        assert (primary / "workspace" / "sqlite-sidecar.txt").is_file()
    else:
        # Refusing a drifted current-protocol plan was right while a blocked
        # fan-in still gated startup: nothing should have been touching the
        # source, so drift meant something unexpected did. Startup now continues
        # whenever the primary profile is usable, which makes the user's own
        # activity the likeliest cause of drift — so the stale plan is discarded
        # and re-prepared instead of refusing on every launch forever, which
        # would mean the recovered conversations never arrive. Re-preparing
        # re-measures every source under the profile locks and re-merges
        # idempotently, so nothing is lost.
        assert resumed.outcome == "consolidated", resumed
        assert not journal_path.exists()
        assert not old_staging.exists()
        assert (primary / "workspace" / "sqlite-sidecar.txt").is_file()


@pytest.mark.parametrize(
    "unsafe_restart_state",
    [
        "later-phase",
        "external-binding",
        "external-route",
        "primary-changed",
        "backup-not-empty",
        "recovery-ids-changed",
    ],
)
def test_legacy_prepared_journal_restart_remains_fail_closed(
    tmp_path: Path,
    monkeypatch,
    unsafe_restart_state: str,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="fail-closed.txt",
        session_key="agent:main:fail-closed",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_merge = consolidate_module._merge_prepared_profiles

    def stop_after_prepared(**_kwargs):
        raise OSError("simulated stop after prepared journal")

    monkeypatch.setattr(
        consolidate_module,
        "_merge_prepared_profiles",
        stop_after_prepared,
    )
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(
        consolidate_module,
        "_merge_prepared_profiles",
        original_merge,
    )
    assert interrupted.outcome == "blocked"

    journal_path = user_data / ".opensquilla-profile-consolidation.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal.pop("source_read_protocol")
    old_staging = Path(journal["staging"])
    old_backup = Path(journal["backup_path"])
    # Make the legacy source token stale so falling back to normal resume must
    # block instead of accidentally completing the transaction.
    (recovery / "opensquilla" / "ordinary.txt").write_text(
        "changed after prepared",
        encoding="utf-8",
    )

    if unsafe_restart_state == "later-phase":
        journal["phase"] = "external_roots_merged"
        journal["staging_merged"] = journal["staging_baseline"]
    elif unsafe_restart_state == "external-binding":
        journal["routes"]["external_bindings"] = [{"path": str(tmp_path / "external")}]
    elif unsafe_restart_state == "external-route":
        journal["routes"]["workspace"]["profile_relative"] = None
    elif unsafe_restart_state == "primary-changed":
        (primary / "config.toml").write_text(
            "primary = true\nchanged = true\n",
            encoding="utf-8",
        )
    elif unsafe_restart_state == "backup-not-empty":
        (old_backup / "unexpected").write_text("preserve", encoding="utf-8")
    elif unsafe_restart_state == "recovery-ids-changed":
        _recovery(
            user_data,
            str(uuid.uuid4()),
            config="second = true\n",
            credential="{}\n",
            memory="second",
            conflict="second",
            extra_name="second.txt",
            session_key="agent:main:second-fail-closed",
        )
    else:  # pragma: no cover - exhaustive parameter guard
        raise AssertionError(unsafe_restart_state)
    journal_path.write_text(
        json.dumps(journal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    resumed = consolidate_recovery_profiles(user_data, primary)

    assert resumed.outcome == "blocked"
    assert journal_path.is_file()
    assert old_staging.is_dir()
    assert old_backup.is_dir()
    if unsafe_restart_state == "backup-not-empty":
        assert (old_backup / "unexpected").read_text(encoding="utf-8") == "preserve"


def test_session_id_remap_moves_transcript_attachment_material(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "config.toml").parent.mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    _session_database(
        primary / "state" / "sessions.db",
        "agent:main:attachment-primary",
        "shared-session-id",
        "primary",
    )
    primary_material = primary / "media" / "transcripts" / "shared-session-id"
    primary_material.mkdir(parents=True)
    (primary_material / "primary-sha").write_bytes(b"primary attachment")

    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:attachment-recovery",
    )
    source_db = recovery / "opensquilla" / "state" / "sessions.db"
    with contextlib.closing(sqlite3.connect(source_db)) as connection, connection:
        connection.execute("UPDATE sessions SET session_id='shared-session-id'")
        connection.execute("UPDATE transcript_entries SET session_id='shared-session-id'")
    recovery_material = recovery / "opensquilla" / "media" / "transcripts" / "shared-session-id"
    recovery_material.mkdir(parents=True)
    (recovery_material / "recovery-sha").write_bytes(b"recovery attachment")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated"
    with contextlib.closing(sqlite3.connect(primary / "state" / "sessions.db")) as merged, merged:
        remapped_session_id = merged.execute(
            "SELECT session_id FROM sessions WHERE session_key=?",
            ("agent:main:attachment-recovery",),
        ).fetchone()[0]
    assert remapped_session_id != "shared-session-id"
    assert (
        primary / "media" / "transcripts" / remapped_session_id / "recovery-sha"
    ).read_bytes() == b"recovery attachment"
    assert (
        primary / "media" / "transcripts" / "shared-session-id" / "primary-sha"
    ).read_bytes() == b"primary attachment"
    assert not (primary / "media" / "transcripts" / "shared-session-id" / "recovery-sha").exists()


def test_copying_workspace_symlink_never_follows_it_for_directory_hint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    source = tmp_path / "source-link"
    try:
        source.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")
    destination = tmp_path / "destination" / "copied-link"
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")

    def reject_follow(*_args, **_kwargs):
        raise AssertionError("Path.is_dir would follow the symlink")

    monkeypatch.setattr(Path, "is_dir", reject_follow)

    consolidate_module._copy_leaf(source, destination)

    assert destination.is_symlink()
    assert _link_assertion_target(os.readlink(destination)) == _link_assertion_target(str(outside))


def _external_primary_config(
    primary: Path,
    *,
    workspace: Path,
    state: Path,
    media: Path,
    agent_workspace: Path | None = None,
) -> bytes:
    lines = [
        f"workspace_dir = {json.dumps(str(workspace))}",
        f"state_dir = {json.dumps(str(state))}",
        "[attachments]",
        f"media_root = {json.dumps(str(media))}",
    ]
    if agent_workspace is not None:
        lines.extend(
            [
                "[[agents]]",
                "id = 'ops'",
                f"workspace = {json.dumps(str(agent_workspace))}",
            ]
        )
    raw = ("\n".join(lines) + "\n").encode()
    primary.mkdir(parents=True)
    (primary / "config.toml").write_bytes(raw)
    return raw


def test_primary_external_roots_receive_active_data_without_canonical_duplicates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    external = tmp_path / "external"
    workspace = external / "workspace"
    state = external / "state"
    media = external / "media"
    agent_workspace = external / "ops"
    for path in (workspace, state, media, agent_workspace):
        path.mkdir(parents=True)
    config_bytes = _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=media,
        agent_workspace=agent_workspace,
    )
    _session_database(
        state / "sessions.db",
        "agent:main:external-primary",
        "shared-external-id",
        "primary",
    )
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="recovery memory\n",
        conflict="recovery",
        extra_name="external-only.txt",
        session_key="agent:main:external-recovery",
    )
    recovery_workspace = recovery / "opensquilla" / "workspace"
    (recovery_workspace / "agents" / "ops").mkdir(parents=True)
    (recovery_workspace / "agents" / "ops" / "OPS.md").write_text(
        "ops data\n",
        encoding="utf-8",
    )
    (recovery_workspace / "agents" / "other").mkdir(parents=True)
    (recovery_workspace / "agents" / "other" / "OTHER.md").write_text(
        "other data\n",
        encoding="utf-8",
    )
    source_db = recovery / "opensquilla" / "state" / "sessions.db"
    with contextlib.closing(sqlite3.connect(source_db)) as connection, connection:
        connection.execute("UPDATE sessions SET session_id='shared-external-id'")
        connection.execute("UPDATE transcript_entries SET session_id='shared-external-id'")
    transcript = recovery / "opensquilla" / "media" / "transcripts" / "shared-external-id"
    transcript.mkdir(parents=True)
    (transcript / "attachment").write_bytes(b"external transcript")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert (primary / "config.toml").read_bytes() == config_bytes
    assert (workspace / "external-only.txt").read_text(encoding="utf-8") == recovery_id
    assert (agent_workspace / "OPS.md").read_text(encoding="utf-8") == "ops data\n"
    assert (workspace / "agents" / "other" / "OTHER.md").read_text(
        encoding="utf-8"
    ) == "other data\n"
    assert not (primary / "workspace" / "external-only.txt").exists()
    assert not (primary / "state" / "sessions.db").exists()
    assert not (primary / "media" / f"{recovery_id}.txt").exists()
    assert (media / f"{recovery_id}.txt").read_text(encoding="utf-8") == "media"
    with contextlib.closing(sqlite3.connect(state / "sessions.db")) as connection, connection:
        row = connection.execute(
            "SELECT session_id FROM sessions WHERE session_key=?",
            ("agent:main:external-recovery",),
        ).fetchone()
    assert row is not None
    remapped_session_id = row[0]
    assert remapped_session_id != "shared-external-id"
    assert (
        media / "transcripts" / remapped_session_id / "attachment"
    ).read_bytes() == b"external transcript"


@pytest.mark.parametrize("relationship", ["same", "arbitrary_nested"])
def test_overlapping_independent_external_roots_fail_before_writes(
    tmp_path: Path,
    monkeypatch,
    relationship: str,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    external = tmp_path / "external"
    workspace = external / "workspace"
    state = workspace if relationship == "same" else workspace / "arbitrary-state-root"
    media = external / "media"
    for path in {workspace, state, media}:
        path.mkdir(parents=True, exist_ok=True)
    _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=media,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="must-not-copy.txt",
        session_key="agent:main:overlap",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "unsafe_path"
    assert not (workspace / "must-not-copy.txt").exists()
    assert not (user_data / ".opensquilla-profile-consolidation.json").exists()
    assert (user_data / "recovery-profiles" / recovery_id).is_dir()


def test_explicit_media_cannot_overlap_canonical_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    workspace = primary / "workspace"
    (primary / "config.toml").write_text(
        f"[attachments]\nmedia_root = {json.dumps(str(workspace))}\n",
        encoding="utf-8",
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="must-not-copy.txt",
        session_key="agent:main:media-canonical-overlap",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "unsafe_path"
    assert not (workspace / "must-not-copy.txt").exists()
    assert not (user_data / ".opensquilla-profile-consolidation.json").exists()
    assert (user_data / "recovery-profiles" / recovery_id).is_dir()


@pytest.mark.parametrize(
    "overlap_role",
    ["workspace", "state", "media", "derived-agent"],
)
def test_explicit_agent_cannot_overlap_any_effective_root(
    tmp_path: Path,
    monkeypatch,
    overlap_role: str,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    roots = {
        "workspace": primary / "workspace",
        "state": primary / "state",
        "media": primary / "media",
        "derived-agent": primary / "workspace" / "agents" / "other",
    }
    lines = [
        "primary = true",
        "[[agents]]",
        "id = 'ops'",
        f"workspace = {json.dumps(str(roots[overlap_role]))}",
    ]
    if overlap_role == "derived-agent":
        lines.extend(
            [
                "[[agents]]",
                "id = 'other'",
                f"workspace = {json.dumps(str(roots['derived-agent']))}",
            ]
        )
    (primary / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="must-not-copy.txt",
        session_key=f"agent:main:agent-{overlap_role}-overlap",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "unsafe_path"
    assert not (primary / "workspace" / "must-not-copy.txt").exists()
    assert not (user_data / ".opensquilla-profile-consolidation.json").exists()
    assert (user_data / "recovery-profiles" / recovery_id).is_dir()


@pytest.mark.parametrize("explicit_role", ["workspace", "state"])
def test_single_explicit_workspace_state_alias_fails_before_writes(
    tmp_path: Path,
    monkeypatch,
    explicit_role: str,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    canonical_workspace = primary / "workspace"
    canonical_state = primary / "state"
    override = canonical_state if explicit_role == "workspace" else canonical_workspace
    (primary / "config.toml").write_text(
        f"{explicit_role}_dir = {json.dumps(str(override))}\n",
        encoding="utf-8",
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="must-not-copy.txt",
        session_key="agent:main:single-explicit-overlap",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "unsafe_path"
    assert not (canonical_workspace / "must-not-copy.txt").exists()
    assert not (canonical_state / "must-not-copy.txt").exists()
    assert not (user_data / ".opensquilla-profile-consolidation.json").exists()
    assert (user_data / "recovery-profiles" / recovery_id).is_dir()


def test_relative_media_and_agent_roots_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    workspace = tmp_path / "external" / "workspace"
    state = tmp_path / "external" / "state"
    workspace.mkdir(parents=True)
    state.mkdir(parents=True)
    primary.mkdir(parents=True)
    (primary / "config.toml").write_text(
        f"workspace_dir = {json.dumps(str(workspace))}\n"
        f"state_dir = {json.dumps(str(state))}\n"
        "[attachments]\n"
        "media_root = 'relative-media'\n"
        "[[agents]]\n"
        "id = 'ops'\n"
        "workspace = 'relative-agent'\n",
        encoding="utf-8",
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="must-not-copy.txt",
        session_key="agent:main:relative-root",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "unsafe_path"
    assert (user_data / "recovery-profiles" / recovery_id).is_dir()


def test_prepared_resume_revalidates_config_before_external_active_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    external = tmp_path / "external"
    workspace = external / "workspace"
    state = external / "state"
    media = external / "media"
    for path in (workspace, state, media):
        path.mkdir(parents=True)
    _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=media,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="must-not-copy.txt",
        session_key="agent:main:prepared-config",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_merge = consolidate_module._merge_prepared_profiles

    def fail_after_prepared(**_kwargs):
        assert (user_data / ".opensquilla-profile-consolidation.json").is_file()
        raise OSError("simulated hard stop after prepared")

    monkeypatch.setattr(
        consolidate_module,
        "_merge_prepared_profiles",
        fail_after_prepared,
    )
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(
        consolidate_module,
        "_merge_prepared_profiles",
        original_merge,
    )
    assert interrupted.outcome == "blocked"
    assert not (workspace / "must-not-copy.txt").exists()

    (primary / "config.toml").write_text(
        (primary / "config.toml").read_text(encoding="utf-8") + "debug = true\n",
        encoding="utf-8",
    )
    resumed = consolidate_recovery_profiles(user_data, primary)

    assert resumed.outcome == "blocked"
    assert resumed.stable_code == "profile_consolidation_source_changed"
    assert not (workspace / "must-not-copy.txt").exists()
    assert (user_data / "recovery-profiles" / recovery_id).is_dir()


def test_partial_external_copy_resumes_without_duplicate_memory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    external = tmp_path / "external"
    workspace = external / "workspace"
    state = external / "state"
    media = external / "media"
    for path in (workspace, state, media):
        path.mkdir(parents=True)
    _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=media,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="one recovered fact\n",
        conflict="recovery",
        extra_name="resume-copy.txt",
        session_key="agent:main:partial-copy",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_copy = consolidate_module._atomic_copy_regular
    injected = False

    def fail_after_one_publish(*args, **kwargs):
        nonlocal injected
        original_copy(*args, **kwargs)
        if not injected:
            injected = True
            raise OSError("simulated hard stop during external merge")

    monkeypatch.setattr(
        consolidate_module,
        "_atomic_copy_regular",
        fail_after_one_publish,
    )
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(
        consolidate_module,
        "_atomic_copy_regular",
        original_copy,
    )
    assert interrupted.outcome == "blocked"
    journal = json.loads(
        (user_data / ".opensquilla-profile-consolidation.json").read_text(encoding="utf-8")
    )
    assert journal["phase"] == "prepared"

    resumed = consolidate_recovery_profiles(user_data, primary)

    assert resumed.outcome == "consolidated", resumed
    assert (workspace / "MEMORY.md").read_text(encoding="utf-8") == ("one recovered fact\n")
    assert (workspace / "resume-copy.txt").read_text(encoding="utf-8") == recovery_id


def test_external_roots_merged_resume_remerges_once_before_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    external = tmp_path / "external"
    workspace = external / "workspace"
    state = external / "state"
    media = external / "media"
    for path in (workspace, state, media):
        path.mkdir(parents=True)
    _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=media,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="merged-once.txt",
        session_key="agent:main:external-stage",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_commit = consolidate_module._commit_primary
    original_merge = consolidate_module._merge_recovery_data

    def fail_before_primary_commit(_journal_path, payload):
        assert payload["phase"] == "external_roots_merged"
        raise OSError("simulated hard stop after external merge")

    monkeypatch.setattr(
        consolidate_module,
        "_commit_primary",
        fail_before_primary_commit,
    )
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(consolidate_module, "_commit_primary", original_commit)
    assert interrupted.outcome == "blocked"
    assert (
        json.loads(
            (user_data / ".opensquilla-profile-consolidation.json").read_text(encoding="utf-8")
        )["phase"]
        == "external_roots_merged"
    )

    remerge_count = 0

    def tracked_remerge(*args, **kwargs):
        nonlocal remerge_count
        remerge_count += 1
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(
        consolidate_module,
        "_merge_recovery_data",
        tracked_remerge,
    )
    resumed = consolidate_recovery_profiles(user_data, primary)

    assert resumed.outcome == "consolidated", resumed
    assert remerge_count == 1
    assert (workspace / "merged-once.txt").read_text(encoding="utf-8") == recovery_id


def test_primary_dotenv_data_root_overrides_are_authoritative(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    external = tmp_path / "external"
    workspace = external / "workspace"
    state = external / "state"
    media = external / "media"
    for path in (workspace, state, media):
        path.mkdir(parents=True)
    dotenv = (
        f"OPENSQUILLA_GATEWAY_WORKSPACE_DIR={workspace}\n"
        f"OPENSQUILLA_GATEWAY_STATE_DIR={state}\n"
        f"OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT={media}\n"
    )
    (primary / ".env").write_text(dotenv, encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="dotenv memory\n",
        conflict="recovery",
        extra_name="dotenv-root.txt",
        session_key="agent:main:dotenv-root",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert (primary / ".env").read_text(encoding="utf-8") == dotenv
    assert (workspace / "dotenv-root.txt").read_text(encoding="utf-8") == recovery_id
    assert (state / "sessions.db").is_file()
    assert (media / f"{recovery_id}.txt").is_file()
    assert not (primary / "workspace" / "dotenv-root.txt").exists()


def test_publish_then_archive_failure_resumes_with_recovery_configuration_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("# intentionally empty\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = 'recovery-source'\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="publish-resume.txt",
        session_key="agent:main:publish-resume",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_finish = consolidate_module._archive_and_finish

    def fail_after_publish(*_args, **_kwargs):
        raise OSError("simulated hard stop after primary publish")

    monkeypatch.setattr(
        consolidate_module,
        "_archive_and_finish",
        fail_after_publish,
    )
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(
        consolidate_module,
        "_archive_and_finish",
        original_finish,
    )
    assert interrupted.outcome == "blocked"
    assert (
        json.loads(
            (user_data / ".opensquilla-profile-consolidation.json").read_text(encoding="utf-8")
        )["phase"]
        == "primary_published"
    )
    assert (primary / "config.toml").read_text(encoding="utf-8") == (
        "selected = 'recovery-source'\n"
    )

    resumed = consolidate_recovery_profiles(user_data, primary)

    assert resumed.outcome == "consolidated", resumed
    assert not (user_data / "recovery-profiles").exists()
    assert (primary / "workspace" / "publish-resume.txt").is_file()


def test_partial_recovered_data_directory_is_completed_on_retry(
    tmp_path: Path,
) -> None:
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "first.txt").write_text("first\n", encoding="utf-8")
    (source / "nested" / "second.txt").write_text("second\n", encoding="utf-8")
    staging = tmp_path / "staging"
    partial = staging / "recovered-data" / "recovery-id" / "profile" / "state"
    partial.mkdir(parents=True)
    (partial / "first.txt").write_text("first\n", encoding="utf-8")

    consolidate_module._preserve_conflict(
        source,
        staging,
        "recovery-id",
        Path("state"),
        scope="profile",
    )

    assert (partial / "nested" / "second.txt").read_text(encoding="utf-8") == ("second\n")


def test_canonical_sessions_external_media_resume_rebuilds_transcript_remap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    media = tmp_path / "external-media"
    media.mkdir()
    primary.mkdir(parents=True)
    (primary / "config.toml").write_text(
        f"primary = true\n[attachments]\nmedia_root = {json.dumps(str(media))}\n",
        encoding="utf-8",
    )
    _session_database(
        primary / "state" / "sessions.db",
        "agent:main:canonical-primary",
        "shared-media-id",
        "primary",
    )
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="canonical-state.txt",
        session_key="agent:main:canonical-recovery",
    )
    source_db = recovery / "opensquilla" / "state" / "sessions.db"
    with contextlib.closing(sqlite3.connect(source_db)) as connection, connection:
        connection.execute("UPDATE sessions SET session_id='shared-media-id'")
        connection.execute("UPDATE transcript_entries SET session_id='shared-media-id'")
    transcript = recovery / "opensquilla" / "media" / "transcripts" / "shared-media-id"
    transcript.mkdir(parents=True)
    (transcript / "attachment").write_bytes(b"remapped")
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_copy = consolidate_module._atomic_copy_regular
    injected = False

    def fail_during_external_media(*args, **kwargs):
        nonlocal injected
        original_copy(*args, **kwargs)
        if not injected:
            injected = True
            raise OSError("simulated media merge hard stop")

    monkeypatch.setattr(
        consolidate_module,
        "_atomic_copy_regular",
        fail_during_external_media,
    )
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(
        consolidate_module,
        "_atomic_copy_regular",
        original_copy,
    )
    assert interrupted.outcome == "blocked"
    assert (
        json.loads(
            (user_data / ".opensquilla-profile-consolidation.json").read_text(encoding="utf-8")
        )["phase"]
        == "prepared"
    )

    resumed = consolidate_recovery_profiles(user_data, primary)

    assert resumed.outcome == "consolidated", resumed
    with (
        contextlib.closing(sqlite3.connect(primary / "state" / "sessions.db")) as connection,
        connection,
    ):
        remapped = connection.execute(
            "SELECT session_id FROM sessions WHERE session_key=?",
            ("agent:main:canonical-recovery",),
        ).fetchone()[0]
    assert remapped != "shared-media-id"
    assert (media / "transcripts" / remapped / "attachment").read_bytes() == b"remapped"
    assert not (media / "transcripts" / "shared-media-id" / "attachment").exists()


def test_recovery_archive_finishes_while_legacy_exclusion_is_held(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="archive-order.txt",
        session_key="agent:main:archive-order",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_acquire = consolidate_module.acquire_legacy_gateway_locks
    original_finish = consolidate_module._archive_and_finish
    legacy_active = False

    @consolidate_module.contextlib.contextmanager
    def tracked_acquire(*args, **kwargs):
        nonlocal legacy_active
        with original_acquire(*args, **kwargs) as held:
            legacy_active = True
            try:
                yield held
            finally:
                legacy_active = False

    def checked_finish(*args, **kwargs):
        assert legacy_active
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(
        consolidate_module,
        "acquire_legacy_gateway_locks",
        tracked_acquire,
    )
    monkeypatch.setattr(
        consolidate_module,
        "_archive_and_finish",
        checked_finish,
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result


def test_missing_external_root_suffixes_are_created_after_prepared_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    external = tmp_path / "external"
    external.mkdir()
    workspace = external / "workspace"
    state = external / "state"
    media = external / "media"
    agent_workspace = external / "ops"
    _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=media,
        agent_workspace=agent_workspace,
    )
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="created-root.txt",
        session_key="agent:main:missing-roots",
    )
    (recovery / "opensquilla" / "workspace" / "agents" / "ops").mkdir(parents=True)
    (recovery / "opensquilla" / "workspace" / "agents" / "ops" / "OPS.md").write_text(
        "ops\n", encoding="utf-8"
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert (workspace / "created-root.txt").is_file()
    assert (state / "sessions.db").is_file()
    assert (media / f"{recovery_id}.txt").is_file()
    assert (agent_workspace / "OPS.md").read_text(encoding="utf-8") == "ops\n"


def test_main_agent_workspace_entry_is_ignored_like_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    workspace = tmp_path / "external" / "workspace"
    ignored_main = tmp_path / "external" / "ignored-main"
    workspace.mkdir(parents=True)
    ignored_main.mkdir(parents=True)
    primary.mkdir(parents=True)
    (primary / "config.toml").write_text(
        f"workspace_dir = {json.dumps(str(workspace))}\n"
        "[[agents]]\n"
        "id = 'main'\n"
        f"workspace = {json.dumps(str(ignored_main))}\n",
        encoding="utf-8",
    )
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="main-root.txt",
        session_key="agent:main:main-workspace",
    )
    main_agent = recovery / "opensquilla" / "workspace" / "agents" / "main"
    main_agent.mkdir(parents=True)
    (main_agent / "MAIN.md").write_text("main agent\n", encoding="utf-8")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert (workspace / "MAIN.md").read_text(encoding="utf-8") == "main agent\n"
    assert not (workspace / "agents" / "main" / "MAIN.md").exists()
    assert not (ignored_main / "MAIN.md").exists()


def test_recovery_agent_ids_are_normalized_when_config_is_rebased(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config=(
            "selected = true\n"
            "[[agents]]\n"
            "id = 'Default'\n"
            "workspace = 'C:\\\\old-default'\n"
            "[[agents]]\n"
            "id = 'Foo Bar'\n"
            "workspace = 'C:\\\\old-foo'\n"
        ),
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="normalized-agent.txt",
        session_key="agent:main:normalized-agent",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    payload = tomllib.loads((primary / "config.toml").read_text(encoding="utf-8"))
    assert payload["agents"][0]["workspace"] == str(primary / "workspace")
    assert payload["agents"][1]["workspace"] == str(primary / "workspace" / "agents" / "foo-bar")


def test_ambient_gateway_root_overrides_match_consolidation_routes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    external = tmp_path / "ambient"
    workspace = external / "workspace"
    state = external / "state"
    media = external / "media"
    for path in (workspace, state, media):
        path.mkdir(parents=True)
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_STATE_DIR", str(state))
    monkeypatch.setenv(
        "OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT",
        str(media),
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="ambient-root.txt",
        session_key="agent:main:ambient-root",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert (workspace / "ambient-root.txt").is_file()
    assert (state / "sessions.db").is_file()
    assert (media / f"{recovery_id}.txt").is_file()
    assert not (primary / "workspace" / "ambient-root.txt").exists()


def test_primary_configuration_appearing_before_profile_lock_wins(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = 'recovery'\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="lock-selection.txt",
        session_key="agent:main:lock-selection",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_acquire = consolidate_module.acquire_profile_locks

    @consolidate_module.contextlib.contextmanager
    def config_appears_before_yield(*args, **kwargs):
        with original_acquire(*args, **kwargs) as held:
            (primary / "config.toml").write_text(
                "selected = 'primary'\n",
                encoding="utf-8",
            )
            yield held

    monkeypatch.setattr(
        consolidate_module,
        "acquire_profile_locks",
        config_appears_before_yield,
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.configuration_source_recovery_id is None
    assert (primary / "config.toml").read_text(encoding="utf-8") == ("selected = 'primary'\n")


def test_new_recovery_profile_before_archive_is_not_silently_consumed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    first_id = str(uuid.uuid4())
    _recovery(
        user_data,
        first_id,
        config="first = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="first",
        extra_name="first.txt",
        session_key="agent:main:first-before-archive",
    )
    second_id = str(uuid.uuid4())
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_finish = consolidate_module._archive_and_finish
    injected = False

    def inject_before_archive(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            _recovery(
                user_data,
                second_id,
                config="second = true\n",
                credential="{}\n",
                memory="memory\n",
                conflict="second",
                extra_name="second.txt",
                session_key="agent:main:second-before-archive",
            )
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(
        consolidate_module,
        "_archive_and_finish",
        inject_before_archive,
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "profile_consolidation_source_changed"
    assert (user_data / "recovery-profiles" / first_id).is_dir()
    assert (user_data / "recovery-profiles" / second_id).is_dir()


def test_state_parent_with_direct_workspace_child_is_supported(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    state = tmp_path / "external" / "OpenSquilla"
    workspace = state / "workspace"
    media = state / "media"
    for path in (workspace, media):
        path.mkdir(parents=True)
    _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=media,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="state-parent.txt",
        session_key="agent:main:state-parent",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert (workspace / "state-parent.txt").is_file()
    assert (state / "sessions.db").is_file()
    assert (media / f"{recovery_id}.txt").is_file()


def test_workspace_parent_with_direct_state_child_protects_reserved_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    workspace = tmp_path / "external" / "OpenSquilla"
    state = workspace / "state"
    media = workspace / "media"
    for path in (state, media):
        path.mkdir(parents=True)
    _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=media,
    )
    (state / "reserved.txt").write_text("primary", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="workspace-parent.txt",
        session_key="agent:main:workspace-parent",
    )
    recovery_workspace_state = recovery / "opensquilla" / "workspace" / "state"
    (recovery_workspace_state / "reserved.txt").write_text(
        "must-not-overwrite",
        encoding="utf-8",
    )
    recovery_workspace_media = recovery / "opensquilla" / "workspace" / "media"
    recovery_workspace_media.mkdir()
    (recovery_workspace_media / "reserved.txt").write_text(
        "workspace-media",
        encoding="utf-8",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert (workspace / "workspace-parent.txt").is_file()
    assert (state / "reserved.txt").read_text(encoding="utf-8") == "primary"
    assert (state / "sessions.db").is_file()
    assert (
        primary / "recovered-data" / recovery_id / "workspace" / "state" / "reserved.txt"
    ).read_text(encoding="utf-8") == "must-not-overwrite"
    assert (
        primary / "recovered-data" / recovery_id / "workspace" / "media" / "reserved.txt"
    ).read_text(encoding="utf-8") == "workspace-media"


def test_invalid_latest_receipt_does_not_fall_back_to_older_pending_credential(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = 'older-pending'\n",
        credential='{"provider":"openai"}\n',
        memory="memory\n",
        conflict="recovery",
        extra_name="older-pending.txt",
        session_key="agent:main:older-pending",
    )
    older = consolidate_recovery_profiles(user_data, primary)
    assert older.outcome == "consolidated", older
    assert older.credential_adoption_status == "pending"

    invalid_root = user_data / "backups" / "profile-consolidation" / str(uuid.uuid4())
    invalid_root.mkdir()
    invalid_receipt = invalid_root / "receipt.json"
    invalid_receipt.write_text("{}\n", encoding="utf-8")
    older_receipt = older.receipt_path
    assert older_receipt is not None
    newer_timestamp = older_receipt.stat().st_mtime_ns + 1_000_000_000
    os.utime(invalid_receipt, ns=(newer_timestamp, newer_timestamp))

    repeated = consolidate_recovery_profiles(user_data, primary)

    assert repeated.outcome == "noop"
    assert repeated.stable_code == "no_recovery_profiles"
    assert repeated.credential_adoption_status == "not_required"


def test_semantically_invalid_newest_credential_only_falls_back(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    older_id = str(uuid.uuid4())
    older = _recovery(
        user_data,
        older_id,
        config="selected = 'older-valid'\n",
        credential='{"provider":"openai","updatedAt":"2026-07-25T01:00:00Z"}\n',
        memory="older\n",
        conflict="older",
        extra_name="older-semantic.txt",
        session_key="agent:main:older-semantic",
    )
    (older / "opensquilla" / ".env").unlink()
    (older / "opensquilla" / "state" / ".env").unlink()
    newer_id = str(uuid.uuid4())
    newer = _recovery(
        user_data,
        newer_id,
        config="newer = true\n",
        credential='{"provider":123,"updatedAt":"2026-07-25T09:00:00Z"}\n',
        memory="newer\n",
        conflict="newer",
        extra_name="newer-semantic.txt",
        session_key="agent:main:newer-semantic",
    )
    (newer / "opensquilla" / "config.toml").unlink()
    (newer / "opensquilla" / ".env").unlink()
    (newer / "opensquilla" / "state" / ".env").unlink()

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.configuration_source_recovery_id == older_id
    assert (primary / "config.toml").read_text(encoding="utf-8") == ("selected = 'older-valid'\n")


def test_legacy_workspace_alias_matches_desktop_gateway_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    standalone_workspace = tmp_path / "standalone-workspace"
    monkeypatch.setenv("OPENSQUILLA_WORKSPACE_DIR", str(standalone_workspace))
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="gateway-route-only.txt",
        session_key="agent:main:gateway-route-only",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert (standalone_workspace / "gateway-route-only.txt").is_file()
    assert not (primary / "workspace" / "gateway-route-only.txt").exists()


def test_recovery_agent_config_tracks_ambient_workspace_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    external_workspace = tmp_path / "external-workspace"
    external_workspace.mkdir()
    monkeypatch.setenv(
        "OPENSQUILLA_GATEWAY_WORKSPACE_DIR",
        str(external_workspace),
    )
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config=(
            "selected = true\n"
            "[[agents]]\n"
            "id = 'ops'\n"
            "workspace = 'C:\\\\legacy-recovery\\\\workspace\\\\agents\\\\ops'\n"
        ),
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="ambient-agent-route.txt",
        session_key="agent:main:ambient-agent-route",
    )
    source_agent = recovery / "opensquilla" / "workspace" / "agents" / "ops"
    source_agent.mkdir(parents=True)
    (source_agent / "OPS.md").write_text("ops data\n", encoding="utf-8")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    effective_agent_workspace = external_workspace / "agents" / "ops"
    assert (effective_agent_workspace / "OPS.md").read_text(encoding="utf-8") == "ops data\n"
    payload = tomllib.loads((primary / "config.toml").read_text(encoding="utf-8"))
    assert payload["agents"][0]["workspace"] == str(effective_agent_workspace)
    assert not (primary / "workspace" / "agents" / "ops" / "OPS.md").exists()


def test_toml_media_root_precedes_ambient_media_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    external = tmp_path / "external"
    workspace = external / "workspace"
    state = external / "state"
    configured_media = external / "configured-media"
    ambient_media = external / "ambient-media"
    for path in (workspace, state, configured_media, ambient_media):
        path.mkdir(parents=True)
    _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=configured_media,
    )
    monkeypatch.setenv(
        "OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT",
        str(ambient_media),
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="toml-media-precedence.txt",
        session_key="agent:main:toml-media-precedence",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert (configured_media / f"{recovery_id}.txt").is_file()
    assert not (ambient_media / f"{recovery_id}.txt").exists()


def test_prepared_resume_rebuilds_and_discards_staging_tampering(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="prepared-rebuild.txt",
        session_key="agent:main:prepared-rebuild",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_merge = consolidate_module._merge_prepared_profiles

    def stop_after_prepared(**_kwargs):
        raise OSError("simulated interruption after prepared journal")

    monkeypatch.setattr(
        consolidate_module,
        "_merge_prepared_profiles",
        stop_after_prepared,
    )
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(
        consolidate_module,
        "_merge_prepared_profiles",
        original_merge,
    )
    assert interrupted.outcome == "blocked"
    journal = json.loads(
        (user_data / ".opensquilla-profile-consolidation.json").read_text(encoding="utf-8")
    )
    assert journal["phase"] == "prepared"
    staging = Path(journal["staging"])
    (staging / "injected-after-journal.txt").write_text(
        "untrusted",
        encoding="utf-8",
    )

    resumed = consolidate_recovery_profiles(user_data, primary)

    assert resumed.outcome == "consolidated", resumed
    assert not (primary / "injected-after-journal.txt").exists()
    assert (primary / "workspace" / "prepared-rebuild.txt").is_file()


def test_external_stage_resume_restores_deleted_recovery_leaf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    external = tmp_path / "external"
    workspace = external / "workspace"
    state = external / "state"
    media = external / "media"
    for path in (workspace, state, media):
        path.mkdir(parents=True)
    _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=media,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="restore-after-delete.txt",
        session_key="agent:main:restore-after-delete",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_commit = consolidate_module._commit_primary

    def stop_before_commit(_journal_path, payload):
        assert payload["phase"] == "external_roots_merged"
        raise OSError("simulated interruption before primary commit")

    monkeypatch.setattr(consolidate_module, "_commit_primary", stop_before_commit)
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(consolidate_module, "_commit_primary", original_commit)
    assert interrupted.outcome == "blocked"
    merged_leaf = workspace / "restore-after-delete.txt"
    assert merged_leaf.is_file()
    merged_leaf.unlink()

    resumed = consolidate_recovery_profiles(user_data, primary)

    assert resumed.outcome == "consolidated", resumed
    assert merged_leaf.read_text(encoding="utf-8") == recovery_id


def test_external_root_identity_replacement_after_merge_is_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    external = tmp_path / "external"
    external.mkdir()
    workspace = external / "workspace"
    state = external / "state"
    media = external / "media"
    _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=media,
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="identity-replacement.txt",
        session_key="agent:main:identity-replacement",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_commit = consolidate_module._commit_primary

    def stop_before_commit(_journal_path, payload):
        assert payload["phase"] == "external_roots_merged"
        raise OSError("simulated interruption before primary commit")

    monkeypatch.setattr(consolidate_module, "_commit_primary", stop_before_commit)
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(consolidate_module, "_commit_primary", original_commit)
    assert interrupted.outcome == "blocked"
    workspace.rename(external / "workspace-replaced")
    workspace.mkdir()

    resumed = consolidate_recovery_profiles(user_data, primary)

    assert resumed.outcome == "blocked"
    assert resumed.stable_code == "unsafe_path"
    assert not (workspace / "identity-replacement.txt").exists()


def test_archived_credential_mutation_before_context_write_blocks_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential='{"provider":"openai","model":"gpt-5"}\n',
        memory="memory\n",
        conflict="recovery",
        extra_name="archived-credential.txt",
        session_key="agent:main:archived-credential",
    )
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_context = consolidate_module._write_primary_context

    def stop_before_context(_user_data):
        raise OSError("simulated interruption before context write")

    monkeypatch.setattr(
        consolidate_module,
        "_write_primary_context",
        stop_before_context,
    )
    interrupted = consolidate_recovery_profiles(user_data, primary)
    monkeypatch.setattr(
        consolidate_module,
        "_write_primary_context",
        original_context,
    )
    assert interrupted.outcome == "blocked"
    journal = json.loads(
        (user_data / ".opensquilla-profile-consolidation.json").read_text(encoding="utf-8")
    )
    assert journal["phase"] == "recoveries_archived"
    archived_credential = (
        Path(journal["backup_path"]) / "recovery-profiles" / recovery_id / "desktop-credential.json"
    )
    _write_text(archived_credential, '{"provider":"openai","model":"tampered"}\n')

    resumed = consolidate_recovery_profiles(user_data, primary)

    assert resumed.outcome == "blocked"
    assert resumed.stable_code == "profile_consolidation_source_changed"


def test_credential_authority_is_captured_after_profile_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    recovery_id = str(uuid.uuid4())
    recovery = _recovery(
        user_data,
        recovery_id,
        config="selected = true\n",
        credential='{"provider":"openai","model":"before"}\n',
        memory="memory\n",
        conflict="recovery",
        extra_name="credential-lock.txt",
        session_key="agent:main:credential-lock",
    )
    updated = b'{"provider":"openai","model":"after"}\n'
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    original_acquire = consolidate_module.acquire_profile_locks

    @consolidate_module.contextlib.contextmanager
    def mutate_before_lock_yield(*args, **kwargs):
        with original_acquire(*args, **kwargs) as held:
            (recovery / "desktop-credential.json").write_bytes(updated)
            yield held

    monkeypatch.setattr(
        consolidate_module,
        "acquire_profile_locks",
        mutate_before_lock_yield,
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.configuration_source_credential_sha256 == hashlib.sha256(updated).hexdigest()
    assert result.configuration_source_credential_size == len(updated)


@pytest.mark.parametrize(
    "stray_name",
    ["desktop.ini", "Thumbs.db", ".localized", "sessions.db.avquarantine", "notes.txt"],
)
def test_consolidate_allows_stray_regular_files_in_recovery_root(
    tmp_path: Path,
    monkeypatch,
    stray_name: str,
) -> None:
    """Shell and antivirus metadata must never strand startup on a repair page.

    Windows Explorer writes ``desktop.ini``/``Thumbs.db`` into any folder the
    user browses, and the recovery container is a folder the app created.  A
    stray regular file is inert: it rides into the archive with the container.
    """

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="selected = 'recovery'\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:stray-file",
    )
    stray = user_data / "recovery-profiles" / stray_name
    stray.write_bytes(b"stray metadata")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.consumed_recovery_ids == (recovery_id,)
    assert result.backup_path is not None
    archived = result.backup_path / "recovery-profiles" / stray_name
    assert archived.read_bytes() == b"stray metadata"


def test_consolidate_with_only_stray_files_boots_without_blocking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A container holding no real profile is a noop, not a blocked startup."""

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    recovery_root = user_data / "recovery-profiles"
    recovery_root.mkdir()
    (recovery_root / "desktop.ini").write_bytes(b"[.ShellClassInfo]\n")
    (recovery_root / "Thumbs.db").write_bytes(b"thumbs")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "noop", result
    assert result.stable_code == "no_recovery_profiles"
    assert (recovery_root / "desktop.ini").exists()
    assert (recovery_root / "Thumbs.db").exists()


def test_consolidate_still_rejects_stray_directory_and_names_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An unrecognized directory could be profile-shaped; stay fail-closed.

    Allowing it would defer the failure to the post-publish archival move, a
    strictly more dangerous phase, so it is refused up front — but the
    diagnostic must name the entry so the user can remove it.
    """

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    recovery_root = user_data / "recovery-profiles"
    recovery_root.mkdir()
    stray = recovery_root / f"{uuid.uuid4()} - Copy"
    stray.mkdir()

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "profile_consolidation_unsafe_recovery_root"
    assert stray.is_dir()


def test_consolidate_still_rejects_stray_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    primary.mkdir(parents=True)
    recovery_root = user_data / "recovery-profiles"
    recovery_root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside\n", encoding="utf-8")
    stray = recovery_root / "notes.txt"
    try:
        stray.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "blocked"
    assert result.stable_code == "profile_consolidation_unsafe_recovery_root"
    assert stray.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_key_collision_keeps_session_id_so_artifacts_stay_downloadable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A recovered conversation must keep its generated files.

    Artifacts live under a directory derived from the session id and record that
    id inside ``meta.json``, which the download path validates. Renumbering a
    session whose id never collided would leave every artifact unreachable even
    though the conversation itself came back, so the production store is used as
    the oracle here rather than asserting on paths.
    """

    from opensquilla.artifacts import ArtifactNotFoundError, ArtifactStore

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("log_file_backup_count = 1\n", encoding="utf-8")
    # The primary owns the same deterministic session key, which is what forces a
    # key collision during the merge.
    _session_database(
        primary / "state" / "sessions.db",
        "agent:main:main",
        "primary-session",
        "primary",
    )

    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="log_file_backup_count = 2\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:main",
    )
    recovery_home = user_data / "recovery-profiles" / recovery_id / "opensquilla"
    recovered_session_id = f"session-{recovery_id}"

    source_store = ArtifactStore(recovery_home / "media")
    published = source_store.publish_bytes(
        b"generated chart bytes",
        session_id=recovered_session_id,
        session_key="agent:main:main",
        name="chart.bin",
        mime="application/octet-stream",
        source="code_exec",
    )
    # The file must be reachable in the source profile before consolidation.
    source_store.resolve_for_download(published.id, session_id=recovered_session_id)

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result
    assert result.consumed_recovery_ids == (recovery_id,)

    with contextlib.closing(sqlite3.connect(primary / "state" / "sessions.db")) as merged, merged:
        rows = dict(merged.execute("SELECT session_key, session_id FROM sessions").fetchall())
    # The colliding key was renamed; the identifier was left alone.
    assert rows["agent:main:main"] == "primary-session"
    recovered_key = next(key for key in rows if key.startswith("agent:main:main:recovered:"))
    assert rows[recovered_key] == recovered_session_id

    merged_store = ArtifactStore(primary / "media")
    ref, material = merged_store.resolve_for_download(
        published.id,
        session_id=recovered_session_id,
    )
    assert material.read_bytes() == b"generated chart bytes"
    assert ref.session_id == recovered_session_id

    # A different session must not be able to reach it.
    with pytest.raises(ArtifactNotFoundError):
        merged_store.resolve_for_download(published.id, session_id="primary-session")


def test_unreadable_recovery_source_defers_startup_and_still_converges(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A broken legacy source must cost the fan-in, never access to the product.

    One unreadable recovery database aborts the whole transaction, and the result
    has to tell Desktop that the primary profile is still usable so startup can
    continue silently. It then has to actually converge: the failed attempt leaves
    a pre-park journal, and a resume rebuilds its staging tree from the primary and
    refuses to continue if the primary changed. Because deferring startup lets the
    gateway write to the primary, that refusal would otherwise be permanent and
    the legacy conversations would never arrive.
    """

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("log_file_backup_count = 1\n", encoding="utf-8")
    _session_database(
        primary / "state" / "sessions.db",
        "agent:main:primary",
        "primary-session",
        "primary",
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="log_file_backup_count = 2\n",
        credential="{}\n",
        memory="memory\n",
        conflict="recovery",
        extra_name="recovery.txt",
        session_key="agent:main:recovered",
    )
    recovery_engine = importlib.import_module("opensquilla.recovery.engine")
    copy_source_file = recovery_engine._copy_source_file_no_follow
    source_db = (
        user_data / "recovery-profiles" / recovery_id / "opensquilla" / "state" / "sessions.db"
    )
    deny_source_read = True

    def fail_once_for_source_database(source: Path, destination: Path):
        if deny_source_read and source == source_db:
            # The journal is written before the real copy boundary, leaving the
            # exact pre-park transaction that the next launch must recover.
            assert (user_data / ".opensquilla-profile-consolidation.json").is_file()
            raise PermissionError(
                errno.EACCES,
                "simulated unreadable recovery source",
                str(source),
            )
        return copy_source_file(source, destination)

    monkeypatch.setattr(
        recovery_engine,
        "_copy_source_file_no_follow",
        fail_once_for_source_database,
    )
    blocked = consolidate_recovery_profiles(user_data, primary)

    assert blocked.outcome == "blocked", blocked
    # The primary is healthy, so Desktop may start against it and retry later.
    assert blocked.primary_home_intact is True
    assert primary.is_dir()
    assert (primary / "config.toml").read_text(encoding="utf-8") == "log_file_backup_count = 1\n"

    # Deferred startup means the gateway runs and writes into the primary, which
    # is precisely what invalidates the prepared transaction's baseline.
    (primary / "state" / "gateway.log").write_text("started after deferral\n", encoding="utf-8")
    deny_source_read = False

    resumed = consolidate_recovery_profiles(user_data, primary)

    assert resumed.outcome == "consolidated", resumed
    assert resumed.consumed_recovery_ids == (recovery_id,)
    assert not (user_data / "recovery-profiles").exists()
    with contextlib.closing(sqlite3.connect(primary / "state" / "sessions.db")) as merged, merged:
        keys = {row[0] for row in merged.execute("SELECT session_key FROM sessions").fetchall()}
    assert "agent:main:primary" in keys
    assert "agent:main:recovered" in keys


def test_parked_primary_keeps_blocking_startup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """While the primary is parked mid-commit there is nothing to start.

    Reporting the primary as usable here would let the profile inspector treat the
    absent home as a fresh install and seed an empty one, presenting an empty
    application while the real data sits in the transaction backup.
    """

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    (primary / "workspace").mkdir(parents=True)
    (primary / "config.toml").write_text("log_file_backup_count = 1\n", encoding="utf-8")

    from opensquilla.recovery.consolidate import _primary_home_survives_failure

    assert _primary_home_survives_failure(user_data, primary) is True

    # An absent primary can never be started.
    shutil.move(str(primary), str(user_data / "parked-primary"))
    assert _primary_home_survives_failure(user_data, primary) is False

    # An empty shell is not a profile worth booting either.
    primary.mkdir(parents=True)
    assert _primary_home_survives_failure(user_data, primary) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_existing_extended_length_external_state_merges_sessions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    user_data = tmp_path / "user-data"
    primary = user_data / "opensquilla"
    workspace = tmp_path / "workspace"
    media = tmp_path / "media"
    workspace.mkdir()
    media.mkdir()
    state_prefix = tmp_path / "external-state"
    state_padding = 280 - len(str(state_prefix)) - (3 * len(os.sep)) - len("state")
    first_padding = state_padding // 2
    second_padding = state_padding - first_padding
    assert 1 <= first_padding <= 200
    assert 1 <= second_padding <= 200
    state = state_prefix / ("s" * first_padding) / ("t" * second_padding) / "state"
    os.makedirs(_native_io_path(state))
    assert len(str(state)) == 280
    _external_primary_config(
        primary,
        workspace=workspace,
        state=state,
        media=media,
    )
    target = state / "sessions.db"
    assert len(str(target)) > 260
    _session_database(
        Path(_native_io_path(target)),
        "agent:main:external-primary-long",
        "primary-session",
        "primary",
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        user_data,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="recovery memory\n",
        conflict="recovery",
        extra_name="external-long.txt",
        session_key="agent:main:external-recovery-long",
    )

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result.errors
    assert result.consumed_recovery_ids == (recovery_id,)
    assert result.backup_path is not None
    assert not str(result.backup_path).startswith("\\\\?\\")
    assert _is_file(state / "gateway.pid.lock")
    with contextlib.closing(sqlite3.connect(_native_io_path(target))) as connection:
        keys = {row[0] for row in connection.execute("SELECT session_key FROM sessions")}
    assert keys == {
        "agent:main:external-primary-long",
        "agent:main:external-recovery-long",
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_extended_length_user_data_root_consolidates_sessions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "locks"))
    seed = tmp_path / "seed-user-data"
    seed_primary = seed / "opensquilla"
    (seed_primary / "workspace").mkdir(parents=True)
    (seed_primary / "config.toml").write_text("primary = true\n", encoding="utf-8")
    _session_database(
        seed_primary / "state" / "sessions.db",
        "agent:main:long-root-primary",
        "primary-session",
        "primary",
    )
    recovery_id = str(uuid.uuid4())
    _recovery(
        seed,
        recovery_id,
        config="recovery = true\n",
        credential="{}\n",
        memory="recovery memory\n",
        conflict="recovery",
        extra_name="long-root.txt",
        session_key="agent:main:long-root-recovery",
    )
    prefix = tmp_path / "long-user-data"
    padding = 270 - len(str(prefix)) - (2 * len(os.sep))
    first_padding = padding // 2
    second_padding = padding - first_padding
    assert 1 <= first_padding <= 200
    assert 1 <= second_padding <= 200
    user_data = prefix / ("u" * first_padding) / ("v" * second_padding)
    shutil.copytree(_native_io_path(seed), _native_io_path(user_data))
    assert len(str(user_data)) == 270
    primary = user_data / "opensquilla"

    result = consolidate_recovery_profiles(user_data, primary)

    assert result.outcome == "consolidated", result.errors
    assert result.consumed_recovery_ids == (recovery_id,)
    assert result.backup_path is not None
    assert result.receipt_path is not None
    assert not str(result.backup_path).startswith("\\\\?\\")
    assert not str(result.receipt_path).startswith("\\\\?\\")
    assert not os.path.exists(_native_io_path(user_data / "recovery-profiles"))
    with contextlib.closing(
        sqlite3.connect(_native_io_path(primary / "state" / "sessions.db"))
    ) as connection:
        keys = {row[0] for row in connection.execute("SELECT session_key FROM sessions")}
    assert keys == {
        "agent:main:long-root-primary",
        "agent:main:long-root-recovery",
    }


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path spelling")
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (r"..\relative-target", r"..\relative-target"),
        (r"\\?\C:\local\target", r"C:\local\target"),
        (r"\\?\UNC\server\share\target", r"\\server\share\target"),
        (
            r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\target",
            r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\target",
        ),
        (
            r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\target",
            r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\target",
        ),
    ],
)
def test_link_assertion_target_only_removes_drive_and_unc_prefixes(
    target: str,
    expected: str,
) -> None:
    assert _link_assertion_target(target) == expected


@pytest.mark.skipif(os.name != "nt", reason="requires Windows reparse semantics")
def test_copy_leaf_preserves_verbatim_symlink_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source-link"
    source.write_bytes(b"synthetic source")
    destination = tmp_path / "destination" / "copied-link"
    target = "\\\\?\\C:\\verbatim\\" + ("x" * 270) + "\\tail. "
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    real_lstat = consolidate_module.os.lstat
    native_source = os.path.normcase(os.path.normpath(str(_native_io_path(source))))

    def link_lstat(path):
        if os.path.normcase(os.path.normpath(str(path))) == native_source:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK,
                st_file_attributes=0x400,
                st_reparse_tag=0xA000000C,
            )
        return real_lstat(path)

    created: list[tuple[str, object, bool]] = []
    monkeypatch.setattr(consolidate_module.os, "lstat", link_lstat)
    monkeypatch.setattr(consolidate_module.os, "readlink", lambda _path: target)
    monkeypatch.setattr(
        consolidate_module.os,
        "symlink",
        lambda link_target, path, *, target_is_directory: created.append(
            (link_target, path, target_is_directory)
        ),
    )

    consolidate_module._copy_leaf(source, destination)

    assert created == [(target, _native_io_path(destination), False)]


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_durable_junction_resume_ignores_crashed_temporary_and_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    source = tmp_path / "source-junction"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(source), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation is unavailable: {completed.stderr}")
    destination = tmp_path / "external" / "workspace-link"
    destination.parent.mkdir()
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    real_copy = consolidate_module._copy_windows_mount_point_no_follow
    attempted_temporaries: list[Path] = []

    def crash_once(source_path, temporary, *, publish_destination=None):
        temporary_path = Path(temporary)
        attempted_temporaries.append(temporary_path)
        if len(attempted_temporaries) == 1:
            temporary_path.mkdir()
            raise RuntimeError("simulated process exit between create and SET")
        return real_copy(
            source_path,
            temporary_path,
            publish_destination=publish_destination,
        )

    monkeypatch.setattr(
        consolidate_module,
        "_copy_windows_mount_point_no_follow",
        crash_once,
    )

    with pytest.raises(RuntimeError, match="simulated process exit"):
        consolidate_module._atomic_copy_windows_mount_point(
            source,
            destination,
            transaction_id="transaction-a",
        )

    orphan = attempted_temporaries[0]
    assert orphan.is_dir()
    assert not os.path.isjunction(orphan)
    assert not os.path.lexists(destination)

    consolidate_module._copy_leaf(
        source,
        destination,
        durable=True,
        transaction_id="transaction-a",
    )

    assert len(attempted_temporaries) == 2
    assert attempted_temporaries[1] != orphan
    assert orphan.is_dir()
    assert os.path.isjunction(destination)
    assert os.path.samefile(destination, target)


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_durable_junction_is_idempotent_when_destination_already_matches(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    source = tmp_path / "source-junction"
    destination_parent = tmp_path / "external"
    destination_parent.mkdir()
    destination = destination_parent / "workspace-link"
    for link in (source, destination):
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            pytest.skip(f"junction creation is unavailable: {completed.stderr}")
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")

    consolidate_module._copy_leaf(
        source,
        destination,
        durable=True,
        transaction_id="transaction-b",
    )

    assert os.path.isjunction(destination)
    assert os.path.samefile(destination, target)
    assert not [
        entry.name
        for entry in os.scandir(destination_parent)
        if entry.name.startswith(".opensquilla-junction-")
    ]


@pytest.mark.skipif(os.name != "nt", reason="requires Windows metadata semantics")
def test_clone_primary_keeps_windows_directory_metadata_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "primary"
    (primary / "workspace").mkdir(parents=True)
    (primary / "workspace" / "MEMORY.md").write_text("preserved\n", encoding="utf-8")
    staging = tmp_path / "staging"
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    calls: list[tuple[object, object, bool]] = []

    def fail_windows_directory_metadata(source, destination, *, follow_symlinks):
        calls.append((source, destination, follow_symlinks))
        error = OSError(errno.EACCES, "injected Windows directory metadata failure")
        error.winerror = 5
        raise error

    monkeypatch.setattr(consolidate_module.shutil, "copystat", fail_windows_directory_metadata)

    consolidate_module._clone_primary(primary, staging)

    assert calls
    assert (staging / "workspace" / "MEMORY.md").read_text(encoding="utf-8") == "preserved\n"


def test_clone_primary_propagates_non_win32_directory_metadata_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / "config.toml").write_text("port = 18789\n", encoding="utf-8")
    staging = tmp_path / "staging"
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")

    def fail_directory_metadata(*_args, **_kwargs):
        raise OSError(errno.EIO, "injected non-Win32 metadata failure")

    monkeypatch.setattr(consolidate_module.shutil, "copystat", fail_directory_metadata)

    with pytest.raises(OSError, match="non-Win32 metadata failure"):
        consolidate_module._clone_primary(primary, staging)

    assert primary.is_dir()
    assert (primary / "config.toml").read_text(encoding="utf-8") == "port = 18789\n"


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_clone_primary_preserves_nested_junction_without_symlink_privilege(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "primary"
    junction_parent = primary / "workspace" / "node_modules"
    junction_parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside\n", encoding="utf-8")
    source_junction = junction_parent / "cache"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(source_junction), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation is unavailable: {completed.stderr}")
    source_target = os.readlink(source_junction)
    staging = tmp_path / "staging"
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")

    def reject_symlink(*_args, **_kwargs) -> None:
        raise AssertionError("junction cloning must not call os.symlink")

    monkeypatch.setattr(consolidate_module.os, "symlink", reject_symlink)

    consolidate_module._clone_primary(primary, staging)

    copied = staging / "workspace" / "node_modules" / "cache"
    assert os.path.isjunction(source_junction)
    assert os.path.isjunction(copied)
    assert source_junction.lstat().st_reparse_tag == copied.lstat().st_reparse_tag == 0xA0000003
    assert os.readlink(copied) == source_target
    assert os.path.samefile(copied, outside)
    assert sentinel.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows reparse semantics")
def test_copy_leaf_rejects_unknown_windows_reparse_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination" / "copied"
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    real_lstat = os.lstat
    native_source = os.path.normcase(os.path.normpath(str(_native_io_path(source))))

    def tagged_lstat(path):
        if os.path.normcase(os.path.normpath(str(path))) == native_source:
            current = real_lstat(path)
            return SimpleNamespace(
                st_mode=current.st_mode,
                st_file_attributes=0x410,
                st_reparse_tag=0xA000001D,
            )
        return real_lstat(path)

    monkeypatch.setattr(consolidate_module.os, "lstat", tagged_lstat)

    with pytest.raises(UnsafePathError, match="unsupported Windows recovery reparse tag"):
        consolidate_module._copy_leaf(source, destination)

    assert not os.path.lexists(_native_io_path(destination))


@pytest.mark.skipif(os.name != "nt", reason="requires Windows reparse semantics")
def test_same_leaf_distinguishes_windows_junction_from_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    source = Path("C:/synthetic/source")
    destination = Path("C:/synthetic/destination")
    values = {
        os.path.normcase(str(_native_io_path(source))): SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=0x410,
            st_reparse_tag=0xA0000003,
        ),
        os.path.normcase(str(_native_io_path(destination))): SimpleNamespace(
            st_mode=stat.S_IFLNK,
            st_file_attributes=0x410,
            st_reparse_tag=0xA000000C,
        ),
    }

    monkeypatch.setattr(
        consolidate_module.os,
        "lstat",
        lambda path: values[os.path.normcase(str(path))],
    )
    monkeypatch.setattr(
        consolidate_module.os,
        "readlink",
        lambda _path: (_ for _ in ()).throw(AssertionError("different tags need no readlink")),
    )

    assert not consolidate_module._same_leaf(source, destination)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows reparse semantics")
def test_same_leaf_keeps_verbatim_and_non_verbatim_targets_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consolidate_module = importlib.import_module("opensquilla.recovery.consolidate")
    source = Path("C:/synthetic/source")
    destination = Path("C:/synthetic/destination")
    values = {
        os.path.normcase(str(_native_io_path(source))): SimpleNamespace(
            st_mode=stat.S_IFLNK,
            st_file_attributes=0x400,
            st_reparse_tag=0xA000000C,
        ),
        os.path.normcase(str(_native_io_path(destination))): SimpleNamespace(
            st_mode=stat.S_IFLNK,
            st_file_attributes=0x400,
            st_reparse_tag=0xA000000C,
        ),
    }
    targets = {
        os.path.normcase(str(_native_io_path(source))): r"\\?\C:\root\tail. ",
        os.path.normcase(str(_native_io_path(destination))): r"C:\root\tail. ",
    }

    monkeypatch.setattr(
        consolidate_module.os,
        "lstat",
        lambda path: values[os.path.normcase(str(path))],
    )
    monkeypatch.setattr(
        consolidate_module.os,
        "readlink",
        lambda path: targets[os.path.normcase(str(path))],
    )

    assert not consolidate_module._same_leaf(source, destination)
