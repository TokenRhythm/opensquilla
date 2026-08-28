"""Exact one-use structured file mutations with identity revalidation."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from opensquilla.sandbox.backup_vault import (
    BackupReceipt,
    BackupTooLarge,
    BackupUnavailable,
    BackupVault,
)
from opensquilla.sandbox.file_policy import FileDecision, decide_file_access
from opensquilla.sandbox.policy_models import SandboxPolicy

RECURSIVE_DELETE_WARNING = (
    "递归删除会永久删除该目录及其中的全部文件和子目录，无法撤回。"
    "如果已开启文件安全备份，OpenSquilla 会在删除前创建可恢复副本，"
    "并可能自动清理最旧的备份以腾出空间；如果仍无法备份，会再次明确询问是否无备份继续。"
)
FILE_DELETE_WARNING = (
    "删除会永久移除该文件，无法撤回。"
    "如果已开启文件安全备份，OpenSquilla 会在删除前创建可恢复副本，"
    "并可能自动清理最旧的备份以腾出空间；如果仍无法备份，会再次明确询问是否无备份继续。"
)
OVERSIZE_BACKUP_WARNING = (
    "该对象的备份大小为 {size_bytes} 字节，超过备份空间上限 {quota_bytes} 字节。"
    "继续将删除目标且不会创建备份，此操作不可撤回。"
)
UNAVAILABLE_BACKUP_WARNING = (
    "OpenSquilla 已自动清理旧备份，但仍无法为该目标创建新备份。"
    "继续将删除目标且不会创建备份，此操作不可撤回。"
)
BACKUP_STORAGE_UNAVAILABLE_WARNING = (
    "文件安全备份存储当前不可用，或旧备份无法完成清理。"
    "继续将删除目标且不会创建备份，此操作不可撤回。"
)


class ApprovalRequired(RuntimeError):  # noqa: N818 - public domain name
    pass


class ObjectIdentityChanged(RuntimeError):  # noqa: N818 - public domain name
    pass


class MutationDenied(RuntimeError):  # noqa: N818 - public domain name
    pass


@dataclass(frozen=True)
class ObjectIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    mode: int

    @classmethod
    def capture(cls, path: Path) -> ObjectIdentity:
        stat = path.lstat()
        return cls(
            device=int(stat.st_dev),
            inode=int(stat.st_ino),
            size=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
            mode=int(stat.st_mode),
        )


def _tree_digest(path: Path) -> str:
    """Fingerprint one file tree without following symlinks."""

    digest = hashlib.sha256()

    def _visit(current: Path, relative: str) -> None:
        metadata = current.lstat()
        digest.update(relative.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(
            json.dumps(
                {
                    "device": int(metadata.st_dev),
                    "inode": int(metadata.st_ino),
                    "size": int(metadata.st_size),
                    "mtimeNs": int(metadata.st_mtime_ns),
                    "mode": int(metadata.st_mode),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\0")
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(os.readlink(current).encode("utf-8", errors="surrogatepass"))
            return
        if stat.S_ISREG(metadata.st_mode):
            with current.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return
        if stat.S_ISDIR(metadata.st_mode):
            for child in sorted(current.iterdir(), key=lambda item: item.name):
                child_relative = f"{relative}/{child.name}" if relative else child.name
                _visit(child, child_relative)

    _visit(path, ".")
    return digest.hexdigest()


@dataclass(frozen=True)
class MutationPlan:
    operation: Literal["delete"]
    target: Path
    recursive: bool
    target_identity: ObjectIdentity
    parent_identity: ObjectIdentity
    tree_digest: str
    approval_required: bool
    warning: str | None
    policy_version: int
    approval_token: str | None = None
    backup_override_token: str | None = None

    def fingerprint(self) -> str:
        payload = {
            "operation": self.operation,
            "target": str(self.target),
            "recursive": self.recursive,
            "targetIdentity": self.target_identity.__dict__,
            "parentIdentity": self.parent_identity.__dict__,
            "treeDigest": self.tree_digest,
            "policyVersion": self.policy_version,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class MutationResult:
    deleted: bool
    backup: BackupReceipt | None = None


class FileMutationBroker:
    def __init__(
        self,
        *,
        policy: SandboxPolicy,
        vault: BackupVault | None = None,
        vault_factory: Callable[[], BackupVault] | None = None,
        authority_roots: tuple[Path, ...] = (),
    ) -> None:
        self._policy = policy.model_copy(deep=True)
        self._vault = vault
        self._vault_factory = vault_factory
        self._authority_roots = authority_roots
        self._approvals: dict[str, str] = {}
        self._backup_overrides: dict[str, tuple[str, str]] = {}

    def plan_delete(
        self,
        target: str | Path,
        *,
        recursive: bool = False,
    ) -> MutationPlan:
        path = Path(target).expanduser().absolute()
        if not path.exists() and not path.is_symlink():
            raise FileNotFoundError(path)
        if recursive and not path.is_dir():
            raise NotADirectoryError(path)
        decision: FileDecision = decide_file_access(
            "delete",
            path,
            self._policy,
            authority_roots=self._authority_roots,
        )
        if not decision.allowed and not decision.approval_required:
            raise MutationDenied(decision.code or "file_mutation_denied")
        approval_required = True
        return MutationPlan(
            operation="delete",
            target=path,
            recursive=recursive,
            target_identity=ObjectIdentity.capture(path),
            parent_identity=ObjectIdentity.capture(path.parent),
            tree_digest=_tree_digest(path),
            approval_required=approval_required,
            warning=RECURSIVE_DELETE_WARNING if recursive else FILE_DELETE_WARNING,
            policy_version=self._policy.policy_version,
        )

    def approve(self, plan: MutationPlan) -> MutationPlan:
        token = secrets.token_urlsafe(24)
        approved = replace(plan, approval_token=token)
        self._approvals[token] = approved.fingerprint()
        return approved

    def approve_without_backup(
        self,
        plan: MutationPlan,
        error: BackupUnavailable,
    ) -> MutationPlan:
        """Issue exact one-use approval for a delete that cannot be backed up."""
        if isinstance(error, BackupTooLarge) and error.size_bytes <= error.quota_bytes:
            raise ValueError("backup override requires an oversize backup")
        approval_token = secrets.token_urlsafe(24)
        override_token = secrets.token_urlsafe(24)
        approved = replace(
            plan,
            approval_token=approval_token,
            backup_override_token=override_token,
            warning=(
                OVERSIZE_BACKUP_WARNING.format(
                    size_bytes=error.size_bytes,
                    quota_bytes=error.quota_bytes,
                )
                if isinstance(error, BackupTooLarge)
                else UNAVAILABLE_BACKUP_WARNING
                if error.reason == "io_error"
                else BACKUP_STORAGE_UNAVAILABLE_WARNING
            ),
        )
        fingerprint = approved.fingerprint()
        self._approvals[approval_token] = fingerprint
        self._backup_overrides[override_token] = (fingerprint, self._backup_error_key(error))
        return approved

    @staticmethod
    def _backup_error_key(error: BackupUnavailable) -> str:
        if isinstance(error, BackupTooLarge):
            return f"quota:{error.size_bytes}:{error.quota_bytes}"
        return f"reason:{error.reason}"

    def _consume_approval(self, plan: MutationPlan) -> None:
        if not plan.approval_required:
            return
        token = plan.approval_token
        expected = self._approvals.pop(str(token), None) if token else None
        if expected is None or not secrets.compare_digest(expected, plan.fingerprint()):
            raise ApprovalRequired("file mutation requires an exact one-use approval")

    def _consume_backup_override(
        self,
        plan: MutationPlan,
        error: BackupUnavailable,
    ) -> bool:
        token = plan.backup_override_token
        expected = self._backup_overrides.pop(str(token), None) if token else None
        if expected is None:
            return False
        fingerprint, error_key = expected
        return (
            secrets.compare_digest(fingerprint, plan.fingerprint())
            and secrets.compare_digest(error_key, self._backup_error_key(error))
        )

    def _backup_vault(self) -> BackupVault:
        if self._vault is not None:
            return self._vault
        if self._vault_factory is None:
            raise BackupUnavailable(reason="backup_vault_unavailable")
        try:
            self._vault = self._vault_factory()
        except BackupUnavailable:
            raise
        except OSError as exc:
            raise BackupUnavailable(reason="backup_vault_unavailable") from exc
        return self._vault

    @staticmethod
    def _verify_identity(plan: MutationPlan) -> None:
        try:
            current_target = ObjectIdentity.capture(plan.target)
            current_parent = ObjectIdentity.capture(plan.target.parent)
        except OSError as exc:
            raise ObjectIdentityChanged("file object changed after approval") from exc
        if (
            current_target != plan.target_identity
            or current_parent != plan.parent_identity
            or _tree_digest(plan.target) != plan.tree_digest
        ):
            raise ObjectIdentityChanged("file object changed after approval")

    def execute(self, plan: MutationPlan) -> MutationResult:
        self._consume_approval(plan)
        self._verify_identity(plan)
        backup: BackupReceipt | None = None
        if self._policy.files.recursive_delete_backup_enabled:
            try:
                backup = self._backup_vault().backup_many(
                    (plan.target,),
                    quota_bytes=self._policy.files.backup_quota_bytes,
                )[0]
            except BackupUnavailable as exc:
                if not self._consume_backup_override(plan, exc):
                    raise
            self._verify_identity(plan)
        if plan.recursive:
            shutil.rmtree(plan.target)
        elif plan.target.is_symlink():
            plan.target.unlink()
        elif plan.target.is_dir():
            plan.target.rmdir()
        else:
            plan.target.unlink()
        return MutationResult(deleted=True, backup=backup)


__all__ = [
    "ApprovalRequired",
    "BACKUP_STORAGE_UNAVAILABLE_WARNING",
    "FILE_DELETE_WARNING",
    "FileMutationBroker",
    "MutationDenied",
    "MutationPlan",
    "MutationResult",
    "ObjectIdentity",
    "ObjectIdentityChanged",
    "OVERSIZE_BACKUP_WARNING",
    "RECURSIVE_DELETE_WARNING",
    "UNAVAILABLE_BACKUP_WARNING",
]
