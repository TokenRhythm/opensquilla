from __future__ import annotations

import json
import multiprocessing
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from opensquilla.recovery import (
    ProfileOperationLock,
    RecoveryError,
    StaleRecoveryTransactionError,
    inspect_profile,
)
from opensquilla.recovery.cleanup import (
    _path_identity_payload,
    abandon_cleanup_transaction,
    cleanup_apply,
    cleanup_inspect,
)


def _home(path: Path, value: str) -> Path:
    workspace = path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text(value + "\n", encoding="utf-8")
    (path / "config.toml").write_text('workspace_dir = "workspace"\n', encoding="utf-8")
    return path


def _recovery_profile(user_data: Path, recovery_id: str, value: str) -> Path:
    root = user_data / "recovery-profiles" / recovery_id
    _home(root / "opensquilla", value)
    (root / "desktop-credential.json").write_text("{}\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "desktop.log").write_text("synthetic\n", encoding="utf-8")
    return root


def _backup(user_data: Path, primary: Path) -> Path:
    transaction_id = str(uuid.uuid4())
    backup = primary.with_name(f"{primary.name}.backup.{transaction_id}")
    _home(backup, "backup")
    history = {
        "schema_version": 1,
        "backups": [
            {
                "transaction_id": transaction_id,
                "committed_at": "2026-07-11T00:00:00+00:00",
                "source": str(user_data / "synthetic-source"),
                "target": str(primary),
                "backup": str(backup),
                "source_identity": _path_identity_payload(backup),
                "target_identity": _path_identity_payload(primary),
                "backup_identity": _path_identity_payload(backup),
            }
        ],
    }
    (user_data / "profile-replacement-history.json").write_text(
        json.dumps(history, indent=2) + "\n",
        encoding="utf-8",
    )
    return backup


def _desktop_primary(user_data: Path) -> Path:
    primary = _home(user_data / "opensquilla", "primary")
    (user_data / "desktop-credential.json").write_text("{}\n", encoding="utf-8")
    (user_data / "logs").mkdir()
    (user_data / "logs" / "desktop.log").write_text("synthetic\n", encoding="utf-8")
    (user_data / "desktop-profile-context.json").write_text(
        '{"schema_version":1,"active_profile_kind":"primary"}\n',
        encoding="utf-8",
    )
    return primary


def _hold_profile_lock(home: str, state_root: str, ready, release) -> None:
    os.environ["OPENSQUILLA_USER_STATE_DIR"] = state_root
    with ProfileOperationLock(home):
        ready.set()
        release.wait(10)


def _hold_gateway(home: str, ready, release) -> None:
    from opensquilla.gateway.pidlock import GatewayPidLock

    state = Path(home) / "state"
    lock = GatewayPidLock(state)
    lock.acquire()
    try:
        ready.set()
        release.wait(10)
    finally:
        lock.release()


def _hold_recreated_legacy_gateway(home: str, ready, release) -> None:
    from opensquilla.gateway.pidlock import GatewayPidLock

    home_path = Path(home)
    state = home_path / "state"
    state.mkdir(parents=True)
    lock = GatewayPidLock(state)
    lock.acquire()
    try:
        workspace = home_path / "workspace"
        workspace.mkdir()
        (workspace / "SOUL.md").write_text("recreated by old gateway\n", encoding="utf-8")
        ready.set()
        release.wait(10)
    finally:
        lock.release()


def test_delete_current_primary_preserves_recovery_profiles_and_backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    recovery_id = str(uuid.uuid4())
    recovery = _recovery_profile(user_data, recovery_id, "recovery")
    backup = _backup(user_data, primary)

    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    assert inspected.outcome == "ready"
    assert all("backup" not in item.kind for item in inspected.items)
    assert not (tmp_path / "lock-state").exists(), "cleanup inspection must remain read-only"

    result = cleanup_apply(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "complete", result.stable_code
    assert not primary.exists()
    assert not (user_data / "desktop-credential.json").exists()
    assert not (user_data / "logs").exists()
    assert not (user_data / "desktop-profile-context.json").exists()
    assert recovery.is_dir()
    assert backup.is_dir()
    assert (user_data / "profile-replacement-history.json").is_file()


def test_delete_current_profile_with_state_but_no_legacy_lock_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    state = primary / "state"
    state.mkdir()
    (state / "sessions.db").write_bytes(b"synthetic sessions")
    assert not (state / "gateway.pid.lock").exists()

    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    result = cleanup_apply(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "complete", result.stable_code
    assert not primary.exists()


def test_cleanup_revision_ignores_directory_metadata_but_not_child_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    state = primary / "state"
    state.mkdir()
    (state / "gateway.pid.lock").write_bytes(b"synthetic lock authority\n")

    before = cleanup_module._build_plan(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
        recovery_id=None,
    )
    for directory in (primary, primary / "workspace", state):
        value = directory.stat()
        os.utime(
            directory,
            ns=(value.st_atime_ns, value.st_mtime_ns + 1_000_000_000),
        )
    metadata_only = cleanup_module._build_plan(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
        recovery_id=None,
    )
    assert metadata_only.revision == before.revision

    (state / "sessions.db").write_bytes(b"new user data\n")
    changed = cleanup_module._build_plan(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
        recovery_id=None,
    )
    assert changed.revision != before.revision


def test_reset_current_settings_preserves_config_workspace_and_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    config = primary / "config.toml"
    config_before = config.read_bytes()
    workspace = primary / "workspace" / "SOUL.md"
    workspace_before = workspace.read_bytes()
    sessions = primary / "state" / "sessions.db"
    sessions.parent.mkdir()
    sessions.write_bytes(b"synthetic session database")
    (user_data / "migration-provider-setup.json").write_text("{}\n", encoding="utf-8")
    (user_data / "migration-last-result.json").write_text("{}\n", encoding="utf-8")

    inspected = cleanup_inspect(
        user_data,
        mode="reset-current-settings",
        profile_kind="primary",
    )
    assert inspected.outcome == "ready"
    assert {item.kind for item in inspected.items} == {
        "primary-credential",
        "migration-pending",
        "migration-result",
    }

    result = cleanup_apply(
        user_data,
        mode="reset-current-settings",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "complete", result.stable_code
    assert not (user_data / "desktop-credential.json").exists()
    assert not (user_data / "migration-provider-setup.json").exists()
    assert not (user_data / "migration-last-result.json").exists()
    assert config.read_bytes() == config_before
    assert workspace.read_bytes() == workspace_before
    assert sessions.read_bytes() == b"synthetic session database"
    assert (user_data / "desktop-profile-context.json").is_file()


def test_reset_recovery_settings_only_clears_selected_recovery_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    recovery_id = str(uuid.uuid4())
    recovery_root = _recovery_profile(user_data, recovery_id, "recovery")
    recovery_home = recovery_root / "opensquilla"
    sessions = recovery_home / "state" / "sessions.db"
    sessions.parent.mkdir()
    sessions.write_bytes(b"synthetic recovery sessions")
    config_before = (recovery_home / "config.toml").read_bytes()
    workspace_before = (recovery_home / "workspace" / "SOUL.md").read_bytes()

    inspected = cleanup_inspect(
        user_data,
        mode="reset-current-settings",
        profile_kind="recovery",
        recovery_id=recovery_id,
    )
    result = cleanup_apply(
        user_data,
        mode="reset-current-settings",
        profile_kind="recovery",
        recovery_id=recovery_id,
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "complete"
    assert not (recovery_root / "desktop-credential.json").exists()
    assert (recovery_root / "logs" / "desktop.log").is_file()
    assert (recovery_home / "config.toml").read_bytes() == config_before
    assert (recovery_home / "workspace" / "SOUL.md").read_bytes() == workspace_before
    assert sessions.read_bytes() == b"synthetic recovery sessions"
    assert primary.is_dir()


def test_delete_current_recovery_preserves_primary_other_recovery_and_backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    selected_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    selected = _recovery_profile(user_data, selected_id, "selected")
    other = _recovery_profile(user_data, other_id, "other")
    backup = _backup(user_data, primary)

    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="recovery",
        recovery_id=selected_id,
    )
    result = cleanup_apply(
        user_data,
        mode="delete-current-profile",
        profile_kind="recovery",
        recovery_id=selected_id,
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "complete", result.stable_code
    assert not selected.exists()
    assert primary.is_dir()
    assert other.is_dir()
    assert backup.is_dir()
    assert (user_data / "profile-replacement-history.json").is_file()
    assert not (user_data / "desktop-profile-context.json").exists()


def test_delete_all_inventory_and_apply_removes_every_owned_category(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    recovery_id = str(uuid.uuid4())
    _recovery_profile(user_data, recovery_id, "recovery")
    backup = _backup(user_data, primary)
    (user_data / "migration-provider-setup.json").write_text("{}\n", encoding="utf-8")
    (user_data / "migration-last-result.json").write_text("{}\n", encoding="utf-8")
    (user_data / "desktop-locale.json").write_text('{"locale":"zh-CN"}\n', encoding="utf-8")
    chromium_storage = user_data / "Local Storage" / "leveldb"
    chromium_storage.mkdir(parents=True)
    (chromium_storage / "000001.log").write_text("synthetic cache\n", encoding="utf-8")

    inspected = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )

    assert inspected.outcome == "ready"
    kinds = {item.kind for item in inspected.items}
    assert {
        "primary-home",
        "primary-credential",
        "primary-logs",
        "profile-context",
        "profile-backup",
        "replacement-history",
        "migration-pending",
        "migration-result",
        "recovery-profiles-container",
        "user-data-entry",
    } <= kinds
    residual_paths = {item.path for item in inspected.items if item.kind == "user-data-entry"}
    assert user_data / "desktop-locale.json" in residual_paths
    assert user_data / "Local Storage" in residual_paths

    result = cleanup_apply(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "complete"
    assert all(not item.exists for item in result.items)
    assert not backup.exists()
    assert list(user_data.iterdir()) == []


def test_delete_all_from_recovery_profile_still_covers_primary_and_every_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    selected_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    _recovery_profile(user_data, selected_id, "selected")
    _recovery_profile(user_data, other_id, "other")
    backup = _backup(user_data, primary)

    inspected = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="recovery",
        recovery_id=selected_id,
    )
    result = cleanup_apply(
        user_data,
        mode="delete-all-user-data",
        profile_kind="recovery",
        recovery_id=selected_id,
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "complete"
    assert all(not item.exists for item in result.items)
    assert not primary.exists()
    assert not backup.exists()
    assert list(user_data.iterdir()) == []


def test_cleanup_apply_requires_exact_user_data_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    inspected = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )

    result = cleanup_apply(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=tmp_path / "different-user-data",
    )

    assert result.outcome == "blocked"
    assert result.stable_code == "cleanup_confirmation_mismatch"
    assert primary.is_dir()


def test_delete_all_retains_overlapping_coordination_locks_without_split_brain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Models macOS: user-state/OpenSquilla is also Electron userData A.
    user_data = tmp_path / "OpenSquilla"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path))
    _desktop_primary(user_data)

    inspected = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )
    assert inspected.outcome == "ready"
    assert all(item.path != user_data / "profile-locks" for item in inspected.items)

    result = cleanup_apply(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "complete"
    assert [path.name for path in user_data.iterdir()] == ["profile-locks"]
    assert (user_data / "profile-locks").is_dir()


def test_cleanup_apply_blocks_when_profile_lock_is_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    state_root = tmp_path / "lock-state"
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(state_root))
    primary = _desktop_primary(user_data)
    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    context = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_profile_lock,
        args=(str(primary), str(state_root), ready, release),
    )
    process.start()
    assert ready.wait(5)
    try:
        result = cleanup_apply(
            user_data,
            mode="delete-current-profile",
            profile_kind="primary",
            transaction_id=inspected.transaction_id,
            expected_revision=inspected.revision,
            confirm_user_data=user_data,
        )
    finally:
        release.set()
        process.join(10)

    assert result.outcome == "blocked"
    assert result.stable_code == "profile_lock_busy"
    assert primary.is_dir()


def test_cleanup_apply_contends_with_replacement_history_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    state_root = tmp_path / "lock-state"
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(state_root))
    _desktop_primary(user_data)
    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    context = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_profile_lock,
        args=(str(user_data), str(state_root), ready, release),
    )
    process.start()
    assert ready.wait(5)
    try:
        result = cleanup_apply(
            user_data,
            mode="delete-current-profile",
            profile_kind="primary",
            transaction_id=inspected.transaction_id,
            expected_revision=inspected.revision,
            confirm_user_data=user_data,
        )
    finally:
        release.set()
        process.join(10)

    assert result.outcome == "blocked"
    assert result.stable_code == "profile_lock_busy"
    assert (user_data / "opensquilla").is_dir()


@pytest.mark.ci_serial
def test_cleanup_apply_refuses_running_legacy_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    (primary / "state").mkdir()
    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    context = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_gateway,
        args=(str(primary), ready, release),
    )
    process.start()
    startup_timeout = 15 if sys.platform == "win32" else 5
    try:
        assert ready.wait(startup_timeout), (
            "legacy gateway lock holder did not start within "
            f"{startup_timeout}s (exitcode={process.exitcode})"
        )
        result = cleanup_apply(
            user_data,
            mode="delete-current-profile",
            profile_kind="primary",
            transaction_id=inspected.transaction_id,
            expected_revision=inspected.revision,
            confirm_user_data=user_data,
        )
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert result.outcome == "blocked"
    assert result.stable_code == "legacy_gateway_running"
    assert primary.is_dir()


def test_cleanup_rejects_linked_user_data_root_without_following_it(tmp_path: Path) -> None:
    real_user_data = tmp_path / "real-user-data"
    real_user_data.mkdir()
    sentinel = real_user_data / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    linked_user_data = tmp_path / "linked-user-data"
    try:
        linked_user_data.symlink_to(real_user_data, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    report = cleanup_inspect(
        linked_user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )

    assert report.outcome == "blocked"
    assert report.stable_code == "cleanup_user_data_unsafe"
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_cleanup_apply_rejects_stale_recursive_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    (primary / "workspace" / "SOUL.md").write_text("changed after inspect\n", encoding="utf-8")

    result = cleanup_apply(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "blocked"
    assert result.stable_code == "cleanup_inventory_stale"
    assert primary.is_dir()


def test_cleanup_never_deletes_canonical_profile_recreated_after_quarantine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    original_remove = cleanup_module._remove_no_follow
    context = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
    ready = context.Event()
    release = context.Event()
    process: multiprocessing.Process | None = None

    def recreate_canonical_before_deleting_quarantine(path: Path) -> None:
        nonlocal process
        if process is None and path.name.startswith(f".{primary.name}.cleanup."):
            process = context.Process(
                target=_hold_recreated_legacy_gateway,
                args=(str(primary), ready, release),
            )
            process.start()
            # Windows spawn must import the test module before acquiring the
            # synthetic Gateway lock. Under a fully loaded high-risk shard that
            # can legitimately exceed five seconds without indicating failure.
            assert ready.wait(15)
        original_remove(path)

    monkeypatch.setattr(
        cleanup_module,
        "_remove_no_follow",
        recreate_canonical_before_deleting_quarantine,
    )

    try:
        result = cleanup_apply(
            user_data,
            mode="delete-current-profile",
            profile_kind="primary",
            transaction_id=inspected.transaction_id,
            expected_revision=inspected.revision,
            confirm_user_data=user_data,
        )
    finally:
        release.set()
        if process is not None:
            process.join(10)

    assert process is not None
    assert process.exitcode == 0
    assert result.outcome == "partial"
    assert (primary / "workspace" / "SOUL.md").read_text(encoding="utf-8") == (
        "recreated by old gateway\n"
    )
    assert (user_data / "desktop-credential.json").is_file()
    assert (user_data / "desktop-profile-context.json").is_file()
    assert (user_data / ".opensquilla.profile-cleanup.json").is_file()


def test_cleanup_releases_legacy_handles_before_deleting_profile_tombstone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module
    import opensquilla.recovery.locking as locking_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    state = primary / "state"
    state.mkdir()
    (state / "gateway.pid.lock").write_bytes(b"synthetic lock authority\n")
    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    monkeypatch.setattr(
        locking_module,
        "_windows_requires_legacy_lock_handoff",
        lambda: True,
    )
    real_acquire = cleanup_module.acquire_legacy_gateway_locks
    captured_locks = []

    @contextmanager
    def capture_locks(*homes, **kwargs):
        with real_acquire(*homes, **kwargs) as locks:
            captured_locks.extend(locks)
            yield locks

    real_remove = cleanup_module._remove_no_follow
    observed_tombstone_delete = False

    def assert_handles_released(path: Path) -> None:
        nonlocal observed_tombstone_delete
        if path.name.startswith(f".{primary.name}.cleanup."):
            observed_tombstone_delete = True
            moved_state = path / "state"
            assert not any(lock.holds_state_root(moved_state) for lock in captured_locks)
        real_remove(path)

    monkeypatch.setattr(cleanup_module, "acquire_legacy_gateway_locks", capture_locks)
    monkeypatch.setattr(cleanup_module, "_remove_no_follow", assert_handles_released)

    result = cleanup_apply(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "complete", result.stable_code
    assert observed_tombstone_delete is True
    assert captured_locks
    assert not primary.exists()


def test_cleanup_rolls_back_directory_swapped_at_no_replace_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module
    import opensquilla.recovery.locking as locking_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    displaced = user_data / "displaced-original-profile"
    real_move = cleanup_module.native_move_no_replace
    swapped = False
    monkeypatch.setattr(
        locking_module,
        "_windows_requires_legacy_lock_handoff",
        lambda: True,
    )

    def swap_then_move(
        source: Path,
        destination: Path,
        **move_options: object,
    ) -> None:
        nonlocal swapped
        if source == primary and not swapped:
            swapped = True
            source.rename(displaced)
            _home(source, "replacement profile")
        real_move(source, destination, **move_options)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup_module, "native_move_no_replace", swap_then_move)

    result = cleanup_apply(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert swapped is True
    assert result.outcome == "partial"
    assert (primary / "workspace" / "SOUL.md").read_text(encoding="utf-8") == (
        "replacement profile\n"
    )
    assert (displaced / "workspace" / "SOUL.md").read_text(encoding="utf-8") == (
        "primary\n"
    )
    assert (user_data / ".opensquilla.profile-cleanup.json").is_file()


def test_cleanup_backup_delete_failure_preserves_history_and_cleanup_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    primary = _desktop_primary(user_data)
    backup = _backup(user_data, primary)
    history = user_data / "profile-replacement-history.json"
    history_before = history.read_bytes()
    inspected = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )
    original_remove = cleanup_module._remove_no_follow

    def fail_backup_delete(path: Path) -> None:
        if path == backup or path.name.startswith(f".{backup.name}.cleanup."):
            raise OSError("synthetic failure after backup quarantine")
        original_remove(path)

    monkeypatch.setattr(cleanup_module, "_remove_no_follow", fail_backup_delete)

    result = cleanup_apply(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "partial"
    assert backup.is_dir()
    assert history.read_bytes() == history_before
    assert (user_data / ".opensquilla.profile-cleanup.json").is_file()


def test_recovery_profile_inspection_sees_global_partial_cleanup_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    _desktop_primary(user_data)
    recovery_id = str(uuid.uuid4())
    recovery_root = _recovery_profile(user_data, recovery_id, "recovery")
    recovery_home = recovery_root / "opensquilla"
    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="recovery",
        recovery_id=recovery_id,
    )
    original_remove = cleanup_module._remove_no_follow

    def fail_recovery_delete(path: Path) -> None:
        if path == recovery_root or path.name.startswith(f".{recovery_root.name}.cleanup."):
            raise OSError("synthetic partial recovery profile deletion")
        original_remove(path)

    monkeypatch.setattr(cleanup_module, "_remove_no_follow", fail_recovery_delete)

    result = cleanup_apply(
        user_data,
        mode="delete-current-profile",
        profile_kind="recovery",
        recovery_id=recovery_id,
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )
    report = inspect_profile(recovery_home, profile_kind="desktop-recovery")

    assert result.outcome == "partial"
    assert recovery_home.is_dir()
    assert report.outcome == "attention"
    assert report.stable_code == "cleanup_transaction_incomplete"
    assert "recover-transaction" not in report.allowed_actions


def test_incomplete_cleanup_journal_blocks_fresh_primary_bootstrap(tmp_path: Path) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    home = user_data / "opensquilla"
    (user_data / ".opensquilla.profile-cleanup.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation": "profile-cleanup",
                "transaction_id": str(uuid.uuid4()),
                "phase": "prepared",
                "primary_home": str(home),
                "tombstones": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = inspect_profile(home, profile_kind="desktop-primary")

    assert report.outcome == "attention"
    assert report.stable_code == "cleanup_transaction_incomplete"
    assert "recover-transaction" not in report.allowed_actions


def test_primary_cleanup_journal_does_not_block_unaffected_recovery_profile(
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    primary = _desktop_primary(user_data)
    recovery_id = str(uuid.uuid4())
    recovery_root = _recovery_profile(user_data, recovery_id, "recovery")
    recovery_home = recovery_root / "opensquilla"
    (recovery_home / "state").mkdir()
    (user_data / ".opensquilla.profile-cleanup.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation": "profile-cleanup",
                "transaction_id": str(uuid.uuid4()),
                "phase": "prepared",
                "primary_home": str(primary),
                "mode": "delete-current-profile",
                "profile_kind": "primary",
                "recovery_id": None,
                "tombstones": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = inspect_profile(recovery_home, profile_kind="desktop-recovery")

    assert report.outcome == "recovery_profile"
    assert report.stable_code != "cleanup_transaction_incomplete"


def test_cleanup_journal_does_not_apply_to_an_ordinary_cli_profile(tmp_path: Path) -> None:
    home = _home(tmp_path / "cli-home", "cli")
    (home / "state").mkdir()
    (home.parent / f".{home.name}.profile-cleanup.json").write_text(
        "malformed guard that belongs only to Desktop\n",
        encoding="utf-8",
    )

    report = inspect_profile(home, profile_kind="cli-home")

    assert report.outcome == "ready"
    assert report.stable_code != "cleanup_transaction_incomplete"


def test_cleanup_unlinks_logs_symlink_without_following_external_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    _desktop_primary(user_data)
    logs = user_data / "logs"
    for child in logs.iterdir():
        child.unlink()
    logs.rmdir()
    external = tmp_path / "external-logs"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    try:
        logs.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    result = cleanup_apply(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "complete"
    assert not os.path.lexists(logs)
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize(
    ("filename", "content", "stable_code", "mode"),
    [
        (
            "profile-replacement-history.json",
            "not-json\n",
            "cleanup_history_invalid",
            "delete-all-user-data",
        ),
        (
            ".opensquilla.profile-replace.json",
            '{"schema_version":1,"phase":"prepared"}\n',
            "cleanup_transaction_incomplete",
            "delete-current-profile",
        ),
    ],
)
def test_malformed_history_or_unfinished_journal_blocks_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filename: str,
    content: str,
    stable_code: str,
    mode: str,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    _desktop_primary(user_data)
    (user_data / filename).write_text(content, encoding="utf-8")

    report = cleanup_inspect(
        user_data,
        mode=mode,
        profile_kind="primary",
    )

    assert report.outcome == "blocked"
    assert report.stable_code == stable_code


@pytest.mark.parametrize("selected_kind", ["primary", "recovery"])
def test_delete_current_ignores_unrelated_bad_history_and_non_uuid_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selected_kind: str,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    _desktop_primary(user_data)
    selected_id = str(uuid.uuid4())
    selected = _recovery_profile(user_data, selected_id, "selected")
    unrelated = user_data / "recovery-profiles" / "not-a-uuid"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep\n", encoding="utf-8")
    history = user_data / "profile-replacement-history.json"
    history.write_text("malformed and unrelated\n", encoding="utf-8")

    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind=selected_kind,
        recovery_id=selected_id if selected_kind == "recovery" else None,
    )

    assert inspected.outcome == "ready"
    result = cleanup_apply(
        user_data,
        mode="delete-current-profile",
        profile_kind=selected_kind,
        recovery_id=selected_id if selected_kind == "recovery" else None,
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )
    assert result.outcome == "complete"
    if selected_kind == "recovery":
        assert not selected.exists()
        assert (user_data / "opensquilla").exists()
    else:
        assert not (user_data / "opensquilla").exists()
        assert selected.exists()
    assert history.read_text(encoding="utf-8") == "malformed and unrelated\n"
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_non_uuid_recovery_entry_blocks_all_cleanup(tmp_path: Path) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    _desktop_primary(user_data)
    (user_data / "recovery-profiles" / "not-a-uuid").mkdir(parents=True)

    report = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )

    assert report.outcome == "blocked"
    assert report.stable_code == "cleanup_recovery_entry_invalid"


def test_cleanup_delete_failure_returns_partial_and_never_claims_all(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    _desktop_primary(user_data)
    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    credential = user_data / "desktop-credential.json"
    original_remove = cleanup_module._remove_no_follow

    def fail_credential(path: Path) -> None:
        if path == credential:
            raise OSError("synthetic sharing violation")
        original_remove(path)

    monkeypatch.setattr(cleanup_module, "_remove_no_follow", fail_credential)

    result = cleanup_apply(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert result.outcome == "partial"
    assert result.stable_code == "cleanup_partial"
    assert credential.exists()


def test_cleanup_reports_partial_when_missing_known_path_is_recreated_after_apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    _desktop_primary(user_data)
    credential = user_data / "desktop-credential.json"
    credential.unlink()
    inspected = cleanup_inspect(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
    )
    credential_item = next(
        item for item in inspected.items if item.kind == "primary-credential"
    )
    assert credential_item.exists is False
    real_current_item = cleanup_module._current_item
    recreated = False

    def recreate_before_final_inventory(planned):
        nonlocal recreated
        if not recreated:
            recreated = True
            credential.write_text("{}\n", encoding="utf-8")
        return real_current_item(planned)

    monkeypatch.setattr(cleanup_module, "_current_item", recreate_before_final_inventory)

    result = cleanup_apply(
        user_data,
        mode="delete-current-profile",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert recreated is True
    assert result.outcome == "partial"
    assert credential.is_file()
    assert (user_data / ".opensquilla.profile-cleanup.json").is_file()


def test_delete_all_reports_partial_for_new_unplanned_user_data_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    _desktop_primary(user_data)
    inspected = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )
    late_entry = user_data / "late-chromium-cache" / "cache.bin"
    real_current_item = cleanup_module._current_item
    recreated = False

    def create_unplanned_entry_before_final_inventory(planned):
        nonlocal recreated
        if not recreated:
            recreated = True
            late_entry.parent.mkdir()
            late_entry.write_bytes(b"synthetic cache")
        return real_current_item(planned)

    monkeypatch.setattr(
        cleanup_module,
        "_current_item",
        create_unplanned_entry_before_final_inventory,
    )

    result = cleanup_apply(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )

    assert recreated is True
    assert result.outcome == "partial"
    assert late_entry.read_bytes() == b"synthetic cache"
    assert (user_data / ".opensquilla.profile-cleanup.json").is_file()


def test_partial_delete_all_can_be_abandoned_then_new_recovery_profile_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    _desktop_primary(user_data)
    inspected = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )
    credential = user_data / "desktop-credential.json"
    original_remove = cleanup_module._remove_no_follow

    def fail_credential(path: Path) -> None:
        if path == credential:
            raise OSError("synthetic partial cleanup")
        original_remove(path)

    monkeypatch.setattr(cleanup_module, "_remove_no_follow", fail_credential)
    result = cleanup_apply(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
        transaction_id=inspected.transaction_id,
        expected_revision=inspected.revision,
        confirm_user_data=user_data,
    )
    assert result.outcome == "partial"

    recovery_id = str(uuid.uuid4())
    recovery_root = _recovery_profile(user_data, recovery_id, "new recovery")
    recovery_home = recovery_root / "opensquilla"
    (recovery_home / "state").mkdir()
    blocked = inspect_profile(recovery_home, profile_kind="desktop-recovery")
    assert blocked.stable_code == "cleanup_transaction_incomplete"
    assert "abandon-cleanup" in blocked.allowed_actions

    preserved = abandon_cleanup_transaction(
        user_data,
        home=recovery_home,
        profile_kind="desktop-recovery",
        transaction_id=blocked.transaction_id,
        expected_revision=blocked.revision,
    )

    assert preserved.is_file()
    assert ".profile-cleanup.abandoned." in preserved.name
    assert not (user_data / ".opensquilla.profile-cleanup.json").exists()
    assert credential.is_file()
    usable = inspect_profile(recovery_home, profile_kind="desktop-recovery")
    assert usable.outcome == "recovery_profile"


def test_malformed_cleanup_journal_can_be_quarantined_without_deleting_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    recovery_id = str(uuid.uuid4())
    recovery_root = _recovery_profile(user_data, recovery_id, "safe recovery")
    recovery_home = recovery_root / "opensquilla"
    (recovery_home / "state").mkdir()
    journal = user_data / ".opensquilla.profile-cleanup.json"
    journal.write_text("malformed cleanup authority\n", encoding="utf-8")
    before = (recovery_home / "workspace" / "SOUL.md").read_bytes()
    blocked = inspect_profile(recovery_home, profile_kind="desktop-recovery")

    preserved = abandon_cleanup_transaction(
        user_data,
        home=recovery_home,
        profile_kind="desktop-recovery",
        transaction_id=blocked.transaction_id,
        expected_revision=blocked.revision,
    )

    assert preserved.read_text(encoding="utf-8") == "malformed cleanup authority\n"
    assert not journal.exists()
    assert (recovery_home / "workspace" / "SOUL.md").read_bytes() == before
    assert inspect_profile(
        recovery_home,
        profile_kind="desktop-recovery",
    ).outcome == "recovery_profile"


@pytest.mark.parametrize("mutation", ["identity", "digest"])
def test_abandon_cleanup_rejects_journal_swapped_after_inspection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    home = _desktop_primary(user_data)
    journal = user_data / ".opensquilla.profile-cleanup.json"
    first = b"malformed cleanup journal alpha\n"
    second = b"malformed cleanup journal bravo\n"
    assert len(first) == len(second)
    journal.write_bytes(first)
    blocked = inspect_profile(home, profile_kind="desktop-primary")
    assert blocked.stable_code == "cleanup_transaction_incomplete"

    before = journal.lstat()
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )
    if mutation == "identity":
        replacement = user_data / ".replacement-cleanup-journal.json"
        replacement.write_bytes(first)
        os.replace(replacement, journal)
        after = journal.lstat()
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
        )
        assert after_identity != before_identity
    else:
        journal.write_bytes(second)
        os.utime(journal, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = journal.lstat()
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
        )
        assert after_identity == before_identity

    current = journal.read_bytes()
    swapped = inspect_profile(home, profile_kind="desktop-primary")
    assert swapped.stable_code == "cleanup_transaction_incomplete"
    assert swapped.transaction_id == blocked.transaction_id
    assert swapped.revision != blocked.revision

    with pytest.raises(StaleRecoveryTransactionError):
        abandon_cleanup_transaction(
            user_data,
            home=home,
            profile_kind="desktop-primary",
            transaction_id=blocked.transaction_id,
            expected_revision=blocked.revision,
        )

    assert journal.read_bytes() == current
    assert not tuple(user_data.glob(".opensquilla.profile-cleanup.abandoned.*.json"))


def test_abandon_cleanup_rechecks_captured_journal_after_internal_inspection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.engine as engine_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    home = _desktop_primary(user_data)
    journal = user_data / ".opensquilla.profile-cleanup.json"
    original = b"original cleanup authority\n"
    replacement_data = b"replacement cleanup authority\n"
    journal.write_bytes(original)
    blocked = inspect_profile(home, profile_kind="desktop-primary")
    real_inspect = engine_module.inspect_profile

    def inspect_then_swap(*args, **kwargs):
        report = real_inspect(*args, **kwargs)
        replacement = user_data / ".replacement-cleanup-journal.json"
        replacement.write_bytes(replacement_data)
        os.replace(replacement, journal)
        return report

    monkeypatch.setattr(engine_module, "inspect_profile", inspect_then_swap)

    with pytest.raises(RecoveryError) as error:
        abandon_cleanup_transaction(
            user_data,
            home=home,
            profile_kind="desktop-primary",
            transaction_id=blocked.transaction_id,
            expected_revision=blocked.revision,
        )

    assert error.value.stable_code == "config_changed"
    assert journal.read_bytes() == replacement_data
    assert not tuple(user_data.glob(".opensquilla.profile-cleanup.abandoned.*.json"))


def test_abandon_cleanup_rolls_back_journal_swapped_at_move_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    home = _desktop_primary(user_data)
    journal = user_data / ".opensquilla.profile-cleanup.json"
    journal.write_bytes(b"original cleanup authority\n")
    blocked = inspect_profile(home, profile_kind="desktop-primary")
    replacement_data = b"replacement cleanup authority\n"
    real_move = cleanup_module.native_move_no_replace
    swapped = False

    def swap_then_move(source: Path, destination: Path) -> None:
        nonlocal swapped
        if source == journal and not swapped:
            swapped = True
            replacement = user_data / ".replacement-cleanup-journal.json"
            replacement.write_bytes(replacement_data)
            os.replace(replacement, journal)
        real_move(source, destination)

    monkeypatch.setattr(cleanup_module, "native_move_no_replace", swap_then_move)

    with pytest.raises(RecoveryError) as error:
        abandon_cleanup_transaction(
            user_data,
            home=home,
            profile_kind="desktop-primary",
            transaction_id=blocked.transaction_id,
            expected_revision=blocked.revision,
        )

    assert error.value.stable_code == "config_changed"
    assert journal.read_bytes() == replacement_data
    assert not tuple(user_data.glob(".opensquilla.profile-cleanup.abandoned.*.json"))


def test_abandon_cleanup_reports_unknown_when_swapped_move_cannot_be_rolled_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.cleanup as cleanup_module

    user_data = tmp_path / "user-data"
    user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    home = _desktop_primary(user_data)
    journal = user_data / ".opensquilla.profile-cleanup.json"
    journal.write_bytes(b"original cleanup authority\n")
    blocked = inspect_profile(home, profile_kind="desktop-primary")
    replacement_data = b"replacement cleanup authority\n"
    recreated_data = b"new canonical cleanup authority\n"
    real_move = cleanup_module.native_move_no_replace
    swapped = False

    def swap_move_and_recreate(source: Path, destination: Path) -> None:
        nonlocal swapped
        if source == journal and not swapped:
            swapped = True
            replacement = user_data / ".replacement-cleanup-journal.json"
            replacement.write_bytes(replacement_data)
            os.replace(replacement, journal)
            real_move(source, destination)
            journal.write_bytes(recreated_data)
            return
        real_move(source, destination)

    monkeypatch.setattr(
        cleanup_module,
        "native_move_no_replace",
        swap_move_and_recreate,
    )

    with pytest.raises(RecoveryError) as error:
        abandon_cleanup_transaction(
            user_data,
            home=home,
            profile_kind="desktop-primary",
            transaction_id=blocked.transaction_id,
            expected_revision=blocked.revision,
        )

    assert error.value.stable_code == "atomic_state_unknown"
    assert journal.read_bytes() == recreated_data
    preserved = tuple(user_data.glob(".opensquilla.profile-cleanup.abandoned.*.json"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == replacement_data


@pytest.mark.parametrize("profile_kind", ["desktop-primary", "desktop-recovery"])
def test_abandon_cleanup_rejects_profile_from_another_user_data_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile_kind: str,
) -> None:
    user_data = tmp_path / "selected-user-data"
    user_data.mkdir()
    foreign_user_data = tmp_path / "foreign-user-data"
    foreign_user_data.mkdir()
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))

    if profile_kind == "desktop-primary":
        foreign_home = _desktop_primary(foreign_user_data)
    else:
        recovery_id = str(uuid.uuid4())
        foreign_home = (
            _recovery_profile(foreign_user_data, recovery_id, "foreign recovery")
            / "opensquilla"
        )

    selected_journal = user_data / ".opensquilla.profile-cleanup.json"
    foreign_journal = foreign_user_data / ".opensquilla.profile-cleanup.json"
    selected_before = b"selected cleanup authority\n"
    foreign_before = b"foreign cleanup authority\n"
    selected_journal.write_bytes(selected_before)
    foreign_journal.write_bytes(foreign_before)
    blocked = inspect_profile(foreign_home, profile_kind=profile_kind)
    assert blocked.stable_code == "cleanup_transaction_incomplete"

    with pytest.raises(RecoveryError) as error:
        abandon_cleanup_transaction(
            user_data,
            home=foreign_home,
            profile_kind=profile_kind,
            transaction_id=blocked.transaction_id,
            expected_revision=blocked.revision,
        )

    assert error.value.stable_code == "cleanup_profile_selector_invalid"
    assert selected_journal.read_bytes() == selected_before
    assert foreign_journal.read_bytes() == foreign_before
    assert not tuple(user_data.glob(".opensquilla.profile-cleanup.abandoned.*.json"))
    assert not tuple(foreign_user_data.glob(".opensquilla.profile-cleanup.abandoned.*.json"))
