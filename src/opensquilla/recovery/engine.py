"""Read-only inspection and conservative RC4 Desktop profile reconciliation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import tomllib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from opensquilla.recovery.atomic import (
    PathIdentity,
    _chmod_open_file,
    _native_io_path,
    is_path_redirecting_stat,
    native_move_no_replace,
)
from opensquilla.recovery.config_patch import (
    ConfigSnapshot,
    patch_workspace_dir,
    recover_workspace_patch,
    state_override,
    workspace_override,
    workspace_patch_exists,
)
from opensquilla.recovery.errors import (
    AtomicStateUnknownError,
    InvalidWorkspaceError,
    RecoveryError,
    RecoveryRequiredError,
    StaleRecoveryTransactionError,
    WorkspaceOverrideError,
)
from opensquilla.recovery.locking import (
    LegacyGatewayLock,
    ProfileOperationLock,
    acquire_profile_locks,
    replacement_history_lock_scope,
    resolve_home_link,
)
from opensquilla.recovery.models import RecoveryOutcome, RecoveryReport, WorkspaceCandidate

SUPPORTED_CONFIG_VERSION = 1
_IMPORT_LAYOUT_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "transaction_id",
        "imported_at",
        "source",
        "source_identity",
        "source_kind",
        "source_version",
        "target",
        "candidate_identity",
        "recovery_outcome",
        "recovery_stable_code",
        "layout",
    }
)
_IMPORT_RECEIPT_IDENTITY_FIELDS = frozenset(
    {"device", "inode", "file_type", "mode", "size", "modified_at_ns"}
)
_IMPORT_SOURCE_KINDS = frozenset({"cli-home", "windows-portable", "desktop-home"})
_REPLACE_JOURNAL_IDENTITY_FIELDS = frozenset(
    {"source", "original_target", "staging", "backup", "candidate"}
)
_IMPORT_REPLACE_JOURNAL_FIELDS = frozenset(
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
_RESTORE_REPLACE_JOURNAL_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "transaction_id",
        "phase",
        "source",
        "target",
        "backup",
        "staging",
        "target_existed",
        "identities",
    }
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

_PROFILE_EVIDENCE_NAMES = frozenset(
    {
        "config.toml",
        "state",
        "workspace",
        "skills",
        "media",
        "session-archive",
        "router",
        ".env",
        "desktop-layout-v2.json",
        "desktop-recovery-v1.json",
    }
)
_WORKSPACE_IDENTITY_FILES = frozenset(
    {
        "AGENTS.md",
        "BOOTSTRAP.md",
        "IDENTITY.md",
        "MEMORY.md",
        "SOUL.md",
        "TOOLS.md",
        "USER.md",
    }
)
_LEGACY_HOME_ROLES: tuple[tuple[str, str, str], ...] = (
    ("workspace", "directory", "workspace"),
    ("skills", "directory", "skills"),
    ("skills-taps", "file", "skills-taps.json"),
    ("skills-lock", "file", "skills-lock.json"),
    ("session-archive", "directory", "session-archive"),
    ("router", "directory", "router"),
    ("dotenv", "file", ".env"),
)
_DESKTOP_PROFILE_KINDS = frozenset({"desktop-primary", "desktop-recovery"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_TRANSACTION_NAMESPACE = uuid.UUID("f287c709-543b-4ae8-9377-a65fd9a6e493")

_ATTENTION_ACTIONS = (
    "keep-current-workspace",
    "choose-workspace",
    "browse-workspace",
)
_RECOVERY_ACTIONS = (
    "show-backups",
    "copy-diagnostics",
)
_CLEANUP_RECOVERY_ACTIONS = ("abandon-cleanup", *_RECOVERY_ACTIONS)
_WORKSPACE_RECOVERY_ACTIONS = ("choose-workspace", *_RECOVERY_ACTIONS)
_UNSAFE_RECOVERY_PROFILE_ACTIONS = (
    "show-backups",
    "copy-diagnostics",
)


@dataclass(frozen=True)
class _ConfigView:
    path: Path
    exists: bool
    payload: dict[str, Any] | None
    workspace: Path | None
    workspace_explicit: bool
    workspace_from_env: bool
    state_dir: Path | None
    state_explicit: bool = False
    state_from_env: bool = False
    error_code: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True)
class _LegacyRole:
    name: str
    source: Path
    destination: Path
    disposition: str


def _absolute(path: str | Path, *, relative_to: Path | None = None) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute() and relative_to is not None:
        value = relative_to / value
    return value.absolute()


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(_absolute(path))))


def _same_path(left: str | Path, right: str | Path) -> bool:
    return _path_key(left) == _path_key(right)


def _lexists(path: str | Path) -> bool:
    return os.path.lexists(_native_io_path(path))


def _native_lstat(path: str | Path) -> os.stat_result:
    return os.lstat(_native_io_path(path))


def _native_stat(path: str | Path) -> os.stat_result:
    return os.stat(_native_io_path(path))


def _native_access(path: str | Path, mode: int) -> bool:
    return os.access(_native_io_path(path), mode)


def _native_is_file(path: str | Path) -> bool:
    return os.path.isfile(_native_io_path(path))


def _native_is_dir(path: str | Path) -> bool:
    return os.path.isdir(_native_io_path(path))


def _read_config(home: Path) -> _ConfigView:
    config_path = home / "config.toml"
    try:
        workspace_env = workspace_override(home)
    except WorkspaceOverrideError as exc:
        return _ConfigView(
            path=config_path,
            exists=_lexists(config_path),
            payload=None,
            workspace=None,
            workspace_explicit=False,
            workspace_from_env=True,
            state_dir=None,
            error_code=exc.stable_code,
            error_detail=str(exc),
        )
    try:
        state_env = state_override(home)
    except RecoveryError as exc:
        return _ConfigView(
            path=config_path,
            exists=_lexists(config_path),
            payload=None,
            workspace=None,
            workspace_explicit=False,
            workspace_from_env=workspace_env is not None,
            state_dir=None,
            state_from_env=True,
            error_code=exc.stable_code,
            error_detail=str(exc),
        )
    try:
        snapshot = ConfigSnapshot.capture(config_path)
    except RecoveryError as exc:
        return _ConfigView(
            path=config_path,
            exists=True,
            payload=None,
            workspace=None,
            workspace_explicit=False,
            workspace_from_env=workspace_env is not None,
            state_dir=None,
            error_code=(
                "config_unsafe_path" if exc.stable_code == "unsafe_path" else "config_unreadable"
            ),
            error_detail=str(exc),
        )
    exists = snapshot.identity is not None
    if not exists:
        payload: dict[str, Any] | None = {}
    else:
        try:
            payload = tomllib.loads(snapshot.data.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            return _ConfigView(
                path=config_path,
                exists=True,
                payload=None,
                workspace=None,
                workspace_explicit=False,
                workspace_from_env=workspace_env is not None,
                state_dir=None,
                error_code="config_invalid",
                error_detail=f"config.toml is not valid TOML: {exc}",
            )

    assert payload is not None
    config_version = payload.get("config_version", 0)
    if isinstance(config_version, bool) or not isinstance(config_version, int):
        return _ConfigView(
            path=config_path,
            exists=exists,
            payload=payload,
            workspace=None,
            workspace_explicit=False,
            workspace_from_env=workspace_env is not None,
            state_dir=None,
            error_code="config_invalid",
            error_detail="config_version must be an integer",
        )
    if config_version > SUPPORTED_CONFIG_VERSION:
        return _ConfigView(
            path=config_path,
            exists=exists,
            payload=payload,
            workspace=None,
            workspace_explicit=False,
            workspace_from_env=workspace_env is not None,
            state_dir=None,
            error_code="config_schema_too_new",
            error_detail=(
                f"config_version {config_version} is newer than this build supports "
                f"({SUPPORTED_CONFIG_VERSION}); upgrade OpenSquilla instead of editing"
            ),
        )

    state_raw = payload.get("state_dir")
    if state_raw is not None and not isinstance(state_raw, str):
        return _ConfigView(
            path=config_path,
            exists=exists,
            payload=payload,
            workspace=None,
            workspace_explicit=False,
            workspace_from_env=workspace_env is not None,
            state_dir=None,
            error_code="config_invalid",
            error_detail="state_dir must be a path string",
        )
    if state_env is not None:
        _state_name, state_value = state_env
        state_dir = _absolute(state_value, relative_to=home)
        state_explicit = True
        state_from_env = True
    elif isinstance(state_raw, str) and state_raw.strip():
        state_dir = _absolute(state_raw, relative_to=home)
        state_explicit = True
        state_from_env = False
    else:
        state_dir = home / "state"
        state_explicit = False
        state_from_env = False

    workspace_raw = payload.get("workspace_dir")
    if workspace_raw is not None and not isinstance(workspace_raw, str):
        return _ConfigView(
            path=config_path,
            exists=exists,
            payload=payload,
            workspace=None,
            workspace_explicit=False,
            workspace_from_env=workspace_env is not None,
            state_dir=state_dir,
            state_explicit=state_explicit,
            state_from_env=state_from_env,
            error_code="config_invalid",
            error_detail="workspace_dir must be a path string",
        )
    if workspace_env is None or state_env is None:
        legacy_probe = _ConfigView(
            path=config_path,
            exists=exists,
            payload=payload,
            workspace=None,
            workspace_explicit=False,
            workspace_from_env=workspace_env is not None,
            state_dir=state_dir,
            state_explicit=state_explicit,
            state_from_env=state_from_env,
        )
        if _legacy_layout_is_proven(home, legacy_probe):
            if workspace_env is None:
                try:
                    workspace_env = workspace_override(home, include_legacy_dotenv=True)
                except WorkspaceOverrideError as exc:
                    return replace(
                        legacy_probe,
                        payload=None,
                        workspace_from_env=True,
                        error_code=exc.stable_code,
                        error_detail=str(exc),
                    )
            if state_env is None:
                try:
                    state_env = state_override(home, include_legacy_dotenv=True)
                except RecoveryError as exc:
                    return replace(
                        legacy_probe,
                        payload=None,
                        state_from_env=True,
                        error_code=exc.stable_code,
                        error_detail=str(exc),
                    )
                if state_env is not None:
                    _state_name, state_value = state_env
                    state_dir = _absolute(state_value, relative_to=home)
                    state_explicit = True
                    state_from_env = True
    if workspace_env is not None:
        _name, override_value = workspace_env
        workspace = _absolute(override_value, relative_to=home)
        explicit = True
        from_env = True
    elif isinstance(workspace_raw, str) and workspace_raw.strip():
        workspace = _absolute(workspace_raw, relative_to=home)
        explicit = True
        from_env = False
    else:
        workspace = home / "workspace"
        explicit = False
        from_env = False
    return _ConfigView(
        path=config_path,
        exists=exists,
        payload=payload,
        workspace=workspace,
        workspace_explicit=explicit,
        workspace_from_env=from_env,
        state_dir=state_dir,
        state_explicit=state_explicit,
        state_from_env=state_from_env,
    )


def _workspace_status(path: Path, *, explicitly_configured: bool) -> WorkspaceCandidate:
    try:
        own_stat = _native_lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return WorkspaceCandidate(
            kind="configured" if explicitly_configured else "workspace",
            path=path,
            exists=False,
            valid=False,
            configured=explicitly_configured,
        )
    except OSError:
        return WorkspaceCandidate(
            kind="configured" if explicitly_configured else "workspace",
            path=path,
            exists=True,
            valid=False,
            configured=explicitly_configured,
        )

    is_link = is_path_redirecting_stat(own_stat)
    try:
        target_stat = _native_stat(path) if is_link and explicitly_configured else own_stat
        valid = stat.S_ISDIR(target_stat.st_mode) and _native_access(path, os.R_OK | os.X_OK)
    except OSError:
        target_stat = own_stat
        valid = False
    if is_link and not explicitly_configured:
        valid = False
    identity = PathIdentity.from_stat(target_stat)
    return WorkspaceCandidate(
        kind="configured" if explicitly_configured else "workspace",
        path=path,
        exists=True,
        valid=valid,
        configured=explicitly_configured,
        identity=identity.token,
        modified_at_ns=identity.modified_at_ns,
    )


def _candidate_set(home: Path, config: _ConfigView) -> tuple[WorkspaceCandidate, ...]:
    canonical = home / "workspace"
    legacy = home / "state" / "workspace"
    paths: list[tuple[str, Path, bool, bool]] = [
        (
            "canonical",
            canonical,
            config.workspace is not None and _same_path(config.workspace, canonical),
            config.workspace_explicit
            and config.workspace is not None
            and _same_path(config.workspace, canonical),
        ),
        (
            "legacy",
            legacy,
            config.workspace is not None and _same_path(config.workspace, legacy),
            config.workspace_explicit
            and config.workspace is not None
            and _same_path(config.workspace, legacy),
        ),
    ]
    if config.workspace is not None and not any(
        _same_path(config.workspace, path) for _, path, _, _ in paths
    ):
        paths.append(("external", config.workspace, True, True))
    candidates: list[WorkspaceCandidate] = []
    for kind, path, configured, explicit in paths:
        candidate = _workspace_status(path, explicitly_configured=explicit)
        candidates.append(
            WorkspaceCandidate(
                kind=kind,
                path=candidate.path,
                exists=candidate.exists,
                valid=candidate.valid,
                configured=configured,
                identity=candidate.identity,
                modified_at_ns=candidate.modified_at_ns,
            )
        )
    if config.state_dir is not None:
        state = _workspace_status(
            config.state_dir,
            explicitly_configured=config.state_explicit,
        )
        # ``candidates`` is intentionally the fixed extensible role list in
        # the recovery wire protocol.  A state candidate lets Desktop preserve
        # the authoritative chat database root without adding another field to
        # the RC4 schema.  UI workspace pickers must filter by kind.
        candidates.append(
            WorkspaceCandidate(
                kind="state",
                path=state.path,
                exists=state.exists,
                valid=state.valid
                and _native_access(state.path, os.R_OK | os.W_OK | os.X_OK),
                configured=True,
                identity=state.identity,
                modified_at_ns=state.modified_at_ns,
            )
        )
    return tuple(candidates)


def _migration_dir_candidates() -> tuple[Path, ...]:
    """Mirror gateway migration discovery without importing runtime bootstrap."""

    candidates: list[Path] = []
    configured = os.environ.get("OPENSQUILLA_MIGRATIONS_DIR", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    try:
        from importlib import resources as importlib_resources

        packaged = importlib_resources.files("opensquilla").joinpath("_migrations")
        if packaged.is_dir():
            candidates.append(Path(str(packaged)))
    except Exception:  # noqa: BLE001 - package resources are optional in checkouts
        pass
    candidates.append(Path(__file__).resolve().parents[3] / "migrations")
    return tuple(candidates)


def _known_migration_ids() -> set[str]:
    for directory in _migration_dir_candidates():
        try:
            known = {entry.stem for entry in directory.glob("V*.py") if entry.is_file()}
        except OSError:
            continue
        if known:
            return known
    return set()


@dataclass(frozen=True)
class _DatabaseSourceSnapshot:
    path: Path
    identity: PathIdentity
    digest: bytes


class _DatabaseSourceChangedError(Exception):
    """The source bundle did not remain stable during offline inspection."""


def _regular_source_stat(path: Path) -> os.stat_result | None:
    try:
        value = os.lstat(_native_io_path(path))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecoveryError(
            "runtime database bundle cannot be inspected",
            stable_code="state_database_unreadable",
        ) from exc
    if (
        is_path_redirecting_stat(value)
        or not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
    ):
        raise RecoveryError(
            "runtime database bundle contains an unsafe path",
            stable_code="state_database_unsafe_path",
        )
    return value


def _copy_source_file_no_follow(source: Path, destination: Path) -> _DatabaseSourceSnapshot:
    """Copy one stable regular file into a private validation directory.

    SQLite's read-only URI can still create a ``-shm`` file beside a WAL
    database.  Recovery therefore never opens the user database at all.  It
    copies the database and durable sidecars through no-follow descriptors and
    permits SQLite to create transient coordination files only in the private
    snapshot directory.
    """

    path_stat = _regular_source_stat(source)
    if path_stat is None:
        raise _DatabaseSourceChangedError
    source_flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    destination_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_fd = os.open(_native_io_path(source), source_flags)
    except FileNotFoundError as exc:
        raise _DatabaseSourceChangedError from exc
    except OSError as exc:
        raise RecoveryError(
            "runtime database bundle cannot be opened without following links",
            stable_code="state_database_unreadable",
        ) from exc
    try:
        before = os.fstat(source_fd)
        if (
            PathIdentity.from_stat(path_stat).metadata_tuple()
            != PathIdentity.from_stat(before).metadata_tuple()
        ):
            raise _DatabaseSourceChangedError
        try:
            destination_fd = os.open(_native_io_path(destination), destination_flags, 0o600)
        except OSError as exc:
            raise RecoveryError(
                "private database validation snapshot cannot be created",
                stable_code="state_database_validation_unavailable",
            ) from exc
        digest = hashlib.sha256()
        try:
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise OSError("short snapshot write")
                    view = view[written:]
            os.fsync(destination_fd)
        except OSError as exc:
            raise RecoveryError(
                "private database validation snapshot could not be completed",
                stable_code="state_database_validation_unavailable",
            ) from exc
        finally:
            os.close(destination_fd)
        after = os.fstat(source_fd)
    finally:
        os.close(source_fd)
    before_identity = PathIdentity.from_stat(before)
    after_identity = PathIdentity.from_stat(after)
    if before_identity.metadata_tuple() != after_identity.metadata_tuple():
        raise _DatabaseSourceChangedError
    return _DatabaseSourceSnapshot(
        path=source,
        identity=after_identity,
        digest=digest.digest(),
    )


def _source_snapshot_is_current(snapshot: _DatabaseSourceSnapshot) -> bool:
    current = _regular_source_stat(snapshot.path)
    if current is None:
        return False
    if PathIdentity.from_stat(current).metadata_tuple() != snapshot.identity.metadata_tuple():
        return False
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(_native_io_path(snapshot.path), flags)
    except OSError:
        return False
    digest = hashlib.sha256()
    try:
        before = os.fstat(fd)
        if PathIdentity.from_stat(before).metadata_tuple() != snapshot.identity.metadata_tuple():
            return False
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    return (
        PathIdentity.from_stat(after).metadata_tuple() == snapshot.identity.metadata_tuple()
        and digest.digest() == snapshot.digest
    )


def _bundle_names(path: Path) -> tuple[Path, ...]:
    return (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-journal"))


def _database_safety_code(path: Path) -> str | None:
    """Validate a stable private SQLite snapshot without opening the source."""

    bundle = _bundle_names(path)
    try:
        present_before = tuple(_regular_source_stat(entry) is not None for entry in bundle)
    except RecoveryError as exc:
        return exc.stable_code
    if not present_before[0]:
        # A durable sidecar without its database is ambiguous and must never be
        # treated as a fresh state root.
        return "state_database_unsafe_path" if any(present_before[1:]) else None

    snapshots: list[_DatabaseSourceSnapshot] = []
    try:
        with tempfile.TemporaryDirectory(prefix="opensquilla-db-inspect-") as temporary:
            snapshot_root = Path(temporary)
            with contextlib.suppress(OSError):
                snapshot_root.chmod(0o700)
            for entry, exists in zip(bundle, present_before, strict=True):
                if exists:
                    snapshots.append(
                        _copy_source_file_no_follow(entry, snapshot_root / entry.name)
                    )
            snapshot_path = snapshot_root / path.name
            if os.name == "nt":
                encoded_path = quote(_native_io_path(snapshot_path), safe="/:")
                snapshot_uri = f"file:{encoded_path}?mode=rw"
            else:
                snapshot_uri = f"{snapshot_path.as_uri()}?mode=rw"
            try:
                # mode=rw forbids accidental creation. Any WAL recovery, SHM
                # creation, or checkpointing is confined to this disposable copy.
                connection = sqlite3.connect(snapshot_uri, uri=True)
                try:
                    check = connection.execute("PRAGMA quick_check").fetchone()
                    if not check or str(check[0]).lower() != "ok":
                        return "state_database_invalid"
                    tables = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name LIKE '%yoyo_migration'"
                    ).fetchall()
                    ledger = next(
                        (
                            name
                            for (name,) in tables
                            if isinstance(name, str) and name.endswith("yoyo_migration")
                        ),
                        None,
                    )
                    if ledger is None:
                        rows: list[tuple[Any, ...]] = []
                    else:
                        quoted_ledger = ledger.replace('"', '""')
                        rows = connection.execute(
                            f'SELECT migration_id FROM "{quoted_ledger}"'
                        ).fetchall()
                finally:
                    connection.close()
            except sqlite3.Error:
                return "state_database_invalid"
            try:
                present_after = tuple(_regular_source_stat(entry) is not None for entry in bundle)
            except RecoveryError as exc:
                return exc.stable_code
            if present_after != present_before or not all(
                _source_snapshot_is_current(snapshot) for snapshot in snapshots
            ):
                raise _DatabaseSourceChangedError
    except _DatabaseSourceChangedError:
        return "state_database_changed"
    except RecoveryError as exc:
        return exc.stable_code

    applied = {str(migration_id) for (migration_id,) in rows if migration_id}
    if not applied:
        return None
    known = _known_migration_ids()
    if not known:
        return "state_migration_set_unavailable"
    if applied - known:
        return "state_schema_too_new"
    return None


def _state_safety_code(state_dir: Path) -> str | None:
    try:
        state_before = PathIdentity.from_stat(_native_stat(state_dir))
    except OSError:
        return "effective_state_unreadable"
    for database_name in ("sessions.db", "scheduler.db"):
        code = _database_safety_code(state_dir / database_name)
        if code is not None:
            return code
    try:
        state_after = PathIdentity.from_stat(_native_stat(state_dir))
    except OSError:
        return "state_database_changed"
    if state_before.metadata_tuple() != state_after.metadata_tuple():
        return "state_database_changed"
    return None


def _profile_has_evidence(home: Path) -> bool:
    try:
        return any(_lexists(home / name) for name in _PROFILE_EVIDENCE_NAMES)
    except OSError:
        return True


def _profile_top_level_entries(home: Path) -> tuple[str, ...] | None:
    """Return a stable, no-follow snapshot of the names directly inside ``H``.

    A missing or existing-empty home is the only filesystem shape that can be
    initialized as fresh.  Inspecting names through ``scandir`` does not follow
    an entry that is a symlink, junction, or other reparse point.  The directory
    identity/metadata check turns a concurrent entry or parent exchange into an
    unknown layout instead of accidentally seeding a third workspace.
    """

    try:
        before = _native_lstat(home)
    except FileNotFoundError:
        return ()
    except OSError:
        return None
    if is_path_redirecting_stat(before) or not stat.S_ISDIR(before.st_mode):
        return None
    try:
        entries = tuple(sorted(entry.name for entry in os.scandir(_native_io_path(home))))
        after = _native_lstat(home)
    except OSError:
        return None
    if PathIdentity.from_stat(before).metadata_tuple() != PathIdentity.from_stat(
        after
    ).metadata_tuple():
        return None
    return entries


def _home_is_unsafe(path: Path) -> bool:
    try:
        value = _native_lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return is_path_redirecting_stat(value) or not stat.S_ISDIR(value.st_mode)


def _elevated_windows_context() -> bool:
    """Return whether this process runs with Windows administrator rights."""

    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def _legacy_layout_is_proven(home: Path, config: _ConfigView) -> bool:
    """Recognize the released nested Desktop shape using filesystem evidence.

    No version string participates in this decision. A bare directory named
    ``state/workspace`` is insufficient: the Desktop-owned canonical state pin
    and an identity/persona Markdown role must both be present.
    """

    legacy = home / "state" / "workspace"
    if config.error_code is not None or not config.exists or config.state_dir is None:
        return False
    if not _same_path(config.state_dir, home / "state"):
        return False
    candidate = _workspace_status(legacy, explicitly_configured=False)
    if not candidate.valid:
        return False
    try:
        return any(_native_is_file(legacy / name) for name in _WORKSPACE_IDENTITY_FILES)
    except OSError:
        return False


def _base_legacy_layout_is_proven(home: Path, config: _ConfigView) -> bool:
    # A canonical state_dir pin and a real H occur in every modern profile and
    # do not prove that H/state/.env, skills, or router were written by RC2.
    # Require the released nested workspace fingerprint (including an identity
    # Markdown role) before activating any ancillary relocation allowlist.
    return _legacy_layout_is_proven(home, config)


def _plain_role_source(path: Path, expected_type: str) -> bool:
    try:
        value = _native_lstat(path)
    except OSError:
        return False
    if is_path_redirecting_stat(value):
        return False
    if expected_type == "directory":
        return stat.S_ISDIR(value.st_mode)
    return stat.S_ISREG(value.st_mode)


def _legacy_roles(
    home: Path,
    config: _ConfigView,
    *,
    base_proven_override: bool | None = None,
) -> tuple[_LegacyRole, ...]:
    """Classify each released RC3 relocation role independently."""
    base_proven = (
        _base_legacy_layout_is_proven(home, config)
        if base_proven_override is None
        else base_proven_override
    )
    roles: list[_LegacyRole] = []
    canonical_workspace = home / "workspace"
    for name, expected_type, entry_name in _LEGACY_HOME_ROLES:
        source = home / "state" / entry_name
        destination = home / entry_name
        source_exists = _lexists(source)
        if not source_exists:
            continue
        destination_exists = _lexists(destination)
        if destination_exists:
            disposition = "conflict"
        elif name == "workspace" and config.workspace_explicit and config.workspace is not None:
            if not _same_path(config.workspace, canonical_workspace):
                # A legacy or external explicit path remains authoritative.
                disposition = "deferred"
            elif base_proven and _legacy_layout_is_proven(home, config):
                disposition = "move"
            else:
                disposition = "unsafe"
        elif not base_proven or not _plain_role_source(source, expected_type):
            disposition = "unsafe"
        elif name == "workspace" and not _legacy_layout_is_proven(home, config):
            disposition = "unsafe"
        else:
            disposition = "move"
        roles.append(_LegacyRole(name, source, destination, disposition))

    nested = home / "state" / "state"
    if _lexists(nested):
        if not _plain_role_source(nested, "directory"):
            roles.append(_LegacyRole("state/*", nested, home / "state", "unsafe"))
        else:
            try:
                entries = sorted(os.scandir(_native_io_path(nested)), key=lambda item: item.name)
            except OSError:
                roles.append(_LegacyRole("state/*", nested, home / "state", "unsafe"))
            else:
                # A completed role-by-role reconciliation can leave this now
                # empty container behind. It contains no data to move, merge,
                # or delete and must not take an otherwise valid profile back
                # to attention on the next inspection.
                if entries and not base_proven:
                    roles.append(_LegacyRole("state/*", nested, home / "state", "unsafe"))
                    return tuple(roles)
                for entry in entries:
                    source = nested / entry.name
                    destination = home / "state" / entry.name
                    if _lexists(destination):
                        disposition = "conflict"
                    elif _plain_role_source(source, "directory") or _plain_role_source(
                        source, "file"
                    ):
                        disposition = "move"
                    else:
                        disposition = "unsafe"
                    roles.append(
                        _LegacyRole(f"state/{entry.name}", source, destination, disposition)
                    )
    return tuple(roles)


def _role_actions(roles: tuple[_LegacyRole, ...]) -> tuple[str, ...]:
    if any(role.disposition == "move" for role in roles):
        return ("reconcile", *_WORKSPACE_RECOVERY_ACTIONS)
    return _WORKSPACE_RECOVERY_ACTIONS


def _marker_is_valid(path: Path) -> bool:
    try:
        snapshot = ConfigSnapshot.capture(path)
    except RecoveryError:
        return False
    if snapshot.identity is None:
        return False
    try:
        payload = json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    schema_version = payload.get("schema_version")
    if "schema_version" in payload and schema_version != 2:
        # The only schema-less form is the exact marker written by RC3.
        # Future versions may assign new meaning to the same field names and
        # must never be silently treated as an RC3/RC4 compatibility receipt.
        return False
    return isinstance(payload.get("migratedAt"), str) and isinstance(
        payload.get("moved"), list
    )


def _finalize_compatibility_marker(
    home: Path,
    report: RecoveryReport,
    *,
    profile_kind: str,
) -> RecoveryReport:
    if profile_kind != "desktop-primary" or report.outcome == "recovery_required":
        return report
    config = _read_config(home)
    roles = _legacy_roles(home, config)
    if any(role.disposition in {"move", "unsafe", "conflict"} for role in roles):
        return report
    effective = report.effective_workspace
    if (
        effective is None
        or not _workspace_status(effective, explicitly_configured=config.workspace_explicit).valid
    ):
        return report
    if _unfinished_cleanup_transaction(
        home,
        profile_kind=profile_kind,
    ) or _unfinished_replace_transaction(home):
        return report

    marker = home / "desktop-layout-v2.json"
    if _lexists(marker):
        if _marker_is_valid(marker):
            return report
        return replace(
            report,
            outcome="attention",
            stable_code="layout_marker_unsafe",
            allowed_actions=_ATTENTION_ACTIONS,
        )
    payload = {
        "schema_version": 2,
        "migratedAt": datetime.now(UTC).isoformat(),
        "moved": [],
        "protectedBy": "rc4",
    }
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(_native_io_path(marker), flags, 0o600)
    except FileExistsError:
        if _marker_is_valid(marker):
            return report
        return replace(
            report,
            outcome="attention",
            stable_code="layout_marker_unsafe",
            allowed_actions=_ATTENTION_ACTIONS,
        )
    except OSError:
        return replace(
            report,
            outcome="attention",
            stable_code="layout_marker_write_failed",
            allowed_actions=_ATTENTION_ACTIONS,
        )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short marker write")
            view = view[written:]
        with contextlib.suppress(OSError):
            _chmod_open_file(fd, 0o600)
        os.fsync(fd)
    except OSError:
        os.close(fd)
        # A short-write marker is retained as explicit unsafe evidence.  Once a
        # pathname has been published, deleting it after a separate identity
        # check would recreate the same TOCTOU window recovery is meant to avoid.
        return replace(
            report,
            outcome="attention",
            stable_code="layout_marker_write_failed",
            allowed_actions=_ATTENTION_ACTIONS,
        )
    os.close(fd)
    return report


def _with_marker_inspection_status(
    home: Path,
    report: RecoveryReport,
    *,
    profile_kind: str,
) -> RecoveryReport:
    """Advertise the safe offline marker finalization without writing in inspect."""

    if profile_kind != "desktop-primary" or report.outcome == "recovery_required":
        return report
    marker = home / "desktop-layout-v2.json"
    if not _lexists(marker):
        actions = tuple(dict.fromkeys(("finalize-layout", *report.allowed_actions)))
        return replace(report, allowed_actions=actions)
    if _marker_is_valid(marker):
        return report
    return replace(
        report,
        outcome="attention",
        stable_code="layout_marker_unsafe",
        allowed_actions=_ATTENTION_ACTIONS,
    )


def _unfinished_replace_transaction(home: Path) -> bool:
    journal = home.parent / f".{home.name}.profile-replace.json"
    try:
        snapshot = ConfigSnapshot.capture(journal)
    except RecoveryError:
        return True
    if snapshot.identity is None:
        return False
    try:
        payload = json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return True
    if not isinstance(payload, dict):
        return True
    phase = payload.get("phase", payload.get("state"))
    if phase != "committed":
        return True
    return not _committed_transaction_is_complete(home, payload)


def profile_replacement_transaction_unfinished(home: str | Path) -> bool:
    """Return whether a profile replacement journal still needs recovery.

    Destructive profile-management operations share this read-only authority
    check with bootstrap.  Keeping the schema validation here prevents cleanup
    callers from depending on an implementation-private parser or accepting a
    malformed/future journal as committed.
    """

    return _unfinished_replace_transaction(Path(home).expanduser().absolute())


def _legacy_import_transaction_present(home: Path) -> bool:
    """Detect a pre-RC4 import journal without trusting or mutating it.

    The released journal predates identity-bound transaction records. RC4 must
    preserve its target, backup, and staging paths for explicit recovery rather
    than repeat the old Electron rename/delete routine automatically.
    """

    journal = home.parent / f".{home.name}.import-commit.json"
    try:
        snapshot = ConfigSnapshot.capture(journal)
    except RecoveryError:
        return True
    return snapshot.identity is not None


def _recovery_profile_identity(home: Path) -> tuple[Path, str] | None:
    """Recognize the reserved A/recovery-profiles/<uuid>/opensquilla identity."""

    if home.name != "opensquilla":
        return None
    recovery_root = home.parent
    recovery_container = recovery_root.parent
    if recovery_container.name != "recovery-profiles":
        return None
    try:
        recovery_id = uuid.UUID(recovery_root.name)
    except ValueError:
        return None
    if recovery_id.version != 4 or str(recovery_id) != recovery_root.name:
        return None
    user_data = recovery_container.parent
    expected = user_data / "recovery-profiles" / recovery_root.name / "opensquilla"
    if not _same_path(home, expected):
        return None
    return user_data, recovery_root.name


def _cleanup_transaction_context(
    home: Path,
    *,
    profile_kind: str,
) -> tuple[Path, Path, str | None] | None:
    """Resolve the exact Desktop user-data cleanup authority for a profile."""

    selected_recovery_id: str | None = None
    if profile_kind == "desktop-primary":
        user_data = home.parent
        journal = home.parent / f".{home.name}.profile-cleanup.json"
    elif profile_kind == "desktop-recovery":
        identity = _recovery_profile_identity(home)
        if identity is None:
            return None
        user_data, selected_recovery_id = identity
        journal = user_data / ".opensquilla.profile-cleanup.json"
    else:
        return None
    return user_data, journal, selected_recovery_id


def _unfinished_cleanup_transaction(home: Path, *, profile_kind: str) -> bool:
    """Find the Desktop-global cleanup guard without affecting ordinary CLI homes."""

    context = _cleanup_transaction_context(home, profile_kind=profile_kind)
    if context is None:
        return False
    user_data, journal, selected_recovery_id = context
    if not _lexists(journal):
        return False
    try:
        snapshot = ConfigSnapshot.capture(journal)
        payload = json.loads(snapshot.data.decode("utf-8"))
    except (OSError, RecoveryError, UnicodeDecodeError, json.JSONDecodeError):
        return True
    expected_fields = {
        "schema_version",
        "operation",
        "transaction_id",
        "phase",
        "primary_home",
        "mode",
        "profile_kind",
        "recovery_id",
        "tombstones",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        return True
    transaction_id = payload.get("transaction_id")
    if not isinstance(transaction_id, str):
        return True
    try:
        if str(uuid.UUID(transaction_id)) != transaction_id:
            return True
    except ValueError:
        return True
    if (
        payload.get("schema_version") != 1
        or payload.get("operation") != "profile-cleanup"
        or payload.get("phase") != "prepared"
        or not isinstance(payload.get("tombstones"), list)
        or not isinstance(payload.get("primary_home"), str)
        or not _same_path(str(payload["primary_home"]), user_data / "opensquilla")
    ):
        return True
    cleanup_mode = payload.get("mode")
    selected_kind = payload.get("profile_kind")
    journal_recovery_id = payload.get("recovery_id")
    if cleanup_mode == "delete-all-user-data":
        return True
    if cleanup_mode != "delete-current-profile":
        return True
    if selected_kind == "primary" and journal_recovery_id is None:
        return profile_kind == "desktop-primary"
    if selected_kind != "recovery" or not isinstance(journal_recovery_id, str):
        return True
    try:
        parsed_recovery_id = uuid.UUID(journal_recovery_id)
    except ValueError:
        return True
    if parsed_recovery_id.version != 4 or str(parsed_recovery_id) != journal_recovery_id:
        return True
    return (
        profile_kind == "desktop-recovery"
        and selected_recovery_id == journal_recovery_id
    )


def _identity_payload_matches(path: Path, expected: object) -> bool:
    if not isinstance(expected, dict):
        return False
    try:
        value = _native_lstat(path)
    except OSError:
        return False
    current = {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "file_type": stat.S_IFMT(value.st_mode),
        "mode": int(value.st_mode),
        "size": int(value.st_size),
        "modified_at_ns": int(value.st_mtime_ns),
    }
    return all(current[key] == expected.get(key) for key in current)


def _object_identity_payload_matches(path: Path, expected: object) -> bool:
    """Match the stable directory object while allowing in-place metadata changes."""

    if not _valid_identity_payload(expected):
        return False
    assert isinstance(expected, dict)
    try:
        value = _native_lstat(path)
    except OSError:
        return False
    current = {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "file_type": stat.S_IFMT(value.st_mode),
    }
    return all(current[key] == expected.get(key) for key in current)


def _valid_identity_payload(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _IMPORT_RECEIPT_IDENTITY_FIELDS
        and all(type(value.get(key)) is int and int(value[key]) >= 0 for key in value)
    )


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        return None
    return value if parsed == value else None


def _timezone_timestamp(value: object) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def _normalized_receipt_path(path: str | Path) -> str:
    value = Path(path).expanduser()
    try:
        value = value.resolve(strict=False)
    except OSError:
        value = value.absolute()
    return os.path.normcase(os.path.normpath(str(value)))


def _load_json_file_no_follow(path: Path) -> dict[str, Any] | None:
    try:
        snapshot = ConfigSnapshot.capture(path)
    except RecoveryError:
        return None
    if snapshot.identity is None:
        return None
    try:
        payload = json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _valid_committed_import_layout_receipt(
    receipt: object,
    *,
    home: Path,
    transaction_id: str,
    source: str,
    source_kind: str,
) -> bool:
    """Validate the complete narrow receipt without consulting report.json.

    The original import source may legitimately be offline or deleted after a
    commit, so its identity is shape-checked but not re-opened here. The target
    identity, transaction result, exact schema, and normalized paths remain
    mandatory before a retained committed journal can be treated as complete.
    """

    if not isinstance(receipt, dict) or set(receipt) != _IMPORT_LAYOUT_RECEIPT_FIELDS:
        return False
    source_identity = receipt.get("source_identity")
    candidate_identity = receipt.get("candidate_identity")
    if (
        not isinstance(source_identity, dict)
        or set(source_identity) != _IMPORT_RECEIPT_IDENTITY_FIELDS
        or not all(
            type(source_identity.get(key)) is int and int(source_identity[key]) >= 0
            for key in source_identity
        )
        or not isinstance(candidate_identity, dict)
        or set(candidate_identity) != _IMPORT_RECEIPT_IDENTITY_FIELDS
        or not all(
            type(candidate_identity.get(key)) is int and int(candidate_identity[key]) >= 0
            for key in candidate_identity
        )
    ):
        return False
    imported_at_raw = receipt.get("imported_at")
    try:
        imported_at = datetime.fromisoformat(str(imported_at_raw))
    except (TypeError, ValueError):
        return False
    receipt_source = receipt.get("source")
    target = receipt.get("target")
    if (
        imported_at.tzinfo is None
        or receipt.get("schema_version") != 1
        or receipt.get("transaction_id") != transaction_id
        or not isinstance(receipt_source, str)
        or not receipt_source
        or _normalized_receipt_path(receipt_source) != receipt_source
        or not isinstance(target, str)
        or target != _normalized_receipt_path(home)
        or receipt_source != source
        or receipt.get("source_kind") != source_kind
        or not isinstance(receipt.get("source_version"), str)
        or receipt.get("layout") != "opensquilla-profile-root-v1"
        or receipt.get("recovery_outcome") not in {"ready", "attention"}
        or not isinstance(receipt.get("recovery_stable_code"), str)
        or not receipt.get("recovery_stable_code")
        or not _object_identity_payload_matches(home, candidate_identity)
    ):
        return False
    return True


def _valid_history_record(record: object) -> bool:
    if not isinstance(record, dict) or set(record) not in {
        _HISTORY_RECORD_FIELDS,
        _CONSUMED_HISTORY_RECORD_FIELDS,
    }:
        return False
    if (
        _canonical_uuid(record.get("transaction_id")) is None
        or not _timezone_timestamp(record.get("committed_at"))
        or any(
            not isinstance(record.get(key), str)
            or not record[key]
            or _normalized_receipt_path(record[key]) != record[key]
            for key in ("source", "target", "backup")
        )
        or any(
            not _valid_identity_payload(record.get(key))
            for key in ("source_identity", "target_identity", "backup_identity")
        )
    ):
        return False
    if set(record) == _CONSUMED_HISTORY_RECORD_FIELDS:
        return (
            _timezone_timestamp(record.get("restored_at"))
            and isinstance(record.get("restored_to"), str)
            and bool(record["restored_to"])
            and _normalized_receipt_path(record["restored_to"]) == record["restored_to"]
            and _canonical_uuid(record.get("consumed_by_transaction_id")) is not None
        )
    return True


def _load_strict_replacement_history(home: Path) -> dict[str, Any] | None:
    history = _load_json_file_no_follow(home.parent / "profile-replacement-history.json")
    if (
        history is None
        or set(history) != _HISTORY_FIELDS
        or history.get("schema_version") != 1
        or not isinstance(history.get("backups"), list)
        or not all(_valid_history_record(record) for record in history["backups"])
    ):
        return None
    return history


def _valid_import_journal_schema(home: Path, journal: dict[str, Any]) -> bool:
    if set(journal) != _IMPORT_REPLACE_JOURNAL_FIELDS:
        return False
    transaction_id = _canonical_uuid(journal.get("transaction_id"))
    source = journal.get("source")
    source_kind = journal.get("source_kind")
    target = journal.get("target")
    staging = journal.get("staging")
    backup = journal.get("backup")
    identities = journal.get("identities")
    target_existed = journal.get("target_existed")
    target_had_real_data = journal.get("target_had_real_data")
    target_was_empty = journal.get("target_was_empty")
    if (
        journal.get("schema_version") != 1
        or journal.get("operation") != "profile-import"
        or journal.get("phase") != "committed"
        or transaction_id is None
        or source_kind not in _IMPORT_SOURCE_KINDS
        or not isinstance(source, str)
        or not source
        or _normalized_receipt_path(source) != source
        or not isinstance(target, str)
        or target != _normalized_receipt_path(home)
        or not isinstance(staging, str)
        or staging
        != _normalized_receipt_path(
            home.parent / f".{home.name}.profile-staging.{transaction_id}"
        )
        or not isinstance(backup, str)
        or backup
        != _normalized_receipt_path(home.with_name(f"{home.name}.backup.{transaction_id}"))
        or type(target_existed) is not bool
        or type(target_had_real_data) is not bool
        or type(target_was_empty) is not bool
        or target_had_real_data != (target_existed and not target_was_empty)
        or target_was_empty != (target_existed and not target_had_real_data)
        or not isinstance(identities, dict)
        or set(identities) != _REPLACE_JOURNAL_IDENTITY_FIELDS
        or not all(
            _valid_identity_payload(identities.get(key))
            for key in ("source", "staging", "candidate")
        )
        or _lexists(staging)
        or not _object_identity_payload_matches(home, identities.get("candidate"))
    ):
        return False
    original_target = identities.get("original_target")
    backup_identity = identities.get("backup")
    if target_existed:
        if (
            not _valid_identity_payload(original_target)
            or not _valid_identity_payload(backup_identity)
            or original_target != backup_identity
            or not _identity_payload_matches(Path(backup), backup_identity)
        ):
            return False
    elif (
        original_target is not None
        or backup_identity is not None
        or _lexists(backup)
    ):
        return False
    receipt = _load_json_file_no_follow(
        home / "migration" / "opensquilla" / transaction_id / "layout-receipt.json"
    )
    if not _valid_committed_import_layout_receipt(
        receipt,
        home=home,
        transaction_id=transaction_id,
        source=source,
        source_kind=source_kind,
    ):
        return False
    if not target_existed:
        return True
    history = _load_strict_replacement_history(home)
    if history is None:
        return False
    matches = [
        item
        for item in history["backups"]
        if item["transaction_id"] == transaction_id
    ]
    if len(matches) != 1 or set(matches[0]) != _HISTORY_RECORD_FIELDS:
        return False
    record = matches[0]
    return (
        record["source"] == source
        and record["target"] == target
        and record["backup"] == backup
        and record["source_identity"] == identities["source"]
        and _object_identity_payload_matches(home, record["target_identity"])
        and _identity_payload_matches(Path(backup), record["backup_identity"])
    )


def _valid_restore_journal_schema(home: Path, journal: dict[str, Any]) -> bool:
    if set(journal) != _RESTORE_REPLACE_JOURNAL_FIELDS:
        return False
    transaction_id = _canonical_uuid(journal.get("transaction_id"))
    source = journal.get("source")
    target = journal.get("target")
    backup = journal.get("backup")
    identities = journal.get("identities")
    target_existed = journal.get("target_existed")
    if (
        journal.get("schema_version") != 1
        or journal.get("operation") != "restore-profile"
        or journal.get("phase") != "committed"
        or transaction_id is None
        or not isinstance(source, str)
        or not source
        or _normalized_receipt_path(source) != source
        or _normalized_receipt_path(Path(source).parent) != _normalized_receipt_path(home.parent)
        or not Path(source).name.startswith(f"{home.name}.backup.")
        or _lexists(source)
        or not isinstance(target, str)
        or target != _normalized_receipt_path(home)
        or not isinstance(backup, str)
        or backup
        != _normalized_receipt_path(home.with_name(f"{home.name}.backup.{transaction_id}"))
        or journal.get("staging") != ""
        or type(target_existed) is not bool
        or not isinstance(identities, dict)
        or set(identities) != _REPLACE_JOURNAL_IDENTITY_FIELDS
        or not _valid_identity_payload(identities.get("source"))
        or identities.get("staging") is not None
        or not _valid_identity_payload(identities.get("candidate"))
        or not _object_identity_payload_matches(home, identities.get("candidate"))
    ):
        return False
    original_target = identities.get("original_target")
    backup_identity = identities.get("backup")
    if target_existed:
        if (
            not _valid_identity_payload(original_target)
            or not _valid_identity_payload(backup_identity)
            or original_target != backup_identity
            or not _identity_payload_matches(Path(backup), backup_identity)
        ):
            return False
    elif (
        original_target is not None
        or backup_identity is not None
        or _lexists(backup)
    ):
        return False
    history = _load_strict_replacement_history(home)
    if history is None:
        return False
    selected = [item for item in history["backups"] if item["backup"] == source]
    if len(selected) != 1 or set(selected[0]) != _CONSUMED_HISTORY_RECORD_FIELDS:
        return False
    selected_record = selected[0]
    if (
        selected_record["consumed_by_transaction_id"] != transaction_id
        or selected_record["restored_to"] != target
        or not _object_identity_payload_matches(home, selected_record["backup_identity"])
        or not _object_identity_payload_matches(home, identities["source"])
    ):
        return False
    restore_records = [
        item for item in history["backups"] if item["transaction_id"] == transaction_id
    ]
    if not target_existed:
        return not restore_records
    if len(restore_records) != 1 or set(restore_records[0]) != _HISTORY_RECORD_FIELDS:
        return False
    record = restore_records[0]
    return (
        record["source"] == source
        and record["target"] == target
        and record["backup"] == backup
        and record["source_identity"] == identities["source"]
        and _object_identity_payload_matches(home, record["target_identity"])
        and _identity_payload_matches(Path(backup), record["backup_identity"])
    )


def _committed_transaction_is_complete(home: Path, journal: dict[str, Any]) -> bool:
    operation = journal.get("operation")
    if operation == "profile-import":
        return _valid_import_journal_schema(home, journal)
    if operation == "restore-profile":
        return _valid_restore_journal_schema(home, journal)
    return False


def _profile_kind(profile_kind: str | None, *, home: Path | None = None) -> str:
    # The recovery directory is a reserved security boundary.  A caller label
    # may request stricter handling, but cannot reinterpret that canonical path
    # as a primary or ordinary CLI profile.
    if home is not None and _recovery_profile_identity(home) is not None:
        return "desktop-recovery"
    if profile_kind is not None:
        return profile_kind.strip().lower()
    value = os.environ.get("OPENSQUILLA_PROFILE_KIND", "").strip().lower()
    if value:
        return value
    if os.environ.get("OPENSQUILLA_DESKTOP", "").strip().lower() in _TRUTHY:
        return "desktop-primary"
    return ""


def _plain_canonical_directory(path: Path) -> bool:
    try:
        value = _native_lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return not is_path_redirecting_stat(value) and stat.S_ISDIR(value.st_mode)


def _recovery_profile_isolation_code(home: Path, config: _ConfigView) -> str | None:
    """Recovery profiles may only use their own two canonical data roots."""

    canonical_workspace = home / "workspace"
    canonical_state = home / "state"
    if config.workspace is None or not _same_path(config.workspace, canonical_workspace):
        return "recovery_profile_external_workspace"
    if config.state_dir is None or not _same_path(config.state_dir, canonical_state):
        return "recovery_profile_external_state"
    if not _plain_canonical_directory(canonical_workspace):
        return "recovery_profile_unsafe_workspace"
    if not _plain_canonical_directory(canonical_state):
        return "recovery_profile_unsafe_state"
    return None


def _is_fresh_canonical_profile(
    home: Path,
    config: _ConfigView,
    *,
    top_level_entries: tuple[str, ...],
) -> bool:
    """Only an evidence-free profile using implicit canonical roots is fresh."""

    return (
        not top_level_entries
        and not config.workspace_explicit
        and not config.state_explicit
        and config.workspace is not None
        and config.state_dir is not None
        and _same_path(config.workspace, home / "workspace")
        and _same_path(config.state_dir, home / "state")
    )


def _revision(
    home: Path,
    config: _ConfigView,
    candidates: tuple[WorkspaceCandidate, ...],
    stable_code: str,
    *,
    cleanup_journal: Path | None = None,
) -> int:
    parts = [str(home), stable_code, config.error_code or ""]
    if _home_is_unsafe(home):
        try:
            value = _native_lstat(home)
        except OSError:
            parts.append("home:unreadable")
        else:
            parts.append(
                f"home:{value.st_dev}:{value.st_ino}:{value.st_size}:"
                f"{value.st_mtime_ns}:{value.st_mode}"
            )
        digest = hashlib.sha256("\n".join(parts).encode("utf-8", "surrogatepass")).digest()
        return int.from_bytes(digest[:8], "big") & ((1 << 53) - 1)
    try:
        value = _native_lstat(config.path)
        parts.append(
            f"config:{value.st_dev}:{value.st_ino}:{value.st_size}:{value.st_mtime_ns}:{value.st_mode}"
        )
    except OSError:
        parts.append("config:missing")
    # A recovery-page CAS must identify the exact interrupted transaction the
    # user inspected.  These journals live beside H, so none of the workspace
    # candidates or legacy-role metadata below would otherwise change when a
    # well-formed journal was swapped for another one.  The digest remains
    # process-local input to the opaque 53-bit revision; it is never emitted as
    # a content hash or persisted in a receipt/diagnostic.
    transaction_paths = [
        ("profile-transaction", home.parent / f".{home.name}.profile-replace.json"),
        ("legacy-import-transaction", home.parent / f".{home.name}.import-commit.json"),
        (
            "settings-transaction",
            home.parent / f".{home.name}.desktop-settings-transaction.json",
        ),
    ]
    if cleanup_journal is not None:
        transaction_paths.append(("cleanup-transaction", cleanup_journal))
    for label, path in transaction_paths:
        try:
            snapshot = ConfigSnapshot.capture(path)
        except RecoveryError:
            parts.append(f"{label}:unsafe")
            continue
        identity = (
            snapshot.identity.metadata_tuple() if snapshot.identity is not None else None
        )
        parts.append(f"{label}:{identity}:{snapshot.digest.hex()}")
    for candidate in candidates:
        # The persistent RC3 gateway lock is intentionally created inside the
        # effective state root before a mutating recovery command. That changes
        # the directory mtime but not the selected state identity or any
        # workspace choice. Exclude only this volatile state-root mtime from
        # the workspace/config CAS revision.
        modified_at_ns = None if candidate.kind == "state" else candidate.modified_at_ns
        parts.append(
            f"{candidate.kind}:{candidate.path}:{candidate.identity}:"
            f"{modified_at_ns}:{candidate.valid}:{candidate.configured}"
        )
    role_paths = [home / "state" / entry for _, _, entry in _LEGACY_HOME_ROLES]
    role_paths.extend(home / entry for _, _, entry in _LEGACY_HOME_ROLES)
    nested = home / "state" / "state"
    role_paths.append(nested)
    try:
        if _plain_role_source(nested, "directory"):
            role_paths.extend(
                nested / entry.name for entry in os.scandir(_native_io_path(nested))
            )
    except OSError:
        parts.append("nested-state:unreadable")
    for path in role_paths:
        try:
            value = _native_lstat(path)
        except OSError:
            continue
        parts.append(
            f"role:{path}:{value.st_dev}:{value.st_ino}:{value.st_size}:"
            f"{value.st_mtime_ns}:{value.st_mode}"
        )
    digest = hashlib.sha256("\n".join(parts).encode("utf-8", "surrogatepass")).digest()
    # JSON consumers include Electron/JavaScript, so keep the opaque CAS
    # revision within Number.MAX_SAFE_INTEGER while retaining 53 bits.
    return int.from_bytes(digest[:8], "big") & ((1 << 53) - 1)


def _sanitize_detail(detail: str | None, home: Path) -> str | None:
    """Spell the profile home as ``<HOME>`` before a detail enters the protocol.

    Desktop diagnostics redact the same prefix, so a detail string never
    widens what the fixed JSON protocol reveals about the local filesystem.
    """

    if detail is None:
        return None
    return detail.replace(str(home), "<HOME>")


def _report(
    *,
    home: Path,
    config: _ConfigView,
    candidates: tuple[WorkspaceCandidate, ...],
    outcome: RecoveryOutcome,
    stable_code: str,
    effective_workspace: Path | None,
    allowed_actions: tuple[str, ...],
    cleanup_journal: Path | None = None,
    detail: str | None = None,
) -> RecoveryReport:
    transaction_id = str(uuid.uuid5(_TRANSACTION_NAMESPACE, _path_key(home)))
    return RecoveryReport(
        outcome=outcome,
        stable_code=stable_code,
        primary_home=home,
        effective_workspace=effective_workspace,
        candidates=candidates,
        allowed_actions=allowed_actions,
        transaction_id=transaction_id,
        revision=_revision(
            home,
            config,
            candidates,
            stable_code,
            cleanup_journal=cleanup_journal,
        ),
        detail=_sanitize_detail(detail, home),
    )


def _resolved_home_path(home: str | Path | None) -> Path:
    if home is None:
        from opensquilla.paths import default_opensquilla_home

        home_path = default_opensquilla_home().expanduser().absolute()
    else:
        home_path = _absolute(home)
    return resolve_home_link(home_path)


def inspect_profile(
    home: str | Path | None = None,
    *,
    profile_kind: str | None = None,
    _ignore_transaction: bool = False,
    _ignore_settings_transaction: bool = False,
) -> RecoveryReport:
    """Inspect a Desktop profile without creating or modifying any path."""

    home_path = _resolved_home_path(home)
    kind = _profile_kind(profile_kind, home=home_path)
    if _home_is_unsafe(home_path):
        config = _ConfigView(
            path=home_path / "config.toml",
            exists=True,
            payload=None,
            workspace=None,
            workspace_explicit=False,
            workspace_from_env=False,
            state_dir=None,
            error_code="profile_unsafe_path",
        )
        blocks = _elevated_windows_context()
        return _report(
            home=home_path,
            config=config,
            candidates=(),
            outcome="recovery_required" if blocks else "attention",
            stable_code="profile_unsafe_path",
            effective_workspace=None,
            allowed_actions=_RECOVERY_ACTIONS,
            detail=f"{home_path} is a link or is not a plain directory",
        )
    config = _read_config(home_path)
    candidates = _candidate_set(home_path, config)
    canonical = next(candidate for candidate in candidates if candidate.kind == "canonical")
    effective = config.workspace
    if config.error_code == "config_schema_too_new" or (
        config.error_code == "config_unsafe_path" and _elevated_windows_context()
    ):
        # The two hard startup gates outrank every pending-journal warning: a
        # config authored by a newer build must not be reinterpreted, and an
        # elevated Windows process must not follow a link-shaped config path,
        # no matter what interrupted transaction is also waiting.
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="recovery_required",
            stable_code=config.error_code,
            effective_workspace=None,
            allowed_actions=_RECOVERY_ACTIONS,
            detail=config.error_detail,
        )
    top_level_entries = _profile_top_level_entries(home_path)
    if top_level_entries is None:
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code="unknown_layout",
            effective_workspace=effective,
            allowed_actions=_RECOVERY_ACTIONS,
        )

    if not _ignore_transaction and _unfinished_cleanup_transaction(
        home_path,
        profile_kind=kind,
    ):
        cleanup_context = _cleanup_transaction_context(home_path, profile_kind=kind)
        assert cleanup_context is not None
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code="cleanup_transaction_incomplete",
            effective_workspace=effective,
            allowed_actions=_CLEANUP_RECOVERY_ACTIONS,
            cleanup_journal=cleanup_context[1],
        )
    if workspace_patch_exists(home_path):
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code="workspace_patch_incomplete",
            effective_workspace=effective,
            allowed_actions=("reconcile", *_RECOVERY_ACTIONS),
        )
    if not _ignore_settings_transaction:
        from opensquilla.recovery.settings_transaction import settings_transaction_exists

        if settings_transaction_exists(home_path):
            return _report(
                home=home_path,
                config=config,
                candidates=candidates,
                outcome="attention",
                stable_code="settings_transaction_incomplete",
                effective_workspace=effective,
                allowed_actions=("recover-settings", *_RECOVERY_ACTIONS),
            )
    if _legacy_import_transaction_present(home_path):
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code="legacy_import_transaction_incomplete",
            effective_workspace=effective,
            allowed_actions=_RECOVERY_ACTIONS,
        )
    if config.error_code is not None:
        # The hard gates were handled before the journal checks above;
        # everything reaching this branch is a warning with a repair action.
        actions: tuple[str, ...] = _RECOVERY_ACTIONS
        if config.error_code in ("config_invalid", "config_unreadable"):
            # The repair restores the newest valid sibling backup or, failing
            # that, preserves the corrupt file and starts with defaults, so it
            # is offered whether or not a backup currently exists.
            actions = ("recover-config", *_RECOVERY_ACTIONS)
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code=config.error_code,
            effective_workspace=None,
            allowed_actions=actions,
            detail=config.error_detail,
        )
    if kind == "desktop-recovery":
        isolation_code = _recovery_profile_isolation_code(home_path, config)
        if isolation_code is not None:
            return _report(
                home=home_path,
                config=config,
                candidates=candidates,
                outcome="attention",
                stable_code=isolation_code,
                effective_workspace=None,
                allowed_actions=_UNSAFE_RECOVERY_PROFILE_ACTIONS,
            )
    if not _ignore_transaction and _unfinished_replace_transaction(home_path):
        from opensquilla.recovery.transaction import typed_transaction_available

        transaction_actions = (
            ("recover-transaction", *_RECOVERY_ACTIONS)
            if typed_transaction_available(home_path)
            else _RECOVERY_ACTIONS
        )
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code="transaction_incomplete",
            effective_workspace=effective,
            allowed_actions=transaction_actions,
        )

    # Only an empty home that still resolves both implicit canonical roots is
    # safe to initialize. Ambient/explicit missing external paths are never
    # mistaken for a fresh profile.
    if _is_fresh_canonical_profile(
        home_path,
        config,
        top_level_entries=top_level_entries,
    ):
        outcome: RecoveryOutcome = "recovery_profile" if kind == "desktop-recovery" else "ready"
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome=outcome,
            stable_code=(
                "fresh_recovery_profile" if outcome == "recovery_profile" else "fresh_profile"
            ),
            effective_workspace=effective,
            allowed_actions=(),
        )

    # An otherwise implicit profile with only unrecognized top-level content is
    # not a fresh install.  Seeding H/workspace here would create a third,
    # empty identity beside data whose historical layout we cannot prove.
    if (
        top_level_entries
        and not _profile_has_evidence(home_path)
        and not config.workspace_explicit
        and not config.state_explicit
    ):
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code="unknown_layout",
            effective_workspace=effective,
            allowed_actions=_RECOVERY_ACTIONS,
        )

    state_candidate = next(
        (candidate for candidate in candidates if candidate.kind == "state"),
        None,
    )
    if state_candidate is None or not state_candidate.valid:
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code=(
                "effective_state_missing"
                if state_candidate is None or not state_candidate.exists
                else "effective_state_unreadable"
            ),
            effective_workspace=effective,
            allowed_actions=_RECOVERY_ACTIONS,
        )
    state_code = _state_safety_code(state_candidate.path)
    if state_code is not None:
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code=state_code,
            effective_workspace=effective,
            allowed_actions=_RECOVERY_ACTIONS,
        )

    effective_candidate = next(
        (
            candidate
            for candidate in candidates
            if effective is not None and _same_path(candidate.path, effective)
        ),
        None,
    )
    effective_valid = effective_candidate.valid if effective_candidate is not None else False
    roles = _legacy_roles(home_path, config)
    pending_roles = tuple(role for role in roles if role.disposition == "move")
    unsafe_roles = tuple(role for role in roles if role.disposition == "unsafe")
    conflict_roles = tuple(role for role in roles if role.disposition == "conflict")
    deferred_roles = tuple(role for role in roles if role.disposition == "deferred")
    workspace_role = next((role for role in roles if role.name == "workspace"), None)

    if not effective_valid:
        if workspace_role is not None and workspace_role.disposition == "move":
            code = "legacy_workspace_reconcile_available"
        elif workspace_role is not None and workspace_role.disposition == "unsafe":
            code = "unknown_legacy_layout"
        else:
            code = "effective_workspace_missing"
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code=code,
            effective_workspace=effective,
            allowed_actions=_role_actions(roles),
        )

    workspace_unsafe = any(role.name == "workspace" for role in unsafe_roles)
    if workspace_unsafe:
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code="unknown_legacy_layout",
            effective_workspace=effective,
            allowed_actions=_RECOVERY_ACTIONS,
        )

    if pending_roles:
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code="legacy_layout_reconcile_available",
            effective_workspace=effective,
            allowed_actions=("reconcile", *_RECOVERY_ACTIONS),
        )

    if unsafe_roles:
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code="legacy_layout_unsafe",
            effective_workspace=effective,
            allowed_actions=_ATTENTION_ACTIONS,
        )

    if conflict_roles:
        workspace_conflict = any(role.name == "workspace" for role in conflict_roles)
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code=("workspace_conflict" if workspace_conflict else "legacy_layout_conflict"),
            effective_workspace=effective,
            allowed_actions=_ATTENTION_ACTIONS,
        )

    if (
        config.workspace_explicit
        and effective is not None
        and _same_path(effective, home_path / "state" / "workspace")
    ):
        if effective_valid:
            return _with_marker_inspection_status(
                home_path,
                _report(
                    home=home_path,
                    config=config,
                    candidates=candidates,
                    outcome="attention",
                    stable_code="legacy_workspace_pinned",
                    effective_workspace=effective,
                    allowed_actions=_ATTENTION_ACTIONS,
                ),
                profile_kind=kind,
            )
        return _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome="attention",
            stable_code="effective_workspace_missing",
            effective_workspace=effective,
            allowed_actions=_WORKSPACE_RECOVERY_ACTIONS,
        )

    if deferred_roles:
        return _with_marker_inspection_status(
            home_path,
            _report(
                home=home_path,
                config=config,
                candidates=candidates,
                outcome="attention",
                stable_code="legacy_workspace_deferred",
                effective_workspace=effective,
                allowed_actions=_ATTENTION_ACTIONS,
            ),
            profile_kind=kind,
        )

    outcome = "recovery_profile" if kind == "desktop-recovery" else "ready"
    if config.workspace_from_env:
        code = "workspace_env_override"
    elif effective is not None and _same_path(effective, canonical.path):
        code = "canonical_workspace"
    else:
        code = "configured_workspace"
    safe_actions: tuple[str, ...]
    if config.workspace_from_env:
        safe_actions = ()
    elif outcome == "recovery_profile":
        # Legacy recovery identities remain inspectable for read-only
        # diagnostics and cleanup compatibility, but are never offered as a
        # runtime target after primary-only consolidation.
        safe_actions = ()
    else:
        safe_actions = ("choose-workspace",)
    return _with_marker_inspection_status(
        home_path,
        _report(
            home=home_path,
            config=config,
            candidates=candidates,
            outcome=outcome,
            stable_code=code,
            effective_workspace=effective,
            allowed_actions=safe_actions,
        ),
        profile_kind=kind,
    )


def reconcile_profile(
    home: str | Path,
    *,
    profile_kind: str | None = None,
    lock_timeout: float = 0.0,
    _ignore_replace_transaction: bool = False,
) -> RecoveryReport:
    """Reconcile each uniquely proven, no-conflict legacy role independently."""

    home_path = resolve_home_link(_absolute(home))
    resolved_kind = _profile_kind(profile_kind, home=home_path)
    with acquire_profile_locks(
        home_path,
        replacement_history_lock_scope(home_path),
        timeout=lock_timeout,
    ):
        with LegacyGatewayLock(home_path, timeout=lock_timeout):
            from opensquilla.recovery.transaction import (
                finalize_committed_profile_transaction,
            )

            with contextlib.suppress(RecoveryError):
                finalize_committed_profile_transaction(home_path)
            if workspace_patch_exists(home_path):
                recover_workspace_patch(home_path, lock_timeout=lock_timeout)
            before = inspect_profile(
                home_path,
                profile_kind=profile_kind,
                _ignore_transaction=_ignore_replace_transaction,
            )
            config = _read_config(home_path)
            released_layout_proven = _base_legacy_layout_is_proven(home_path, config)
            pending = tuple(
                role
                for role in _legacy_roles(
                    home_path,
                    config,
                    base_proven_override=released_layout_proven,
                )
                if role.disposition == "move"
            )
            if not pending:
                return _finalize_compatibility_marker(
                    home_path,
                    before,
                    profile_kind=resolved_kind,
                )
            raw_operation_failure = False
            role_operation_failure = False
            atomic_state_unknown = False
            for planned in pending:
                current = next(
                    (
                        role
                        for role in _legacy_roles(
                            home_path,
                            _read_config(home_path),
                            base_proven_override=released_layout_proven,
                        )
                        if role.name == planned.name
                        and _same_path(role.source, planned.source)
                        and _same_path(role.destination, planned.destination)
                    ),
                    None,
                )
                if current is None or current.disposition != "move":
                    continue
                try:
                    native_move_no_replace(current.source, current.destination)
                except OSError:
                    raw_operation_failure = True
                    role_operation_failure = True
                    continue
                except AtomicStateUnknownError:
                    # The native rename may already have succeeded. Never
                    # reinterpret an unverifiable post-move tree as ready or
                    # stamp the downgrade marker merely because a later
                    # inspection sees the destination in place.
                    atomic_state_unknown = True
                    continue
                except RecoveryError:
                    # A failure is local to this role. Already-completed atomic
                    # moves remain valid; the final inspection exposes whatever
                    # still requires retry or explicit attention.
                    role_operation_failure = True
                    continue
            final = inspect_profile(
                home_path,
                profile_kind=profile_kind,
                _ignore_transaction=_ignore_replace_transaction,
            )
            if atomic_state_unknown:
                # The native rename may already have succeeded. Never
                # reinterpret an unverifiable post-move tree as ready; startup
                # proceeds with a warning and mutations keep failing closed.
                return replace(
                    final,
                    outcome="attention",
                    stable_code="atomic_state_unknown",
                    allowed_actions=_RECOVERY_ACTIONS,
                )
            effective_is_valid = final.effective_workspace is not None and _workspace_status(
                final.effective_workspace,
                explicitly_configured=True,
            ).valid
            if (
                role_operation_failure
                and effective_is_valid
                and final.outcome == "attention"
            ):
                # A failure confined to an ancillary legacy role must not take
                # a valid identity/session profile offline. Leave the source in
                # place, start with the current effective workspace, and expose
                # the deferred role through attention.
                return replace(
                    final,
                    outcome="attention",
                    stable_code="layout_reconcile_deferred",
                    allowed_actions=_ATTENTION_ACTIONS,
                )
            if raw_operation_failure and final.outcome == "attention":
                return replace(
                    final,
                    outcome="attention",
                    stable_code="layout_reconcile_failed",
                    allowed_actions=tuple(
                        dict.fromkeys(("reconcile", *final.allowed_actions, *_RECOVERY_ACTIONS))
                    ),
                )
            return _finalize_compatibility_marker(
                home_path,
                final,
                profile_kind=resolved_kind,
            )


def choose_workspace(
    home: str | Path,
    *,
    transaction_id: str,
    expected_revision: int,
    workspace: str | Path,
    profile_kind: str | None = None,
    lock_timeout: float = 0.0,
) -> RecoveryReport:
    """CAS-patch the configured workspace after explicit user selection."""

    home_path = resolve_home_link(_absolute(home))
    workspace_path = _absolute(workspace, relative_to=home_path)
    resolved_kind = _profile_kind(profile_kind, home=home_path)
    with ProfileOperationLock(home_path, timeout=lock_timeout):
        with LegacyGatewayLock(home_path, timeout=lock_timeout):
            before = inspect_profile(home_path, profile_kind=profile_kind)
            if before.transaction_id != transaction_id or before.revision != expected_revision:
                raise StaleRecoveryTransactionError(
                    "profile candidates changed; inspect again before choosing a workspace"
                )
            config = _read_config(home_path)
            if config.workspace_from_env:
                override = workspace_override(home_path, include_legacy_dotenv=True)
                name = override[0] if override is not None else "profile dotenv workspace override"
                raise WorkspaceOverrideError(
                    f"remove {name} before changing the persisted workspace path"
                )
            if "choose-workspace" not in before.allowed_actions:
                if resolved_kind == "desktop-recovery":
                    raise InvalidWorkspaceError(
                        "recovery profile workspace is fixed to its canonical workspace"
                    )
                raise InvalidWorkspaceError(
                    "workspace selection is not safe for the current recovery state"
                )
            if resolved_kind == "desktop-recovery" and not _same_path(
                workspace_path,
                home_path / "workspace",
            ):
                raise InvalidWorkspaceError(
                    "recovery profile workspace is fixed to its canonical workspace"
                )
            selected = _workspace_status(workspace_path, explicitly_configured=True)
            if not selected.valid:
                raise InvalidWorkspaceError(
                    "selected workspace is missing, unreadable, or not a directory: "
                    f"{workspace_path}"
                )
            patch_workspace_dir(home_path, workspace_path)
            return inspect_profile(home_path, profile_kind=profile_kind)


def guard_desktop_profile(home: str | Path | None = None) -> RecoveryReport | None:
    """Fail before runtime writes only for the two hard startup gates.

    After the recovery-governance downgrade, ``inspect_profile`` returns
    ``recovery_required`` only for a config authored by a newer build and for
    unsafe path shapes in an elevated Windows process. Every other finding is
    a warning: the profile starts and individual mutations fail closed on
    their own checks. Ordinary CLI profiles are outside this reconciler.
    """

    home_path = _resolved_home_path(home)
    kind = _profile_kind(None, home=home_path)
    if kind not in _DESKTOP_PROFILE_KINDS:
        return None
    report = inspect_profile(home_path, profile_kind=kind)
    if report.outcome == "recovery_required":
        raise RecoveryRequiredError(report)
    return report


@contextlib.contextmanager
def guarded_desktop_profile(
    home: str | Path | None = None,
    *,
    lock_timeout: float = 0.0,
) -> Iterator[RecoveryReport | None]:
    """Retain profile/legacy leases for a writer's complete lifetime.

    Gateway, standalone chat, cron, and channel processes should wrap their
    complete write-capable lifecycle in this context. Desktop-owned profiles
    additionally run the safety inspection before writes; ordinary CLI
    profiles take the universal lock contract without entering the Desktop
    reconciler. Nested service builders may also acquire the profile lock;
    same-process acquisition is intentionally reentrant.
    """

    home_path = _resolved_home_path(home)
    with ProfileOperationLock(home_path, timeout=lock_timeout):
        kind = _profile_kind(None, home=home_path)
        report: RecoveryReport | None = None
        if kind in _DESKTOP_PROFILE_KINDS:
            # The profile lock lives outside H and is therefore safe to take
            # before inspection.  LegacyGatewayLock may create
            # state/gateway.pid.lock, so a hard-gated Desktop profile must be
            # rejected before that compatibility write can happen.
            report = inspect_profile(home_path, profile_kind=kind)
            if report.outcome == "recovery_required":
                raise RecoveryRequiredError(report)
        with LegacyGatewayLock(home_path, timeout=lock_timeout):
            yield report


__all__ = [
    "SUPPORTED_CONFIG_VERSION",
    "choose_workspace",
    "guard_desktop_profile",
    "guarded_desktop_profile",
    "inspect_profile",
    "reconcile_profile",
]
