"""Strict whole-profile restore transactions for recorded sibling backups."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opensquilla.recovery.atomic import (
    PathIdentity,
    _chmod_open_file,
    native_move_no_replace,
    profile_no_follow_manifest,
)
from opensquilla.recovery.config_patch import ConfigSnapshot
from opensquilla.recovery.engine import inspect_profile
from opensquilla.recovery.errors import (
    AtomicStateUnknownError,
    RecoveryError,
    RestoreValidationError,
)
from opensquilla.recovery.locking import (
    LegacyGatewayLock,
    acquire_legacy_gateway_locks,
    acquire_profile_locks,
    effective_state_roots,
    move_profile_no_replace,
    replacement_history_lock_scope,
)
from opensquilla.recovery.models import RecoveryReport

_HISTORY_NAME = "profile-replacement-history.json"
_IDENTITY_FIELDS = frozenset(
    {"device", "inode", "file_type", "mode", "size", "modified_at_ns"}
)
_HISTORY_FIELDS = frozenset({"schema_version", "backups"})
_HISTORY_RECORD_FIELDS = frozenset(
    {
        "transaction_id",
        "committed_at",
        "source",
        "target",
        "backup",
        "source_identity",
        "target_identity",
        "backup_identity",
    }
)
_CONSUMED_HISTORY_RECORD_FIELDS = frozenset(
    {*_HISTORY_RECORD_FIELDS, "restored_at", "restored_to", "consumed_by_transaction_id"}
)


class _PostPublishSyncError(RuntimeError):
    """A replacement became visible before its parent directory failed to sync."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"replacement was published but its parent did not sync: {path}")
        self.published_path = path


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _normalized(path: str | Path) -> str:
    candidate = Path(path).expanduser()
    try:
        candidate = candidate.resolve(strict=False)
    except OSError:
        candidate = candidate.absolute()
    return os.path.normcase(os.path.normpath(str(candidate)))


def _identity_payload(path: Path) -> dict[str, int]:
    value = path.lstat()
    identity = PathIdentity.from_stat(value)
    return {
        "device": identity.device,
        "inode": identity.inode,
        "file_type": stat.S_IFMT(identity.mode),
        "mode": identity.mode,
        "size": identity.size,
        "modified_at_ns": identity.modified_at_ns,
    }


def _identity_matches(path: Path, expected: object) -> bool:
    if not isinstance(expected, dict):
        return False
    try:
        current = _identity_payload(path)
    except OSError:
        return False
    return all(current[key] == expected.get(key) for key in current)


def _object_identity_matches(path: Path, expected: object) -> bool:
    if not isinstance(expected, dict):
        return False
    try:
        current = _identity_payload(path)
    except OSError:
        return False
    return all(
        current[key] == expected.get(key)
        for key in ("device", "inode", "file_type")
    )


def _load_history(path: Path) -> tuple[ConfigSnapshot, dict[str, Any]]:
    snapshot = ConfigSnapshot.capture(path)
    if snapshot.identity is None:
        raise RestoreValidationError("profile replacement history is missing")
    try:
        payload = json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreValidationError("profile replacement history is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _HISTORY_FIELDS
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("backups"), list)
        or not all(_valid_history_record(record) for record in payload.get("backups", []))
    ):
        raise RestoreValidationError("profile replacement history schema is unsupported")
    return snapshot, payload


def _valid_history_record(record: object) -> bool:
    if not isinstance(record, dict) or set(record) not in {
        _HISTORY_RECORD_FIELDS,
        _CONSUMED_HISTORY_RECORD_FIELDS,
    }:
        return False
    try:
        transaction_id = str(record.get("transaction_id", ""))
        committed_at = datetime.fromisoformat(str(record.get("committed_at", "")))
        if str(uuid.UUID(transaction_id)) != transaction_id:
            return False
    except ValueError:
        return False
    if committed_at.tzinfo is None:
        return False
    if any(
        not isinstance(record.get(key), str)
        or not record[key]
        or _normalized(record[key]) != record[key]
        for key in ("source", "target", "backup")
    ):
        return False
    if not all(
        isinstance(record.get(key), dict)
        and set(record[key]) == _IDENTITY_FIELDS
        and all(
            type(record[key][field]) is int and record[key][field] >= 0
            for field in _IDENTITY_FIELDS
        )
        for key in ("source_identity", "target_identity", "backup_identity")
    ):
        return False
    if set(record) == _CONSUMED_HISTORY_RECORD_FIELDS:
        try:
            consumed_id = str(record.get("consumed_by_transaction_id", ""))
            restored_at = datetime.fromisoformat(str(record.get("restored_at", "")))
            if str(uuid.UUID(consumed_id)) != consumed_id:
                return False
        except ValueError:
            return False
        restored_to = record.get("restored_to")
        if (
            restored_at.tzinfo is None
            or not isinstance(restored_to, str)
            or _normalized(restored_to) != restored_to
        ):
            return False
    return True


def _recorded_backup(backup: Path) -> tuple[ConfigSnapshot, dict[str, Any], dict[str, Any], Path]:
    history_path = backup.parent / _HISTORY_NAME
    snapshot, history = _load_history(history_path)
    normalized_backup = _normalized(backup)
    matches = [
        item
        for item in history["backups"]
        if isinstance(item, dict) and item.get("backup") == normalized_backup
    ]
    if len(matches) != 1:
        raise RestoreValidationError("backup is not uniquely recorded in replacement history")
    record = matches[0]
    transaction_id = str(record.get("transaction_id", ""))
    try:
        if str(uuid.UUID(transaction_id)) != transaction_id:
            raise ValueError
    except ValueError as exc:
        raise RestoreValidationError("backup transaction id is not a canonical UUID") from exc
    target_raw = record.get("target")
    if not isinstance(target_raw, str) or not target_raw:
        raise RestoreValidationError("backup history target is missing")
    target = Path(target_raw)
    expected_backup = target.with_name(f"{target.name}.backup.{transaction_id}")
    if _normalized(expected_backup) != normalized_backup:
        raise RestoreValidationError("backup is not the exact UUID sibling recorded for target")
    if _normalized(target.parent) != _normalized(backup.parent):
        raise RestoreValidationError("backup and target are not siblings")
    if not _identity_matches(backup, record.get("backup_identity")):
        raise RestoreValidationError("backup filesystem identity no longer matches history")
    try:
        value = backup.lstat()
    except OSError as exc:
        raise RestoreValidationError("recorded backup is inaccessible") from exc
    attributes = int(getattr(value, "st_file_attributes", 0))
    if stat.S_ISLNK(value.st_mode) or attributes & 0x400 or not stat.S_ISDIR(value.st_mode):
        raise RestoreValidationError("recorded backup is not a real directory")
    profile_no_follow_manifest(backup)
    return snapshot, history, record, target


def recorded_backup_target(backup: str | Path) -> Path:
    """Return the exact target recorded for ``backup`` without mutating either profile."""

    backup_path = Path(backup).expanduser().absolute()
    _snapshot, _history, _record, target = _recorded_backup(backup_path)
    return target.expanduser().absolute()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short write")
        view = view[count:]


def _write_json_no_replace(path: Path, payload: dict[str, Any]) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(path, flags, 0o600)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    os.close(fd)
    _fsync_directory(path.parent)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _replace_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary: Path | None = Path(temporary_name)
    try:
        _chmod_open_file(fd, mode)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        assert temporary is not None
        os.replace(temporary, path)
        temporary = None
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            # The destination already names the new bytes. Preserve that fact so
            # callers can roll back the publication instead of treating this as
            # a write-before-publication failure and discarding their journal.
            raise _PostPublishSyncError(path) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _replace_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    _replace_bytes(path, _json_bytes(payload), mode=mode)


def _replace_journal_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        _replace_json(path, payload)
    except _PostPublishSyncError as exc:
        raise AtomicStateUnknownError(
            "restore journal phase publication durability is unknown"
        ) from exc


def _rollback_history(
    snapshot: ConfigSnapshot,
    *,
    expected_current: bytes,
) -> None:
    current = ConfigSnapshot.capture(snapshot.path)
    if current.identity is None or current.data != expected_current:
        raise AtomicStateUnknownError(
            "restore history changed after publication; rollback state is uncertain"
        )
    _replace_bytes(snapshot.path, snapshot.data, mode=snapshot.mode)


def _validate_restored_target(target: Path) -> RecoveryReport:
    report = inspect_profile(
        target,
        profile_kind="desktop-primary",
        _ignore_transaction=True,
    )
    if report.outcome == "recovery_required":
        raise RestoreValidationError(
            f"restored target did not pass profile inspection ({report.stable_code})"
        )
    return report


def _require_existing_backup_lock_authority(
    backup: Path,
    locks: tuple[LegacyGatewayLock, ...],
) -> None:
    """Prove every old-version state root is already lockable without writes."""

    state_roots = effective_state_roots(
        backup,
        include_process_environment=False,
    )
    if not state_roots or any(
        not os.path.lexists(state_root)
        or not any(lock.holds_state_root(state_root) for lock in locks)
        for state_root in state_roots
    ):
        raise RestoreValidationError(
            "recorded backup requires a pre-existing legacy gateway lock authority; "
            "the backup was not changed—continue with a recovery profile or use the "
            "complete profile importer to copy this backup into the stopped primary profile",
            stable_code="restore_backup_lock_authority_missing",
        )


def _rollback_restore(
    *,
    target: Path,
    selected_backup: Path,
    current_backup: Path,
    target_existed: bool,
) -> None:
    try:
        selected_was_published = not os.path.lexists(selected_backup)
        if selected_was_published and os.path.lexists(target):
            move_profile_no_replace(target, selected_backup, move=native_move_no_replace)
        if target_existed and os.path.lexists(current_backup):
            move_profile_no_replace(current_backup, target, move=native_move_no_replace)
        _fsync_directory(target.parent)
    except Exception as exc:
        raise AtomicStateUnknownError(
            "restore failed and the previous target could not be rolled back"
        ) from exc


def restore_profile(backup: str | Path, *, lock_timeout: float = 0.0) -> RecoveryReport:
    """Restore one exact history-recorded sibling backup as a whole profile."""

    backup_path = Path(backup).expanduser().absolute()
    history_snapshot, history, record, target = _recorded_backup(backup_path)
    target = target.expanduser().absolute()
    journal = target.parent / f".{target.name}.profile-replace.json"
    if os.path.lexists(journal):
        raise RestoreValidationError("an existing replacement journal must be reconciled first")

    restore_id = str(uuid.uuid4())
    current_backup = target.with_name(f"{target.name}.backup.{restore_id}")
    target_existed = os.path.lexists(target)
    if target_existed:
        profile_no_follow_manifest(target)
    selected_identity = _identity_payload(backup_path)
    original_identity = _identity_payload(target) if target_existed else None
    payload: dict[str, Any] = {
        "schema_version": 1,
        "operation": "restore-profile",
        "transaction_id": restore_id,
        "phase": "prepared",
        "source": _normalized(backup_path),
        "target": _normalized(target),
        "backup": _normalized(current_backup),
        "staging": "",
        "target_existed": target_existed,
        "identities": {
            "source": selected_identity,
            "original_target": original_identity,
            "staging": None,
            "backup": None,
            "candidate": selected_identity,
        },
    }

    committed = False
    history_updated = False
    published_history_data: bytes | None = None
    with acquire_profile_locks(
        target,
        backup_path,
        replacement_history_lock_scope(target),
        timeout=lock_timeout,
    ):
        with acquire_legacy_gateway_locks(
            target,
            backup_path,
            read_only_homes=(backup_path,),
            timeout=lock_timeout,
        ) as legacy_locks:
            # A recorded backup is immutable recovery input. Require its old-
            # version authority files to exist already, then keep those inodes
            # locked across backup_path -> target. Otherwise safely refuse the
            # automatic restore before its journal or either profile is moved.
            _require_existing_backup_lock_authority(backup_path, legacy_locks)
            # Revalidate every authority after lock acquisition.
            current_snapshot, current_history, current_record, current_target = _recorded_backup(
                backup_path
            )
            if (
                current_snapshot.identity != history_snapshot.identity
                or current_snapshot.digest != history_snapshot.digest
                or current_record != record
                or _normalized(current_target) != _normalized(target)
            ):
                raise RestoreValidationError("replacement history changed during restore preflight")
            if os.path.lexists(target) != target_existed or (
                target_existed and _identity_payload(target) != original_identity
            ):
                raise RestoreValidationError("current target changed during restore preflight")
            if target_existed:
                profile_no_follow_manifest(target)
            history = current_history
            from opensquilla.recovery.transaction import (
                finalize_committed_profile_transaction,
            )

            with contextlib.suppress(RecoveryError):
                finalize_committed_profile_transaction(target)
            if os.path.lexists(journal):
                raise RestoreValidationError(
                    "an existing replacement journal must be reconciled first"
                )
            _write_json_no_replace(journal, payload)
            try:
                if target_existed:
                    move_profile_no_replace(
                        target,
                        current_backup,
                        move=native_move_no_replace,
                    )
                    _fsync_directory(target.parent)
                    payload["identities"]["backup"] = _identity_payload(current_backup)
                payload["phase"] = "target_parked"
                _replace_journal_json(journal, payload)

                move_profile_no_replace(
                    backup_path,
                    target,
                    move=native_move_no_replace,
                )
                _fsync_directory(target.parent)
                payload["identities"]["candidate"] = _identity_payload(target)
                payload["phase"] = "candidate_published_unvalidated"
                _replace_journal_json(journal, payload)

                _validate_restored_target(target)
                payload["phase"] = "validated"
                _replace_journal_json(journal, payload)

                backups: list[object] = []
                for item in history["backups"]:
                    if not (
                        isinstance(item, dict) and item.get("backup") == _normalized(backup_path)
                    ):
                        backups.append(item)
                        continue
                    consumed = dict(item)
                    consumed.update(
                        {
                            "restored_at": datetime.now(UTC).isoformat(),
                            "restored_to": _normalized(target),
                            "consumed_by_transaction_id": restore_id,
                        }
                    )
                    backups.append(consumed)
                if target_existed:
                    backups.append(
                        {
                            "transaction_id": restore_id,
                            "committed_at": datetime.now(UTC).isoformat(),
                            "source": _normalized(backup_path),
                            "target": _normalized(target),
                            "backup": _normalized(current_backup),
                            "source_identity": selected_identity,
                            "target_identity": _identity_payload(target),
                            "backup_identity": _identity_payload(current_backup),
                        }
                    )
                history["backups"] = backups
                history_snapshot.assert_current()
                published_history_data = _json_bytes(history)
                _replace_json(
                    history_snapshot.path,
                    history,
                    mode=history_snapshot.mode,
                )
                history_updated = True

                # The target and its recovery index are now durable. Only this
                # point may advance the replacement journal to committed.
                payload["phase"] = "committed"
                _replace_journal_json(journal, payload)
                committed = True
                from opensquilla.recovery.transaction import (
                    finalize_committed_profile_transaction,
                )

                if not finalize_committed_profile_transaction(target):
                    raise RestoreValidationError(
                        "committed restore journal could not be finalized"
                    )
            except AtomicStateUnknownError:
                # A no-replace move may have completed before its native
                # post-state verification failed.  Local booleans and journal
                # phase updates cannot prove which name owns each inode, so do
                # not attempt rollback or journal cleanup.  Offline recovery
                # must reconcile the exact paths and identities later.
                raise
            except BaseException as exc:
                if not committed:
                    history_was_published = history_updated or (
                        isinstance(exc, _PostPublishSyncError)
                        and exc.published_path == history_snapshot.path
                    )
                    if history_was_published and published_history_data is not None:
                        _rollback_history(
                            history_snapshot,
                            expected_current=published_history_data,
                        )
                    _rollback_restore(
                        target=target,
                        selected_backup=backup_path,
                        current_backup=current_backup,
                        target_existed=target_existed,
                    )
                    with contextlib.suppress(OSError):
                        journal.unlink()
                        _fsync_directory(journal.parent)
                if isinstance(exc, RecoveryError) or not isinstance(exc, Exception):
                    raise
                raise RestoreValidationError("restore transaction could not be completed") from exc
    return inspect_profile(target, profile_kind="desktop-primary")


def _unlink_matching_restore_journal(journal: Path, payload: dict[str, Any]) -> None:
    snapshot = ConfigSnapshot.capture(journal)
    if snapshot.identity is None:
        raise RestoreValidationError("restore journal disappeared during recovery")
    try:
        current = json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreValidationError("restore journal changed during recovery") from exc
    if current != payload:
        raise RestoreValidationError("restore journal changed during recovery")
    snapshot.assert_current()
    journal.unlink()
    _fsync_directory(journal.parent)


def _restore_history_state(
    *,
    target: Path,
    selected_backup: Path,
    current_backup: Path,
    restore_id: str,
    target_existed: bool,
    candidate_identity: object,
    current_backup_identity: object,
) -> str:
    """Return ``old``, ``committed``, or ``ambiguous`` for restore history."""

    try:
        _snapshot, history = _load_history(target.parent / _HISTORY_NAME)
    except RestoreValidationError:
        return "ambiguous"
    entries = [item for item in history["backups"] if isinstance(item, dict)]
    selected_records = [
        item for item in entries if item.get("backup") == _normalized(selected_backup)
    ]
    restore_records = [item for item in entries if item.get("transaction_id") == restore_id]
    committed_selected = [
        item
        for item in selected_records
        if item.get("consumed_by_transaction_id") == restore_id
        and item.get("restored_to") == _normalized(target)
    ]
    if len(committed_selected) == 1:
        if target_existed:
            if len(restore_records) != 1:
                return "ambiguous"
            record = restore_records[0]
            if (
                record.get("target") != _normalized(target)
                or record.get("backup") != _normalized(current_backup)
                or not _object_identity_matches(target, record.get("target_identity"))
                or not _object_identity_matches(
                    current_backup,
                    record.get("backup_identity"),
                )
            ):
                return "ambiguous"
        elif restore_records:
            return "ambiguous"
        return "committed"

    if restore_records or len(selected_records) != 1:
        return "ambiguous"
    selected_record = selected_records[0]
    if selected_record.get("consumed_by_transaction_id") is not None:
        return "ambiguous"
    # Before publication the recorded backup identity belonged to the selected
    # sibling. After its no-replace move it must identify the candidate target.
    if not _object_identity_matches(target, selected_record.get("backup_identity")):
        return "ambiguous"
    if not _object_identity_matches(target, candidate_identity):
        return "ambiguous"
    if target_existed and not _object_identity_matches(
        current_backup,
        current_backup_identity,
    ):
        return "ambiguous"
    return "old"


def _rollback_validated_restore_history(
    *,
    target: Path,
    selected_backup: Path,
    current_backup: Path,
    restore_id: str,
    target_existed: bool,
) -> None:
    """CAS-remove only the exact pre-commit restore history mutation."""

    snapshot, history = _load_history(target.parent / _HISTORY_NAME)
    backups = history["backups"]
    assert isinstance(backups, list)
    consumed = [
        item
        for item in backups
        if isinstance(item, dict)
        and item.get("backup") == _normalized(selected_backup)
        and item.get("consumed_by_transaction_id") == restore_id
        and item.get("restored_to") == _normalized(target)
    ]
    restore_records = [
        item
        for item in backups
        if isinstance(item, dict) and item.get("transaction_id") == restore_id
    ]
    original_selected = [
        item
        for item in backups
        if isinstance(item, dict)
        and item.get("backup") == _normalized(selected_backup)
        and set(item) == _HISTORY_RECORD_FIELDS
    ]
    if len(original_selected) == 1 and not consumed and not restore_records:
        return
    if len(consumed) != 1 or set(consumed[0]) != _CONSUMED_HISTORY_RECORD_FIELDS:
        raise RestoreValidationError("validated restore history mutation is ambiguous")
    if target_existed:
        if (
            len(restore_records) != 1
            or set(restore_records[0]) != _HISTORY_RECORD_FIELDS
            or restore_records[0].get("target") != _normalized(target)
            or restore_records[0].get("backup") != _normalized(current_backup)
        ):
            raise RestoreValidationError("validated restore backup history is ambiguous")
    elif restore_records:
        raise RestoreValidationError("first restore unexpectedly created backup history")

    consumed_record = consumed[0]
    original_record = {
        key: value
        for key, value in consumed_record.items()
        if key in _HISTORY_RECORD_FIELDS
    }
    if not _valid_history_record(original_record):
        raise RestoreValidationError("validated restore source history cannot be restored")
    restored: list[object] = []
    for item in backups:
        if item is consumed_record:
            restored.append(original_record)
        elif target_existed and item is restore_records[0]:
            continue
        else:
            restored.append(item)
    history["backups"] = restored
    snapshot.assert_current()
    expected = _json_bytes(history)
    _replace_bytes(snapshot.path, expected, mode=snapshot.mode)
    current = ConfigSnapshot.capture(snapshot.path)
    if current.data != expected:
        raise AtomicStateUnknownError(
            "restore history rollback publication could not be proven"
        )


def recover_interrupted_profile_restore(target: Path, payload: dict[str, Any]) -> None:
    """Identity-gated rollback/finalize for one typed restore transaction."""

    if payload.get("operation") != "restore-profile":
        raise RestoreValidationError("replacement journal is not a profile restore")
    restore_id = payload.get("transaction_id")
    target_raw = payload.get("target")
    selected_raw = payload.get("source")
    current_backup_raw = payload.get("backup")
    phase = payload.get("phase")
    identities = payload.get("identities")
    if (
        not isinstance(restore_id, str)
        or not isinstance(target_raw, str)
        or not isinstance(selected_raw, str)
        or not isinstance(current_backup_raw, str)
        or phase
        not in {
            "prepared",
            "target_parked",
            "candidate_published_unvalidated",
            "validated",
            "committed",
        }
        or not isinstance(identities, dict)
    ):
        raise RestoreValidationError("restore journal metadata is incomplete")
    try:
        if str(uuid.UUID(restore_id)) != restore_id:
            raise ValueError
    except ValueError as exc:
        raise RestoreValidationError("restore journal transaction id is invalid") from exc

    target = target.expanduser().absolute()
    selected_backup = Path(selected_raw).expanduser().absolute()
    current_backup = Path(current_backup_raw).expanduser().absolute()
    expected_current = target.with_name(f"{target.name}.backup.{restore_id}")
    if (
        _normalized(Path(target_raw)) != _normalized(target)
        or _normalized(current_backup) != _normalized(expected_current)
        or _normalized(selected_backup.parent) != _normalized(target.parent)
        or payload.get("staging") not in {"", None}
    ):
        raise RestoreValidationError("restore journal paths are outside the target transaction")

    from opensquilla.recovery.transaction import _load_typed_transaction

    _journal_snapshot, exact_payload = _load_typed_transaction(target)
    if exact_payload != payload or exact_payload.get("operation") != "restore-profile":
        raise RestoreValidationError("restore journal changed or has an unsafe schema")

    target_existed = payload.get("target_existed") is True
    source_identity = identities.get("source")
    original_identity = identities.get("original_target")
    backup_identity = identities.get("backup") or original_identity
    candidate_identity = identities.get("candidate") or source_identity
    journal = target.parent / f".{target.name}.profile-replace.json"

    def target_is_candidate() -> bool:
        return _object_identity_matches(target, candidate_identity)

    def target_is_original() -> bool:
        return _object_identity_matches(target, original_identity)

    if phase != "committed":
        target_present = os.path.lexists(target)
        selected_present = os.path.lexists(selected_backup)
        current_backup_present = os.path.lexists(current_backup)
        target_candidate = target_present and target_is_candidate()
        target_original = target_present and target_existed and target_is_original()
        selected_candidate = selected_present and _object_identity_matches(
            selected_backup,
            source_identity,
        )
        current_original = (
            current_backup_present
            and target_existed
            and _object_identity_matches(current_backup, backup_identity)
        )
        if target_present and not (target_candidate or target_original):
            raise RestoreValidationError("restore target identity is ambiguous")
        if selected_present and not selected_candidate:
            raise RestoreValidationError("selected restore backup identity changed")
        if current_backup_present and not current_original:
            raise RestoreValidationError("current target backup identity changed")
        if target_candidate and selected_present:
            raise RestoreValidationError("restore candidate exists at two paths")
        if target_original and current_backup_present:
            raise RestoreValidationError("original target exists at two paths")

        if phase == "validated":
            _rollback_validated_restore_history(
                target=target,
                selected_backup=selected_backup,
                current_backup=current_backup,
                restore_id=restore_id,
                target_existed=target_existed,
            )
        if target_candidate:
            move_profile_no_replace(target, selected_backup, move=native_move_no_replace)
            _fsync_directory(target.parent)
            selected_present = True
            selected_candidate = True
            target_present = False
            target_original = False
        if not selected_candidate:
            raise RestoreValidationError("restore candidate cannot be returned to its backup path")
        if target_existed:
            if target_original:
                if current_backup_present:
                    raise RestoreValidationError("original restore target is duplicated")
            elif not target_present and current_original:
                move_profile_no_replace(
                    current_backup,
                    target,
                    move=native_move_no_replace,
                )
                _fsync_directory(target.parent)
                target_present = True
                target_original = True
                current_backup_present = False
            else:
                raise RestoreValidationError("original restore target cannot be recovered")
        elif target_present or current_backup_present:
            raise RestoreValidationError("first restore rollback contains unexpected paths")
        _unlink_matching_restore_journal(journal, payload)
        return

    # A committed phase is removable only when target and recovery history
    # independently prove the completed restore.
    if not target_is_candidate() or os.path.lexists(selected_backup):
        raise RestoreValidationError("committed restore target identity is ambiguous")
    if _restore_history_state(
        target=target,
        selected_backup=selected_backup,
        current_backup=current_backup,
        restore_id=restore_id,
        target_existed=target_existed,
        candidate_identity=candidate_identity,
        current_backup_identity=backup_identity,
    ) != "committed":
        raise RestoreValidationError("committed restore history is incomplete")
    from opensquilla.recovery.transaction import (
        finalize_committed_profile_transaction,
    )

    if not finalize_committed_profile_transaction(target):
        raise RestoreValidationError("committed restore journal could not be finalized")


__all__ = [
    "recorded_backup_target",
    "recover_interrupted_profile_restore",
    "restore_profile",
]
