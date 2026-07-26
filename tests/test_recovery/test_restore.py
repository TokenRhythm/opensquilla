from __future__ import annotations

import errno
import json
import multiprocessing
import os
import stat
import sys
import threading
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opensquilla.cli.recovery_cmd import recovery_app
from opensquilla.recovery import (
    AtomicStateUnknownError,
    DestinationExistsError,
    RestoreValidationError,
    inspect_profile,
)
from opensquilla.recovery.atomic import PathIdentity, no_follow_manifest
from opensquilla.recovery.errors import ProfileLockBusyError
from opensquilla.recovery.locking import ProfileOperationLock
from opensquilla.recovery.restore import _identity_payload, restore_profile
from opensquilla.recovery.transaction import (
    finalize_committed_profile_transaction,
    recover_profile_transaction,
)

runner = CliRunner()


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve())))


def _contend_for_restored_gateway(state_dir: str, queue: multiprocessing.Queue) -> None:
    from opensquilla.gateway.pidlock import GatewayPidLock

    lock = GatewayPidLock(state_dir)
    try:
        lock.acquire()
    except SystemExit:
        queue.put("busy")
    else:
        queue.put("acquired")
        lock.release()


def _profile(home: Path, value: str, *, with_legacy_lock: bool = False) -> None:
    workspace = home / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text(value + "\n", encoding="utf-8")
    state = home / "state"
    state.mkdir()
    if with_legacy_lock:
        (state / "gateway.pid.lock").write_bytes(b"synthetic-lock-authority\n")
    (home / "config.toml").write_text(
        'state_dir = "state"\nworkspace_dir = "workspace"\n',
        encoding="utf-8",
    )


def _profile_file_bytes(
    root: Path,
    manifest: dict[str, PathIdentity],
) -> dict[str, bytes]:
    return {
        relative: (root / relative).read_bytes()
        for relative, identity in manifest.items()
        if relative != "." and stat.S_ISREG(identity.mode)
    }


def _assert_profile_snapshot_unchanged(
    root: Path,
    before: dict[str, PathIdentity],
    file_bytes: dict[str, bytes],
) -> None:
    after = no_follow_manifest(root)
    assert after.keys() == before.keys()
    for relative, expected in before.items():
        current = after[relative]
        if sys.platform == "win32" and stat.S_ISDIR(expected.mode):
            # NTFS can publish a delayed directory mtime after a child handle
            # closes or a directory is atomically moved away and restored.
            # Recursive membership, directory identity/mode, every regular
            # file's full metadata, and file bytes remain the data contract.
            assert (current.device, current.inode, current.mode) == (
                expected.device,
                expected.inode,
                expected.mode,
            )
        else:
            assert current == expected
    assert {
        relative: (root / relative).read_bytes() for relative in file_bytes
    } == file_bytes


def _record_backup(target: Path, backup: Path, transaction_id: str) -> Path:
    history = target.parent / "profile-replacement-history.json"
    history.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "backups": [
                    {
                        "transaction_id": transaction_id,
                        "committed_at": "2026-07-11T00:00:00+00:00",
                        "source": _normalized_path(target.parent / "synthetic-source"),
                        "target": _normalized_path(target),
                        "backup": _normalized_path(backup),
                        "source_identity": _identity_payload(backup),
                        "target_identity": _identity_payload(target),
                        "backup_identity": _identity_payload(backup),
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return history


def test_profile_snapshot_comparison_ignores_only_windows_directory_mtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    _profile(profile, "original")
    before = no_follow_manifest(profile)
    file_bytes = _profile_file_bytes(profile, before)
    workspace = profile / "workspace"
    value = workspace.stat()
    os.utime(
        workspace,
        ns=(value.st_atime_ns, value.st_mtime_ns + 1_000_000_000),
    )
    monkeypatch.setattr(sys, "platform", "win32")

    _assert_profile_snapshot_unchanged(profile, before, file_bytes)

    (workspace / "SOUL.md").write_text("modified\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_profile_snapshot_unchanged(profile, before, file_bytes)


def test_restore_profile_swaps_recorded_backup_and_indexes_previous_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(backup, "recorded backup", with_legacy_lock=True)
    history_path = _record_backup(target, backup, transaction_id)

    report = restore_profile(backup)

    assert report.outcome == "ready"
    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == ("recorded backup\n")
    assert not backup.exists()
    parked = [path for path in tmp_path.glob("opensquilla.backup.*") if path != backup]
    assert len(parked) == 1
    assert (parked[0] / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "current\n"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert [entry["backup"] for entry in history["backups"]] == [
        _normalized_path(backup),
        _normalized_path(parked[0]),
    ]
    assert history["backups"][0]["restored_to"] == _normalized_path(target)
    assert history["backups"][0]["consumed_by_transaction_id"]
    assert not (tmp_path / ".opensquilla.profile-replace.json").exists()


def test_restore_profile_preserves_workspace_links_and_opaque_code_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(backup, "recorded backup", with_legacy_lock=True)

    def add_links(profile: Path) -> None:
        code_task_bin = profile / "code-task" / "run" / "repo" / ".venv" / "bin"
        code_task_bin.mkdir(parents=True)
        (profile / "workspace" / "SOUL.alias.md").symlink_to("SOUL.md")
        historical_workspace = profile / "state" / "workspace"
        historical_workspace.mkdir(parents=True, exist_ok=True)
        (historical_workspace / "COPYING").write_text("license\n", encoding="utf-8")
        (historical_workspace / "LICENSE").symlink_to("COPYING")
        (code_task_bin / "python").symlink_to("../../../../missing-python")

    try:
        add_links(target)
        add_links(backup)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    _record_backup(target, backup, transaction_id)

    report = restore_profile(backup)

    # Both canonical and historical workspaces intentionally remain present,
    # so post-restore reconciliation asks the user to resolve that ambiguity.
    assert report.outcome == "attention"
    assert report.stable_code == "workspace_conflict"
    assert os.readlink(target / "workspace" / "SOUL.alias.md") == "SOUL.md"
    assert os.readlink(target / "state" / "workspace" / "LICENSE") == "COPYING"
    assert os.readlink(
        target / "code-task" / "run" / "repo" / ".venv" / "bin" / "python"
    ) == "../../../../missing-python"
    parked = [path for path in tmp_path.glob("opensquilla.backup.*") if path != backup]
    assert len(parked) == 1
    assert os.readlink(parked[0] / "workspace" / "SOUL.alias.md") == "SOUL.md"
    assert os.readlink(parked[0] / "state" / "workspace" / "LICENSE") == "COPYING"
    assert os.readlink(
        parked[0] / "code-task" / "run" / "repo" / ".venv" / "bin" / "python"
    ) == "../../../../missing-python"


def test_restore_cli_uses_history_target_and_primary_profile_kind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-recovery")
    target = tmp_path / "custom-primary-name"
    transaction_id = str(uuid.uuid4())
    backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(backup, "recorded backup", with_legacy_lock=True)
    _record_backup(target, backup, transaction_id)

    result = runner.invoke(
        recovery_app,
        ["restore-profile", "--backup", str(backup), "--json"],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["primary_home"] == _normalized_path(target)
    assert payload["outcome"] == "ready"
    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == (
        "recorded backup\n"
    )


def test_restore_contends_with_desktop_global_cleanup_lock(tmp_path: Path) -> None:
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(backup, "recorded backup", with_legacy_lock=True)
    history = _record_backup(target, backup, transaction_id)
    history_before = history.read_bytes()
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
        with pytest.raises(ProfileLockBusyError):
            restore_profile(backup)
    finally:
        release.set()
        holder.join(timeout=5)

    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "current\n"
    assert (backup / "workspace" / "SOUL.md").read_text(encoding="utf-8") == (
        "recorded backup\n"
    )
    assert history.read_bytes() == history_before
    assert not (tmp_path / ".opensquilla.profile-replace.json").exists()


def test_committed_restore_journal_finalize_never_overwrites_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    selected_backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(selected_backup, "recorded backup", with_legacy_lock=True)
    _record_backup(target, selected_backup, transaction_id)
    journal = tmp_path / ".opensquilla.profile-replace.json"
    restore_id = "00000000-0000-0000-0000-000000000001"
    original_uuid4 = uuid.uuid4
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(restore_id))
    committed_journal = tmp_path / f".opensquilla.profile-replace.{restore_id}.committed.json"
    collision = b"existing recovery authority\n"
    committed_journal.write_bytes(collision)

    with pytest.raises(DestinationExistsError):
        restore_profile(selected_backup)

    monkeypatch.setattr(uuid, "uuid4", original_uuid4)

    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["phase"] == "committed"
    assert committed_journal.read_bytes() == collision
    complete = inspect_profile(target, profile_kind="desktop-primary")
    assert complete.outcome == "ready"

    committed_journal.unlink()
    assert finalize_committed_profile_transaction(target) is True
    assert not journal.exists()
    assert json.loads(committed_journal.read_text(encoding="utf-8"))["phase"] == "committed"


def test_restore_missing_backup_lock_authority_fails_without_mutating_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(backup, "recorded backup")
    _record_backup(target, backup, transaction_id)
    backup_before = no_follow_manifest(backup)
    backup_file_bytes = _profile_file_bytes(backup, backup_before)

    with pytest.raises(
        RestoreValidationError,
        match="pre-existing legacy gateway lock",
    ) as exc_info:
        restore_profile(backup)

    assert exc_info.value.stable_code == "restore_backup_lock_authority_missing"
    _assert_profile_snapshot_unchanged(backup, backup_before, backup_file_bytes)
    assert not (backup / "state" / "gateway.pid.lock").exists()
    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "current\n"
    assert not (tmp_path / ".opensquilla.profile-replace.json").exists()


def test_restore_holds_candidate_legacy_lock_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.restore as restore_module

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(backup, "recorded backup", with_legacy_lock=True)
    _record_backup(target, backup, transaction_id)
    assert (backup / "state" / "gateway.pid.lock").read_bytes() == (
        b"synthetic-lock-authority\n"
    )

    original_validate = restore_module._validate_restored_target
    observations: list[str] = []

    def validate_while_old_gateway_contends(candidate: Path):
        context = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
        queue = context.Queue()
        process = context.Process(
            target=_contend_for_restored_gateway,
            args=(str(candidate / "state"), queue),
        )
        process.start()
        process.join(timeout=10)
        assert process.exitcode == 0
        observations.append(queue.get(timeout=1))
        return original_validate(candidate)

    monkeypatch.setattr(
        restore_module,
        "_validate_restored_target",
        validate_while_old_gateway_contends,
    )

    report = restore_profile(backup)

    assert report.outcome == "ready"
    assert observations == ["busy"]
    assert (target / "state" / "gateway.pid.lock").read_bytes() == (
        b"synthetic-lock-authority\n"
    )


def test_restore_validation_failure_rolls_back_both_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.restore as restore_module

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(backup, "recorded backup", with_legacy_lock=True)
    history_path = _record_backup(target, backup, transaction_id)
    history_before = history_path.read_bytes()
    backup_before = no_follow_manifest(backup)
    backup_file_bytes = _profile_file_bytes(backup, backup_before)

    def fail_validation(_target: Path):
        raise RestoreValidationError("synthetic validation failure")

    monkeypatch.setattr(restore_module, "_validate_restored_target", fail_validation)

    with pytest.raises(RestoreValidationError, match="synthetic"):
        restore_profile(backup)

    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "current\n"
    assert (backup / "workspace" / "SOUL.md").read_text(encoding="utf-8") == ("recorded backup\n")
    _assert_profile_snapshot_unchanged(backup, backup_before, backup_file_bytes)
    assert history_path.read_bytes() == history_before
    assert not (tmp_path / ".opensquilla.profile-replace.json").exists()


@pytest.mark.parametrize("failure_phase", ["target_parking", "backup_publication"])
def test_restore_post_move_unknown_state_preserves_journal_and_observed_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_phase: str,
) -> None:
    import opensquilla.recovery.restore as restore_module

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    selected_backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(selected_backup, "recorded backup", with_legacy_lock=True)
    history_path = _record_backup(target, selected_backup, transaction_id)
    history_before = history_path.read_bytes()
    original_move = restore_module.native_move_no_replace

    def move_then_lose_post_state(
        source: Path,
        destination: Path,
        **move_options: object,
    ) -> None:
        is_parking = source == target and ".backup." in destination.name
        is_publication = source == selected_backup and destination == target
        original_move(source, destination, **move_options)
        if (
            failure_phase == "target_parking"
            and is_parking
            or failure_phase == "backup_publication"
            and is_publication
        ):
            raise AtomicStateUnknownError("synthetic post-move state is unknown")

    monkeypatch.setattr(
        restore_module,
        "native_move_no_replace",
        move_then_lose_post_state,
    )

    with pytest.raises(AtomicStateUnknownError):
        restore_profile(selected_backup)

    journal = tmp_path / ".opensquilla.profile-replace.json"
    assert journal.is_file()
    payload = json.loads(journal.read_text(encoding="utf-8"))
    current_backup = Path(payload["backup"])
    assert history_path.read_bytes() == history_before
    if failure_phase == "target_parking":
        assert not target.exists()
        assert selected_backup.is_dir()
        assert (current_backup / "workspace" / "SOUL.md").read_text(
            encoding="utf-8"
        ) == "current\n"
    else:
        assert not selected_backup.exists()
        assert (target / "workspace" / "SOUL.md").read_text(
            encoding="utf-8"
        ) == "recorded backup\n"
        assert (current_backup / "workspace" / "SOUL.md").read_text(
            encoding="utf-8"
        ) == "current\n"
    report = inspect_profile(target, profile_kind="desktop-primary")
    assert report.outcome == "recovery_required"
    assert report.stable_code == "transaction_incomplete"


def test_restore_rejects_unrecorded_or_renamed_backup_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    recorded = target.with_name(f"{target.name}.backup.{transaction_id}")
    unrecorded = target.with_name(f"{target.name}.backup.{uuid.uuid4()}")
    _profile(target, "current")
    _profile(recorded, "recorded backup")
    _profile(unrecorded, "unrecorded")
    _record_backup(target, recorded, transaction_id)

    with pytest.raises(RestoreValidationError, match="not uniquely recorded"):
        restore_profile(unrecorded)

    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "current\n"
    assert (unrecorded / "workspace" / "SOUL.md").read_text(encoding="utf-8") == ("unrecorded\n")


def test_restore_history_write_failure_rolls_back_profiles_and_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.restore as restore_module

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(backup, "recorded backup", with_legacy_lock=True)
    history_path = _record_backup(target, backup, transaction_id)
    history_before = history_path.read_bytes()
    original_replace_json = restore_module._replace_json

    def fail_history(path: Path, payload: dict, *, mode: int = 0o600) -> None:
        if path.name == "profile-replacement-history.json":
            raise OSError(errno.ENOSPC, "synthetic disk full while writing history")
        original_replace_json(path, payload, mode=mode)

    monkeypatch.setattr(restore_module, "_replace_json", fail_history)

    with pytest.raises(RestoreValidationError, match="could not be completed"):
        restore_profile(backup)

    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "current\n"
    assert (backup / "workspace" / "SOUL.md").read_text(encoding="utf-8") == ("recorded backup\n")
    assert history_path.read_bytes() == history_before
    assert not (tmp_path / ".opensquilla.profile-replace.json").exists()


def test_restore_history_post_publish_sync_failure_rolls_back_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.restore as restore_module

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(backup, "recorded backup", with_legacy_lock=True)
    history_path = _record_backup(target, backup, transaction_id)
    history_before = history_path.read_bytes()
    original_fsync_directory = restore_module._fsync_directory
    failed_after_publication = False

    def fail_once_after_history_publication(path: Path) -> None:
        nonlocal failed_after_publication
        if (
            not failed_after_publication
            and path == history_path.parent
            and history_path.read_bytes() != history_before
        ):
            failed_after_publication = True
            raise OSError(errno.EIO, "synthetic directory sync failure after history replace")
        original_fsync_directory(path)

    monkeypatch.setattr(
        restore_module,
        "_fsync_directory",
        fail_once_after_history_publication,
    )

    with pytest.raises(RestoreValidationError, match="could not be completed"):
        restore_profile(backup)

    assert failed_after_publication is True
    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "current\n"
    assert (backup / "workspace" / "SOUL.md").read_text(encoding="utf-8") == (
        "recorded backup\n"
    )
    assert history_path.read_bytes() == history_before
    assert not (tmp_path / ".opensquilla.profile-replace.json").exists()


def test_restore_journal_post_publish_sync_failure_is_atomic_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.recovery.restore as restore_module

    journal = tmp_path / ".opensquilla.profile-replace.json"
    journal.write_text('{"phase":"prepared"}\n', encoding="utf-8")
    payload = {"phase": "validated", "transaction_id": str(uuid.uuid4())}

    def fail_directory_sync(_path: Path) -> None:
        raise OSError("synthetic journal directory fsync failure")

    monkeypatch.setattr(restore_module, "_fsync_directory", fail_directory_sync)

    with pytest.raises(AtomicStateUnknownError):
        restore_module._replace_journal_json(journal, payload)

    assert json.loads(journal.read_text(encoding="utf-8")) == payload


def test_restore_commit_write_failure_rolls_back_published_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.recovery.restore as restore_module

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(backup, "recorded backup", with_legacy_lock=True)
    history_path = _record_backup(target, backup, transaction_id)
    history_before = history_path.read_bytes()
    original_replace_json = restore_module._replace_json

    def fail_commit(path: Path, payload: dict, *, mode: int = 0o600) -> None:
        if path.name == ".opensquilla.profile-replace.json" and payload.get("phase") == "committed":
            raise OSError("synthetic journal commit failure")
        original_replace_json(path, payload, mode=mode)

    monkeypatch.setattr(restore_module, "_replace_json", fail_commit)

    with pytest.raises(RestoreValidationError, match="could not be completed"):
        restore_profile(backup)

    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "current\n"
    assert (backup / "workspace" / "SOUL.md").read_text(encoding="utf-8") == ("recorded backup\n")
    assert history_path.read_bytes() == history_before
    assert not (tmp_path / ".opensquilla.profile-replace.json").exists()


@pytest.mark.parametrize("history_published", [False, True])
def test_validated_restore_crash_recovers_by_rolling_back_history_and_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    history_published: bool,
) -> None:
    import opensquilla.recovery.restore as restore_module

    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    target = tmp_path / "opensquilla"
    transaction_id = str(uuid.uuid4())
    selected = target.with_name(f"{target.name}.backup.{transaction_id}")
    _profile(target, "current")
    _profile(selected, "selected", with_legacy_lock=True)
    history_path = _record_backup(target, selected, transaction_id)
    history_before = history_path.read_bytes()
    original_replace_journal = restore_module._replace_journal_json

    def crash_at_validated_boundary(path: Path, payload: dict) -> None:
        if not history_published and payload.get("phase") == "validated":
            original_replace_journal(path, payload)
            raise AtomicStateUnknownError("synthetic crash before history publication")
        if history_published and payload.get("phase") == "committed":
            raise AtomicStateUnknownError("synthetic crash after history publication")
        original_replace_journal(path, payload)

    monkeypatch.setattr(
        restore_module,
        "_replace_journal_json",
        crash_at_validated_boundary,
    )

    with pytest.raises(AtomicStateUnknownError):
        restore_profile(selected)

    journal = tmp_path / ".opensquilla.profile-replace.json"
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["phase"] == "validated"
    before = inspect_profile(target, profile_kind="desktop-primary")
    result = recover_profile_transaction(
        target,
        transaction_id=before.transaction_id,
        expected_revision=before.revision,
    )

    assert result.outcome == "ready"
    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "current\n"
    assert (selected / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "selected\n"
    assert json.loads(history_path.read_bytes()) == json.loads(history_before)
    assert not journal.exists()
