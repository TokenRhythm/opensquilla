"""Lock-owning Desktop data-cleanup inventory and apply transactions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from opensquilla.recovery.atomic import native_move_no_replace, reparse_tag_redirects
from opensquilla.recovery.config_patch import ConfigSnapshot
from opensquilla.recovery.engine import profile_replacement_transaction_unfinished
from opensquilla.recovery.errors import (
    AtomicStateUnknownError,
    ConfigChangedError,
    RecoveryError,
)
from opensquilla.recovery.locking import (
    ProfileOperationLock,
    acquire_legacy_gateway_locks,
    acquire_profile_locks,
    effective_state_roots,
    move_profile_no_replace,
    profile_lock_path,
    replacement_history_lock_scope,
)

CleanupMode = Literal[
    "reset-current-settings",
    "delete-current-profile",
    "delete-all-user-data",
]
ProfileKind = Literal["primary", "recovery"]

_MODES = frozenset(
    {"reset-current-settings", "delete-current-profile", "delete-all-user-data"}
)
_PROFILE_KINDS = frozenset({"primary", "recovery"})
_RECOVERY_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_NAMESPACE = uuid.UUID("bb0fce5a-ce12-4b55-806f-a4d55352e49b")
_REPARSE_ATTRIBUTE = 0x400
_CLEANUP_JOURNAL_SUFFIX = ".profile-cleanup.json"


class CleanupBlockedError(RecoveryError):
    stable_code = "cleanup_blocked"


@dataclass(frozen=True)
class CleanupItem:
    kind: str
    path: Path
    exists: bool
    identity: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "exists": self.exists,
            "identity": self.identity,
        }


@dataclass(frozen=True)
class CleanupReport:
    outcome: str
    stable_code: str
    mode: CleanupMode
    items: tuple[CleanupItem, ...]
    transaction_id: str
    revision: int
    scope_fingerprint: str
    schema_version: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome,
            "stable_code": self.stable_code,
            "mode": self.mode,
            "items": [item.as_dict() for item in self.items],
            "transaction_id": self.transaction_id,
            "revision": self.revision,
            "scope_fingerprint": self.scope_fingerprint,
        }


@dataclass(frozen=True)
class _ManifestEntry:
    relative: str
    device: int
    inode: int
    mode: int
    size: int
    modified_at_ns: int
    entry_type: str

    def signature(self) -> str:
        if self.entry_type == "directory":
            # A recursively enumerated directory's size and mtime are
            # platform coordination metadata, not user content. Windows can
            # publish those values lazily when descendant handles close. Its
            # identity, mode, type, relative path, and every child entry stay
            # in the manifest, so additions, removals, renames, path swaps,
            # and file mutations still invalidate the cleanup CAS.
            return (
                f"{self.relative}:{self.device}:{self.inode}:{self.mode}:"
                f"directory"
            )
        return (
            f"{self.relative}:{self.device}:{self.inode}:{self.mode}:"
            f"{self.size}:{self.modified_at_ns}:{self.entry_type}"
        )


@dataclass(frozen=True)
class _PlannedItem:
    item: CleanupItem
    manifest: tuple[_ManifestEntry, ...]
    delete: bool
    priority: int
    container_only: bool = False


@dataclass(frozen=True)
class _CleanupPlan:
    user_data: Path
    mode: CleanupMode
    profile_kind: ProfileKind
    recovery_id: str | None
    items: tuple[_PlannedItem, ...]
    profile_homes: tuple[Path, ...]
    transaction_id: str
    revision: int

    def report(
        self, *, outcome: str = "ready", stable_code: str = "cleanup_ready"
    ) -> CleanupReport:
                    return CleanupReport(
            outcome=outcome,
            stable_code=stable_code,
            mode=self.mode,
            items=tuple(planned.item for planned in self.items),
            transaction_id=self.transaction_id,
            revision=self.revision,
            scope_fingerprint=cleanup_scope_fingerprint(
                self.mode,
                (planned.item for planned in self.items),
            ),
        )


def cleanup_scope_fingerprint(
    mode: CleanupMode,
    items: Iterable[CleanupItem],
) -> str:
    """Return an ephemeral digest of the exact kind/path scope shown to the user."""

    parts: list[str] = [mode]
    parts.extend(
        f"{item.kind}\0{_lexical_normalized(item.path)}"
        for item in sorted(
            items,
            key=lambda value: (value.kind, _lexical_normalized(value.path)),
        )
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8", "surrogatepass")).hexdigest()


def _is_reparse(value: os.stat_result) -> bool:
    if not int(getattr(value, "st_file_attributes", 0)) & _REPARSE_ATTRIBUTE:
        return False
    return reparse_tag_redirects(int(getattr(value, "st_reparse_tag", 0)))


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _cleanup_journal_path(primary_home: Path) -> Path:
    return primary_home.parent / f".{primary_home.name}{_CLEANUP_JOURNAL_SUFFIX}"


def _normalized(path: str | Path) -> str:
    candidate = _absolute(path)
    try:
        candidate = candidate.resolve(strict=False)
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(str(candidate)))


def _lexical_normalized(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(_absolute(path))))


def _validate_user_data(path: str | Path) -> Path:
    candidate = _absolute(path)
    try:
        value = candidate.lstat()
    except OSError as exc:
        raise CleanupBlockedError(
            "Desktop user-data directory is unavailable",
            stable_code="cleanup_user_data_unsafe",
        ) from exc
    if stat.S_ISLNK(value.st_mode) or _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
        raise CleanupBlockedError(
            "Desktop user-data must be a real directory",
            stable_code="cleanup_user_data_unsafe",
        )
    return candidate


def _validate_abandon_profile_home(
    user_data: Path,
    home: str | Path,
    profile_kind: str,
) -> Path:
    """Bind cleanup abandonment to one canonical Desktop profile under A."""

    candidate = _absolute(home)
    if profile_kind == "desktop-primary":
        expected = user_data / "opensquilla"
    elif profile_kind == "desktop-recovery":
        recovery_root = candidate.parent
        recovery_container = recovery_root.parent
        if (
            candidate.name != "opensquilla"
            or _lexical_normalized(recovery_container)
            != _lexical_normalized(user_data / "recovery-profiles")
        ):
            raise CleanupBlockedError(
                "cleanup profile does not belong to Desktop user-data",
                stable_code="cleanup_profile_selector_invalid",
            )
        try:
            recovery_id = uuid.UUID(recovery_root.name)
        except ValueError as exc:
            raise CleanupBlockedError(
                "cleanup recovery profile requires a version-4 UUID",
                stable_code="cleanup_profile_selector_invalid",
            ) from exc
        if recovery_id.version != 4 or str(recovery_id) != recovery_root.name:
            raise CleanupBlockedError(
                "cleanup recovery profile requires a version-4 UUID",
                stable_code="cleanup_profile_selector_invalid",
            )
        expected = (
            user_data / "recovery-profiles" / recovery_root.name / "opensquilla"
        )
    else:
        raise CleanupBlockedError(
            "unknown Desktop profile kind",
            stable_code="cleanup_profile_selector_invalid",
        )
    if _lexical_normalized(candidate) != _lexical_normalized(expected):
        raise CleanupBlockedError(
            "cleanup profile does not belong to Desktop user-data",
            stable_code="cleanup_profile_selector_invalid",
        )
    return expected


def _validate_selector(
    mode: str,
    profile_kind: str,
    recovery_id: str | None,
) -> tuple[CleanupMode, ProfileKind, str | None]:
    if mode not in _MODES:
        raise CleanupBlockedError("unknown cleanup mode", stable_code="cleanup_mode_invalid")
    if profile_kind not in _PROFILE_KINDS:
        raise CleanupBlockedError(
            "unknown Desktop profile kind",
            stable_code="cleanup_profile_selector_invalid",
        )
    if profile_kind == "primary":
        if recovery_id:
            raise CleanupBlockedError(
                "primary cleanup cannot select a recovery id",
                stable_code="cleanup_profile_selector_invalid",
            )
        selected_id = None
    else:
        if not recovery_id or not _RECOVERY_ID_RE.fullmatch(recovery_id):
            raise CleanupBlockedError(
                "recovery cleanup requires a version-4 UUID",
                stable_code="cleanup_profile_selector_invalid",
            )
        selected_id = recovery_id
    return mode, profile_kind, selected_id  # type: ignore[return-value]


def _ensure_contained(path: Path, user_data: Path) -> None:
    path_key = os.path.normcase(os.path.normpath(str(_absolute(path))))
    root_key = os.path.normcase(os.path.normpath(str(user_data)))
    try:
        common = os.path.commonpath((path_key, root_key))
    except ValueError as exc:
        raise CleanupBlockedError(
            "cleanup path is outside Desktop user-data",
            stable_code="cleanup_containment_unsafe",
        ) from exc
    if common != root_key or path_key == root_key:
        raise CleanupBlockedError(
            "cleanup path is outside Desktop user-data",
            stable_code="cleanup_containment_unsafe",
        )


def _entry_type(value: os.stat_result) -> str:
    if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
        return "link"
    if stat.S_ISDIR(value.st_mode):
        return "directory"
    if stat.S_ISREG(value.st_mode):
        return "file"
    return "special"


def _manifest(path: Path) -> tuple[_ManifestEntry, ...]:
    try:
        root_stat = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return ()
    except OSError as exc:
        raise CleanupBlockedError(
            f"cleanup path cannot be inspected: {path}",
            stable_code="cleanup_path_unreadable",
        ) from exc
    result: list[_ManifestEntry] = []

    def visit(candidate: Path, relative: str, value: os.stat_result) -> None:
        entry_type = _entry_type(value)
        if entry_type == "special":
            raise CleanupBlockedError(
                f"cleanup refuses special file: {candidate}",
                stable_code="cleanup_special_file",
            )
        result.append(
            _ManifestEntry(
                relative=relative,
                device=int(value.st_dev),
                inode=int(value.st_ino),
                mode=int(value.st_mode),
                size=int(value.st_size),
                modified_at_ns=int(value.st_mtime_ns),
                entry_type=entry_type,
            )
        )
        if entry_type != "directory":
            return
        try:
            entries = sorted(os.scandir(candidate), key=lambda item: item.name)
        except OSError as exc:
            raise CleanupBlockedError(
                f"cleanup directory cannot be enumerated: {candidate}",
                stable_code="cleanup_path_unreadable",
            ) from exc
        for entry in entries:
            try:
                child_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise CleanupBlockedError(
                    f"cleanup entry cannot be inspected: {entry.path}",
                    stable_code="cleanup_path_unreadable",
                ) from exc
            child_relative = entry.name if relative == "." else f"{relative}/{entry.name}"
            visit(Path(entry.path), child_relative, child_stat)

    visit(path, ".", root_stat)
    return tuple(result)


def _cleanup_item(kind: str, path: Path, manifest: tuple[_ManifestEntry, ...]) -> CleanupItem:
    root = manifest[0] if manifest else None
    return CleanupItem(
        kind=kind,
        path=path,
        exists=root is not None,
        identity=f"{root.device}:{root.inode}" if root is not None else None,
    )


def _path_identity_payload(path: Path) -> dict[str, int]:
    value = path.lstat()
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "file_type": stat.S_IFMT(value.st_mode),
        "mode": int(value.st_mode),
        "size": int(value.st_size),
        "modified_at_ns": int(value.st_mtime_ns),
    }


def _identity_matches(path: Path, expected: object) -> bool:
    if not isinstance(expected, dict):
        return False
    try:
        current = _path_identity_payload(path)
    except OSError:
        return False
    return all(current[key] == expected.get(key) for key in current)


def _load_history(user_data: Path) -> tuple[Path, dict[str, Any] | None]:
    path = user_data / "profile-replacement-history.json"
    try:
        snapshot = ConfigSnapshot.capture(path)
    except RecoveryError as exc:
        raise CleanupBlockedError(
            "replacement history is unsafe",
            stable_code="cleanup_history_invalid",
        ) from exc
    if snapshot.identity is None:
        return path, None
    try:
        payload = json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupBlockedError(
            "replacement history is malformed",
            stable_code="cleanup_history_invalid",
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("backups"), list)
    ):
        raise CleanupBlockedError(
            "replacement history schema is unsupported",
            stable_code="cleanup_history_invalid",
        )
    return path, payload


def _validate_backups(
    user_data: Path,
    primary_home: Path,
) -> tuple[Path, dict[str, Any] | None, tuple[Path, ...]]:
    history_path, history = _load_history(user_data)
    disk_backups: dict[str, Path] = {}
    prefix = f"{primary_home.name}.backup."
    try:
        entries = list(os.scandir(user_data))
    except OSError as exc:
        raise CleanupBlockedError(
            "Desktop user-data cannot be enumerated",
            stable_code="cleanup_user_data_unsafe",
        ) from exc
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        suffix = entry.name.removeprefix(prefix)
        if not _RECOVERY_ID_RE.fullmatch(suffix):
            raise CleanupBlockedError(
                "unknown profile backup entry",
                stable_code="cleanup_history_invalid",
            )
        disk_backups[_normalized(Path(entry.path))] = Path(entry.path)
    if history is None:
        if disk_backups:
            raise CleanupBlockedError(
                "profile backups exist without replacement history",
                stable_code="cleanup_history_invalid",
            )
        return history_path, None, ()

    recorded: dict[str, Path] = {}
    seen_transactions: set[str] = set()
    for raw in history["backups"]:
        if not isinstance(raw, dict):
            raise CleanupBlockedError(
                "replacement history contains an unknown entry",
                stable_code="cleanup_history_invalid",
            )
        transaction_id = raw.get("transaction_id")
        target_raw = raw.get("target")
        backup_raw = raw.get("backup")
        if (
            not isinstance(transaction_id, str)
            or not _RECOVERY_ID_RE.fullmatch(transaction_id)
            or transaction_id in seen_transactions
            or not isinstance(target_raw, str)
            or not isinstance(backup_raw, str)
        ):
            raise CleanupBlockedError(
                "replacement history entry is invalid",
                stable_code="cleanup_history_invalid",
            )
        seen_transactions.add(transaction_id)
        backup = Path(backup_raw)
        expected = primary_home.with_name(f"{primary_home.name}.backup.{transaction_id}")
        if _normalized(target_raw) != _normalized(primary_home) or _normalized(
            backup
        ) != _normalized(expected):
            raise CleanupBlockedError(
                "replacement history escapes Desktop user-data",
                stable_code="cleanup_containment_unsafe",
            )
        backup_key = _normalized(backup)
        if backup_key in recorded:
            raise CleanupBlockedError(
                "replacement history duplicates a backup",
                stable_code="cleanup_history_invalid",
            )
        if backup_key not in disk_backups:
            consumed = raw.get("consumed_by_transaction_id")
            if not isinstance(consumed, str) or not _RECOVERY_ID_RE.fullmatch(consumed):
                raise CleanupBlockedError(
                    "replacement history backup is missing",
                    stable_code="cleanup_history_invalid",
                )
            continue
        if not _identity_matches(backup, raw.get("backup_identity")):
            raise CleanupBlockedError(
                "replacement backup identity changed",
                stable_code="cleanup_history_invalid",
            )
        _manifest(backup)
        recorded[backup_key] = backup
    if set(recorded) != set(disk_backups):
        raise CleanupBlockedError(
            "an unrecorded profile backup exists",
            stable_code="cleanup_history_invalid",
        )
    return history_path, history, tuple(recorded[key] for key in sorted(recorded))


def _recovery_roots(user_data: Path) -> tuple[tuple[str, Path, Path], ...]:
    root = user_data / "recovery-profiles"
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise CleanupBlockedError(
            "recovery profile root is unreadable",
            stable_code="cleanup_recovery_root_invalid",
        ) from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or _is_reparse(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        raise CleanupBlockedError(
            "recovery profile root is unsafe",
            stable_code="cleanup_recovery_root_invalid",
        )
    try:
        entries = sorted(os.scandir(root), key=lambda item: item.name)
    except OSError as exc:
        raise CleanupBlockedError(
            "recovery profile root cannot be enumerated",
            stable_code="cleanup_recovery_root_invalid",
        ) from exc
    result: list[tuple[str, Path, Path]] = []
    for entry in entries:
        if not _RECOVERY_ID_RE.fullmatch(entry.name):
            raise CleanupBlockedError(
                "recovery profile root contains a non-UUID entry",
                stable_code="cleanup_recovery_entry_invalid",
            )
        profile_root = Path(entry.path)
        try:
            value = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise CleanupBlockedError(
                "recovery profile entry cannot be inspected",
                stable_code="cleanup_recovery_entry_invalid",
            ) from exc
        if stat.S_ISLNK(value.st_mode) or _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
            raise CleanupBlockedError(
                "recovery profile entry is unsafe",
                stable_code="cleanup_recovery_entry_invalid",
            )
        result.append((entry.name, profile_root, profile_root / "opensquilla"))
    return tuple(result)


def _selected_recovery_root(user_data: Path, recovery_id: str) -> tuple[Path, Path]:
    recovery_root = user_data / "recovery-profiles"
    try:
        root_stat = recovery_root.lstat()
    except FileNotFoundError:
        profile_root = recovery_root / recovery_id
        return profile_root, profile_root / "opensquilla"
    except OSError as exc:
        raise CleanupBlockedError(
            "selected recovery root is unreadable",
            stable_code="cleanup_recovery_root_invalid",
        ) from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or _is_reparse(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        raise CleanupBlockedError(
            "selected recovery root is unsafe",
            stable_code="cleanup_recovery_root_invalid",
        )
    profile_root = recovery_root / recovery_id
    try:
        profile_stat = profile_root.lstat()
    except FileNotFoundError:
        return profile_root, profile_root / "opensquilla"
    except OSError as exc:
        raise CleanupBlockedError(
            "selected recovery profile is unreadable",
            stable_code="cleanup_recovery_entry_invalid",
        ) from exc
    if (
        stat.S_ISLNK(profile_stat.st_mode)
        or _is_reparse(profile_stat)
        or not stat.S_ISDIR(profile_stat.st_mode)
    ):
        raise CleanupBlockedError(
            "selected recovery profile is unsafe",
            stable_code="cleanup_recovery_entry_invalid",
        )
    return profile_root, profile_root / "opensquilla"


def _plan_item(
    user_data: Path,
    kind: str,
    path: Path,
    *,
    delete: bool = True,
    priority: int = 50,
    container_only: bool = False,
) -> _PlannedItem:
    _ensure_contained(path, user_data)
    manifest = _manifest(path)
    return _PlannedItem(
        item=_cleanup_item(kind, path, manifest),
        manifest=manifest,
        delete=delete,
        priority=priority,
        container_only=container_only,
    )


def _build_plan(
    user_data: str | Path,
    *,
    mode: str,
    profile_kind: str,
    recovery_id: str | None,
) -> _CleanupPlan:
    root = _validate_user_data(user_data)
    selected_mode, selected_kind, selected_id = _validate_selector(mode, profile_kind, recovery_id)
    primary_home = root / "opensquilla"
    cleanup_journal = _cleanup_journal_path(primary_home)
    if os.path.lexists(cleanup_journal):
        raise CleanupBlockedError(
            "a previous profile cleanup has not been reconciled",
            stable_code="cleanup_transaction_incomplete",
        )
    history_path = root / "profile-replacement-history.json"
    history: dict[str, Any] | None = None
    backups: tuple[Path, ...] = ()
    recovery_profiles: tuple[tuple[str, Path, Path], ...] = ()
    journal = root / ".opensquilla.profile-replace.json"

    def require_no_settings_transaction(home: Path) -> None:
        from opensquilla.recovery.settings_transaction import settings_transaction_exists

        if settings_transaction_exists(home):
            raise CleanupBlockedError(
                "Desktop settings transaction must be recovered first",
                stable_code="cleanup_settings_transaction_incomplete",
            )

    items: list[_PlannedItem] = []
    profile_homes: list[Path] = []

    def add_profile(
        kind_prefix: str,
        home: Path,
        credential: Path,
        logs: Path,
        *,
        profile_root: Path | None = None,
    ) -> None:
        profile_homes.append(home)
        if profile_root is None:
            items.extend(
                (
                    _plan_item(root, f"{kind_prefix}-home", home, priority=30),
                    _plan_item(root, f"{kind_prefix}-credential", credential, priority=20),
                    _plan_item(root, f"{kind_prefix}-logs", logs, priority=20),
                )
            )
            return
        root_item = _plan_item(root, f"{kind_prefix}-root", profile_root, priority=30)
        items.extend(
            (
                root_item,
                _plan_item(root, f"{kind_prefix}-home", home, delete=False),
                _plan_item(root, f"{kind_prefix}-credential", credential, delete=False),
                _plan_item(root, f"{kind_prefix}-logs", logs, delete=False),
            )
        )

    if selected_mode == "reset-current-settings":
        if selected_kind == "primary":
            require_no_settings_transaction(primary_home)
            if os.path.lexists(journal) and profile_replacement_transaction_unfinished(
                primary_home
            ):
                raise CleanupBlockedError(
                    "profile replacement transaction is not fully committed",
                    stable_code="cleanup_transaction_incomplete",
                )
            profile_homes.append(primary_home)
            items.extend(
                (
                    _plan_item(
                        root,
                        "primary-credential",
                        root / "desktop-credential.json",
                        priority=20,
                    ),
                    _plan_item(
                        root,
                        "migration-pending",
                        root / "migration-provider-setup.json",
                        priority=20,
                    ),
                    _plan_item(
                        root,
                        "migration-result",
                        root / "migration-last-result.json",
                        priority=20,
                    ),
                )
            )
        else:
            assert selected_id is not None
            profile_root, home = _selected_recovery_root(root, selected_id)
            require_no_settings_transaction(home)
            profile_homes.append(home)
            items.append(
                _plan_item(
                    root,
                    "recovery-credential",
                    profile_root / "desktop-credential.json",
                    priority=20,
                )
            )
    elif selected_mode == "delete-current-profile":
        if selected_kind == "primary":
            require_no_settings_transaction(primary_home)
            if os.path.lexists(journal) and profile_replacement_transaction_unfinished(
                primary_home
            ):
                raise CleanupBlockedError(
                    "profile replacement transaction is not fully committed",
                    stable_code="cleanup_transaction_incomplete",
                )
            add_profile(
                "primary",
                primary_home,
                root / "desktop-credential.json",
                root / "logs",
            )
        else:
            assert selected_id is not None
            profile_root, home = _selected_recovery_root(root, selected_id)
            require_no_settings_transaction(home)
            add_profile(
                "recovery",
                home,
                profile_root / "desktop-credential.json",
                profile_root / "logs",
                profile_root=profile_root,
            )
        items.append(
            _plan_item(root, "profile-context", root / "desktop-profile-context.json", priority=10)
        )
        if selected_kind == "primary" and os.path.lexists(journal):
            items.append(_plan_item(root, "replacement-journal", journal, priority=80))
    else:
        history_path, history, backups = _validate_backups(root, primary_home)
        recovery_profiles = _recovery_roots(root)
        require_no_settings_transaction(primary_home)
        for _recovery_name, _profile_root, recovery_home in recovery_profiles:
            require_no_settings_transaction(recovery_home)
        if os.path.lexists(journal) and profile_replacement_transaction_unfinished(
            primary_home
        ):
            raise CleanupBlockedError(
                "profile replacement transaction is not fully committed",
                stable_code="cleanup_transaction_incomplete",
            )
        add_profile(
            "primary",
            primary_home,
            root / "desktop-credential.json",
            root / "logs",
        )
        for recovery_name, profile_root, home in recovery_profiles:
            add_profile(
                f"recovery:{recovery_name}",
                home,
                profile_root / "desktop-credential.json",
                profile_root / "logs",
                profile_root=profile_root,
            )
        recovery_container = root / "recovery-profiles"
        items.append(
            _plan_item(
                root,
                "recovery-profiles-container",
                recovery_container,
                priority=90,
                container_only=True,
            )
        )
        items.extend(
            (
                _plan_item(
                    root, "profile-context", root / "desktop-profile-context.json", priority=10
                ),
                _plan_item(
                    root, "migration-pending", root / "migration-provider-setup.json", priority=20
                ),
                _plan_item(
                    root, "migration-result", root / "migration-last-result.json", priority=20
                ),
            )
        )
        for backup in backups:
            profile_homes.append(backup)
            items.append(_plan_item(root, "profile-backup", backup, priority=40))
        if history is not None:
            items.append(_plan_item(root, "replacement-history", history_path, priority=80))
        if os.path.lexists(journal):
            items.append(_plan_item(root, "replacement-journal", journal, priority=80))

        # "Delete all user data" covers the complete Electron userData root,
        # including Chromium caches/storage and future Desktop-owned files. The
        # profile/recovery/history authorities above are validated first; only
        # then are otherwise-unclassified direct children admitted as bounded
        # no-follow deletion items.
        covered = {
            _lexical_normalized(planned.item.path)
            for planned in items
            if _lexical_normalized(planned.item.path.parent) == _lexical_normalized(root)
        }
        # On macOS the specified OS user-state root and Electron userData can
        # both resolve to ~/Library/Application Support/OpenSquilla. The
        # external coordination lock directory is not user content and must
        # never be inventoried or unlinked while this transaction holds locks;
        # doing so would permit a second inode with the same pathname and split
        # lock ownership. It is intentionally retained after "delete all".
        coordination_lock_root = profile_lock_path(primary_home).parent
        overlapping_coordination_lock = (
            _lexical_normalized(coordination_lock_root.parent)
            == _lexical_normalized(root)
        )
        try:
            remaining_entries = sorted(os.scandir(root), key=lambda entry: entry.name)
        except OSError as exc:
            raise CleanupBlockedError(
                "Desktop user-data cannot be enumerated",
                stable_code="cleanup_user_data_unsafe",
            ) from exc
        for entry in remaining_entries:
            entry_path = Path(entry.path)
            if (
                overlapping_coordination_lock
                and _lexical_normalized(entry_path)
                == _lexical_normalized(coordination_lock_root)
            ):
                continue
            if _lexical_normalized(entry_path) in covered:
                continue
            items.append(_plan_item(root, "user-data-entry", entry_path, priority=70))

    selector = f"{_normalized(root)}\n{selected_mode}\n{selected_kind}\n{selected_id or ''}"
    transaction_id = str(uuid.uuid5(_NAMESPACE, selector))
    coordination_lock_paths: set[str] = set()
    coordination_parent_paths: set[str] = set()
    for profile_home in profile_homes:
        for state_root in effective_state_roots(profile_home):
            # Creating or touching gateway.pid.lock can update directory
            # metadata all the way up the containing tree on Windows. Ignore
            # size/mtime only for those known ancestors; every child entry and
            # each ancestor's device/inode/mode remain part of the CAS
            # signature, so unrelated additions, removals, mutations, or path
            # swaps still change the revision.
            ancestor = _absolute(state_root)
            root_key = _lexical_normalized(root)
            while _lexical_normalized(ancestor) != root_key:
                try:
                    if os.path.commonpath(
                        (_lexical_normalized(ancestor), root_key)
                    ) != root_key:
                        break
                except ValueError:
                    break
                coordination_parent_paths.add(_lexical_normalized(ancestor))
                parent = ancestor.parent
                if parent == ancestor:
                    break
                ancestor = parent
            coordination_lock_paths.add(
                _lexical_normalized(state_root / "gateway.pid.lock")
            )
    signature = [selector]
    for planned in sorted(items, key=lambda value: (str(value.item.path), value.item.kind)):
        signature.append(
            f"item:{planned.item.kind}:{planned.item.path}:{planned.delete}:{planned.container_only}"
        )
        for manifest_entry in planned.manifest:
            entry_path = (
                planned.item.path
                if manifest_entry.relative == "."
                else planned.item.path / Path(manifest_entry.relative)
            )
            entry_key = _lexical_normalized(entry_path)
            if entry_key in coordination_lock_paths:
                continue
            if entry_key in coordination_parent_paths:
                # Creating the persistent compatibility lock changes only its
                # state directory's size/mtime. Child entries remain fully CAS
                # checked, while device/inode/mode still detect parent swaps.
                signature.append(
                    f"{manifest_entry.relative}:{manifest_entry.device}:"
                    f"{manifest_entry.inode}:{manifest_entry.mode}:"
                    f"coordination-parent:{manifest_entry.entry_type}"
                )
                continue
            signature.append(manifest_entry.signature())
    digest = hashlib.sha256("\n".join(signature).encode("utf-8", "surrogatepass")).digest()
    revision = int.from_bytes(digest[:8], "big") & ((1 << 53) - 1)
    unique_homes: dict[str, Path] = {}
    for home in profile_homes:
        try:
            value = home.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CleanupBlockedError(
                "profile home cannot be inspected",
                stable_code="cleanup_profile_home_unsafe",
            ) from exc
        else:
            if stat.S_ISLNK(value.st_mode) or _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
                raise CleanupBlockedError(
                    "profile home must be a real directory",
                    stable_code="cleanup_profile_home_unsafe",
                )
        unique_homes.setdefault(_normalized(home), home)
    return _CleanupPlan(
        user_data=root,
        mode=selected_mode,
        profile_kind=selected_kind,
        recovery_id=selected_id,
        items=tuple(items),
        profile_homes=tuple(unique_homes[key] for key in sorted(unique_homes)),
        transaction_id=transaction_id,
        revision=revision,
    )


def _blocked_report(
    *,
    user_data: str | Path,
    mode: str,
    profile_kind: str,
    recovery_id: str | None,
    error: RecoveryError,
) -> CleanupReport:
    safe_mode: CleanupMode = (
        mode if mode in _MODES else "delete-current-profile"  # type: ignore[assignment]
    )
    selector = f"{_absolute(user_data)}\n{mode}\n{profile_kind}\n{recovery_id or ''}"
    return CleanupReport(
        outcome="blocked",
        stable_code=error.stable_code,
        mode=safe_mode,
        items=(),
        transaction_id=str(uuid.uuid5(_NAMESPACE, selector)),
        revision=0,
        scope_fingerprint=cleanup_scope_fingerprint(safe_mode, ()),
    )


def cleanup_inspect(
    user_data: str | Path,
    *,
    mode: str,
    profile_kind: str,
    recovery_id: str | None = None,
) -> CleanupReport:
    """Build a completely read-only cleanup inventory."""
    try:
        plan = _build_plan(
            user_data,
            mode=mode,
            profile_kind=profile_kind,
            recovery_id=recovery_id,
        )
    except RecoveryError as exc:
        return _blocked_report(
            user_data=user_data,
            mode=mode,
            profile_kind=profile_kind,
            recovery_id=recovery_id,
            error=exc,
        )
    except OSError:
        return _blocked_report(
            user_data=user_data,
            mode=mode,
            profile_kind=profile_kind,
            recovery_id=recovery_id,
            error=CleanupBlockedError(
                "cleanup inventory could not be read safely",
                stable_code="cleanup_io_error",
            ),
        )
    return plan.report()


def _remove_no_follow(path: Path) -> None:
    try:
        value = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return
    entry_type = _entry_type(value)
    if entry_type == "special":
        raise OSError(f"refusing to delete special file: {path}")
    if entry_type == "link":
        if stat.S_ISDIR(value.st_mode):
            os.rmdir(path)
        else:
            path.unlink()
        return
    if entry_type == "file":
        path.unlink()
        return
    # CPython uses its descriptor-based, symlink-attack-resistant rmtree on
    # capable platforms and has explicit Windows junction protection. The root
    # itself was lstat-verified as a real directory immediately above.
    shutil.rmtree(path)


def _current_item(planned: _PlannedItem) -> CleanupItem:
    try:
        value = planned.item.path.lstat()
    except OSError:
        return replace(planned.item, exists=False, identity=None)
    return replace(
        planned.item,
        exists=True,
        identity=f"{int(value.st_dev)}:{int(value.st_ino)}",
    )


def _manifest_matches(
    current: tuple[_ManifestEntry, ...],
    expected: tuple[_ManifestEntry, ...],
) -> bool:
    """Compare content-bearing manifest state without volatile directory times."""

    return tuple(item.signature() for item in current) == tuple(
        item.signature() for item in expected
    )


def _delete_all_has_residual_root_entries(
    user_data: Path,
    *,
    cleanup_journal: Path,
) -> bool:
    """Detect data created after the confirmed delete-all inventory.

    The cleanup journal is the transaction authority currently protecting the
    empty profile, and an overlapping OS coordination-lock directory must stay
    alive until all held locks are released.  Every other direct child is user
    data that this transaction did not prove it deleted, so completion would be
    false and the journal must remain visible for recovery.
    """

    allowed = {_lexical_normalized(cleanup_journal)}
    coordination_lock_root = profile_lock_path(user_data / "opensquilla").parent
    if _lexical_normalized(coordination_lock_root.parent) == _lexical_normalized(
        user_data
    ):
        allowed.add(_lexical_normalized(coordination_lock_root))
    try:
        entries = os.scandir(user_data)
        with entries:
            return any(
                _lexical_normalized(Path(entry.path)) not in allowed
                for entry in entries
            )
    except OSError:
        # An unreadable or concurrently replaced root can never be declared
        # empty.  The caller retains the cleanup journal and reports partial.
        return True


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while creating cleanup journal")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # The journal file itself is flushed before publication. CPython cannot
        # open directory descriptors for fsync on Windows; the real Windows
        # crash/failpoint suite remains the durability gate for this boundary.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes_no_replace(path: Path, data: bytes, *, sync_parent: bool = True) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if sync_parent:
        _fsync_directory(path.parent)


def _write_cleanup_journal(path: Path, payload: dict[str, Any]) -> ConfigSnapshot:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    _write_bytes_no_replace(path, data)
    snapshot = ConfigSnapshot.capture(path)
    if snapshot.identity is None or snapshot.data != data:
        raise CleanupBlockedError(
            "cleanup journal could not be verified after publication",
            stable_code="cleanup_journal_unsafe",
        )
    return snapshot


def _unlink_cleanup_journal(snapshot: ConfigSnapshot) -> None:
    snapshot.assert_current()
    snapshot.path.unlink()
    try:
        _fsync_directory(snapshot.path.parent)
    except OSError:
        # The profile may already be absent. Restore a visible bootstrap guard
        # before surfacing the durability error so a restart cannot seed a new
        # canonical profile while cleanup state is uncertain.
        if not os.path.lexists(snapshot.path):
            with contextlib.suppress(OSError):
                _write_bytes_no_replace(snapshot.path, snapshot.data, sync_parent=False)
        raise


def _tombstone_path(path: Path, *, transaction_id: str, index: int) -> Path:
    return path.with_name(f".{path.name}.cleanup.{transaction_id}.{index}")


def _rollback_quarantined_directory(
    source: Path,
    tombstone: Path,
) -> bool:
    if not os.path.lexists(tombstone) or os.path.lexists(source):
        return False
    try:
        move_profile_no_replace(tombstone, source, move=native_move_no_replace)
    except (OSError, RecoveryError):
        return False
    return True


def _snapshot_matches(left: ConfigSnapshot, right: ConfigSnapshot) -> bool:
    left_identity = (
        left.identity.metadata_tuple() if left.identity is not None else None
    )
    right_identity = (
        right.identity.metadata_tuple() if right.identity is not None else None
    )
    return left_identity == right_identity and left.digest == right.digest


def _rollback_moved_cleanup_journal(
    moved: ConfigSnapshot,
    journal: Path,
) -> bool:
    if moved.identity is None or os.path.lexists(journal):
        return False
    try:
        moved.assert_current()
        native_move_no_replace(moved.path, journal)
        restored = ConfigSnapshot.capture(journal)
        _fsync_directory(journal.parent)
    except (OSError, RecoveryError):
        return False
    return _snapshot_matches(moved, restored)


def abandon_cleanup_transaction(
    user_data: str | Path,
    *,
    home: str | Path,
    profile_kind: str,
    transaction_id: str,
    expected_revision: int,
    lock_timeout: float = 0.0,
) -> Path:
    """Quarantine a stopped cleanup journal without deleting any more data.

    The global user-data profile lock is also held by RC4 cleanup writers. Once
    acquired here it proves no such writer is still traversing its confirmed
    inventory. The journal is moved no-replace to an owner-visible backup; all
    surviving canonical paths and cleanup tombstones are left untouched.
    """

    root = _validate_user_data(user_data)
    selected_home = _validate_abandon_profile_home(root, home, profile_kind)
    journal = _cleanup_journal_path(root / "opensquilla")
    with ProfileOperationLock(
        replacement_history_lock_scope(root / "opensquilla"),
        timeout=lock_timeout,
    ):
        from opensquilla.recovery.engine import inspect_profile
        from opensquilla.recovery.errors import StaleRecoveryTransactionError

        snapshot = ConfigSnapshot.capture(journal)
        inspected = inspect_profile(selected_home, profile_kind=profile_kind)
        if (
            inspected.stable_code != "cleanup_transaction_incomplete"
            or inspected.transaction_id != transaction_id
            or inspected.revision != expected_revision
        ):
            raise StaleRecoveryTransactionError(
                "cleanup recovery state changed; inspect again before abandoning it"
            )
        if snapshot.identity is None:
            raise CleanupBlockedError(
                "no cleanup transaction is present",
                stable_code="cleanup_transaction_missing",
            )
        destination = root / (
            f".opensquilla.profile-cleanup.abandoned.{uuid.uuid4()}.json"
        )
        snapshot.assert_current()
        native_move_no_replace(journal, destination)
        try:
            moved = ConfigSnapshot.capture(destination)
        except RecoveryError as exc:
            raise AtomicStateUnknownError(
                "cleanup journal archive identity could not be verified"
            ) from exc
        if not _snapshot_matches(snapshot, moved):
            if _rollback_moved_cleanup_journal(moved, journal):
                raise ConfigChangedError(
                    "cleanup journal changed during archive publication"
                )
            raise AtomicStateUnknownError(
                "cleanup journal changed and its archive could not be rolled back"
            )
        _fsync_directory(root)
        return destination


def _quarantine_directory(
    planned: _PlannedItem,
    tombstone: Path,
) -> bool:
    source = planned.item.path
    if not _manifest_matches(_manifest(source), planned.manifest):
        return False
    moved = False
    try:
        move_profile_no_replace(source, tombstone, move=native_move_no_replace)
        moved = True
        # The native primitive verifies the identity it actually moved, while
        # the confirmed cleanup plan identifies the only object we are allowed
        # to quarantine. Compare both after publication to close the final
        # check-to-rename window for an uncooperative old process.
        if not _manifest_matches(_manifest(tombstone), planned.manifest):
            raise ConfigChangedError("cleanup source changed while it was quarantined")
        return True
    except (OSError, RecoveryError):
        if not moved and not os.path.lexists(source) and os.path.lexists(tombstone):
            moved = True
        if moved:
            _rollback_quarantined_directory(source, tombstone)
        return False


def _delete_quarantined_directory(
    planned: _PlannedItem,
    tombstone: Path,
) -> bool:
    """Delete only the identity-bound tombstone after legacy handles close."""

    source = planned.item.path
    try:
        if not _manifest_matches(_manifest(tombstone), planned.manifest):
            return False
        # Canonical ``source`` is observation-only from this point onward. An
        # old binary may recreate it with a second lock inode; recursive removal
        # remains permanently scoped to the verified tombstone.
        _remove_no_follow(tombstone)
    except (OSError, RecoveryError):
        # A fail-before-delete leaves the complete quarantine recoverable at its
        # canonical path. A partial recursive deletion is retained with the
        # journal instead of guessing or merging the remaining tree.
        intact = False
        if not os.path.lexists(source) and os.path.lexists(tombstone):
            try:
                intact = _manifest_matches(_manifest(tombstone), planned.manifest)
            except (OSError, RecoveryError):
                pass
        if intact:
            _rollback_quarantined_directory(source, tombstone)
        return False
    return not os.path.lexists(tombstone) and not os.path.lexists(source)


def cleanup_apply(
    user_data: str | Path,
    *,
    mode: str,
    profile_kind: str,
    transaction_id: str,
    expected_revision: int,
    confirm_user_data: str | Path,
    recovery_id: str | None = None,
    lock_timeout: float = 0.0,
) -> CleanupReport:
    """CAS-recheck, lock, and apply one Desktop cleanup inventory."""
    inspected = cleanup_inspect(
        user_data,
        mode=mode,
        profile_kind=profile_kind,
        recovery_id=recovery_id,
    )
    if inspected.outcome == "blocked":
        return inspected
    try:
        initial = _build_plan(
            user_data,
            mode=mode,
            profile_kind=profile_kind,
            recovery_id=recovery_id,
        )
        if _lexical_normalized(confirm_user_data) != _lexical_normalized(initial.user_data):
            raise CleanupBlockedError(
                "cleanup confirmation does not match Desktop user-data",
                stable_code="cleanup_confirmation_mismatch",
            )
        history_scope = replacement_history_lock_scope(
            initial.user_data / "opensquilla"
        )
        with acquire_profile_locks(
            *initial.profile_homes,
            history_scope,
            timeout=lock_timeout,
        ):
            with acquire_legacy_gateway_locks(
                *initial.profile_homes,
                timeout=lock_timeout,
            ) as legacy_locks:
                authoritative = _build_plan(
                    user_data,
                    mode=mode,
                    profile_kind=profile_kind,
                    recovery_id=recovery_id,
                )
                if (
                    authoritative.transaction_id != transaction_id
                    or authoritative.revision != expected_revision
                ):
                    raise CleanupBlockedError(
                        "cleanup inventory changed; inspect again",
                        stable_code="cleanup_inventory_stale",
                    )

                actions = sorted(
                    (item for item in authoritative.items if item.delete),
                    key=lambda item: (
                        item.priority,
                        -len(item.item.path.parts),
                        str(item.item.path),
                    ),
                )
                failed = False
                journal_snapshot: ConfigSnapshot | None = None
                if authoritative.mode == "reset-current-settings":
                    for planned in actions:
                        try:
                            if not _manifest_matches(
                                _manifest(planned.item.path), planned.manifest
                            ):
                                failed = True
                                break
                            _remove_no_follow(planned.item.path)
                        except (OSError, RecoveryError):
                            failed = True
                            break
                else:
                    directory_actions = tuple(
                        planned
                        for planned in actions
                        if not planned.container_only
                        and bool(planned.manifest)
                        and planned.manifest[0].entry_type == "directory"
                    )
                    tombstones = tuple(
                        _tombstone_path(
                            planned.item.path,
                            transaction_id=authoritative.transaction_id,
                            index=index,
                        )
                        for index, planned in enumerate(directory_actions)
                    )
                    if any(os.path.lexists(path) for path in tombstones):
                        raise CleanupBlockedError(
                            "a cleanup tombstone already exists",
                            stable_code="cleanup_tombstone_exists",
                        )
                    journal_path = _cleanup_journal_path(
                        authoritative.user_data / "opensquilla"
                    )
                    journal_payload: dict[str, Any] = {
                        "schema_version": 1,
                        "operation": "profile-cleanup",
                        "transaction_id": authoritative.transaction_id,
                        "phase": "prepared",
                        "primary_home": str(authoritative.user_data / "opensquilla"),
                        "mode": authoritative.mode,
                        "profile_kind": authoritative.profile_kind,
                        "recovery_id": authoritative.recovery_id,
                        "tombstones": [
                            {
                                "kind": planned.item.kind,
                                "source": str(planned.item.path),
                                "tombstone": str(tombstone),
                                "source_identity": {
                                    "device": planned.manifest[0].device,
                                    "inode": planned.manifest[0].inode,
                                    "mode": planned.manifest[0].mode,
                                    "size": planned.manifest[0].size,
                                    "modified_at_ns": planned.manifest[0].modified_at_ns,
                                },
                            }
                            for planned, tombstone in zip(
                                directory_actions,
                                tombstones,
                                strict=True,
                            )
                        ],
                    }
                    journal_snapshot = _write_cleanup_journal(journal_path, journal_payload)

                    quarantined: list[tuple[_PlannedItem, Path]] = []
                    for planned, tombstone in zip(
                        directory_actions,
                        tombstones,
                        strict=True,
                    ):
                        if not _quarantine_directory(planned, tombstone):
                            failed = True
                            break
                        quarantined.append((planned, tombstone))

                    if failed:
                        for planned, tombstone in reversed(quarantined):
                            _rollback_quarantined_directory(
                                planned.item.path,
                                tombstone,
                            )

                    if not failed:
                        # On Windows a directory containing an open file handle
                        # cannot be removed. All identity-bound moves must finish
                        # before releasing old-gateway exclusion, but recursive
                        # deletion must happen only after the compatibility lock
                        # handles inside those tombstones are closed. The outer
                        # profile-operation locks remain held throughout.
                        for legacy_lock in reversed(legacy_locks):
                            legacy_lock.release()

                        for planned, tombstone in quarantined:
                            if not _delete_quarantined_directory(planned, tombstone):
                                failed = True
                                break

                    if not failed:
                        directory_ids = {id(planned) for planned in directory_actions}
                        for planned in actions:
                            if id(planned) in directory_ids:
                                continue
                            try:
                                if planned.container_only:
                                    planned.item.path.rmdir()
                                else:
                                    if not _manifest_matches(
                                        _manifest(planned.item.path), planned.manifest
                                    ):
                                        failed = True
                                        break
                                    _remove_no_follow(planned.item.path)
                            except FileNotFoundError:
                                if planned.item.exists:
                                    failed = True
                                    break
                            except (OSError, RecoveryError):
                                failed = True
                                break

                current_items = tuple(_current_item(item) for item in authoritative.items)
                remaining = any(
                    current.exists
                    for planned, current in zip(authoritative.items, current_items, strict=True)
                    if planned.delete
                )
                failed = failed or remaining
                if (
                    authoritative.mode == "delete-all-user-data"
                    and journal_snapshot is not None
                    and _delete_all_has_residual_root_entries(
                        authoritative.user_data,
                        cleanup_journal=journal_snapshot.path,
                    )
                ):
                    failed = True
                if journal_snapshot is not None and not failed:
                    try:
                        _unlink_cleanup_journal(journal_snapshot)
                    except (OSError, RecoveryError):
                        failed = True
                    current_items = tuple(_current_item(item) for item in authoritative.items)
                if journal_snapshot is not None and os.path.lexists(journal_snapshot.path):
                    failed = True
                return CleanupReport(
                    outcome="partial" if failed else "complete",
                    stable_code="cleanup_partial" if failed else "cleanup_complete",
                    mode=authoritative.mode,
                    items=current_items,
                        transaction_id=authoritative.transaction_id,
                        revision=authoritative.revision,
                        scope_fingerprint=cleanup_scope_fingerprint(
                            authoritative.mode,
                            (planned.item for planned in authoritative.items),
                        ),
                    )
    except RecoveryError as exc:
        return _blocked_report(
            user_data=user_data,
            mode=mode,
            profile_kind=profile_kind,
            recovery_id=recovery_id,
            error=exc,
        )
    except OSError:
        return _blocked_report(
            user_data=user_data,
            mode=mode,
            profile_kind=profile_kind,
            recovery_id=recovery_id,
            error=CleanupBlockedError(
                "cleanup transaction could not acquire or verify filesystem state",
                stable_code="cleanup_io_error",
            ),
        )


__all__ = [
    "abandon_cleanup_transaction",
    "CleanupItem",
    "CleanupReport",
    "cleanup_apply",
    "cleanup_inspect",
    "cleanup_scope_fingerprint",
]
