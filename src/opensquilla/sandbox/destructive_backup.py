"""Fingerprint-bound approval and backup gate for exact destructive file changes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.sandbox.backup_vault import (
    BackupReceipt,
    BackupUnavailable,
    BackupVault,
)
from opensquilla.sandbox.elevation import (
    ApprovalDisplay,
    ElevationAction,
    ElevationGateResult,
    gate_elevated_action,
)
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.sandbox.policy_models import SandboxPolicy

BACKUP_ENABLED_WARNING = (
    "A recoverable copy will be created before this change. Older backups may be "
    "removed automatically to make room."
)
BACKUP_DISABLED_WARNING = (
    "No backup will be created and this change may be impossible to recover. "
    "You can enable backups in Settings > Sandbox > File safety."
)
BACKUP_UNAVAILABLE_WARNING = (
    "OpenSquilla could not create a backup after removing old backups. Continuing "
    "will make this exact change without a backup and it may be impossible to recover."
)


@dataclass(frozen=True)
class DestructiveBackupResult:
    allowed: bool
    envelope: dict[str, object] | None = None
    receipts: tuple[BackupReceipt, ...] = ()
    without_backup: bool = False


class DestructiveBackupGate:
    """Authorize one exact destructive action, then secure its current contents."""

    def __init__(
        self,
        *,
        queue: ApprovalQueue | None = None,
        vault_factory: Callable[[Path], BackupVault] | None = None,
    ) -> None:
        self._queue = queue
        self._vault_factory = vault_factory or (
            lambda state_dir: BackupVault(state_dir / "backup-vault")
        )

    @staticmethod
    def _existing_targets(targets: Iterable[str | Path]) -> tuple[Path, ...]:
        existing: list[Path] = []
        seen: set[Path] = set()
        for target in targets:
            path = Path(target).expanduser().absolute()
            if path in seen or (not path.exists() and not path.is_symlink()):
                continue
            seen.add(path)
            existing.append(path)
        return tuple(existing)

    @staticmethod
    def _first_action(
        action: ElevationAction,
        *,
        backup_enabled: bool,
    ) -> ElevationAction:
        base_display = action.display or ApprovalDisplay(kind="sensitive_operation")
        return replace(
            action,
            display=replace(
                base_display,
                destructive=True,
                irreversible=not backup_enabled,
                backup_state="enabled" if backup_enabled else "disabled",
            ),
        )

    @staticmethod
    def _without_backup_action(action: ElevationAction) -> ElevationAction:
        base_display = action.display or ApprovalDisplay(kind="sensitive_operation")
        return replace(
            action,
            action_kind=f"{action.action_kind}_without_backup",
            risk_markers=tuple((*action.risk_markers, "without-backup")),
            display=replace(
                base_display,
                destructive=True,
                irreversible=True,
                backup_state="unavailable_requires_confirmation",
            ),
        )

    @staticmethod
    def _envelope(
        gate: ElevationGateResult,
        *,
        action: ElevationAction,
        warning: str,
    ) -> dict[str, object]:
        payload = gate.to_envelope()
        display = action.display or ApprovalDisplay(kind="sensitive_operation")
        payload.update(
            {
                "warning": warning,
                "target": display.target,
                "destructive": True,
                "irreversible": display.irreversible,
                "backup_state": display.backup_state,
            }
        )
        return payload

    def _gate(
        self,
        action: ElevationAction,
        *,
        approval_id: str | None,
        session_key: str | None,
    ) -> ElevationGateResult:
        return gate_elevated_action(
            action,
            approval_id=approval_id,
            session_key=session_key,
            queue=self._queue,
            reviewer="user",
            # Structured file tools execute the exact fingerprinted mutation;
            # this is not a grant for arbitrary host reads or commands.
            file_system_profile=FileSystemPermissionProfile.full_access(),
        )

    def _approval_queue(self) -> ApprovalQueue:
        if self._queue is not None:
            return self._queue
        from opensquilla.gateway.approval_queue import get_approval_queue

        return get_approval_queue()

    async def evaluate(
        self,
        action: ElevationAction,
        *,
        approval_id: str | None,
        targets: Iterable[str | Path],
        policy: SandboxPolicy,
        state_dir: str | Path,
        session_key: str | None,
    ) -> DestructiveBackupResult:
        existing = self._existing_targets(targets)
        if not existing:
            return DestructiveBackupResult(allowed=True)

        backup_enabled = bool(policy.files.recursive_delete_backup_enabled)
        first_action = self._first_action(action, backup_enabled=backup_enabled)
        without_backup_action = self._without_backup_action(action)

        selected_action = first_action
        selected_without_backup = False
        if approval_id:
            try:
                entry = self._approval_queue().get(approval_id)
            except KeyError:
                entry = None
            if (
                entry is not None
                and str(entry.params.get("fingerprint") or "")
                == without_backup_action.fingerprint()
            ):
                selected_action = without_backup_action
                selected_without_backup = True

        reviewed = self._gate(
            selected_action,
            approval_id=approval_id,
            session_key=session_key,
        )
        if not reviewed.allowed:
            warning = (
                BACKUP_UNAVAILABLE_WARNING
                if selected_without_backup
                else BACKUP_ENABLED_WARNING
                if backup_enabled
                else BACKUP_DISABLED_WARNING
            )
            return DestructiveBackupResult(
                allowed=False,
                envelope=self._envelope(
                    reviewed,
                    action=selected_action,
                    warning=warning,
                ),
            )

        if selected_without_backup or not backup_enabled:
            return DestructiveBackupResult(
                allowed=True,
                without_backup=True,
            )

        try:
            state_text = str(state_dir).strip()
            if not state_text:
                raise BackupUnavailable(reason="state_dir_unavailable")
            vault = self._vault_factory(Path(state_text).expanduser().absolute())
            receipts = await asyncio.to_thread(
                vault.backup_many,
                existing,
                quota_bytes=policy.files.backup_quota_bytes,
            )
        except (BackupUnavailable, OSError):
            second = self._gate(
                without_backup_action,
                approval_id=None,
                session_key=session_key,
            )
            return DestructiveBackupResult(
                allowed=False,
                envelope=self._envelope(
                    second,
                    action=without_backup_action,
                    warning=BACKUP_UNAVAILABLE_WARNING,
                ),
            )
        return DestructiveBackupResult(
            allowed=True,
            receipts=receipts,
        )


__all__ = [
    "BACKUP_DISABLED_WARNING",
    "BACKUP_ENABLED_WARNING",
    "BACKUP_UNAVAILABLE_WARNING",
    "DestructiveBackupGate",
    "DestructiveBackupResult",
]
