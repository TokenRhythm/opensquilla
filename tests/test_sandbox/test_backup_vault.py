from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.sandbox.backup_vault import (
    BackupTooLarge,
    BackupUnavailable,
    BackupVault,
)


def test_recursive_directory_backup_preserves_tree_and_manifest(tmp_path: Path) -> None:
    vault = BackupVault(tmp_path / "vault")
    target = tmp_path / "project"
    (target / "nested").mkdir(parents=True)
    (target / "nested" / "data.txt").write_text("important", encoding="utf-8")

    receipt = vault.backup(target, quota_bytes=1024)

    assert receipt.original_path == str(target.resolve())
    assert receipt.size_bytes >= len("important")
    assert (receipt.entry_path / "content" / "nested" / "data.txt").read_text(
        encoding="utf-8"
    ) == "important"
    assert (receipt.entry_path / "manifest.json").is_file()


def test_quota_evicts_oldest_committed_backup(tmp_path: Path) -> None:
    vault = BackupVault(tmp_path / "vault")
    first = vault.commit_bytes("first", b"a" * 8, quota_bytes=16, created_at=1)
    second = vault.commit_bytes("second", b"b" * 8, quota_bytes=16, created_at=2)

    vault.enforce_quota(8)

    assert not first.entry_path.exists()
    assert second.entry_path.exists()


def test_oversize_backup_clears_old_entries_before_reporting_unavailable(
    tmp_path: Path,
) -> None:
    vault = BackupVault(tmp_path / "vault")
    existing = vault.commit_bytes("existing", b"a" * 8, quota_bytes=8)
    target = tmp_path / "large.bin"
    target.write_bytes(b"x" * 9)

    with pytest.raises(BackupTooLarge):
        vault.backup(target, quota_bytes=8)

    assert not existing.entry_path.exists()


def test_backup_many_stages_every_target_and_evicts_oldest_entries(
    tmp_path: Path,
) -> None:
    vault = BackupVault(tmp_path / "vault")
    old = vault.commit_bytes("old", b"o" * 8, quota_bytes=16, created_at=1)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"a" * 6)
    second.write_bytes(b"b" * 6)

    receipts = vault.backup_many((first, second), quota_bytes=12)

    assert not old.entry_path.exists()
    assert [receipt.original_path for receipt in receipts] == [
        str(first.resolve()),
        str(second.resolve()),
    ]
    assert (receipts[0].entry_path / "content").read_bytes() == b"a" * 6
    assert (receipts[1].entry_path / "content").read_bytes() == b"b" * 6


def test_backup_retries_after_clearing_old_entries_on_copy_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = BackupVault(tmp_path / "vault")
    old = vault.commit_bytes("old", b"old", quota_bytes=32)
    target = tmp_path / "target.txt"
    target.write_text("new", encoding="utf-8")
    original_stage = vault.stage
    attempts = 0

    def _flaky_stage(path: str | Path, *, created_at: int | None = None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("disk full")
        return original_stage(path, created_at=created_at)

    monkeypatch.setattr(vault, "stage", _flaky_stage)

    receipt = vault.backup(target, quota_bytes=32)

    assert attempts == 2
    assert not old.entry_path.exists()
    assert (receipt.entry_path / "content").read_text(encoding="utf-8") == "new"


def test_persistent_backup_io_failure_is_classified_after_old_entries_are_cleared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = BackupVault(tmp_path / "vault")
    old = vault.commit_bytes("old", b"old", quota_bytes=32)
    target = tmp_path / "target.txt"
    target.write_text("new", encoding="utf-8")

    def _failed_stage(path: str | Path, *, created_at: int | None = None):
        del path, created_at
        raise OSError("permission denied")

    monkeypatch.setattr(vault, "stage", _failed_stage)

    with pytest.raises(BackupUnavailable, match="backup_unavailable") as raised:
        vault.backup(target, quota_bytes=32)

    assert raised.value.reason == "io_error"
    assert not old.entry_path.exists()


def test_staging_content_is_not_counted_as_committed_backup(tmp_path: Path) -> None:
    vault = BackupVault(tmp_path / "vault")
    target = tmp_path / "data.txt"
    target.write_text("hello", encoding="utf-8")

    staged = vault.stage(target)

    assert vault.list_receipts() == ()
    staged.discard()
    assert not staged.staging_path.exists()
