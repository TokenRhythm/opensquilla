from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from opensquilla.skills.hub.transaction import (
    SkillTransactionJournal,
    ensure_safe_transaction_roots,
    fsync_directory,
    recover_pending_skill_transaction,
    remove_transaction_journal,
    rollback_root,
    staging_root,
)


def _write_skill(path: Path, marker: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(marker, encoding="utf-8")


def _assert_transaction_id_removed(stage: Path, rollback: Path) -> None:
    assert not stage.parent.exists()
    assert not rollback.parent.exists()


def test_prepared_recovery_leaves_old_tree_and_lock_unchanged(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    target = managed / "example"
    stage = staging_root(managed) / "tx" / "example"
    rollback = rollback_root(managed) / "tx" / "example"
    lockfile = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(target, "old")
    _write_skill(stage, "new")
    old_lock = b'{"version":1,"installed":{"example":{"version":"old"}}}\n'
    lockfile.write_bytes(old_lock)
    journal = SkillTransactionJournal.prepare(
        operation="update",
        managed_dir=managed,
        name="example",
        target=target,
        staging=stage,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.write(journal_path)

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )

    assert [item.code for item in diagnostics] == ["TRANSACTION_RECOVERED"]
    assert diagnostics[0].details["phase"] == "prepared"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "old"
    assert lockfile.read_bytes() == old_lock
    assert not stage.exists()
    assert not rollback.exists()
    _assert_transaction_id_removed(stage, rollback)
    assert not journal_path.exists()


def test_pre_journal_crash_reservations_are_safely_swept(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    transaction_id = "a" * 32
    stage = staging_root(managed) / transaction_id
    rollback = rollback_root(managed) / transaction_id
    _write_skill(stage / "example", "candidate")
    rollback.mkdir(parents=True)

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=tmp_path / "missing-journal.json",
        sweep_orphan_staging=True,
    )

    assert [item.code for item in diagnostics] == ["ORPHAN_STAGING_RECOVERED"]
    assert not stage.exists()
    assert not rollback.exists()


def test_missing_journal_with_nonempty_rollback_requires_operator_recovery(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    transaction_id = "b" * 32
    stage = staging_root(managed) / transaction_id
    rollback = rollback_root(managed) / transaction_id
    _write_skill(stage / "example", "candidate")
    _write_skill(rollback / "example", "possibly-previous")

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=tmp_path / "missing-journal.json",
        sweep_orphan_staging=True,
    )

    assert [item.code for item in diagnostics] == ["RECOVERY_REQUIRED"]
    assert diagnostics[0].blocking is True
    assert (stage / "example" / "SKILL.md").read_text(encoding="utf-8") == "candidate"
    assert (rollback / "example" / "SKILL.md").read_text(encoding="utf-8") == (
        "possibly-previous"
    )


@pytest.mark.parametrize("operation", ["update", "uninstall"])
def test_recovery_infers_old_move_before_phase_advance(
    tmp_path: Path,
    operation: str,
) -> None:
    managed = tmp_path / "managed"
    target = managed / "example"
    stage = staging_root(managed) / "tx" / "example"
    rollback = rollback_root(managed) / "tx" / "example"
    lockfile = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(target, "old")
    _write_skill(stage, "new")
    old_lock = b'{"version":1,"installed":{"example":{}}}\n'
    lockfile.write_bytes(old_lock)
    journal = SkillTransactionJournal.prepare(
        operation=operation,
        managed_dir=managed,
        name="example",
        target=target,
        staging=stage,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.write(journal_path)
    rollback.parent.mkdir(parents=True, exist_ok=True)
    os.replace(target, rollback)
    # Simulate a crash between the atomic rename and advance("old_moved").

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )

    assert [item.code for item in diagnostics] == ["TRANSACTION_RECOVERED"]
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "old"
    assert lockfile.read_bytes() == old_lock
    assert not rollback.exists()
    _assert_transaction_id_removed(stage, rollback)
    assert not journal_path.exists()


def test_recovery_infers_new_move_before_phase_advance(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    target = managed / "example"
    stage = staging_root(managed) / "tx" / "example"
    rollback = rollback_root(managed) / "tx" / "example"
    lockfile = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(target, "old")
    _write_skill(stage, "new")
    old_lock = b'{"version":1,"installed":{"example":{}}}\n'
    lockfile.write_bytes(old_lock)
    journal = SkillTransactionJournal.prepare(
        operation="update",
        managed_dir=managed,
        name="example",
        target=target,
        staging=stage,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.write(journal_path)
    rollback.parent.mkdir(parents=True, exist_ok=True)
    os.replace(target, rollback)
    journal.advance("old_moved", journal_path)
    os.replace(stage, target)
    # Simulate a crash between the atomic publish and advance("new_moved").

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )

    assert [item.code for item in diagnostics] == ["TRANSACTION_RECOVERED"]
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "old"
    assert lockfile.read_bytes() == old_lock
    assert not rollback.exists()
    _assert_transaction_id_removed(stage, rollback)
    assert not journal_path.exists()


def test_install_recovery_removes_publish_before_phase_advance(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    target = managed / "example"
    stage = staging_root(managed) / "tx" / "example"
    rollback = rollback_root(managed) / "tx" / "example"
    lockfile = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(stage, "new")
    journal = SkillTransactionJournal.prepare(
        operation="install",
        managed_dir=managed,
        name="example",
        target=target,
        staging=stage,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.write(journal_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage, target)

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )

    assert [item.code for item in diagnostics] == ["TRANSACTION_RECOVERED"]
    assert not target.exists()
    assert not lockfile.exists()
    _assert_transaction_id_removed(stage, rollback)
    assert not journal_path.exists()


@pytest.mark.parametrize("phase", ["old_moved", "new_moved", "lock_written"])
def test_recovery_restores_old_tree_and_lock_for_interrupted_update(
    tmp_path: Path,
    phase: str,
) -> None:
    managed = tmp_path / "managed"
    target = managed / "example"
    stage = staging_root(managed) / "tx" / "example"
    rollback = rollback_root(managed) / "tx" / "example"
    lockfile = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(target, "old")
    _write_skill(stage, "new")
    lockfile.write_text('{"version":1,"installed":{"example":{}}}', encoding="utf-8")

    journal = SkillTransactionJournal.prepare(
        operation="update",
        managed_dir=managed,
        name="example",
        target=target,
        staging=stage,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.write(journal_path)
    rollback.parent.mkdir(parents=True, exist_ok=True)
    os.replace(target, rollback)
    journal.advance("old_moved", journal_path)
    if phase in {"new_moved", "lock_written"}:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, target)
        journal.advance("new_moved", journal_path)
    if phase == "lock_written":
        lockfile.write_text(
            '{"version":2,"installed":{"example":{"version":"new"}}}',
            encoding="utf-8",
        )
        journal.advance("lock_written", journal_path)

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )

    assert [item.code for item in diagnostics] == ["TRANSACTION_RECOVERED"]
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "old"
    assert '"version":1' in lockfile.read_text(encoding="utf-8")
    assert not rollback.exists()
    _assert_transaction_id_removed(stage, rollback)
    assert not journal_path.exists()


def test_committed_recovery_keeps_new_tree_and_cleans_reservations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.skills.hub import transaction as transaction_module

    managed = tmp_path / "managed"
    target = managed / "example"
    stage = staging_root(managed) / "tx" / "example"
    rollback = rollback_root(managed) / "tx" / "example"
    lockfile = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(target, "old")
    _write_skill(stage, "new")
    lockfile.write_text("old-lock", encoding="utf-8")
    journal = SkillTransactionJournal.prepare(
        operation="update",
        managed_dir=managed,
        name="example",
        target=target,
        staging=stage,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.write(journal_path)
    rollback.parent.mkdir(parents=True, exist_ok=True)
    os.replace(target, rollback)
    os.replace(stage, target)
    lockfile.write_text("new-lock", encoding="utf-8")
    journal.advance("committed", journal_path)
    real_remove = transaction_module.remove_transaction_journal
    removed: list[Path] = []

    def observe_committed_removal(path: Path) -> None:
        persisted = SkillTransactionJournal.load(path)
        assert persisted is not None
        assert persisted.phase == "committed"
        removed.append(path)
        real_remove(path)

    monkeypatch.setattr(
        transaction_module,
        "remove_transaction_journal",
        observe_committed_removal,
    )

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )

    assert [item.code for item in diagnostics] == ["TRANSACTION_RECOVERED"]
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "new"
    assert lockfile.read_text(encoding="utf-8") == "new-lock"
    assert not rollback.exists()
    _assert_transaction_id_removed(stage, rollback)
    assert not journal_path.exists()
    assert removed == [journal_path]


def test_recovery_preserves_nonempty_and_other_transaction_directories(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    target = managed / "example"
    stage = staging_root(managed) / "tx" / "example"
    rollback = rollback_root(managed) / "tx" / "example"
    lockfile = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(target, "old")
    _write_skill(stage, "new")
    lockfile.write_text("old-lock", encoding="utf-8")
    journal = SkillTransactionJournal.prepare(
        operation="update",
        managed_dir=managed,
        name="example",
        target=target,
        staging=stage,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.write(journal_path)
    rollback.parent.mkdir(parents=True, exist_ok=True)
    os.replace(target, rollback)
    os.replace(stage, target)
    journal.advance("committed", journal_path)
    (stage.parent / "keep-stage.txt").write_text("keep", encoding="utf-8")
    (rollback.parent / "keep-rollback.txt").write_text("keep", encoding="utf-8")
    other_staging = staging_root(managed) / "other-transaction"
    other_rollback = rollback_root(managed) / "other-transaction"
    other_staging.mkdir()
    other_rollback.mkdir()
    (other_staging / "keep.txt").write_text("keep", encoding="utf-8")
    (other_rollback / "keep.txt").write_text("keep", encoding="utf-8")

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )

    assert [item.code for item in diagnostics] == [
        "TRANSACTION_RECOVERED",
        "TRANSACTION_CLEANUP_PENDING",
    ]
    assert (stage.parent / "keep-stage.txt").read_text(encoding="utf-8") == "keep"
    assert (rollback.parent / "keep-rollback.txt").read_text(encoding="utf-8") == "keep"
    assert (other_staging / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (other_rollback / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert journal_path.exists()


def test_recovery_fails_closed_for_foreign_managed_root(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    other = tmp_path / "other"
    target = managed / "example"
    stage = staging_root(managed) / "tx" / "example"
    rollback = rollback_root(managed) / "tx" / "example"
    lockfile = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(stage, "new")
    journal = SkillTransactionJournal.prepare(
        operation="install",
        managed_dir=managed,
        name="example",
        target=target,
        staging=stage,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.write(journal_path)

    diagnostics = recover_pending_skill_transaction(
        managed_dir=other,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )

    assert diagnostics[0].code == "RECOVERY_REQUIRED"
    assert diagnostics[0].blocking is True
    assert journal_path.exists()
    assert stage.exists()


@pytest.mark.parametrize(
    "reserved_name",
    [".opensquilla-staging", ".opensquilla-rollback"],
)
def test_transaction_roots_reject_non_directory_entries(
    tmp_path: Path,
    reserved_name: str,
) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / reserved_name).write_text("occupied", encoding="utf-8")

    with pytest.raises(ValueError, match="ordinary directory"):
        ensure_safe_transaction_roots(managed)


def test_transaction_roots_reject_cross_device_reserved_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = tmp_path / "managed"
    ensure_safe_transaction_roots(managed)
    rollback_directory = rollback_root(managed)
    real_lstat = Path.lstat

    def cross_device_lstat(path: Path) -> os.stat_result:
        info = real_lstat(path)
        if path == rollback_directory:
            fields = list(info)
            fields[2] = int(info.st_dev) + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", cross_device_lstat)

    with pytest.raises(ValueError, match="different filesystem"):
        ensure_safe_transaction_roots(managed)


def test_directory_fsync_failure_is_strict_on_posix_and_best_effort_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(path: Path, flags: int) -> int:
        raise OSError("synthetic directory handle failure")

    monkeypatch.setattr(os, "open", fail_open)

    if os.name == "nt":
        fsync_directory(tmp_path)
    else:
        with pytest.raises(OSError, match="synthetic directory handle failure"):
            fsync_directory(tmp_path)


def test_transaction_journal_removal_unlinks_before_syncing_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.skills.hub import transaction as transaction_module

    journal_path = tmp_path / "state" / "transaction.json"
    journal_path.parent.mkdir()
    journal_path.write_text("journal", encoding="utf-8")
    synced: list[Path] = []

    def observe_parent_sync(directory: Path) -> None:
        assert not journal_path.exists()
        synced.append(directory)

    monkeypatch.setattr(transaction_module, "fsync_directory", observe_parent_sync)

    remove_transaction_journal(journal_path)

    assert synced == [journal_path.parent]


def test_recovery_rejects_journal_with_noncanonical_logical_layout(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    target = managed / "example"
    stage = staging_root(managed) / "tx" / "example"
    rollback = rollback_root(managed) / "tx" / "example"
    lockfile = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(stage, "new")
    journal = SkillTransactionJournal.prepare(
        operation="install",
        managed_dir=managed,
        name="example",
        target=target,
        staging=stage,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.staging = str(staging_root(managed) / "tx" / ".." / "other" / "example")
    journal.write(journal_path)

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )

    assert [item.code for item in diagnostics] == ["RECOVERY_REQUIRED"]
    assert "unsafe logical layout" in diagnostics[0].message
    assert journal_path.exists()
    assert (stage / "SKILL.md").read_text(encoding="utf-8") == "new"


def test_recovery_rejects_symlink_ancestor_below_reserved_root(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    target = managed / "example"
    stage = staging_root(managed) / "tx" / "example"
    rollback = rollback_root(managed) / "tx" / "example"
    lockfile = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(stage, "staged")
    journal = SkillTransactionJournal.prepare(
        operation="install",
        managed_dir=managed,
        name="example",
        target=target,
        staging=stage,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.write(journal_path)
    outside = tmp_path / "outside-transaction"
    _write_skill(outside / "example", "outside")
    shutil.rmtree(stage.parent)
    try:
        stage.parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )

    assert [item.code for item in diagnostics] == ["RECOVERY_REQUIRED"]
    assert "contains a symlink" in diagnostics[0].message
    assert journal_path.exists()
    assert (outside / "example" / "SKILL.md").read_text(encoding="utf-8") == "outside"


def test_recovery_rejects_symlinked_reserved_transaction_root(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    target = managed / "example"
    stage = staging_root(managed) / "tx" / "example"
    rollback = rollback_root(managed) / "tx" / "example"
    lockfile = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    _write_skill(stage, "outside")
    journal = SkillTransactionJournal.prepare(
        operation="install",
        managed_dir=managed,
        name="example",
        target=target,
        staging=stage,
        rollback=rollback,
        lockfile_path=lockfile,
    )
    journal.write(journal_path)
    outside = tmp_path / "outside-staging"
    os.replace(staging_root(managed), outside)
    try:
        staging_root(managed).symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal_path,
    )

    assert [item.code for item in diagnostics] == ["RECOVERY_REQUIRED"]
    assert "must not be a symlink" in diagnostics[0].message
    assert journal_path.exists()
    assert (outside / "tx" / "example" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "outside"
