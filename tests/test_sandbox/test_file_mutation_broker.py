from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.sandbox.backup_vault import (
    BackupTooLarge,
    BackupUnavailable,
    BackupVault,
)
from opensquilla.sandbox.file_mutation_broker import (
    RECURSIVE_DELETE_WARNING,
    ApprovalRequired,
    FileMutationBroker,
    ObjectIdentityChanged,
)
from opensquilla.sandbox.policy_models import SandboxPolicy


def _policy(*, backup: bool = True, quota: int = 1024) -> SandboxPolicy:
    return SandboxPolicy.model_validate(
        {
            "files": {
                "recursiveDeleteBackupEnabled": backup,
                "backupQuotaBytes": quota,
            }
        }
    )


def test_broker_rejects_identity_change_after_approval(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    broker = FileMutationBroker(
        policy=_policy(),
        vault=BackupVault(tmp_path / "vault"),
    )
    plan = broker.plan_delete(target)
    approved = broker.approve(plan)
    target.unlink()
    target.write_text("replacement", encoding="utf-8")

    with pytest.raises(ObjectIdentityChanged):
        broker.execute(approved)

    assert target.read_text(encoding="utf-8") == "replacement"


def test_non_recursive_symlink_delete_never_deletes_directory_referent(
    tmp_path: Path,
) -> None:
    referent = tmp_path / "referent"
    referent.mkdir()
    (referent / "important.txt").write_text("important", encoding="utf-8")
    link = tmp_path / "discard-link"
    link.symlink_to(referent, target_is_directory=True)
    broker = FileMutationBroker(
        policy=_policy(),
        vault=BackupVault(tmp_path / "vault"),
    )

    result = broker.execute(broker.approve(broker.plan_delete(link)))

    assert result.deleted is True
    assert not link.exists()
    assert not link.is_symlink()
    assert (referent / "important.txt").read_text(encoding="utf-8") == "important"


def test_recursive_delete_requires_approval_with_strong_warning(tmp_path: Path) -> None:
    target = tmp_path / "tree"
    target.mkdir()
    broker = FileMutationBroker(
        policy=_policy(),
        vault=BackupVault(tmp_path / "vault"),
    )

    plan = broker.plan_delete(target, recursive=True)

    assert plan.approval_required is True
    assert plan.warning == RECURSIVE_DELETE_WARNING
    assert "无法撤回" in plan.warning
    with pytest.raises(ApprovalRequired):
        broker.execute(plan)


def test_recursive_delete_backs_up_then_deletes_and_consumes_approval(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tree"
    target.mkdir()
    (target / "data.txt").write_text("important", encoding="utf-8")
    broker = FileMutationBroker(
        policy=_policy(),
        vault=BackupVault(tmp_path / "vault"),
    )
    approved = broker.approve(broker.plan_delete(target, recursive=True))

    result = broker.execute(approved)

    assert not target.exists()
    assert result.backup is not None
    assert (result.backup.entry_path / "content" / "data.txt").read_text(
        encoding="utf-8"
    ) == "important"
    with pytest.raises(ApprovalRequired):
        broker.execute(approved)


def test_non_recursive_file_delete_requires_approval_and_creates_backup(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ordinary.txt"
    target.write_text("remove", encoding="utf-8")
    broker = FileMutationBroker(
        policy=_policy(),
        vault=BackupVault(tmp_path / "vault"),
    )

    plan = broker.plan_delete(target)

    assert plan.approval_required is True
    with pytest.raises(ApprovalRequired):
        broker.execute(plan)

    result = broker.execute(broker.approve(plan))

    assert result.deleted is True
    assert result.backup is not None
    assert (result.backup.entry_path / "content").read_text(encoding="utf-8") == "remove"
    assert not target.exists()


def test_lazy_vault_failure_requires_second_confirmation_for_file_delete(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ordinary.txt"
    target.write_text("remove", encoding="utf-8")

    def unavailable_vault() -> BackupVault:
        raise OSError("state directory is unavailable")

    broker = FileMutationBroker(
        policy=_policy(),
        vault_factory=unavailable_vault,
    )
    approved = broker.approve(broker.plan_delete(target))

    with pytest.raises(BackupUnavailable) as raised:
        broker.execute(approved)

    assert target.exists()
    second = broker.approve_without_backup(approved, raised.value)
    assert "已自动清理旧备份" not in str(second.warning)
    assert "备份存储" in str(second.warning)
    assert broker.execute(second).deleted is True
    assert not target.exists()


def test_oversize_backup_requires_second_exact_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "tree"
    target.mkdir()
    (target / "payload.bin").write_bytes(b"x" * 32)
    broker = FileMutationBroker(
        policy=_policy(quota=8),
        vault=BackupVault(tmp_path / "vault"),
    )
    approved = broker.approve(broker.plan_delete(target, recursive=True))

    with pytest.raises(BackupTooLarge) as raised:
        broker.execute(approved)

    assert target.exists()
    second = broker.approve_without_backup(approved, raised.value)
    assert "不会创建备份" in str(second.warning)
    assert "不可撤回" in str(second.warning)
    assert broker.execute(second).deleted is True
    assert not target.exists()
    with pytest.raises(ApprovalRequired):
        broker.execute(second)


def test_persistent_backup_failure_can_only_continue_with_second_exact_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "tree"
    target.mkdir()
    (target / "payload.bin").write_bytes(b"important")
    vault = BackupVault(tmp_path / "vault")
    broker = FileMutationBroker(policy=_policy(), vault=vault)
    approved = broker.approve(broker.plan_delete(target, recursive=True))

    def _fail_backup_many(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise BackupUnavailable(reason="io_error")

    monkeypatch.setattr(vault, "backup_many", _fail_backup_many)

    with pytest.raises(BackupUnavailable) as raised:
        broker.execute(approved)

    assert target.exists()
    second = broker.approve_without_backup(approved, raised.value)
    assert "不会创建备份" in str(second.warning)
    assert broker.execute(second).deleted is True
    assert not target.exists()
