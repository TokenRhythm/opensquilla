from __future__ import annotations

import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path

import pytest

from opensquilla.recovery import (
    AtomicStateUnknownError,
    InvalidWorkspaceError,
    RecoveryRequiredError,
    WorkspaceOverrideError,
    choose_workspace,
    guard_desktop_profile,
    inspect_profile,
    reconcile_profile,
)
from opensquilla.recovery.atomic import _native_io_path
from opensquilla.recovery.config_patch import (
    state_override,
    workspace_override,
)


def _workspace(path: Path, marker: str = "synthetic") -> Path:
    path.mkdir(parents=True)
    (path / "SOUL.md").write_text(f"{marker}\n", encoding="utf-8")
    return path


def _desktop_config(home: Path, *, workspace: Path | None = None, extra: str = "") -> None:
    lines = [f"state_dir = {json.dumps(str(home / 'state'))}"]
    if workspace is not None:
        lines.append(f"workspace_dir = {json.dumps(str(workspace))}")
    if extra:
        lines.append(extra.rstrip("\n"))
    home.mkdir(parents=True, exist_ok=True)
    (home / "state").mkdir(exist_ok=True)
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_offline_cli_ignores_cwd_dotenv_but_reads_profile_override(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "cwd"
    home = tmp_path / "profile"
    cwd.mkdir()
    external = _workspace(tmp_path / "profile-workspace")
    _workspace(home / "workspace")
    _desktop_config(home)
    (cwd / ".env").write_text(
        f"OPENSQUILLA_GATEWAY_WORKSPACE_DIR={tmp_path / 'wrong-cwd'}\n",
        encoding="utf-8",
    )
    (home / ".env").write_text(
        f"OPENSQUILLA_GATEWAY_WORKSPACE_DIR={external}\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("OPENSQUILLA_GATEWAY_WORKSPACE_DIR", None)
    environment["OPENSQUILLA_STATE_DIR"] = str(home)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "opensquilla.cli.main",
            "recovery",
            "inspect",
            "--home",
            str(home),
            "--json",
        ],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["effective_workspace"] == str(external)
    assert payload["stable_code"] == "workspace_env_override"


@pytest.mark.parametrize(
    ("profile_kind", "outcome"),
    [("desktop-primary", "ready"), ("desktop-recovery", "recovery_profile")],
)
def test_empty_profile_is_safe_to_initialize(
    tmp_path: Path,
    profile_kind: str,
    outcome: str,
) -> None:
    home = tmp_path / "not-created"

    report = inspect_profile(home, profile_kind=profile_kind)

    assert report.outcome == outcome
    assert report.stable_code.startswith("fresh_")
    assert not home.exists(), "read-only inspection must not create a fresh home"


def test_existing_empty_profile_is_safe_to_initialize(tmp_path: Path) -> None:
    home = tmp_path / "empty-profile"
    home.mkdir()

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "ready"
    assert report.stable_code == "fresh_profile"
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("entry_kind", ["file", "directory"])
def test_unknown_only_profile_is_never_seeded_as_fresh(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    home = tmp_path / "unknown-profile"
    home.mkdir()
    unknown = home / "unknown-layout"
    if entry_kind == "file":
        unknown.write_text("synthetic unknown profile evidence\n", encoding="utf-8")
    else:
        unknown.mkdir()
        (unknown / "USER.md").write_text("synthetic preserved identity\n", encoding="utf-8")
    before = sorted(path.relative_to(home) for path in home.rglob("*"))

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "unknown_layout"
    assert sorted(path.relative_to(home) for path in home.rglob("*")) == before
    assert not (home / "workspace").exists()
    assert not (home / "state").exists()


def test_unknown_profile_symlink_is_not_followed_or_seeded(tmp_path: Path) -> None:
    home = tmp_path / "unknown-profile"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    sentinel = outside / "USER.md"
    sentinel.write_text("synthetic preserved identity\n", encoding="utf-8")
    linked = home / "unknown-layout"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "unknown_layout"
    assert linked.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "synthetic preserved identity\n"
    assert not (home / "workspace").exists()
    assert not (home / "state").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires a real Windows junction")
def test_unknown_profile_junction_is_not_followed_or_seeded(tmp_path: Path) -> None:
    home = tmp_path / "unknown-profile"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    sentinel = outside / "USER.md"
    sentinel.write_text("synthetic preserved identity\n", encoding="utf-8")
    junction = home / "unknown-layout"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation is unavailable: {completed.stderr}")

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "unknown_layout"
    assert sentinel.read_text(encoding="utf-8") == "synthetic preserved identity\n"
    assert not (home / "workspace").exists()
    assert not (home / "state").exists()


@pytest.mark.parametrize(
    ("override_name", "stable_code"),
    [
        ("OPENSQUILLA_GATEWAY_WORKSPACE_DIR", "effective_state_missing"),
        ("OPENSQUILLA_GATEWAY_STATE_DIR", "effective_state_missing"),
    ],
)
def test_empty_primary_with_missing_environment_override_is_not_fresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    override_name: str,
    stable_code: str,
) -> None:
    home = tmp_path / "not-created"
    missing = tmp_path / "missing-external"
    monkeypatch.setenv(override_name, str(missing))

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == stable_code
    assert not home.exists()
    assert not missing.exists()


@pytest.mark.parametrize(
    ("external_role", "stable_code"),
    [
        ("workspace", "recovery_profile_external_workspace"),
        ("state", "recovery_profile_external_state"),
    ],
)
def test_recovery_profile_rejects_external_primary_data_roots(
    tmp_path: Path,
    external_role: str,
    stable_code: str,
) -> None:
    recovery_home = tmp_path / "recovery-profiles" / str(uuid.uuid4()) / "opensquilla"
    primary_home = tmp_path / "opensquilla"
    primary_workspace = _workspace(primary_home / "workspace", "primary identity")
    primary_state = primary_home / "state"
    primary_state.mkdir()
    recovery_workspace = _workspace(recovery_home / "workspace", "recovery identity")
    recovery_state = recovery_home / "state"
    recovery_state.mkdir()
    workspace = primary_workspace if external_role == "workspace" else recovery_workspace
    state = primary_state if external_role == "state" else recovery_state
    (recovery_home / "config.toml").write_text(
        f"state_dir = {json.dumps(str(state))}\n"
        f"workspace_dir = {json.dumps(str(workspace))}\n",
        encoding="utf-8",
    )

    report = inspect_profile(recovery_home, profile_kind="desktop-recovery")

    assert report.outcome == "recovery_required"
    assert report.stable_code == stable_code
    assert report.effective_workspace is None
    assert "continue-recovery-profile" not in report.allowed_actions
    assert "create-recovery-profile" not in report.allowed_actions
    assert "retry-primary-profile" not in report.allowed_actions
    assert "show-backups" in report.allowed_actions


def test_healthy_recovery_profile_cannot_be_repointed_to_external_workspace(
    tmp_path: Path,
) -> None:
    recovery_home = tmp_path / "recovery-profiles" / str(uuid.uuid4()) / "opensquilla"
    canonical_workspace = _workspace(recovery_home / "workspace", "recovery identity")
    (recovery_home / "state").mkdir()
    _desktop_config(recovery_home, workspace=canonical_workspace)
    external_workspace = _workspace(tmp_path / "external-workspace", "external identity")
    config_path = recovery_home / "config.toml"
    config_before = config_path.read_bytes()

    before = inspect_profile(recovery_home, profile_kind="desktop-recovery")

    assert before.outcome == "recovery_profile"
    assert before.stable_code == "canonical_workspace"
    assert "choose-workspace" not in before.allowed_actions
    with pytest.raises(InvalidWorkspaceError, match="recovery profile"):
        choose_workspace(
            recovery_home,
            transaction_id=before.transaction_id,
            expected_revision=before.revision,
            workspace=external_workspace,
            profile_kind="desktop-recovery",
        )

    assert config_path.read_bytes() == config_before
    after = inspect_profile(recovery_home, profile_kind="desktop-recovery")
    assert after.outcome == "recovery_profile"
    assert after.effective_workspace == canonical_workspace


def test_empty_recovery_profile_rejects_ambient_primary_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recovery_home = tmp_path / "recovery-profiles" / str(uuid.uuid4()) / "opensquilla"
    primary_workspace = _workspace(tmp_path / "opensquilla" / "workspace")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_WORKSPACE_DIR", str(primary_workspace))

    report = inspect_profile(recovery_home, profile_kind="desktop-recovery")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "recovery_profile_external_workspace"
    assert not recovery_home.exists()


def test_recovery_profile_canonical_symlink_cannot_escape_to_primary(
    tmp_path: Path,
) -> None:
    recovery_home = tmp_path / "recovery-profiles" / str(uuid.uuid4()) / "opensquilla"
    recovery_home.mkdir(parents=True)
    primary_workspace = _workspace(tmp_path / "opensquilla" / "workspace")
    try:
        (recovery_home / "workspace").symlink_to(primary_workspace, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    (recovery_home / "state").mkdir()
    (recovery_home / "config.toml").write_text(
        f"state_dir = {json.dumps(str(recovery_home / 'state'))}\n"
        f"workspace_dir = {json.dumps(str(recovery_home / 'workspace'))}\n",
        encoding="utf-8",
    )

    report = inspect_profile(recovery_home, profile_kind="desktop-recovery")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "recovery_profile_unsafe_workspace"
    assert (primary_workspace / "SOUL.md").read_text(encoding="utf-8") == "synthetic\n"


def test_pinned_legacy_workspace_stays_in_place_and_is_attention(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    legacy = _workspace(home / "state" / "workspace", "legacy")
    _desktop_config(home, workspace=legacy)

    before = inspect_profile(home)
    after = reconcile_profile(home)

    assert before.outcome == after.outcome == "attention"
    assert before.stable_code == after.stable_code == "legacy_workspace_pinned"
    assert after.effective_workspace == legacy
    assert legacy.is_dir()
    assert not (home / "workspace").exists()


def test_dual_workspace_preserves_configured_current_path(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    canonical = _workspace(home / "workspace", "canonical changed")
    legacy = _workspace(home / "state" / "workspace", "legacy changed")
    _desktop_config(home, workspace=legacy)

    report = reconcile_profile(home)

    assert report.outcome == "attention"
    assert report.stable_code == "workspace_conflict"
    assert report.effective_workspace == legacy
    assert (canonical / "SOUL.md").read_text(encoding="utf-8") == "canonical changed\n"
    assert (legacy / "SOUL.md").read_text(encoding="utf-8") == "legacy changed\n"


def test_clean_proven_legacy_workspace_moves_only_during_reconcile(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    legacy = _workspace(home / "state" / "workspace", "legacy")
    _desktop_config(home)
    legacy_env = home / "state" / ".env"
    legacy_env.write_text("SYNTHETIC=value\n", encoding="utf-8")

    inspected = inspect_profile(home)

    assert inspected.outcome == "recovery_required"
    assert inspected.stable_code == "legacy_workspace_reconcile_available"
    assert legacy.is_dir()
    assert not (home / "workspace").exists()

    reconciled = reconcile_profile(home)

    assert reconciled.outcome == "ready"
    assert reconciled.effective_workspace == home / "workspace"
    assert not legacy.exists()
    assert (home / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "legacy\n"
    assert not legacy_env.exists()
    assert (home / ".env").read_text(encoding="utf-8") == "SYNTHETIC=value\n"
    assert (home / "state" / "gateway.pid.lock").is_file()


def test_primary_reconcile_finalizes_v2_marker_without_touching_config_or_database(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    database = home / "state" / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE synthetic_sessions (id TEXT PRIMARY KEY)")
    config_before = (home / "config.toml").read_bytes()
    database_before = database.read_bytes()

    report = reconcile_profile(home, profile_kind="desktop-primary")

    marker = home / "desktop-layout-v2.json"
    assert report.outcome == "ready"
    assert marker.is_file()
    if os.name != "nt":
        assert marker.stat().st_mode & 0o777 == 0o600
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["moved"] == []
    assert (home / "config.toml").read_bytes() == config_before
    assert database.read_bytes() == database_before


def test_recovery_or_required_profile_never_writes_primary_compatibility_marker(
    tmp_path: Path,
) -> None:
    recovery_home = tmp_path / "recovery"
    _workspace(recovery_home / "workspace")
    _desktop_config(recovery_home)
    recovery = reconcile_profile(recovery_home, profile_kind="desktop-recovery")
    assert recovery.outcome == "recovery_profile"
    assert not (recovery_home / "desktop-layout-v2.json").exists()

    unsafe_home = tmp_path / "unsafe"
    _desktop_config(unsafe_home, workspace=tmp_path / "missing")
    required = reconcile_profile(unsafe_home, profile_kind="desktop-primary")
    assert required.outcome == "recovery_required"
    assert not (unsafe_home / "desktop-layout-v2.json").exists()


def test_marker_is_not_written_while_any_rc3_role_conflicts(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    (home / "skills").mkdir()
    (home / "state" / "skills").mkdir(parents=True)

    report = reconcile_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "attention"
    assert not (home / "desktop-layout-v2.json").exists()


def test_explicit_legacy_workspace_pin_is_safe_for_marker_finalization(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    legacy = _workspace(home / "state" / "workspace")
    _desktop_config(home, workspace=legacy)

    report = reconcile_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "attention"
    assert report.stable_code == "legacy_workspace_pinned"
    assert (home / "desktop-layout-v2.json").is_file()
    assert legacy.is_dir()


def test_unsafe_existing_marker_is_attention_and_never_overwritten(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    outside = tmp_path / "outside-marker.json"
    outside.write_text("do not touch\n", encoding="utf-8")
    try:
        (home / "desktop-layout-v2.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    report = reconcile_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "attention"
    assert report.stable_code == "layout_marker_unsafe"
    assert (home / "desktop-layout-v2.json").is_symlink()
    assert outside.read_text(encoding="utf-8") == "do not touch\n"


def test_future_layout_marker_schema_is_attention_and_never_reinterpreted(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    marker = home / "desktop-layout-v2.json"
    marker_bytes = (
        b'{"schema_version":3,"migratedAt":"2099-01-01T00:00:00Z",'
        b'"moved":[],"future":"preserve"}\n'
    )
    marker.write_bytes(marker_bytes)

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "attention"
    assert report.stable_code == "layout_marker_unsafe"
    assert marker.read_bytes() == marker_bytes


def test_unproven_legacy_directory_is_never_guessed_or_moved(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    legacy = home / "state" / "workspace"
    legacy.mkdir(parents=True)
    (legacy / "arbitrary.bin").write_bytes(b"synthetic")
    _desktop_config(home)

    report = reconcile_profile(home)

    assert report.outcome == "recovery_required"
    assert report.stable_code == "unknown_legacy_layout"
    assert legacy.is_dir()
    assert not (home / "workspace").exists()


def test_canonical_profile_user_dotenv_under_state_is_not_guessed_as_legacy(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    legacy_env = home / "state" / ".env"
    legacy_env.parent.mkdir(exist_ok=True)
    legacy_env.write_text("SYNTHETIC_SECRET=not-a-real-secret\n", encoding="utf-8")

    report = reconcile_profile(home)

    assert report.outcome == "attention"
    assert report.stable_code == "legacy_layout_unsafe"
    assert legacy_env.is_file()
    assert not (home / ".env").exists()


def test_raw_native_move_oserror_returns_recovery_protocol_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.engine as recovery_engine

    home = tmp_path / "opensquilla"
    canonical = _workspace(home / "workspace")
    _workspace(home / "state" / "workspace", "legacy conflict")
    _desktop_config(home, workspace=canonical)
    legacy_env = home / "state" / ".env"
    legacy_env.parent.mkdir(exist_ok=True)
    legacy_env.write_text("SYNTHETIC=value\n", encoding="utf-8")

    def fail_move(_source: Path, _destination: Path) -> None:
        raise OSError("synthetic native failure")

    monkeypatch.setattr(recovery_engine, "native_move_no_replace", fail_move)

    report = reconcile_profile(home)

    assert report.outcome == "attention"
    assert report.stable_code == "layout_reconcile_deferred"
    assert legacy_env.is_file()
    assert not (home / ".env").exists()


def test_post_move_verification_failure_never_becomes_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.engine as recovery_engine

    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _workspace(home / "state" / "workspace", "legacy conflict")
    _desktop_config(home)
    legacy_env = home / "state" / ".env"
    legacy_env.parent.mkdir(exist_ok=True)
    legacy_env.write_text("SYNTHETIC=value\n", encoding="utf-8")

    def move_then_fail(source: Path, destination: Path) -> None:
        source.rename(destination)
        raise AtomicStateUnknownError("synthetic post-move verification failure")

    monkeypatch.setattr(recovery_engine, "native_move_no_replace", move_then_fail)

    report = reconcile_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "atomic_state_unknown"
    assert not legacy_env.exists()
    assert (home / ".env").is_file()
    assert not (home / "desktop-layout-v2.json").exists()


def test_skills_conflict_is_attention_and_never_moved_or_merged(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    legacy = home / "state" / "skills"
    current = home / "skills"
    legacy.mkdir(parents=True)
    current.mkdir()
    (legacy / "legacy.txt").write_text("legacy\n", encoding="utf-8")
    (current / "current.txt").write_text("current\n", encoding="utf-8")

    report = reconcile_profile(home)

    assert report.outcome == "attention"
    assert report.stable_code == "legacy_layout_conflict"
    assert (legacy / "legacy.txt").read_text(encoding="utf-8") == "legacy\n"
    assert (current / "current.txt").read_text(encoding="utf-8") == "current\n"
    assert not (current / "legacy.txt").exists()


def test_nested_state_conflict_is_attention_and_never_moved(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    nested = home / "state" / "state" / "approvals.json"
    current = home / "state" / "approvals.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("legacy\n", encoding="utf-8")
    current.write_text("current\n", encoding="utf-8")

    report = reconcile_profile(home)

    assert report.outcome == "attention"
    assert report.stable_code == "legacy_layout_unsafe"
    assert nested.read_text(encoding="utf-8") == "legacy\n"
    assert current.read_text(encoding="utf-8") == "current\n"


def test_ancillary_legacy_symlink_is_attention_not_primary_recovery(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    (home / "state").mkdir(exist_ok=True)
    outside = tmp_path / "outside-skills"
    outside.mkdir()
    try:
        (home / "state" / "skills").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    report = reconcile_profile(home)

    assert report.outcome == "attention"
    assert report.stable_code == "legacy_layout_unsafe"
    assert (home / "state" / "skills").is_symlink()
    assert not (home / "skills").exists()


def test_existing_profile_with_missing_effective_workspace_requires_recovery(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    _desktop_config(home, workspace=tmp_path / "missing-external")

    report = inspect_profile(home)

    assert report.outcome == "recovery_required"
    assert report.stable_code == "effective_workspace_missing"
    assert "choose-workspace" in report.allowed_actions


def test_missing_external_state_blocks_before_a_new_chat_database_can_be_created(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    workspace = _workspace(home / "workspace")
    missing_state = tmp_path / "detached-state"
    _desktop_config(home, workspace=workspace)
    (home / "config.toml").write_text(
        f"state_dir = {json.dumps(str(missing_state))}\n"
        f"workspace_dir = {json.dumps(str(workspace))}\n",
        encoding="utf-8",
    )

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "effective_state_missing"
    assert not missing_state.exists(), "inspection must never seed a replacement state root"
    state = next(candidate for candidate in report.candidates if candidate.kind == "state")
    assert state.path == missing_state
    assert state.exists is False


def test_profile_dotenv_state_override_is_the_authoritative_chat_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    external_state = tmp_path / "external-state"
    external_state.mkdir()
    (home / ".env").write_text(
        f"OPENSQUILLA_GATEWAY_STATE_DIR={external_state}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_STATE_DIR", raising=False)

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "ready"
    state = next(candidate for candidate in report.candidates if candidate.kind == "state")
    assert state.path == external_state
    assert state.valid is True


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_extended_length_external_state_is_ready_for_gateway_startup(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    workspace = _workspace(home / "workspace")
    external_state = tmp_path / "external-state"
    index = 0
    while len(str(external_state)) < 275:
        external_state /= f"state-segment-{index:02d}-0123456789"
        index += 1
    os.makedirs(_native_io_path(external_state))
    _desktop_config(home, workspace=workspace)
    (home / "config.toml").write_text(
        f"state_dir = {json.dumps(str(external_state))}\n"
        f"workspace_dir = {json.dumps(str(workspace))}\n",
        encoding="utf-8",
    )
    database = external_state / "sessions.db"
    connection = sqlite3.connect(_native_io_path(database))
    try:
        connection.execute("CREATE TABLE user_data (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    assert os.path.isdir(_native_io_path(external_state))

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "ready", report
    state = next(candidate for candidate in report.candidates if candidate.kind == "state")
    assert state.path == external_state
    assert state.exists is True
    assert state.valid is True


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_extended_length_temp_root_does_not_invalidate_state_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    workspace = _workspace(home / "workspace")
    _desktop_config(home, workspace=workspace)
    database = home / "state" / "sessions.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE user_data (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    temp_root = tmp_path / "long-temp-root"
    index = 0
    while len(str(temp_root)) < 275:
        temp_root /= f"temp-segment-{index:02d}-0123456789"
        index += 1
    os.makedirs(_native_io_path(temp_root))
    assert len(str(temp_root)) > 260
    monkeypatch.setattr(tempfile, "tempdir", _native_io_path(temp_root))

    try:
        report = inspect_profile(home, profile_kind="desktop-primary")

        assert report.outcome == "ready", report
        assert report.stable_code != "state_database_invalid"
    finally:
        shutil.rmtree(_native_io_path(temp_root))


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_extended_length_home_dotenv_routes_remain_authoritative(
    tmp_path: Path,
) -> None:
    external_workspace = _workspace(tmp_path / "external-workspace")
    external_state = tmp_path / "external-state"
    external_state.mkdir()
    home = tmp_path / "long-home"
    index = 0
    while len(str(home)) < 275:
        home /= f"home-segment-{index:02d}-0123456789"
        index += 1
    os.makedirs(_native_io_path(home))
    dotenv = (
        f"OPENSQUILLA_GATEWAY_WORKSPACE_DIR={external_workspace}\n"
        f"OPENSQUILLA_GATEWAY_STATE_DIR={external_state}\n"
    )
    with open(_native_io_path(home / ".env"), "w", encoding="utf-8") as handle:
        handle.write(dotenv)
    with open(_native_io_path(home / "config.toml"), "w", encoding="utf-8") as handle:
        handle.write(
            f"state_dir = {json.dumps(str(external_state))}\n"
            f"workspace_dir = {json.dumps(str(external_workspace))}\n"
        )
    assert os.path.lexists(_native_io_path(home / ".env"))

    assert workspace_override(
        home,
        include_process_environment=False,
    ) == ("OPENSQUILLA_GATEWAY_WORKSPACE_DIR", str(external_workspace))
    assert state_override(
        home,
        include_process_environment=False,
    ) == ("OPENSQUILLA_GATEWAY_STATE_DIR", str(external_state))

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "ready", report
    assert report.effective_workspace == external_workspace
    state = next(candidate for candidate in report.candidates if candidate.kind == "state")
    assert state.path == external_state
    assert state.valid is True


@pytest.mark.parametrize(
    ("database_setup", "stable_code"),
    [
        ("corrupt", "state_database_invalid"),
        ("future-schema", "state_schema_too_new"),
    ],
)
def test_unsafe_session_database_blocks_before_gateway_migrations(
    tmp_path: Path,
    database_setup: str,
    stable_code: str,
) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    database = home / "state" / "sessions.db"
    if database_setup == "corrupt":
        database.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE _yoyo_migration ("
                "migration_hash TEXT PRIMARY KEY, migration_id TEXT, applied_at_utc TEXT)"
            )
            connection.execute(
                "INSERT INTO _yoyo_migration VALUES (?, ?, ?)",
                ("synthetic", "V999__future", "2026-07-11T00:00:00Z"),
            )

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == stable_code


def test_wal_database_without_shm_is_validated_from_private_read_only_source_snapshot(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    database = home / "state" / "sessions.db"

    origin = tmp_path / "origin.db"
    connection = sqlite3.connect(origin)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE synthetic_sessions (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO synthetic_sessions VALUES ('session-1')")
        connection.commit()
        origin_wal = origin.with_name(f"{origin.name}-wal")
        assert origin_wal.is_file()
        shutil.copyfile(origin, database)
        shutil.copyfile(origin_wal, database.with_name(f"{database.name}-wal"))
    finally:
        connection.close()

    source_shm = database.with_name(f"{database.name}-shm")
    assert not source_shm.exists()
    before = {
        entry.name: (entry.read_bytes(), entry.stat().st_mode, entry.stat().st_mtime_ns)
        for entry in (database, database.with_name(f"{database.name}-wal"))
    }
    state_before = (home / "state").stat()

    report = inspect_profile(home, profile_kind="desktop-primary")

    state_after = (home / "state").stat()
    after = {
        entry.name: (entry.read_bytes(), entry.stat().st_mode, entry.stat().st_mtime_ns)
        for entry in (database, database.with_name(f"{database.name}-wal"))
    }
    assert report.outcome == "ready"
    assert before == after
    assert state_before.st_mtime_ns == state_after.st_mtime_ns
    assert not source_shm.exists(), "inspection must not create SQLite coordination files"


def test_database_snapshot_preserves_binary_sqlite_bytes(tmp_path: Path) -> None:
    import opensquilla.recovery.engine as recovery_engine

    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    payload = b"before\r\ncontrol-z:\x1a\nafter"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE payloads (value BLOB NOT NULL)")
        connection.execute("INSERT INTO payloads VALUES (?)", (payload,))

    source_bytes = source.read_bytes()
    assert payload in source_bytes

    snapshot = recovery_engine._copy_source_file_no_follow(source, destination)

    assert destination.read_bytes() == source_bytes
    assert recovery_engine._source_snapshot_is_current(snapshot) is True
    with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM payloads").fetchone() == (payload,)


def test_database_snapshot_requests_binary_mode_for_every_crt_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.engine as recovery_engine

    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    source.write_bytes(b"binary\r\ncontrol-z:\x1a\n")
    real_open = os.open
    native_binary = int(getattr(os, "O_BINARY", 0))
    sentinel_binary = 1 << 30
    opened: list[tuple[Path, int]] = []

    def tracked_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        opened.append((Path(path), flags))
        native_flags = (flags & ~sentinel_binary) | native_binary
        return real_open(path, native_flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(recovery_engine.os, "O_BINARY", sentinel_binary, raising=False)
    monkeypatch.setattr(recovery_engine.os, "open", tracked_open)

    snapshot = recovery_engine._copy_source_file_no_follow(source, destination)
    assert recovery_engine._source_snapshot_is_current(snapshot) is True

    expected_paths = [source, destination, source]
    assert len(opened) == len(expected_paths)
    assert all(
        os.path.samefile(actual, expected)
        for (actual, _flags), expected in zip(opened, expected_paths, strict=True)
    )
    assert all(flags & sentinel_binary for _path, flags in opened)


def test_database_snapshot_source_change_fails_closed_without_mutating_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.engine as recovery_engine

    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    database = home / "state" / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE synthetic_sessions (id TEXT PRIMARY KEY)")
    before = database.read_bytes()
    monkeypatch.setattr(recovery_engine, "_source_snapshot_is_current", lambda _snapshot: False)

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "state_database_changed"
    assert database.read_bytes() == before


def test_database_wal_symlink_is_never_followed(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    database = home / "state" / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE synthetic_sessions (id TEXT PRIMARY KEY)")
    outside = tmp_path / "outside-wal"
    outside.write_bytes(b"synthetic outside bytes")
    wal = database.with_name(f"{database.name}-wal")
    try:
        wal.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "state_database_unsafe_path"
    assert outside.read_bytes() == b"synthetic outside bytes"


@pytest.mark.parametrize("unsafe_state", ["future-config", "transaction-incomplete"])
def test_workspace_choice_is_blocked_when_config_mutation_is_not_safe(
    tmp_path: Path,
    unsafe_state: str,
) -> None:
    home = tmp_path / "opensquilla"
    selected = _workspace(tmp_path / "selected")
    _workspace(home / "workspace")
    extra = "config_version = 999" if unsafe_state == "future-config" else ""
    _desktop_config(home, extra=extra)
    if unsafe_state == "transaction-incomplete":
        (tmp_path / ".opensquilla.profile-replace.json").write_text(
            '{"schema_version":1,"phase":"prepared"}\n',
            encoding="utf-8",
        )
    config_before = (home / "config.toml").read_bytes()
    report = inspect_profile(home)

    assert report.outcome == "recovery_required"
    assert "choose-workspace" not in report.allowed_actions
    with pytest.raises(InvalidWorkspaceError):
        choose_workspace(
            home,
            transaction_id=report.transaction_id,
            expected_revision=report.revision,
            workspace=selected,
        )
    assert (home / "config.toml").read_bytes() == config_before


def test_workspace_choice_never_follows_unsafe_profile_home(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    selected = _workspace(tmp_path / "selected")
    _workspace(real_home / "workspace")
    _desktop_config(real_home)
    linked_home = tmp_path / "linked-home"
    try:
        linked_home.symlink_to(real_home, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    config_before = (real_home / "config.toml").read_bytes()
    report = inspect_profile(linked_home)

    assert report.stable_code == "profile_unsafe_path"
    assert "choose-workspace" not in report.allowed_actions
    with pytest.raises(InvalidWorkspaceError):
        choose_workspace(
            linked_home,
            transaction_id=report.transaction_id,
            expected_revision=report.revision,
            workspace=selected,
        )
    assert (real_home / "config.toml").read_bytes() == config_before


@pytest.mark.parametrize(
    ("config", "stable_code"),
    [
        (
            'workspace_dir = "unterminated\n',
            "config_invalid",
        ),
        ("config_version = 999\n", "config_schema_too_new"),
    ],
)
def test_invalid_or_future_config_requires_recovery(
    tmp_path: Path,
    config: str,
    stable_code: str,
) -> None:
    home = tmp_path / "opensquilla"
    home.mkdir()
    (home / "config.toml").write_text(config, encoding="utf-8")

    report = inspect_profile(home)

    assert report.outcome == "recovery_required"
    assert report.stable_code == stable_code


def test_replacement_journal_symlink_is_never_followed(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    _workspace(home / "workspace")
    _desktop_config(home)
    outside = tmp_path / "outside-journal.json"
    outside.write_text('{"phase":"committed"}\n', encoding="utf-8")
    journal = home.parent / f".{home.name}.profile-replace.json"
    try:
        journal.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    report = inspect_profile(home)

    assert report.outcome == "recovery_required"
    assert report.stable_code == "transaction_incomplete"
    assert outside.read_text(encoding="utf-8") == '{"phase":"committed"}\n'


def test_legacy_import_journal_blocks_without_automatic_mutation(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    workspace = _workspace(home / "workspace", "preserved identity")
    _desktop_config(home, workspace=workspace)
    journal = home.parent / f".{home.name}.import-commit.json"
    journal.write_text(
        json.dumps(
            {
                "phase": "target-backed-up",
                "target": str(home),
                "backup": str(tmp_path / "opensquilla.backup.synthetic"),
                "staging": str(tmp_path / ".opensquilla-import-synthetic"),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    config_before = (home / "config.toml").read_bytes()
    identity_before = (workspace / "SOUL.md").read_bytes()
    journal_before = journal.read_bytes()

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "legacy_import_transaction_incomplete"
    assert "recover-transaction" not in report.allowed_actions
    assert (home / "config.toml").read_bytes() == config_before
    assert (workspace / "SOUL.md").read_bytes() == identity_before
    assert journal.read_bytes() == journal_before


def test_legacy_import_journal_prevents_missing_target_from_looking_fresh(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    journal = home.parent / f".{home.name}.import-commit.json"
    journal.write_text('{"phase":"target-backed-up"}\n', encoding="utf-8")
    before = journal.read_bytes()

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "legacy_import_transaction_incomplete"
    assert not home.exists()
    assert journal.read_bytes() == before


@pytest.mark.parametrize("as_file", [False, True])
def test_profile_root_link_or_special_path_fails_closed(
    tmp_path: Path,
    as_file: bool,
) -> None:
    profile = tmp_path / "opensquilla"
    if as_file:
        profile.write_text("not a profile directory\n", encoding="utf-8")
    else:
        real = tmp_path / "real-profile"
        _workspace(real / "workspace")
        _desktop_config(real)
        try:
            profile.symlink_to(real, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation is unavailable")

    report = inspect_profile(profile)

    assert report.outcome == "recovery_required"
    assert report.stable_code == "profile_unsafe_path"
    assert report.candidates == ()


def test_choose_workspace_preserves_comments_unknown_keys_and_nested_key(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    legacy = _workspace(home / "state" / "workspace")
    selected = _workspace(tmp_path / "selected")
    _desktop_config(
        home,
        workspace=legacy,
        extra='unknown_future = "keep"\n\n[agent]\nworkspace_dir = "nested-keep"',
    )
    config_path = home / "config.toml"
    original = config_path.read_text(encoding="utf-8")
    config_path.write_text("# owner comment\n" + original, encoding="utf-8")
    config_path.chmod(0o640)
    before = inspect_profile(home)

    after = choose_workspace(
        home,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
        workspace=selected,
    )

    patched = config_path.read_text(encoding="utf-8")
    assert after.effective_workspace == selected
    assert "# owner comment" in patched
    assert 'unknown_future = "keep"' in patched
    assert 'workspace_dir = "nested-keep"' in patched
    if os.name != "nt":
        assert config_path.stat().st_mode & 0o777 == 0o640
    backups = list(home.glob("config.toml.backup.*"))
    assert len(backups) == 1
    if os.name != "nt":
        assert backups[0].stat().st_mode & 0o777 == 0o600


def test_choose_workspace_never_changes_chat_database_bytes_or_identity(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    legacy = _workspace(home / "state" / "workspace")
    selected = _workspace(tmp_path / "selected")
    _desktop_config(home, workspace=legacy)
    database = home / "state" / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE synthetic_chat (id TEXT PRIMARY KEY, transcript TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO synthetic_chat VALUES (?, ?)",
            ("session-before-workspace-choice", "synthetic transcript stays in state"),
        )
    before_identity = (database.stat().st_dev, database.stat().st_ino)
    before_bytes = database.read_bytes()
    inspected = inspect_profile(home)

    result = choose_workspace(
        home,
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        workspace=selected,
    )

    assert result.effective_workspace == selected
    assert (database.stat().st_dev, database.stat().st_ino) == before_identity
    assert database.read_bytes() == before_bytes
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT id, transcript FROM synthetic_chat").fetchone() == (
            "session-before-workspace-choice",
            "synthetic transcript stays in state",
        )


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="filesystem xattrs are unavailable")
def test_choose_workspace_preserves_config_extended_attributes(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    legacy = _workspace(home / "state" / "workspace")
    selected = _workspace(tmp_path / "selected")
    _desktop_config(home, workspace=legacy)
    config_path = home / "config.toml"
    attribute = "com.opensquilla.synthetic" if sys.platform == "darwin" else "user.opensquilla"
    try:
        os.setxattr(config_path, attribute, b"preserve", follow_symlinks=False)
    except OSError as exc:
        pytest.skip(f"test filesystem does not support user xattrs: {exc}")
    before = inspect_profile(home)

    choose_workspace(
        home,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
        workspace=selected,
    )

    assert os.getxattr(config_path, attribute, follow_symlinks=False) == b"preserve"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACL regression")
def test_choose_workspace_preserves_macos_config_acl(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    legacy = _workspace(home / "state" / "workspace")
    selected = _workspace(tmp_path / "selected")
    _desktop_config(home, workspace=legacy)
    config_path = home / "config.toml"
    subprocess.run(
        ["chmod", "+a", "everyone allow read", str(config_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    def acl_lines() -> list[str]:
        output = subprocess.run(
            ["ls", "-led", str(config_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        return [line.strip() for line in output[1:] if line.strip()]

    acl_before = acl_lines()
    assert acl_before
    before = inspect_profile(home)

    choose_workspace(
        home,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
        workspace=selected,
    )

    assert acl_lines() == acl_before


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL regression")
def test_choose_workspace_preserves_windows_config_dacl(tmp_path: Path) -> None:
    from opensquilla.recovery.atomic import _windows_extended_path

    home = tmp_path / "opensquilla"
    legacy = _workspace(home / "state" / "workspace")
    selected = _workspace(tmp_path / "selected")
    _desktop_config(home, workspace=legacy)
    config_path = home / "config.toml"
    subprocess.run(
        ["icacls", str(config_path), "/inheritance:d"],
        check=True,
        capture_output=True,
        text=True,
    )

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_file_security = advapi32.GetFileSecurityW
    get_file_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    get_file_security.restype = ctypes.c_int

    def dacl_semantics() -> tuple[int, bytes]:
        required = ctypes.c_uint32()
        get_file_security(
            _windows_extended_path(config_path),
            0x00000004,
            None,
            0,
            ctypes.byref(required),
        )
        assert required.value > 0
        buffer = ctypes.create_string_buffer(required.value)
        assert get_file_security(
            _windows_extended_path(config_path),
            0x00000004,
            buffer,
            required.value,
            ctypes.byref(required),
        )
        descriptor = bytes(buffer.raw[: required.value])
        control = int.from_bytes(descriptor[2:4], "little")
        dacl_offset = int.from_bytes(descriptor[16:20], "little")
        # Windows can normalize the AUTO_INHERITED bookkeeping bit when the
        # same DACL is attached to a new file. Preserve and compare the access
        # entries plus the flags that determine DACL presence/defaulting and
        # whether future inheritance is blocked.
        access_control_flags = control & 0x100C
        dacl = descriptor[dacl_offset:] if dacl_offset else b""
        return access_control_flags, dacl

    dacl_before = dacl_semantics()
    before = inspect_profile(home)
    choose_workspace(
        home,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
        workspace=selected,
    )

    assert dacl_semantics() == dacl_before


def test_workspace_env_override_blocks_config_patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla"
    configured = _workspace(tmp_path / "configured")
    selected = _workspace(tmp_path / "selected")
    _desktop_config(home, workspace=configured)
    original = (home / "config.toml").read_bytes()
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_WORKSPACE_DIR", str(configured))
    before = inspect_profile(home)

    with pytest.raises(WorkspaceOverrideError):
        choose_workspace(
            home,
            transaction_id=before.transaction_id,
            expected_revision=before.revision,
            workspace=selected,
        )

    assert (home / "config.toml").read_bytes() == original


def test_config_publication_preserves_a_mutation_in_the_final_cas_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.config_patch as config_patch

    home = tmp_path / "opensquilla"
    configured = _workspace(tmp_path / "configured")
    selected = _workspace(tmp_path / "selected")
    _desktop_config(home, workspace=configured)
    before = inspect_profile(home)
    config = home / "config.toml"
    original_move = config_patch.native_move_no_replace
    concurrent_text = config.read_text(encoding="utf-8") + "external_edit = true\n"

    def mutate_during_park(source: Path, destination: Path) -> None:
        if source == config and destination.name.startswith("config.toml.backup."):
            source.write_text(concurrent_text, encoding="utf-8")
        original_move(source, destination)

    monkeypatch.setattr(config_patch, "native_move_no_replace", mutate_during_park)

    with pytest.raises(AtomicStateUnknownError):
        choose_workspace(
            home,
            transaction_id=before.transaction_id,
            expected_revision=before.revision,
            workspace=selected,
        )

    journal = config_patch.workspace_patch_journal(home)
    payload = json.loads(journal.read_text(encoding="utf-8"))
    backup = Path(payload["paths"]["backup"])
    staged = Path(payload["paths"]["staged"])
    assert not config.exists()
    assert backup.read_text(encoding="utf-8") == concurrent_text
    assert staged.is_file()
    blocked = inspect_profile(home)
    assert blocked.outcome == "recovery_required"
    assert blocked.stable_code == "workspace_patch_incomplete"
    with pytest.raises(AtomicStateUnknownError):
        reconcile_profile(home)
    assert backup.read_text(encoding="utf-8") == concurrent_text
    assert staged.is_file()


def test_crashed_workspace_config_park_is_recovered_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.config_patch as config_patch

    class SimulatedCrash(BaseException):
        pass

    home = tmp_path / "opensquilla"
    configured = _workspace(tmp_path / "configured")
    selected = _workspace(tmp_path / "selected")
    _desktop_config(home, workspace=configured)
    config = home / "config.toml"
    old_text = config.read_text(encoding="utf-8")
    before = inspect_profile(home)
    original_move = config_patch.native_move_no_replace
    crashed = False

    def crash_after_config_park(source: Path, destination: Path) -> None:
        nonlocal crashed
        original_move(source, destination)
        if source == config and destination.name.startswith("config.toml.backup."):
            crashed = True
            raise SimulatedCrash

    monkeypatch.setattr(config_patch, "native_move_no_replace", crash_after_config_park)

    with pytest.raises(SimulatedCrash):
        choose_workspace(
            home,
            transaction_id=before.transaction_id,
            expected_revision=before.revision,
            workspace=selected,
        )

    assert crashed
    blocked = inspect_profile(home)
    assert blocked.outcome == "recovery_required"
    assert blocked.stable_code == "workspace_patch_incomplete"
    recovered = reconcile_profile(home)

    assert recovered.outcome == "ready"
    assert recovered.effective_workspace == selected
    assert tomllib.loads(config.read_text(encoding="utf-8"))["workspace_dir"] == str(selected)
    backups = list(home.glob("config.toml.backup.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == old_text
    if os.name != "nt":
        assert backups[0].stat().st_mode & 0o777 == 0o600
    assert not config_patch.workspace_patch_journal(home).exists()


def test_bootstrap_guard_is_desktop_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    _desktop_config(home, workspace=tmp_path / "missing")
    monkeypatch.delenv("OPENSQUILLA_PROFILE_KIND", raising=False)
    monkeypatch.delenv("OPENSQUILLA_DESKTOP", raising=False)
    assert guard_desktop_profile(home) is None

    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    with pytest.raises(RecoveryRequiredError) as caught:
        guard_desktop_profile(home)
    assert caught.value.report.stable_code == "effective_workspace_missing"


def test_legacy_profile_dotenv_pin_prevents_workspace_move(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    legacy = _workspace(home / "state" / "workspace", "legacy identity")
    _desktop_config(home)
    legacy_env = home / "state" / ".env"
    legacy_env.write_text(
        f"OPENSQUILLA_GATEWAY_WORKSPACE_DIR={legacy}\n",
        encoding="utf-8",
    )

    before = inspect_profile(home, profile_kind="desktop-primary")
    assert before.effective_workspace == legacy
    assert before.outcome == "recovery_required"

    after = reconcile_profile(home, profile_kind="desktop-primary")

    assert after.outcome == "attention"
    assert after.stable_code == "legacy_workspace_pinned"
    assert after.effective_workspace == legacy
    assert legacy.is_dir()
    assert not (home / "workspace").exists()
    assert not legacy_env.exists()
    assert (home / ".env").read_text(encoding="utf-8").endswith(f"={legacy}\n")


def test_interpolated_profile_dotenv_override_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    legacy = _workspace(home / "state" / "workspace")
    _desktop_config(home)
    legacy_env = home / "state" / ".env"
    legacy_env.write_text(
        "OPENSQUILLA_GATEWAY_WORKSPACE_DIR=${UNSAFE_DYNAMIC_ROOT}/workspace\n",
        encoding="utf-8",
    )

    report = reconcile_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "recovery_required"
    assert report.stable_code == "workspace_env_override_unsafe"
    assert legacy.is_dir()
    assert legacy_env.is_file()
    assert not (home / "workspace").exists()
    assert not (home / ".env").exists()
