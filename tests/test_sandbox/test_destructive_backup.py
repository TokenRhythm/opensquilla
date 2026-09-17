from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.sandbox.backup_vault import BackupVault
from opensquilla.sandbox.destructive_backup import DestructiveBackupGate
from opensquilla.sandbox.elevation import ApprovalDisplay, ElevationAction
from opensquilla.sandbox.policy_models import SandboxPolicy


def _policy(*, enabled: bool = True, quota: int = 1024) -> SandboxPolicy:
    return SandboxPolicy.model_validate(
        {
            "files": {
                "recursiveDeleteBackupEnabled": enabled,
                "backupQuotaBytes": quota,
            }
        }
    )


def _modify_action(target: Path) -> ElevationAction:
    return ElevationAction(
        tool_name="edit_file",
        action_kind="fs.edit",
        argv=("edit_file", str(target)),
        cwd=str(target.parent),
        sandbox_permissions="require_escalated",
        justification="Modify the exact file requested by the user.",
        target_paths=((str(target), "write"),),
        content_digest="sha256:new-content",
        display=ApprovalDisplay(kind="modify", target=str(target)),
    )


@pytest.mark.asyncio
async def test_enabled_mutation_backs_up_existing_target_after_first_approval(
    tmp_path: Path,
) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    vault = BackupVault(tmp_path / "vault")
    gate = DestructiveBackupGate(queue=queue, vault_factory=lambda _state: vault)
    target = tmp_path / "important.txt"
    target.write_text("before", encoding="utf-8")
    action = _modify_action(target)
    try:
        first = await gate.evaluate(
            action,
            approval_id=None,
            targets=(target,),
            policy=_policy(enabled=True),
            state_dir=tmp_path,
            session_key="session-1",
        )
        first_id = str(first.envelope["approval_id"])
        first_entry = queue.get(first_id)
        assert first.allowed is False
        assert first_entry.params["action"]["display"] == {
            "kind": "modify",
            "target": str(target),
            "destructive": True,
            "irreversible": False,
            "backup_state": "enabled",
        }

        queue.resolve(first_id, True)
        resumed = await gate.evaluate(
            action,
            approval_id=first_id,
            targets=(target,),
            policy=_policy(enabled=True),
            state_dir=tmp_path,
            session_key="session-1",
        )

        assert resumed.allowed is True
        assert resumed.without_backup is False
        assert len(resumed.receipts) == 1
        assert resumed.receipts[0].original_path == str(target.resolve())
        assert (resumed.receipts[0].entry_path / "content").read_text(
            encoding="utf-8"
        ) == "before"
        assert target.read_text(encoding="utf-8") == "before"
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_disabled_backup_is_bound_to_irreversible_first_confirmation(
    tmp_path: Path,
) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    gate = DestructiveBackupGate(queue=queue)
    target = tmp_path / "important.txt"
    target.write_text("before", encoding="utf-8")
    action = _modify_action(target)
    try:
        first = await gate.evaluate(
            action,
            approval_id=None,
            targets=(target,),
            policy=_policy(enabled=False),
            state_dir=tmp_path,
            session_key="session-1",
        )
        first_id = str(first.envelope["approval_id"])
        display = queue.get(first_id).params["action"]["display"]
        assert display["backup_state"] == "disabled"
        assert display["irreversible"] is True

        queue.resolve(first_id, True)
        resumed = await gate.evaluate(
            action,
            approval_id=first_id,
            targets=(target,),
            policy=_policy(enabled=False),
            state_dir=tmp_path,
            session_key="session-1",
        )

        assert resumed.allowed is True
        assert resumed.without_backup is True
        assert resumed.receipts == ()
        assert target.exists()
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_backup_failure_requests_distinct_irreversible_confirmation(
    tmp_path: Path,
) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    vault = BackupVault(tmp_path / "vault")
    gate = DestructiveBackupGate(queue=queue, vault_factory=lambda _state: vault)
    target = tmp_path / "important.txt"
    target.write_bytes(b"x" * 32)
    action = _modify_action(target)
    try:
        first = await gate.evaluate(
            action,
            approval_id=None,
            targets=(target,),
            policy=_policy(enabled=True, quota=8),
            state_dir=tmp_path,
            session_key="session-1",
        )
        first_id = str(first.envelope["approval_id"])
        queue.resolve(first_id, True)

        second = await gate.evaluate(
            action,
            approval_id=first_id,
            targets=(target,),
            policy=_policy(enabled=True, quota=8),
            state_dir=tmp_path,
            session_key="session-1",
        )

        assert second.allowed is False
        second_id = str(second.envelope["approval_id"])
        assert second_id != first_id
        second_action = queue.get(second_id).params["action"]
        assert second_action["action_kind"] == "fs.edit_without_backup"
        assert second_action["display"]["backup_state"] == (
            "unavailable_requires_confirmation"
        )
        assert second_action["display"]["irreversible"] is True
        assert target.exists()

        queue.resolve(second_id, True)
        final = await gate.evaluate(
            action,
            approval_id=second_id,
            targets=(target,),
            policy=_policy(enabled=True, quota=8),
            state_dir=tmp_path,
            session_key="session-1",
        )

        assert final.allowed is True
        assert final.without_backup is True
        assert final.receipts == ()
        assert target.exists()
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_new_file_needs_neither_destructive_approval_nor_backup(
    tmp_path: Path,
) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    gate = DestructiveBackupGate(queue=queue)
    target = tmp_path / "new.txt"
    try:
        result = await gate.evaluate(
            _modify_action(target),
            approval_id=None,
            targets=(target,),
            policy=_policy(enabled=True),
            state_dir=tmp_path,
            session_key="session-1",
        )

        assert result.allowed is True
        assert result.receipts == ()
        assert queue.list_pending("exec") == []
    finally:
        queue.close()
