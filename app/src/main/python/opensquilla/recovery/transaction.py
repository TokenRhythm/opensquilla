"""Typed, identity-gated recovery for interrupted whole-profile transactions."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opensquilla.recovery.atomic import native_move_no_replace
from opensquilla.recovery.config_patch import ConfigSnapshot
from opensquilla.recovery.errors import RecoveryError, StaleRecoveryTransactionError
from opensquilla.recovery.locking import (
    acquire_legacy_gateway_locks,
    acquire_profile_locks,
    replacement_history_lock_scope,
)
from opensquilla.recovery.models import RecoveryReport

_IMPORT_KINDS = frozenset({"cli-home", "windows-portable", "desktop-home"})
_IDENTITY_FIELDS = frozenset(
    {"device", "inode", "file_type", "mode", "size", "modified_at_ns"}
)
_IDENTITY_KEYS = frozenset(
    {"source", "original_target", "staging", "backup", "candidate"}
)
_IMPORT_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "transaction_id",
        "source",
        "source_kind",
        "target",
        "staging",
        "backup",
        "phase",
        "target_existed",
        "target_had_real_data",
        "target_was_empty",
        "identities",
    }
)
_RESTORE_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "transaction_id",
        "source",
        "target",
        "staging",
        "backup",
        "phase",
        "target_existed",
        "identities",
    }
)
_PHASES = frozenset(
    {
        "prepared",
        "target_parked",
        "candidate_published_unvalidated",
        "validated",
        "committed",
    }
)


def _normalized(path: str | Path) -> str:
    value = Path(path).expanduser()
    try:
        value = value.resolve(strict=False)
    except OSError:
        value = value.absolute()
    return os.path.normcase(os.path.normpath(str(value)))


def _journal_path(home: Path) -> Path:
    return home.parent / f".{home.name}.profile-replace.json"


def _valid_identity(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _IDENTITY_FIELDS
        and all(type(value[key]) is int and value[key] >= 0 for key in _IDENTITY_FIELDS)
    )


def _load_typed_transaction(home: Path) -> tuple[ConfigSnapshot, dict[str, Any]]:
    home = home.expanduser().absolute()
    journal = _journal_path(home)
    snapshot = ConfigSnapshot.capture(journal)
    if snapshot.identity is None:
        raise RecoveryError(
            "replacement transaction journal is missing",
            stable_code="transaction_missing",
        )
    try:
        payload = json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            "replacement transaction journal is unreadable",
            stable_code="transaction_recovery_unsafe",
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RecoveryError(
            "replacement transaction journal schema is unsupported",
            stable_code="transaction_recovery_unsafe",
        )
    operation = payload.get("operation")
    transaction_id = payload.get("transaction_id")
    target_raw = payload.get("target")
    backup_raw = payload.get("backup")
    staging_raw = payload.get("staging")
    phase = payload.get("phase")
    identities = payload.get("identities")
    expected_fields = (
        _IMPORT_FIELDS if operation == "profile-import" else _RESTORE_FIELDS
    )
    if (
        operation not in {"profile-import", "restore-profile"}
        or set(payload) != expected_fields
        or not isinstance(transaction_id, str)
        or not isinstance(payload.get("source"), str)
        or not isinstance(target_raw, str)
        or not isinstance(backup_raw, str)
        or phase not in _PHASES
        or not isinstance(identities, dict)
        or set(identities) != _IDENTITY_KEYS
        or type(payload.get("target_existed")) is not bool
    ):
        raise RecoveryError(
            "replacement transaction metadata is incomplete",
            stable_code="transaction_recovery_unsafe",
        )
    try:
        if str(uuid.UUID(transaction_id)) != transaction_id:
            raise ValueError
    except ValueError as exc:
        raise RecoveryError(
            "replacement transaction id is invalid",
            stable_code="transaction_recovery_unsafe",
        ) from exc
    expected_backup = home.with_name(f"{home.name}.backup.{transaction_id}")
    source_raw = str(payload["source"])
    if (
        target_raw != _normalized(home)
        or backup_raw != _normalized(expected_backup)
        or source_raw != _normalized(source_raw)
    ):
        raise RecoveryError(
            "replacement transaction paths are outside the selected profile",
            stable_code="transaction_recovery_unsafe",
        )
    if operation == "profile-import":
        expected_staging = home.parent / f".{home.name}.profile-staging.{transaction_id}"
        target_existed = payload["target_existed"]
        target_had_real_data = payload.get("target_had_real_data")
        target_was_empty = payload.get("target_was_empty")
        candidate_required = phase in {
            "candidate_published_unvalidated",
            "validated",
            "committed",
        }
        if (
            not isinstance(staging_raw, str)
            or staging_raw != _normalized(expected_staging)
            or payload.get("source_kind") not in _IMPORT_KINDS
            or type(target_had_real_data) is not bool
            or type(target_was_empty) is not bool
            or target_had_real_data != (target_existed and not target_was_empty)
            or target_was_empty != (target_existed and not target_had_real_data)
            or not _valid_identity(identities.get("source"))
            or not _valid_identity(identities.get("staging"))
            or (
                target_existed
                and (
                    not _valid_identity(identities.get("original_target"))
                    or identities.get("backup") != identities.get("original_target")
                )
            )
            or (
                not target_existed
                and (
                    identities.get("original_target") is not None
                    or identities.get("backup") is not None
                )
            )
            or (
                candidate_required
                and not _valid_identity(identities.get("candidate"))
            )
            or (not candidate_required and identities.get("candidate") is not None)
        ):
            raise RecoveryError(
                "profile import transaction metadata is unsafe",
                stable_code="transaction_recovery_unsafe",
            )
    else:
        target_existed = payload["target_existed"]
        backup_required = target_existed and phase != "prepared"
        if (
            staging_raw != ""
            or _normalized(Path(source_raw).parent) != _normalized(home.parent)
            or not _valid_identity(identities.get("source"))
            or not _valid_identity(identities.get("candidate"))
            or identities.get("staging") is not None
            or (
                target_existed
                and not _valid_identity(identities.get("original_target"))
            )
            or (backup_required and not _valid_identity(identities.get("backup")))
            or (
                target_existed
                and phase == "prepared"
                and identities.get("backup") is not None
            )
            or (
                not target_existed
                and (
                    identities.get("original_target") is not None
                    or identities.get("backup") is not None
                )
            )
        ):
            raise RecoveryError(
                "profile restore transaction metadata is unsafe",
                stable_code="transaction_recovery_unsafe",
            )
    snapshot.assert_current()
    return snapshot, payload


def typed_transaction_available(home: str | Path) -> bool:
    try:
        _load_typed_transaction(Path(home))
    except (OSError, RecoveryError):
        return False
    return True


def finalize_committed_profile_transaction(home: str | Path) -> bool:
    """CAS-remove one exact committed journal after independent proof."""

    home_path = Path(home).expanduser().absolute()
    journal = _journal_path(home_path)
    try:
        snapshot, payload = _load_typed_transaction(home_path)
    except RecoveryError as exc:
        if exc.stable_code == "transaction_missing":
            return False
        raise
    if payload.get("phase") != "committed":
        return False
    from opensquilla.recovery.engine import _committed_transaction_is_complete

    if not _committed_transaction_is_complete(home_path, payload):
        raise RecoveryError(
            "committed replacement transaction proof is incomplete",
            stable_code="transaction_recovery_unsafe",
        )
    current = ConfigSnapshot.capture(journal)
    if (
        current.identity != snapshot.identity
        or current.digest != snapshot.digest
        or current.data != snapshot.data
    ):
        raise StaleRecoveryTransactionError("committed replacement journal changed")
    current.assert_current()
    transaction_id = str(payload["transaction_id"])
    finalized = journal.with_name(
        f".{home_path.name}.profile-replace.{transaction_id}.committed.json"
    )
    native_move_no_replace(journal, finalized)
    if os.name != "nt":
        descriptor = os.open(journal.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return True


def recover_profile_transaction(
    home: str | Path,
    *,
    transaction_id: str,
    expected_revision: int,
    import_recoverer: Callable[[Path, dict[str, Any]], None] | None = None,
    lock_timeout: float = 0.0,
) -> RecoveryReport:
    """Recover only the typed journal selected by a fresh inspection CAS."""

    from opensquilla.recovery.engine import inspect_profile

    home_path = Path(home).expanduser().absolute()
    before = inspect_profile(home_path, profile_kind="desktop-primary")
    if before.transaction_id != transaction_id or before.revision != expected_revision:
        raise StaleRecoveryTransactionError(
            "profile transaction changed; inspect again before recovery"
        )
    if "recover-transaction" not in before.allowed_actions:
        raise RecoveryError(
            "the current journal cannot be recovered automatically",
            stable_code="transaction_recovery_unsafe",
        )
    _snapshot, initial_payload = _load_typed_transaction(home_path)
    source_raw = initial_payload.get("source")
    lock_paths = [home_path, replacement_history_lock_scope(home_path)]
    if isinstance(source_raw, str) and source_raw:
        lock_paths.append(Path(source_raw))
    # _load_typed_transaction has already constrained these paths to exact
    # transaction-owned siblings. Their legacy lock handles must travel with
    # backup/staging -> target during rollback. The user-selected import source
    # is intentionally excluded because it remains strictly read-only.
    legacy_lock_homes = [home_path, Path(str(initial_payload["backup"]))]
    if initial_payload["operation"] == "profile-import":
        legacy_lock_homes.append(Path(str(initial_payload["staging"])))

    with acquire_profile_locks(*lock_paths, timeout=lock_timeout):
        with acquire_legacy_gateway_locks(*legacy_lock_homes, timeout=lock_timeout):
            current = inspect_profile(home_path, profile_kind="desktop-primary")
            if (
                current.transaction_id != transaction_id
                or current.revision != expected_revision
                or "recover-transaction" not in current.allowed_actions
            ):
                raise StaleRecoveryTransactionError(
                    "profile transaction changed after lock acquisition"
                )
            snapshot, payload = _load_typed_transaction(home_path)
            if payload != initial_payload:
                raise StaleRecoveryTransactionError("replacement journal changed")
            snapshot.assert_current()
            try:
                if payload["operation"] == "profile-import":
                    if import_recoverer is None:
                        raise RecoveryError(
                            "profile import recovery adapter is unavailable",
                            stable_code="transaction_recovery_unsafe",
                        )
                    import_recoverer(home_path, payload)
                else:
                    from opensquilla.recovery.restore import (
                        recover_interrupted_profile_restore,
                    )

                    recover_interrupted_profile_restore(home_path, payload)
            except RecoveryError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                raise RecoveryError(
                    "replacement transaction cannot be recovered without ambiguity",
                    stable_code="transaction_recovery_unsafe",
                ) from exc
    return inspect_profile(home_path, profile_kind="desktop-primary")


__all__ = [
    "finalize_committed_profile_transaction",
    "recover_profile_transaction",
    "typed_transaction_available",
]
