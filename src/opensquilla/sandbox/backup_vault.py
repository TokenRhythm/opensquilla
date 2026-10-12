"""Staged, atomic recursive-delete backups with bounded oldest-first eviction."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


class BackupUnavailable(RuntimeError):  # noqa: N818 - public domain name
    """Backup could not be created even after old entries were removed."""

    def __init__(self, *, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(f"backup_unavailable: {self.reason}")


class BackupTooLarge(BackupUnavailable):  # noqa: N818 - public domain name
    def __init__(self, *, size_bytes: int, quota_bytes: int) -> None:
        self.size_bytes = int(size_bytes)
        self.quota_bytes = int(quota_bytes)
        self.reason = "quota_exceeded"
        RuntimeError.__init__(
            self,
            f"recursive_backup_too_large: {self.size_bytes} > {self.quota_bytes}"
        )


@dataclass(frozen=True)
class BackupReceipt:
    backup_id: str
    original_path: str
    entry_path: Path
    size_bytes: int
    created_at: int


BackupReceiptSummary = dict[str, str | int]


def summarize_backup_receipts(
    receipts: tuple[BackupReceipt, ...] | list[BackupReceipt],
) -> tuple[BackupReceiptSummary, ...]:
    """Return model-safe backup metadata without exposing vault authority paths."""
    return tuple(
        {
            "backupId": receipt.backup_id,
            "target": receipt.original_path,
            "sizeBytes": receipt.size_bytes,
            "createdAt": receipt.created_at,
        }
        for receipt in receipts
    )


def _tree_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        try:
            total += child.lstat().st_size if child.is_file() or child.is_symlink() else 0
        except OSError:
            continue
    return total


class StagedBackup:
    def __init__(
        self,
        vault: BackupVault,
        *,
        backup_id: str,
        original_path: Path,
        staging_path: Path,
        size_bytes: int,
        created_at: int,
    ) -> None:
        self._vault = vault
        self.backup_id = backup_id
        self.original_path = original_path
        self.staging_path = staging_path
        self.size_bytes = int(size_bytes)
        self.created_at = int(created_at)

    def publish(self, *, quota_bytes: int) -> BackupReceipt:
        self._vault.evict_for_capacity(
            required_bytes=self.size_bytes,
            quota_bytes=int(quota_bytes),
        )
        if self.size_bytes > int(quota_bytes):
            self.discard()
            raise BackupTooLarge(
                size_bytes=self.size_bytes,
                quota_bytes=int(quota_bytes),
            )
        manifest = {
            "backupId": self.backup_id,
            "originalPath": str(self.original_path),
            "sizeBytes": self.size_bytes,
            "createdAt": self.created_at,
        }
        (self.staging_path / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        entry_path = self._vault.entries_root / self.backup_id
        os.replace(self.staging_path, entry_path)
        receipt = BackupReceipt(
            backup_id=self.backup_id,
            original_path=str(self.original_path),
            entry_path=entry_path,
            size_bytes=self.size_bytes,
            created_at=self.created_at,
        )
        self._vault.enforce_quota(int(quota_bytes), preserve_id=self.backup_id)
        return receipt

    def discard(self) -> None:
        if self.staging_path.exists():
            shutil.rmtree(self.staging_path)


class BackupVault:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().absolute()
        self.staging_root = self.root / "staging"
        self.entries_root = self.root / "entries"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.entries_root.mkdir(parents=True, exist_ok=True)

    def stage(
        self,
        target: str | Path,
        *,
        created_at: int | None = None,
    ) -> StagedBackup:
        source = Path(target).expanduser().absolute()
        if not source.exists() and not source.is_symlink():
            raise FileNotFoundError(source)
        backup_id = f"{int(created_at or time.time())}-{secrets.token_hex(8)}"
        staging_path = self.staging_root / backup_id
        staging_path.mkdir()
        content = staging_path / "content"
        try:
            if source.is_symlink():
                content.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
            elif source.is_dir():
                shutil.copytree(source, content, symlinks=True)
            else:
                shutil.copy2(source, content, follow_symlinks=False)
        except Exception:
            shutil.rmtree(staging_path, ignore_errors=True)
            raise
        return StagedBackup(
            self,
            backup_id=backup_id,
            original_path=source,
            staging_path=staging_path,
            size_bytes=_tree_size(content),
            created_at=int(created_at or time.time()),
        )

    def backup(
        self,
        target: str | Path,
        *,
        quota_bytes: int,
    ) -> BackupReceipt:
        return self.backup_many((target,), quota_bytes=quota_bytes)[0]

    def backup_many(
        self,
        targets: tuple[str | Path, ...] | list[str | Path],
        *,
        quota_bytes: int,
    ) -> tuple[BackupReceipt, ...]:
        """Back up all targets, clearing old entries and retrying I/O once."""

        normalized = tuple(targets)
        if not normalized:
            return ()
        last_error: OSError | None = None
        for attempt in range(2):
            staged: list[StagedBackup] = []
            try:
                staged = [self.stage(target) for target in normalized]
                total_size = sum(item.size_bytes for item in staged)
                self.evict_for_capacity(
                    required_bytes=total_size,
                    quota_bytes=int(quota_bytes),
                )
                if total_size > int(quota_bytes):
                    raise BackupTooLarge(
                        size_bytes=total_size,
                        quota_bytes=int(quota_bytes),
                    )
                return tuple(
                    item.publish(quota_bytes=int(quota_bytes)) for item in staged
                )
            except BackupTooLarge:
                for item in staged:
                    item.discard()
                raise
            except OSError as exc:
                last_error = exc
                for item in staged:
                    item.discard()
                try:
                    self.clear_committed_backups()
                except OSError as cleanup_error:
                    raise BackupUnavailable(reason="cleanup_failed") from cleanup_error
                if attempt == 0:
                    continue
        raise BackupUnavailable(reason="io_error") from last_error

    def commit_bytes(
        self,
        name: str,
        payload: bytes,
        *,
        quota_bytes: int,
        created_at: int | None = None,
    ) -> BackupReceipt:
        backup_id = f"{int(created_at or time.time())}-{secrets.token_hex(8)}"
        staging_path = self.staging_root / backup_id
        staging_path.mkdir()
        (staging_path / "content").write_bytes(payload)
        staged = StagedBackup(
            self,
            backup_id=backup_id,
            original_path=Path(name),
            staging_path=staging_path,
            size_bytes=len(payload),
            created_at=int(created_at or time.time()),
        )
        return staged.publish(quota_bytes=quota_bytes)

    def list_receipts(self) -> tuple[BackupReceipt, ...]:
        receipts: list[BackupReceipt] = []
        for entry in self.entries_root.iterdir():
            manifest_path = entry / "manifest.json"
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                receipts.append(
                    BackupReceipt(
                        backup_id=str(raw["backupId"]),
                        original_path=str(raw["originalPath"]),
                        entry_path=entry,
                        size_bytes=int(raw["sizeBytes"]),
                        created_at=int(raw["createdAt"]),
                    )
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(sorted(receipts, key=lambda item: (item.created_at, item.backup_id)))

    def enforce_quota(
        self,
        quota_bytes: int,
        *,
        preserve_id: str | None = None,
    ) -> None:
        receipts = list(self.list_receipts())
        total = sum(receipt.size_bytes for receipt in receipts)
        for receipt in receipts:
            if total <= int(quota_bytes):
                break
            if receipt.backup_id == preserve_id:
                continue
            shutil.rmtree(receipt.entry_path)
            total -= receipt.size_bytes

    def evict_for_capacity(self, *, required_bytes: int, quota_bytes: int) -> None:
        """Delete oldest committed backups until a new backup could fit."""

        required = max(0, int(required_bytes))
        quota = max(0, int(quota_bytes))
        receipts = list(self.list_receipts())
        total = sum(receipt.size_bytes for receipt in receipts)
        for receipt in receipts:
            if total + required <= quota:
                break
            shutil.rmtree(receipt.entry_path)
            total -= receipt.size_bytes

    def clear_committed_backups(self) -> None:
        """Remove committed backup content to recover space for one retry."""

        for entry in tuple(self.entries_root.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)


__all__ = [
    "BackupReceipt",
    "BackupTooLarge",
    "BackupUnavailable",
    "BackupVault",
    "StagedBackup",
]
