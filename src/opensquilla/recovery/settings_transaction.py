"""Crash-recoverable Desktop credential/config pair updates.

The Desktop process sends candidate bytes over the child process stdin.  This
module never accepts credentials on argv and never includes file contents or
content digests in its journal, protocol, exceptions, or logs.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tomllib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opensquilla.recovery.atomic import (
    PathIdentity,
    _chmod_open_file,
    _native_io_path,
    native_move_no_replace,
)
from opensquilla.recovery.config_patch import ConfigSnapshot
from opensquilla.recovery.errors import AtomicStateUnknownError, RecoveryError
from opensquilla.recovery.locking import LegacyGatewayLock, ProfileOperationLock
from opensquilla.recovery.models import RecoveryReport

SETTINGS_TRANSACTION_SCHEMA_VERSION = 1
MAX_SETTINGS_INPUT_BYTES = 8 * 1024 * 1024
_DESKTOP_PROFILE_KINDS = frozenset({"desktop-primary", "desktop-recovery"})
_SETTINGS_TRANSACTION_PHASES = frozenset(
    {"prepared", "credential_published", "config_published", "committed"}
)


def _normalized(path: str | Path) -> str:
    candidate = Path(path).expanduser()
    try:
        candidate = candidate.resolve(strict=False)
    except OSError:
        candidate = candidate.absolute()
    return os.path.normcase(os.path.normpath(str(candidate)))


def settings_transaction_journal(home: str | Path) -> Path:
    home_path = Path(home).expanduser().absolute()
    return home_path.parent / f".{home_path.name}.desktop-settings-transaction.json"


def _lexists(path: str | Path) -> bool:
    return os.path.lexists(_native_io_path(path))


def _native_lstat(path: str | Path) -> os.stat_result:
    return os.lstat(_native_io_path(path))


def settings_transaction_exists(home: str | Path) -> bool:
    try:
        return _lexists(settings_transaction_journal(home))
    except OSError:
        return True


def _require_desktop_profile_kind() -> None:
    profile_kind = os.environ.get("OPENSQUILLA_PROFILE_KIND", "").strip().lower()
    if profile_kind not in _DESKTOP_PROFILE_KINDS:
        raise RecoveryError(
            "Desktop settings transactions require an explicit Desktop profile kind",
            stable_code="settings_profile_kind_invalid",
        )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short settings transaction write")
        view = view[count:]


def _write_no_replace(path: Path, data: bytes, *, mode: int = 0o600) -> PathIdentity:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(_native_io_path(path), flags, mode)
    try:
        with contextlib.suppress(OSError):
            _chmod_open_file(fd, mode)
        _write_all(fd, data)
        os.fsync(fd)
        value = os.fstat(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(_native_io_path(path))
        raise
    os.close(fd)
    _fsync_directory(path.parent)
    return PathIdentity.from_stat(value)


def _restrict_existing_credential_to_owner(path: Path) -> None:
    snapshot = ConfigSnapshot.capture(path)
    if snapshot.identity is None:
        return
    can_harden_posix_mode = callable(getattr(os, "fchmod", None))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(_native_io_path(path), flags)
    try:
        if PathIdentity.from_stat(os.fstat(fd)) != snapshot.identity:
            raise AtomicStateUnknownError("Desktop credential changed before backup hardening")
        if can_harden_posix_mode:
            _chmod_open_file(fd, 0o600)
            os.fsync(fd)
    finally:
        os.close(fd)
    after = ConfigSnapshot.capture(path)
    if (
        after.identity is None
        or after.data != snapshot.data
        or (can_harden_posix_mode and after.identity.mode & 0o777 != 0o600)
    ):
        raise AtomicStateUnknownError("Desktop credential backup is not owner-only")


def _durable_move_no_replace(source: Path, destination: Path) -> None:
    # Keep every settings publication on the shared handle/dirfd-bound
    # no-replace primitive.  A path-based Windows MoveFileExW call leaves a gap
    # in which either parent can be exchanged for a junction after preflight.
    # POSIX can additionally fsync the containing directory; Windows has no
    # equivalent directory-fsync contract here, so safety must not be weakened
    # by falling back to a path-only WRITE_THROUGH move.
    native_move_no_replace(source, destination)
    _fsync_directory(destination.parent)


def _write_journal(path: Path, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
    _write_no_replace(path, data)


def _identity_payload(identity: PathIdentity) -> dict[str, int]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "mode": identity.mode,
        "size": identity.size,
        "modified_at_ns": identity.modified_at_ns,
    }


def _identity_matches(path: Path, expected: object) -> bool:
    if not isinstance(expected, dict):
        return False
    try:
        value = _native_lstat(path)
    except OSError:
        return False
    attributes = int(getattr(value, "st_file_attributes", 0))
    if stat.S_ISLNK(value.st_mode) or attributes & 0x400 or not stat.S_ISREG(value.st_mode):
        return False
    identity = PathIdentity.from_stat(value)
    current = _identity_payload(identity)
    return all(current[key] == expected.get(key) for key in current)


def _plain_directory(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            os.mkdir(_native_io_path(path), mode=0o700)
        except FileExistsError:
            pass
    try:
        value = _native_lstat(path)
    except OSError as exc:
        raise RecoveryError(
            "Desktop settings parent is unavailable",
            stable_code="settings_parent_unavailable",
        ) from exc
    attributes = int(getattr(value, "st_file_attributes", 0))
    if stat.S_ISLNK(value.st_mode) or attributes & 0x400 or not stat.S_ISDIR(value.st_mode):
        raise RecoveryError(
            "Desktop settings parent is unsafe",
            stable_code="settings_parent_unsafe",
        )
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def _parse_input(payload: object) -> tuple[str | None, str, str | None, str]:
    if not isinstance(payload, dict) or set(payload) != {
        "expected_config",
        "config",
        "expected_credential",
        "credential",
    }:
        raise RecoveryError(
            "Desktop settings input schema is invalid",
            stable_code="settings_input_invalid",
        )
    expected_config = payload.get("expected_config")
    config = payload.get("config")
    expected_credential = payload.get("expected_credential")
    credential = payload.get("credential")
    if (
        expected_config is not None
        and not isinstance(expected_config, str)
        or expected_credential is not None
        and not isinstance(expected_credential, str)
        or not isinstance(config, str)
        or not isinstance(credential, str)
    ):
        raise RecoveryError(
            "Desktop settings input types are invalid",
            stable_code="settings_input_invalid",
        )
    if len(config.encode()) > MAX_SETTINGS_INPUT_BYTES or len(credential.encode()) > 1024 * 1024:
        raise RecoveryError(
            "Desktop settings input is too large",
            stable_code="settings_input_too_large",
        )
    try:
        config_payload = tomllib.loads(config)
        credential_payload = json.loads(credential)
    except (tomllib.TOMLDecodeError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            "Desktop settings candidate is invalid",
            stable_code="settings_input_invalid",
        ) from exc
    if not isinstance(config_payload, dict) or not isinstance(credential_payload, dict):
        raise RecoveryError(
            "Desktop settings candidate is invalid",
            stable_code="settings_input_invalid",
        )
    return expected_config, config, expected_credential, credential


def _assert_imported_credential_matches_config(
    config_text: str,
    credential: dict[str, Any],
) -> None:
    if credential.get("configAuthority") != "profile":
        return
    try:
        config = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as exc:
        raise RecoveryError(
            "Imported Desktop credential cannot be bound to config",
            stable_code="settings_credential_config_mismatch",
        ) from exc
    llm = config.get("llm")
    if not isinstance(llm, dict):
        raise RecoveryError(
            "Imported Desktop credential requires an LLM config",
            stable_code="settings_credential_config_mismatch",
        )
    fields = (
        ("provider", "provider", True),
        ("model", "model", False),
        ("base_url", "baseUrl", False),
        ("api_key_env", "apiKeyEnv", False),
    )
    for config_key, credential_key, required in fields:
        config_value = llm.get(config_key, "")
        credential_value = credential.get(credential_key, "")
        if not isinstance(config_value, str) or not isinstance(credential_value, str):
            raise RecoveryError(
                "Imported Desktop credential connection is invalid",
                stable_code="settings_credential_config_mismatch",
            )
        expected = config_value.strip()
        actual = credential_value.strip()
        if config_key == "provider":
            expected = expected.lower()
            actual = actual.lower()
        if (required and not expected) or (expected and expected != actual):
            raise RecoveryError(
                "Imported Desktop credential does not match config",
                stable_code="settings_credential_config_mismatch",
            )


def _assert_cas(snapshot: ConfigSnapshot, expected: str | None, *, label: str) -> None:
    if expected is None:
        matches = snapshot.identity is None
    else:
        try:
            expected_bytes = expected.encode("utf-8")
        except UnicodeEncodeError:
            matches = False
        else:
            matches = snapshot.identity is not None and snapshot.data == expected_bytes
    if not matches:
        raise RecoveryError(
            f"Desktop {label} changed after settings preflight",
            stable_code="settings_source_changed",
        )


def _resolved_config_path(home: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = home / candidate
    return candidate.absolute()


def _assert_data_roots_unchanged(
    home: Path,
    old_data: bytes,
    new_text: str,
    inspection: RecoveryReport,
) -> None:
    try:
        old = tomllib.loads(old_data.decode("utf-8")) if old_data else {}
        new = tomllib.loads(new_text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RecoveryError(
            "Desktop config cannot be compared safely",
            stable_code="settings_input_invalid",
        ) from exc
    for key in ("workspace_dir", "state_dir", "media_dir"):
        if key in old and new.get(key) != old.get(key):
            raise RecoveryError(
                "Desktop settings cannot change profile data roots",
                stable_code="settings_data_root_changed",
            )
    expected_workspace = inspection.effective_workspace
    expected_state = next(
        (candidate.path for candidate in inspection.candidates if candidate.kind == "state"),
        None,
    )
    for key, expected in (
        ("workspace_dir", expected_workspace),
        ("state_dir", expected_state),
    ):
        if key not in old and key in new:
            candidate = _resolved_config_path(home, new.get(key))
            if (
                expected is None
                or candidate is None
                or _normalized(candidate) != _normalized(expected)
            ):
                raise RecoveryError(
                    "Desktop settings cannot introduce a different profile data root",
                    stable_code="settings_data_root_changed",
                )
    if "media_dir" not in old and "media_dir" in new:
        raise RecoveryError(
            "Desktop settings cannot introduce a media data root",
            stable_code="settings_data_root_changed",
        )
    old_attachments = old.get("attachments")
    new_attachments = new.get("attachments")
    old_media_root = (
        old_attachments.get("media_root") if isinstance(old_attachments, dict) else None
    )
    new_media_root = (
        new_attachments.get("media_root") if isinstance(new_attachments, dict) else None
    )
    old_has_media_root = isinstance(old_attachments, dict) and "media_root" in old_attachments
    new_has_media_root = isinstance(new_attachments, dict) and "media_root" in new_attachments
    if (
        old_has_media_root != new_has_media_root
        or (old_has_media_root and new_media_root != old_media_root)
    ):
        raise RecoveryError(
            "Desktop settings cannot change attachment media data roots",
            stable_code="settings_data_root_changed",
        )


def _artifact_paths(
    home: Path,
    transaction_id: str,
    import_transaction_id: str = "",
) -> dict[str, Path]:
    credential = home.parent / "desktop-credential.json"
    config = home / "config.toml"
    journal = settings_transaction_journal(home)
    return {
        "credential": credential,
        "config": config,
        "credential_new": credential.with_name(f".{credential.name}.{transaction_id}.new"),
        "config_new": config.with_name(f".{config.name}.{transaction_id}.new"),
        "credential_backup": credential.with_name(
            f"desktop-credential.import-backup.{import_transaction_id}.json"
            if import_transaction_id
            else f".{credential.name}.{transaction_id}.old"
        ),
        "config_backup": config.with_name(f".{config.name}.{transaction_id}.old"),
        "journal_committed": journal.with_name(
            f".{home.name}.desktop-settings.{transaction_id}.committed.json"
        ),
    }


def _old_destination_matches(path: Path, expected: object) -> bool:
    if expected is None:
        return not _lexists(path)
    return _identity_matches(path, expected)


def _park_old_destination(
    live: Path,
    backup: Path,
    expected_old: object,
) -> None:
    if expected_old is None:
        if _lexists(live) or _lexists(backup):
            raise AtomicStateUnknownError("settings destination changed before publication")
        return
    if _identity_matches(backup, expected_old):
        if _lexists(live):
            raise AtomicStateUnknownError("settings destination was recreated after parking")
        return
    if _lexists(backup) or not _identity_matches(live, expected_old):
        raise AtomicStateUnknownError("settings destination changed before it could be parked")
    _durable_move_no_replace(live, backup)
    if not _identity_matches(backup, expected_old):
        raise AtomicStateUnknownError("settings destination changed while it was being parked")


def _publish(
    source: Path,
    destination: Path,
    expected: object,
) -> None:
    if not _identity_matches(source, expected):
        raise AtomicStateUnknownError("settings transaction candidate identity is ambiguous")
    if _lexists(destination):
        raise AtomicStateUnknownError("settings transaction destination changed before publication")
    _durable_move_no_replace(source, destination)
    if not _identity_matches(destination, expected):
        raise AtomicStateUnknownError("settings transaction publication state is ambiguous")


def _cleanup_committed(journal: Path, payload: dict[str, Any], paths: dict[str, Path]) -> None:
    identities = payload.get("identities")
    if not isinstance(identities, dict):
        raise AtomicStateUnknownError("settings transaction identities are unavailable")
    snapshot = ConfigSnapshot.capture(journal)
    if snapshot.identity is None:
        raise AtomicStateUnknownError("settings transaction journal disappeared before commit")
    _durable_move_no_replace(journal, paths["journal_committed"])
    committed = ConfigSnapshot.capture(paths["journal_committed"])
    if (
        committed.identity is None
        or committed.identity != snapshot.identity
        or committed.data != snapshot.data
    ):
        raise AtomicStateUnknownError("settings transaction commit receipt is ambiguous")

    # The immutable committed receipt is the point of no return. Cleanup below
    # is best-effort and identity-gated: a changed artifact is retained for
    # diagnostics rather than overwritten, merged, or removed.
    retained_credential_backup = bool(payload.get("import_transaction_id"))
    for role in ("credential_backup", "config_backup", "credential_new", "config_new"):
        if role == "credential_backup" and retained_credential_backup:
            continue
        expected = identities.get(role)
        if expected is not None and _identity_matches(paths[role], expected):
            with contextlib.suppress(OSError):
                os.unlink(_native_io_path(paths[role]))
                _fsync_directory(paths[role].parent)
    _fsync_directory(paths["journal_committed"].parent)


def _rollback_one(
    *,
    live: Path,
    staged: Path,
    backup: Path,
    old_identity: object,
    new_identity: object,
    backup_identity: object,
) -> None:
    if _identity_matches(live, new_identity):
        if _lexists(staged):
            raise AtomicStateUnknownError("settings rollback destination is occupied")
        _durable_move_no_replace(live, staged)
    elif (
        old_identity is not None
        and not _lexists(live)
        and _identity_matches(backup, backup_identity)
    ):
        # The old file was parked, but this role's candidate was not published.
        # This is the normal rollback state for a later-role publication error.
        pass
    elif not _old_destination_matches(live, old_identity):
        raise AtomicStateUnknownError("settings rollback live identity is ambiguous")

    if old_identity is None:
        if _lexists(live):
            raise AtomicStateUnknownError("settings rollback could not restore missing state")
        return
    if _identity_matches(live, old_identity):
        return
    if not _identity_matches(backup, backup_identity):
        raise AtomicStateUnknownError("settings rollback backup identity is ambiguous")
    _durable_move_no_replace(backup, live)


def _rollback_settings(journal: Path, payload: dict[str, Any], paths: dict[str, Path]) -> None:
    identities = payload.get("identities")
    if not isinstance(identities, dict):
        raise AtomicStateUnknownError("settings rollback identities are unavailable")
    # Restore config first so no runtime can observe a new config with an old
    # credential if an operator ignores the bootstrap guard during recovery.
    for role in ("config", "credential"):
        _rollback_one(
            live=paths[role],
            staged=paths[f"{role}_new"],
            backup=paths[f"{role}_backup"],
            old_identity=identities.get(f"old_{role}"),
            new_identity=identities.get(f"{role}_new"),
            backup_identity=identities.get(f"{role}_backup"),
        )
    _cleanup_committed(journal, payload, paths)


def _load_journal(home: Path) -> tuple[Path, dict[str, Any], dict[str, Path]]:
    journal = settings_transaction_journal(home)
    snapshot = ConfigSnapshot.capture(journal)
    if snapshot.identity is None:
        raise RecoveryError(
            "Desktop settings transaction does not exist",
            stable_code="settings_transaction_missing",
        )
    try:
        payload = json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            "Desktop settings transaction journal is invalid",
            stable_code="settings_transaction_invalid",
        ) from exc
    if not isinstance(payload, dict):
        raise RecoveryError(
            "Desktop settings transaction journal is invalid",
            stable_code="settings_transaction_invalid",
        )
    transaction_id = payload.get("transaction_id")
    try:
        canonical_id = str(uuid.UUID(str(transaction_id)))
    except ValueError as exc:
        raise RecoveryError(
            "Desktop settings transaction id is invalid",
            stable_code="settings_transaction_invalid",
        ) from exc
    import_transaction_id = payload.get("import_transaction_id", "")
    if not isinstance(import_transaction_id, str):
        raise RecoveryError(
            "Desktop settings import transaction id is invalid",
            stable_code="settings_transaction_invalid",
        )
    if import_transaction_id:
        try:
            if str(uuid.UUID(import_transaction_id)) != import_transaction_id:
                raise ValueError
        except ValueError as exc:
            raise RecoveryError(
                "Desktop settings import transaction id is invalid",
                stable_code="settings_transaction_invalid",
            ) from exc
    paths = _artifact_paths(home, canonical_id, import_transaction_id)
    expected_paths = {key: _normalized(path) for key, path in paths.items()}
    if (
        payload.get("schema_version") != SETTINGS_TRANSACTION_SCHEMA_VERSION
        or payload.get("operation") != "desktop-settings"
        or transaction_id != canonical_id
        or payload.get("phase") not in _SETTINGS_TRANSACTION_PHASES
        or payload.get("home") != _normalized(home)
        or str(payload.get("import_transaction_id") or "") != import_transaction_id
        or payload.get("paths") != expected_paths
        or not isinstance(payload.get("fresh_canonical"), bool)
        or not isinstance(payload.get("identities"), dict)
    ):
        raise RecoveryError(
            "Desktop settings transaction journal is invalid",
            stable_code="settings_transaction_invalid",
        )
    return journal, payload, paths


def _assert_recovery_phase_state(
    payload: dict[str, Any],
    paths: dict[str, Path],
) -> None:
    identities = payload["identities"]

    def publication_state(role: str) -> tuple[bool, bool]:
        live = paths[role]
        staged = paths[f"{role}_new"]
        backup = paths[f"{role}_backup"]
        old_identity = identities.get(f"old_{role}")
        new_identity = identities.get(f"{role}_new")
        if old_identity is None:
            old_is_safe = not _lexists(backup)
            old_unpublished = old_is_safe and not _lexists(live)
        else:
            old_at_live = _identity_matches(live, old_identity) and not _lexists(backup)
            old_at_backup = _identity_matches(backup, old_identity) and not _lexists(live)
            old_is_safe = _identity_matches(backup, old_identity)
            old_unpublished = old_at_live or old_at_backup
        unpublished = (
            old_unpublished and _identity_matches(staged, new_identity)
        )
        published = (
            _identity_matches(live, new_identity)
            and not _lexists(staged)
            and old_is_safe
        )
        return unpublished, published

    credential_unpublished, credential_published = publication_state("credential")
    config_unpublished, config_published = publication_state("config")
    phase = payload["phase"]
    allowed = {
        # A process can die after the native publish and before its next journal
        # fsync.  New journals remain immutable in ``prepared`` and infer the
        # exact prefix from file identities; the legacy phase names stay readable
        # for pre-activation development fixtures.
        "prepared": (
            (credential_unpublished and config_unpublished)
            or (credential_published and config_unpublished)
            or (credential_published and config_published)
        ),
        "credential_published": (
            credential_published and (config_unpublished or config_published)
        ),
        "config_published": credential_published and config_published,
        "committed": credential_published and config_published,
    }
    if allowed.get(phase) is not True:
        raise AtomicStateUnknownError(
            "settings transaction phase and file identities are inconsistent"
        )


def recover_desktop_settings(
    home: str | Path,
    *,
    lock_timeout: float = 0.0,
) -> RecoveryReport:
    """Finish an identity-proven interrupted pair publication."""

    _require_desktop_profile_kind()
    home_path = Path(home).expanduser().absolute()
    with ProfileOperationLock(home_path, timeout=lock_timeout):
        with LegacyGatewayLock(home_path, timeout=lock_timeout):
            journal, payload, paths = _load_journal(home_path)
            identities = payload["identities"]
            credential_new = identities.get("credential_new")
            config_new = identities.get("config_new")
            credential_live_new = _identity_matches(paths["credential"], credential_new)
            config_live_new = _identity_matches(paths["config"], config_new)
            credential_staged = _identity_matches(paths["credential_new"], credential_new)
            config_staged = _identity_matches(paths["config_new"], config_new)
            _assert_recovery_phase_state(payload, paths)

            if payload.get("fresh_canonical") is True:
                _plain_directory(home_path)
                for canonical_root in (home_path / "workspace", home_path / "state"):
                    _plain_directory(canonical_root, create=True)

            if not credential_live_new:
                _park_old_destination(
                    paths["credential"],
                    paths["credential_backup"],
                    identities.get("old_credential"),
                )
                if not credential_staged:
                    raise AtomicStateUnknownError(
                        "settings credential publication state is ambiguous"
                    )
                _publish(
                    paths["credential_new"],
                    paths["credential"],
                    credential_new,
                )
            if not config_live_new:
                _park_old_destination(
                    paths["config"],
                    paths["config_backup"],
                    identities.get("old_config"),
                )
                if not config_staged:
                    raise AtomicStateUnknownError("settings config publication state is ambiguous")
                _publish(
                    paths["config_new"],
                    paths["config"],
                    config_new,
                )
            _cleanup_committed(journal, payload, paths)

    from opensquilla.recovery.engine import inspect_profile

    return inspect_profile(home_path)


def apply_desktop_settings(
    home: str | Path,
    *,
    transaction_id: str,
    expected_revision: int,
    payload: object,
    lock_timeout: float = 0.0,
    _failpoint: Callable[[str], None] | None = None,
) -> RecoveryReport:
    """CAS-check and durably publish one credential/config pair."""

    _require_desktop_profile_kind()
    expected_config, config_text, expected_credential, credential_text = _parse_input(payload)
    candidate_credential = json.loads(credential_text)
    config_authority = candidate_credential.get("configAuthority")
    import_transaction_id = candidate_credential.get("importTransactionId")
    if config_authority == "profile":
        try:
            import_transaction_id = str(uuid.UUID(str(import_transaction_id)))
        except ValueError as exc:
            raise RecoveryError(
                "Imported Desktop credential transaction id is invalid",
                stable_code="settings_credential_invalid",
            ) from exc
    elif import_transaction_id not in {None, ""}:
        raise RecoveryError(
            "Generated Desktop credential cannot retain an import transaction",
            stable_code="settings_credential_invalid",
        )
    else:
        import_transaction_id = ""
    _assert_imported_credential_matches_config(config_text, candidate_credential)
    home_path = Path(home).expanduser().absolute()
    journal = settings_transaction_journal(home_path)
    callback = _failpoint or (lambda _phase: None)

    with ProfileOperationLock(home_path, timeout=lock_timeout):
        with LegacyGatewayLock(home_path, timeout=lock_timeout):
            if _lexists(journal):
                raise RecoveryError(
                    "An interrupted Desktop settings transaction must be recovered first",
                    stable_code="settings_transaction_incomplete",
                )
            from opensquilla.recovery.engine import inspect_profile

            before = inspect_profile(home_path, _ignore_settings_transaction=True)
            if (
                before.outcome == "recovery_required"
                or before.transaction_id != transaction_id
                or before.revision != expected_revision
            ):
                raise RecoveryError(
                    "Desktop profile changed after settings inspection",
                    stable_code="stale_recovery_transaction",
                )

            _plain_directory(home_path.parent)
            if not _lexists(home_path):
                _plain_directory(home_path, create=True)
            else:
                _plain_directory(home_path)
            operation_id = str(uuid.uuid4())
            paths = _artifact_paths(home_path, operation_id, import_transaction_id)
            if import_transaction_id:
                if _lexists(paths["credential_backup"]):
                    raise RecoveryError(
                        "The imported Desktop credential backup already exists",
                        stable_code="settings_credential_backup_exists",
                    )
                _restrict_existing_credential_to_owner(paths["credential"])
            config_snapshot = ConfigSnapshot.capture(paths["config"])
            credential_snapshot = ConfigSnapshot.capture(paths["credential"])
            _assert_cas(config_snapshot, expected_config, label="config")
            _assert_cas(credential_snapshot, expected_credential, label="credential")
            if credential_snapshot.identity is not None:
                try:
                    existing_credential = json.loads(credential_snapshot.data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RecoveryError(
                        "Existing Desktop credential is invalid",
                        stable_code="settings_credential_invalid",
                    ) from exc
                if not isinstance(existing_credential, dict):
                    raise RecoveryError(
                        "Existing Desktop credential is invalid",
                        stable_code="settings_credential_invalid",
                    )
            _assert_data_roots_unchanged(
                home_path,
                config_snapshot.data,
                config_text,
                before,
            )

            identities: dict[str, object] = {
                "old_credential": (
                    _identity_payload(credential_snapshot.identity)
                    if credential_snapshot.identity is not None
                    else None
                ),
                "old_config": (
                    _identity_payload(config_snapshot.identity)
                    if config_snapshot.identity is not None
                    else None
                ),
            }
            identities["credential_new"] = _identity_payload(
                _write_no_replace(paths["credential_new"], credential_text.encode())
            )
            identities["config_new"] = _identity_payload(
                _write_no_replace(paths["config_new"], config_text.encode())
            )
            # The live files themselves become the rollback backups.  Copying
            # bytes and then replacing the live pathname would reintroduce a
            # check-then-overwrite window and would also lose ACL/xattr identity.
            identities["credential_backup"] = identities["old_credential"]
            identities["config_backup"] = identities["old_config"]
            journal_payload: dict[str, Any] = {
                "schema_version": SETTINGS_TRANSACTION_SCHEMA_VERSION,
                "operation": "desktop-settings",
                "transaction_id": operation_id,
                "phase": "prepared",
                "home": _normalized(home_path),
                "import_transaction_id": import_transaction_id,
                "fresh_canonical": before.stable_code
                in {"fresh_profile", "fresh_recovery_profile"},
                "paths": {key: _normalized(path) for key, path in paths.items()},
                "identities": identities,
            }
            _write_journal(journal, journal_payload)
            commit_started = False
            try:
                if before.stable_code in {"fresh_profile", "fresh_recovery_profile"}:
                    for canonical_root in (home_path / "workspace", home_path / "state"):
                        _plain_directory(canonical_root, create=True)
                _park_old_destination(
                    paths["credential"],
                    paths["credential_backup"],
                    identities["old_credential"],
                )
                _park_old_destination(
                    paths["config"],
                    paths["config_backup"],
                    identities["old_config"],
                )
                callback("prepared")
                _publish(
                    paths["credential_new"],
                    paths["credential"],
                    identities["credential_new"],
                )
                callback("credential_published")
                _publish(
                    paths["config_new"],
                    paths["config"],
                    identities["config_new"],
                )
                callback("config_published")
                commit_started = True
                _cleanup_committed(journal, journal_payload, paths)
            except Exception as exc:
                if commit_started:
                    raise AtomicStateUnknownError(
                        "Desktop settings commit state is uncertain"
                    ) from exc
                if before.stable_code in {"fresh_profile", "fresh_recovery_profile"}:
                    raise AtomicStateUnknownError(
                        "Fresh Desktop settings publication must be recovered before bootstrap"
                    ) from exc
                try:
                    _rollback_settings(journal, journal_payload, paths)
                except Exception as rollback_error:
                    raise AtomicStateUnknownError(
                        "Desktop settings failed and rollback state is uncertain"
                    ) from rollback_error
                if isinstance(exc, RecoveryError):
                    raise
                raise RecoveryError(
                    "Desktop settings transaction was rolled back",
                    stable_code="settings_apply_failed",
                ) from exc

    return inspect_profile(home_path)


__all__ = [
    "MAX_SETTINGS_INPUT_BYTES",
    "apply_desktop_settings",
    "recover_desktop_settings",
    "settings_transaction_exists",
    "settings_transaction_journal",
]
