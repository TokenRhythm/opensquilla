"""OpenSquilla self-migration: import a legacy OpenSquilla home into this install.

Unlike the foreign-runtime migrators (OpenClaw, Hermes) this source is
shape-identical to the target: a legacy CLI home, an orphaned Windows
portable data dir, and the desktop Electron home all share the OpenSquilla
home layout. The import is therefore a guarded whole-home copy — excluding
machine-local code-task run worktrees — with pre-flight checks, a transactional
staged copy, and a small set of transforms (config path unpinning, inline-secret
relocation, scheduler pause), rather than a per-item semantic mapping.

The report dict returned by :meth:`OpenSquillaHomeMigrator.migrate` is a
pinned wire contract covered by
``tests/test_contracts/test_migration_report_wire.py``.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import tomllib
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

import structlog
from pydantic import ValidationError

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.config_migration import migrate_config_payload
from opensquilla.migration.env_file import (
    env_line_key,
    merge_env_lines,
    write_secret_env_file,
)
from opensquilla.migration.openclaw import ItemResult
from opensquilla.migration.source_snapshot_windows import (
    WindowsDirectoryAuthoritySnapshot,
    WindowsSourceSnapshot,
    capture_windows_bounded_files,
    copy_windows_snapshot_file,
    scan_windows_source_tree,
)
from opensquilla.paths import default_opensquilla_home
from opensquilla.recovery.locking import (
    LegacyGatewayLock,
    LegacyGatewayLockFileSnapshot,
)

log = structlog.get_logger(__name__)

OPENSQUILLA_SOURCE_KINDS: tuple[str, ...] = ("cli-home", "windows-portable", "desktop-home")

#: Free-space headroom demanded on top of the source home size.
_DISK_MARGIN_BYTES = 64 * 1024 * 1024
#: Legacy RC3 completion marker. RC4 treats it as a display hint only and never
#: writes it; the target-side receipt is the sole import authority.
IMPORT_MARKER_FILENAME = ".opensquilla-imported.json"
#: Whole-profile replacement journal. It deliberately lives beside the target
#: so parking/replacing the target cannot move the recovery authority itself.
_COMMIT_JOURNAL = "profile-replace.json"
#: Target-parent backup index consumed by ``recovery restore-profile``.
_REPLACEMENT_HISTORY = "profile-replacement-history.json"
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
_LAYOUT_RECEIPT_FILENAME = "layout-receipt.json"
_LAYOUT_RECEIPT_SCHEMA_VERSION = 1
_LAYOUT_CONTRACT = "opensquilla-profile-root-v1"
_LAYOUT_RECEIPT_MAX_BYTES = 64 * 1024
_LAYOUT_RECEIPT_MAX_ENTRIES = 128
_CANDIDATE_METADATA_MAX_ENTRIES = 20_000
_CANDIDATE_METADATA_MAX_DEPTH = 64
_CANDIDATE_METADATA_MAX_CONFIG_BYTES = 1024 * 1024
_PROFILE_DOTENV_MAX_BYTES = 1024 * 1024
_SAFE_RAW_AGENT_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_CANDIDATE_METADATA_MAX_SQLITE_BYTES = 256 * 1024 * 1024
_CANDIDATE_ENUMERATION_MAX_ENTRIES = 256
_CANDIDATE_ENUMERATION_MAX_CANDIDATES = 32
_RECEIPT_IDENTITY_FIELDS = frozenset(
    {"device", "inode", "file_type", "mode", "size", "modified_at_ns"}
)
_LAYOUT_RECEIPT_FIELDS = frozenset(
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
#: Top-level source dirs never copied (profile homes nest whole other homes).
_EXCLUDED_TOP_LEVEL_DIRS = frozenset(
    {
        "profiles",
        "migration",
        "recovery",
        "recovery-profiles",
    }
)
#: Machine-local run worktrees are intentionally not portable profile data.
#: They contain cloned repositories, virtual environments, native build output,
#: and per-run provider snapshots.  The source is left untouched so operators
#: can still recover a patch or transcript from the originating installation.
_EXCLUDED_NONPORTABLE_TOP_LEVEL_DIRS = frozenset({"code-task"})
#: The initial snapshot is inspection-only: it supplies bounded config/dotenv
#: reads before data roots are classified, and is never copied.  On POSIX it may
#: therefore record any relative link that stays lexically inside the source
#: home.  The later role snapshots are the actual copy authority and permit
#: links only inside workspace roots (including configured/agent workspaces).
_INITIAL_INSPECTION_LINK_ROOTS = frozenset({Path()})
#: Layout/import/recovery authority belongs to the source installation and may
#: never become authoritative in the imported target.
_EXCLUDED_AUTHORITY_NAMES = frozenset(
    {
        IMPORT_MARKER_FILENAME,
        "desktop-layout-v2.json",
        "desktop-layout-v3.json",
        "desktop-recovery-v1.json",
        ".opensquilla-layout-v2.json",
        ".opensquilla-layout-v3.json",
        "desktop-migration-pending.json",
        ".opensquilla-migration-pending.json",
        "migration-pending.json",
    }
)
#: Runtime lock files under ``state/`` never copied.
_EXCLUDED_STATE_FILES = ("gateway.pid", "gateway.pid.lock")
#: SQLite stores whose durable ``-wal`` sidecars must travel with them.
_SQLITE_STORES = (
    Path("state/sessions.db"),
    Path("state/scheduler.db"),
    Path("state/approval_queue.sqlite"),
    Path("state/sandbox_user_grants.sqlite"),
    Path("state/channel_delivery.sqlite"),
)
_SQLITE_DURABLE_SIDECAR_SUFFIXES = ("-wal",)
_SQLITE_TRANSIENT_SIDECAR_SUFFIXES = ("-shm",)
_SQLITE_ALL_SIDECAR_SUFFIXES = (
    *_SQLITE_DURABLE_SIDECAR_SUFFIXES,
    *_SQLITE_TRANSIENT_SIDECAR_SUFFIXES,
)
_WINDOWS_LOCK_ERROR_CODES = frozenset({32, 33})
_REPARSE_POINT_ATTRIBUTE = 0x400
_GATEWAY_AUTHORITY_MAX_BYTES = 64 * 1024
_JOURNAL_PHASES = frozenset(
    {
        "prepared",
        "target_parked",
        "candidate_published_unvalidated",
        "validated",
        "committed",
    }
)
_LEGACY_DEFAULT_PORT = 18790
_CURRENT_DEFAULT_PORT = 18791
_FALLBACK_LLM_ENV_KEY = "OPENSQUILLA_LLM_API_KEY"
_ELEVENLABS_ENV_KEY = "ELEVENLABS_API_KEY"
_DOTENV_DATA_ROOT_KEYS: dict[str, tuple[str, ...]] = {
    "state": ("OPENSQUILLA_GATEWAY_STATE_DIR",),
    "workspace": (
        "OPENSQUILLA_GATEWAY_WORKSPACE_DIR",
        "OPENSQUILLA_WORKSPACE_DIR",
    ),
    "media": (
        "OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT",
        "OPENSQUILLA_ATTACHMENTS_MEDIA_ROOT",
    ),
}
_DOTENV_HOME_SELECTOR_KEYS = (
    "OPENSQUILLA_STATE_DIR",
    "OPENSQUILLA_HOME",
    "OPENSQUILLA_PROFILE",
    "OPENSQUILLA_GATEWAY_CONFIG_PATH",
)
_DOTENV_PROFILE_SCOPED_KEYS = frozenset(
    {
        *_DOTENV_HOME_SELECTOR_KEYS,
        *(key for keys in _DOTENV_DATA_ROOT_KEYS.values() for key in keys),
    }
)


def _ext(path: Path) -> str:
    """Return an extended-length path string on Windows, a plain string elsewhere.

    Deep portable workspace trees routinely exceed the 260-character default
    Windows path limit; the ``\\\\?\\`` prefix lifts it for copy operations.
    """
    if sys.platform == "win32":  # pragma: no cover - Windows-only path
        return "\\\\?\\" + str(path.resolve())
    return str(path)


def _as_path(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser()


def _same_path(first: Path, second: Path) -> bool:
    try:
        return first.resolve(strict=False) == second.resolve(strict=False)
    except OSError:
        return first == second


def _path_pin_is_absolute(value: str) -> bool:
    """Recognize native, POSIX, and Windows drive/UNC absolute config pins."""
    stripped = value.strip()
    if not stripped:
        return False
    return (
        Path(stripped).expanduser().is_absolute()
        or PurePosixPath(stripped).is_absolute()
        or PureWindowsPath(stripped).is_absolute()
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    """Return whether either resolved path contains the other."""
    try:
        resolved_first = first.resolve(strict=False)
        resolved_second = second.resolve(strict=False)
    except OSError:
        resolved_first, resolved_second = first, second
    return resolved_first.is_relative_to(resolved_second) or resolved_second.is_relative_to(
        resolved_first
    )


def _path_is_relative_to_lexical(path: Path, root: Path) -> bool:
    """Return whether ``path`` is equal to or below ``root`` without following links."""

    path_absolute = path.expanduser().absolute()
    root_absolute = root.expanduser().absolute()
    path_value = os.path.normcase(os.path.normpath(str(path_absolute)))
    root_value = os.path.normcase(os.path.normpath(str(root_absolute)))
    try:
        if os.path.commonpath([path_value, root_value]) == root_value:
            return True
    except ValueError:
        return False

    def plain_directory_identity(candidate: Path) -> tuple[int, int] | None:
        try:
            result = candidate.lstat()
        except OSError:
            return None
        attributes = int(getattr(result, "st_file_attributes", 0) or 0)
        if (
            stat.S_ISLNK(result.st_mode)
            or attributes & _REPARSE_POINT_ATTRIBUTE
            or not stat.S_ISDIR(result.st_mode)
        ):
            return None
        return int(result.st_dev), int(result.st_ino)

    # Darwin's ``normcase`` is intentionally a no-op even on the usual
    # case-insensitive APFS volume. Compare existing plain-directory identities
    # so an alternate spelling such as CODE-TASK cannot bypass the lexical
    # exclusion, while a distinct directory on a case-sensitive volume remains
    # valid. Missing descendants are skipped until an existing ancestor is found.
    root_identity = plain_directory_identity(root_absolute)
    if root_identity is None:
        return False
    current = path_absolute
    while True:
        if plain_directory_identity(current) == root_identity:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def is_valid_opensquilla_home(path: Path) -> bool:
    """Return True when ``path`` plausibly holds an OpenSquilla home."""
    try:
        root_stat = path.lstat()
        if _supported_entry_type(path, root_stat) != "directory":
            return False
    except OSError:
        return False
    for candidate, expected in (
        (path / "config.toml", "file"),
        (path / "state", "directory"),
        (path / "workspace", "directory"),
    ):
        try:
            if _supported_entry_type(candidate, candidate.lstat()) == expected:
                return True
        except OSError:
            continue
    return False


def detect_legacy_cli_home(target: Path) -> Path | None:
    """Return ``~/.opensquilla`` when it is a legacy home distinct from ``target``.

    A plain CLI user whose active home IS ``~/.opensquilla`` must never see
    their own live home offered as a migration source; only installs whose
    target resolves elsewhere (desktop spawns, relocated state dirs) get the
    CLI home auto-detected.
    """
    legacy = Path.home() / ".opensquilla"
    if not is_valid_opensquilla_home(legacy):
        return None
    if _same_path(legacy, target):
        return None
    return legacy


def _source_was_previously_imported(
    source: Path,
    target: Path,
    *,
    source_kind: str,
) -> bool:
    """Return a display hint without suppressing an otherwise valid source."""

    return _matching_import_receipt(source, target, source_kind=source_kind) is not None


def _source_marker_matches_target(_source: Path, _target: Path) -> bool:
    """Compatibility hook for old advisory callers; markers never hide sources."""

    return False


def _valid_layout_import_receipt(
    receipt: object,
    *,
    source: Path,
    target: Path,
    transaction_id: str,
    require_validated_recovery: bool = True,
) -> bool:
    if not isinstance(receipt, dict):
        return False
    if set(receipt) != _LAYOUT_RECEIPT_FIELDS:
        return False
    try:
        parsed_transaction_id = str(uuid.UUID(transaction_id))
        imported_at = datetime.fromisoformat(str(receipt.get("imported_at", "")))
    except (ValueError, TypeError):
        return False
    if parsed_transaction_id != transaction_id or imported_at.tzinfo is None:
        return False
    recovery_outcome = receipt.get("recovery_outcome")
    recovery_stable_code = receipt.get("recovery_stable_code")
    recovery_valid = (
        recovery_outcome in {"ready", "attention"}
        and isinstance(recovery_stable_code, str)
        and bool(recovery_stable_code)
    )
    if require_validated_recovery and not recovery_valid:
        return False
    if not require_validated_recovery and not (
        recovery_valid
        or (recovery_outcome == "pending" and recovery_stable_code == "")
    ):
        return False
    if (
        receipt.get("schema_version") != _LAYOUT_RECEIPT_SCHEMA_VERSION
        or receipt.get("transaction_id") != transaction_id
        or receipt.get("source") != _normalized_path(source)
        or receipt.get("target") != _normalized_path(target)
        or receipt.get("source_kind") not in OPENSQUILLA_SOURCE_KINDS
        or not isinstance(receipt.get("source_version"), str)
        or receipt.get("layout") != _LAYOUT_CONTRACT
        or not _valid_receipt_identity_payload(receipt.get("source_identity"))
        or not _valid_receipt_identity_payload(receipt.get("candidate_identity"))
        or not _object_identity_matches(source, receipt.get("source_identity"))
        or not _object_identity_matches(target, receipt.get("candidate_identity"))
    ):
        return False
    return True


def _report_from_layout_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Build the public wire report without treating a persisted report as authority."""

    transaction_id = str(receipt["transaction_id"])
    source = str(receipt["source"])
    target = str(receipt["target"])
    output_dir = str(Path(target) / "migration" / "opensquilla" / transaction_id)
    return {
        "source": source,
        "source_kind": str(receipt["source_kind"]),
        "target": target,
        "output_dir": output_dir,
        "apply": True,
        "items": [
            {
                "kind": "layout-receipt",
                "source": source,
                "destination": target,
                "status": "skipped",
                "reason": "this exact profile import was already committed",
                "details": {"transaction_id": transaction_id},
            }
        ],
        "candidates": [],
        "config_transforms": [],
        "secret_relocations": [],
        "paused_jobs": [],
        "preflight": {
            "source_gateway_running": False,
            "target_gateway_running": False,
            "schema_ahead": False,
            "disk_required_bytes": 0,
            "disk_free_bytes": 0,
            "session_count": None,
        },
        "notes": ["A matching committed layout receipt was found; no files were changed."],
    }


def _matching_import_receipt(
    source: Path,
    target: Path,
    *,
    transaction_id: str | None = None,
    source_kind: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Return the sole narrow completion authority for ``source`` -> ``target``."""
    transaction_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
    receipt_root = target / "migration" / "opensquilla"
    if transaction_id is not None:
        if not transaction_pattern.fullmatch(transaction_id):
            return None
        candidates = [receipt_root / transaction_id]
    else:
        try:
            candidates = sorted(receipt_root.iterdir(), reverse=True)
        except OSError:
            return None
    for candidate in candidates:
        try:
            if (
                _supported_entry_type(candidate, candidate.lstat()) != "directory"
                or not transaction_pattern.fullmatch(candidate.name)
            ):
                continue
        except OSError:
            continue
        receipt_path = candidate / _LAYOUT_RECEIPT_FILENAME
        try:
            receipt_stat = receipt_path.lstat()
            if (
                _supported_entry_type(receipt_path, receipt_stat) != "file"
                or receipt_stat.st_size > _LAYOUT_RECEIPT_MAX_BYTES
            ):
                continue
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not _valid_layout_import_receipt(
            receipt,
            source=source,
            target=target,
            transaction_id=candidate.name,
        ):
            continue
        if source_kind is not None and receipt.get("source_kind") != source_kind:
            continue
        return candidate.name, receipt
    return None


def verify_committed_profile_import(
    source: Path,
    target: Path,
    *,
    source_kind: str,
    transaction_id: str | None = None,
    excluded_transaction_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Verify committed import receipts while holding the profile lock pair.

    This is the only receipt-verification surface intended for Desktop.  It
    returns metadata already present in the import protocol and never reads or
    emits credentials, Markdown, chat content, or content hashes.
    """

    normalized_source = Path(_normalized_path(source))
    normalized_target = Path(_normalized_path(target))
    base: dict[str, Any] = {
        "schema_version": 1,
        "outcome": "not_found",
        "stable_code": "profile_import_receipt_not_found",
        "source": str(normalized_source),
        "source_kind": source_kind,
        "target": str(normalized_target),
        "transaction_id": "",
        "matching_transaction_ids": [],
        "provider_connection": None,
        "report": None,
    }
    if source_kind not in OPENSQUILLA_SOURCE_KINDS:
        return {
            **base,
            "outcome": "invalid",
            "stable_code": "profile_import_source_kind_invalid",
        }

    transaction_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
    requested_ids = (*excluded_transaction_ids, *((transaction_id,) if transaction_id else ()))
    if any(not transaction_pattern.fullmatch(value) for value in requested_ids):
        return {
            **base,
            "outcome": "invalid",
            "stable_code": "profile_import_transaction_id_invalid",
        }
    excluded = frozenset(excluded_transaction_ids)
    if transaction_id and transaction_id in excluded:
        return base

    from opensquilla.recovery import acquire_profile_locks

    with acquire_profile_locks(normalized_source, normalized_target):
        try:
            source_before = _path_identity_payload(normalized_source)
            source_type = _supported_entry_type(normalized_source, normalized_source.lstat())
            if source_type != "directory":
                raise OSError("profile import receipt source must be a directory")
        except OSError:
            return {
                **base,
                "outcome": "unsafe",
                "stable_code": "profile_import_receipt_path_unsafe",
            }

        try:
            target_before = _path_identity_payload(normalized_target)
            target_type = _supported_entry_type(normalized_target, normalized_target.lstat())
            if target_type != "directory":
                raise OSError("profile import receipt target must be a directory")
        except FileNotFoundError:
            # A first import intentionally has no target profile yet. Prove the
            # missing leaf against a stable, ordinary parent while holding the
            # same source/target profile lock pair used by later receipt reads.
            # This is a read-only baseline; the importer still performs its own
            # no-replace preflight before publishing anything.
            parent = normalized_target.parent
            try:
                parent_before = _path_identity_payload(parent)
                parent_type = _supported_entry_type(parent, parent.lstat())
                if parent_type != "directory":
                    raise OSError("profile import target parent must be a directory")
            except OSError:
                return {
                    **base,
                    "outcome": "unsafe",
                    "stable_code": "profile_import_receipt_path_unsafe",
                }
            source_unchanged = _identity_payload_matches(normalized_source, source_before)
            parent_unchanged = _object_identity_matches(parent, parent_before)
            try:
                normalized_target.lstat()
            except FileNotFoundError:
                target_still_missing = True
            except OSError:
                return {
                    **base,
                    "outcome": "unsafe",
                    "stable_code": "profile_import_receipt_path_unsafe",
                }
            else:
                target_still_missing = False
            if source_unchanged and parent_unchanged and target_still_missing:
                return base
            return {
                **base,
                "outcome": "unsafe",
                "stable_code": "profile_import_receipt_root_changed",
            }
        except OSError:
            return {
                **base,
                "outcome": "unsafe",
                "stable_code": "profile_import_receipt_path_unsafe",
            }

        receipt_root = normalized_target / "migration" / "opensquilla"
        if transaction_id:
            candidates = [transaction_id]
        else:
            candidates = []
            try:
                with os.scandir(receipt_root) as entries:
                    for inspected, entry in enumerate(entries, start=1):
                        if inspected > _LAYOUT_RECEIPT_MAX_ENTRIES:
                            return {
                                **base,
                                "outcome": "unsafe",
                                "stable_code": "profile_import_receipt_limit_exceeded",
                            }
                        if entry.name in excluded or not transaction_pattern.fullmatch(entry.name):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            candidates.append(entry.name)
            except FileNotFoundError:
                candidates = []
            except OSError:
                return {
                    **base,
                    "outcome": "unsafe",
                    "stable_code": "profile_import_receipt_directory_unreadable",
                }

        matches: list[tuple[str, dict[str, Any]]] = []
        for candidate_id in sorted(candidates, reverse=True):
            if candidate_id in excluded:
                continue
            match = _matching_import_receipt(
                normalized_source,
                normalized_target,
                transaction_id=candidate_id,
                source_kind=source_kind,
            )
            if match is not None:
                matches.append(match)

        provider_connection: dict[str, str] | None = None
        if matches:
            try:
                provider_connection = _verified_provider_connection(normalized_target)
            except ValueError:
                return {
                    **base,
                    "outcome": "unsafe",
                    "stable_code": "profile_import_provider_connection_unsafe",
                }
        if (
            not _identity_payload_matches(normalized_source, source_before)
            or not _identity_payload_matches(normalized_target, target_before)
        ):
            return {
                **base,
                "outcome": "unsafe",
                "stable_code": "profile_import_receipt_root_changed",
            }
        if not matches:
            return base

        selected_id, receipt = matches[0]
        return {
            **base,
            "outcome": "verified",
            "stable_code": "profile_import_receipt_verified",
            "transaction_id": selected_id,
            "matching_transaction_ids": [item[0] for item in matches],
            "provider_connection": provider_connection,
            "report": _report_from_layout_receipt(receipt),
        }


def _verified_provider_connection(target: Path) -> dict[str, str] | None:
    """Read only the non-secret provider connection from the verified target."""

    config_bytes = _read_small_regular_bytes(
        target / "config.toml",
        limit=_CANDIDATE_METADATA_MAX_CONFIG_BYTES,
    )
    if config_bytes is None:
        return None
    try:
        payload = tomllib.loads(config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    llm = payload.get("llm")
    if not isinstance(llm, dict):
        return None
    connection: dict[str, str] = {}
    for field in ("provider", "model", "base_url", "api_key_env"):
        value = llm.get(field, "")
        if not isinstance(value, str):
            raise ValueError("provider connection field has an unsafe type")
        connection[field] = value.strip()
    api_key_env = connection["api_key_env"]
    if api_key_env and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*_(?:KEY|TOKEN)",
        api_key_env,
        re.IGNORECASE,
    ):
        raise ValueError("provider API key environment name is invalid")
    base_url = connection["base_url"]
    if base_url:
        parsed_url = urlsplit(base_url)
        if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ValueError("provider base URL contains private URL components")
    return connection if connection["provider"] else None


def _commit_journal_path(target: Path) -> Path:
    return target.parent / f".{target.name}.{_COMMIT_JOURNAL}"


def _source_snapshot_failure_details(
    exc: BaseException,
    *,
    fallback_code: str,
) -> dict[str, Any]:
    """Return a stable, non-secret Desktop failure classification."""

    raw_code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    try:
        error_code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        error_code = None
    message = str(exc).lower()
    if error_code in _WINDOWS_LOCK_ERROR_CODES:
        stable_code = "source_snapshot_locked"
    elif "changed" in message or "mutation" in message:
        stable_code = "source_snapshot_changed"
    else:
        stable_code = fallback_code
    path = getattr(exc, "filename", None)
    details: dict[str, Any] = {"stable_code": stable_code}
    if isinstance(path, (str, os.PathLike)) and str(path):
        details["path"] = str(path)
    return details


def _normalized_path(path: Path) -> str:
    """Return the comparison/receipt spelling for a profile path."""
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        resolved = path.expanduser().absolute()
    return os.path.normcase(os.path.normpath(str(resolved)))


@dataclass(frozen=True)
class _PathIdentity:
    device: int
    inode: int
    file_type: int

    def as_json(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "file_type": self.file_type,
        }


@dataclass(frozen=True)
class _ManifestEntry:
    source: Path
    relative: Path
    entry_type: str
    identity: _PathIdentity
    mode: int
    size: int
    mtime_ns: int
    digest: str | None
    link_target: str | None = None


@dataclass(frozen=True)
class _SourceSnapshot:
    root: Path
    destination_prefix: Path
    identity: _PathIdentity
    root_mode: int
    root_mtime_ns: int
    entries: tuple[_ManifestEntry, ...]
    role: str
    excluded: frozenset[Path]
    excluded_leaf_suffixes: tuple[str, ...]
    allowed_symlink_roots: frozenset[Path] = frozenset()
    windows_snapshot: WindowsSourceSnapshot | None = None


@dataclass(frozen=True)
class _LegacyGatewayAuthoritySnapshot:
    """No-follow identity of one source state root and its excluded authority files."""

    root: Path
    root_identity: _PathIdentity | None
    root_mode: int | None
    root_mtime_ns: int | None
    pid: _ManifestEntry | None
    pid_value: int | None
    lock: _ManifestEntry | None
    windows_authority: WindowsDirectoryAuthoritySnapshot | None = dataclass_field(
        default=None,
        compare=False,
    )


@dataclass(frozen=True)
class _HistoryPublication:
    path: Path
    before: Any
    after: Any


def _identity_from_stat(result: os.stat_result) -> _PathIdentity:
    return _PathIdentity(
        device=int(result.st_dev),
        inode=int(result.st_ino),
        file_type=stat.S_IFMT(result.st_mode),
    )


def _advisory_identity(result: os.stat_result) -> _PathIdentity | None:
    """Return a usable read-only dedupe identity, if the platform exposed one.

    ``DirEntry.stat()`` can report a zero inode for directories on Windows.
    Treating that sentinel as a real identity collapses unrelated Portable
    profiles into one candidate. Candidate discovery is advisory, so when the
    identity is unavailable it is safer to show both paths and let the user
    choose than to silently hide data.
    """
    identity = _identity_from_stat(result)
    if sys.platform == "win32" and identity.inode == 0:
        return None
    return identity


def _path_identity_payload(path: Path) -> dict[str, int]:
    result = path.lstat()
    identity = _identity_from_stat(result)
    return {
        **identity.as_json(),
        "mode": int(result.st_mode),
        "size": int(result.st_size),
        "modified_at_ns": int(result.st_mtime_ns),
    }


def _identity_payload_matches(path: Path, expected: object) -> bool:
    if not isinstance(expected, dict):
        return False
    try:
        current = _path_identity_payload(path)
    except OSError:
        return False
    return all(current.get(key) == expected.get(key) for key in current)


def _object_identity_matches(path: Path, expected: object) -> bool:
    """Match the stable filesystem object even if directory metadata changed."""
    if not isinstance(expected, dict):
        return False
    try:
        current = _path_identity_payload(path)
    except OSError:
        return False
    return all(
        current.get(key) == expected.get(key)
        for key in ("device", "inode", "file_type")
    )


def _valid_receipt_identity_payload(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _RECEIPT_IDENTITY_FIELDS:
        return False
    return all(type(value[key]) is int and value[key] >= 0 for key in _RECEIPT_IDENTITY_FIELDS)


def _valid_history_record_payload(value: object) -> bool:
    if not isinstance(value, dict) or set(value) not in {
        _HISTORY_RECORD_FIELDS,
        _CONSUMED_HISTORY_RECORD_FIELDS,
    }:
        return False
    try:
        transaction_id = str(value.get("transaction_id", ""))
        committed_at = datetime.fromisoformat(str(value.get("committed_at", "")))
        if str(uuid.UUID(transaction_id)) != transaction_id:
            return False
    except ValueError:
        return False
    if committed_at.tzinfo is None:
        return False
    if any(
        not isinstance(value.get(key), str)
        or not value[key]
        or _normalized_path(Path(value[key])) != value[key]
        for key in ("source", "target", "backup")
    ):
        return False
    if not all(
        _valid_receipt_identity_payload(value.get(key))
        for key in ("source_identity", "target_identity", "backup_identity")
    ):
        return False
    if set(value) == _CONSUMED_HISTORY_RECORD_FIELDS:
        try:
            consumed_id = str(value.get("consumed_by_transaction_id", ""))
            restored_at = datetime.fromisoformat(str(value.get("restored_at", "")))
            if str(uuid.UUID(consumed_id)) != consumed_id:
                return False
        except ValueError:
            return False
        restored_to = value.get("restored_to")
        if (
            restored_at.tzinfo is None
            or not isinstance(restored_to, str)
            or _normalized_path(Path(restored_to)) != restored_to
        ):
            return False
    return True


def _unlink_matching_journal(path: Path, expected: dict[str, Any]) -> None:
    """Remove only the exact journal transaction that was just validated."""
    from opensquilla.recovery.config_patch import ConfigSnapshot

    try:
        snapshot = ConfigSnapshot.capture(path)
        current = json.loads(snapshot.data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("replacement journal changed before cleanup") from exc
    if snapshot.identity is None or current != expected:
        raise OSError("replacement journal transaction changed before cleanup")
    snapshot.assert_current()
    path.unlink()
    _fsync_directory(path.parent)


def _remove_matching_staging(path: Path, expected: object) -> None:
    """Delete only the UUID staging tree whose identity the journal recorded."""
    if not _object_identity_matches(path, expected):
        raise OSError("staging identity changed before cleanup")
    from opensquilla.recovery.atomic import no_follow_manifest

    # A current import never stages code-task.  Accept its link leaves only for
    # cleanup compatibility with a candidate created by the earlier whole-home
    # copier, while still inspecting every descendant and rejecting specials.
    no_follow_manifest(
        path,
        link_leaf_directories=frozenset(
            {"workspace", "state/workspace", "code-task"}
        ),
    )
    if not _object_identity_matches(path, expected):
        raise OSError("staging identity changed during cleanup validation")
    shutil.rmtree(_ext(path))


def _has_reparse_attribute(result: os.stat_result) -> bool:
    attributes = int(getattr(result, "st_file_attributes", 0) or 0)
    return bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _supported_entry_type(path: Path, result: os.stat_result) -> str:
    if stat.S_ISLNK(result.st_mode) or _has_reparse_attribute(result):
        raise OSError(f"symbolic link, junction, or reparse point is not importable: {path}")
    if stat.S_ISDIR(result.st_mode):
        return "directory"
    if stat.S_ISREG(result.st_mode):
        return "file"
    raise OSError(f"special file is not importable: {path}")


def _allowed_symlink_root(
    relative: Path,
    allowed_roots: set[Path] | frozenset[Path],
) -> Path | None:
    """Return the narrowest workspace policy root containing ``relative``.

    The root itself must remain a real directory.  Only descendants may be
    represented by links, so an exchanged ``workspace`` entry is still rejected
    before any link payload is inspected.
    """

    candidates = [
        root
        for root in allowed_roots
        if relative != root and relative.is_relative_to(root)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item.parts))


def _validate_relative_symlink_target(
    *,
    source_path: Path,
    relative: Path,
    target: str,
    policy_root: Path,
) -> None:
    """Validate a POSIX link lexically without resolving or reading its target.

    Dangling links are valid workspace data.  What matters for import safety is
    that the frozen relative payload can never address a location outside the
    same workspace snapshot once recreated under the target profile.
    """

    raw = PurePosixPath(target)
    if not target or raw.is_absolute():
        raise OSError(f"source link target must be relative: {source_path}")

    root_parts = tuple(policy_root.parts)
    stack = list(relative.parent.parts)
    if tuple(stack[: len(root_parts)]) != root_parts:
        raise OSError(f"source link path escapes its allowed root: {source_path}")
    for part in raw.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if len(stack) <= len(root_parts):
                raise OSError(
                    f"source link target escapes its allowed root: {source_path}"
                )
            stack.pop()
            continue
        stack.append(part)

    if tuple(stack[: len(root_parts)]) != root_parts:
        raise OSError(f"source link target escapes its allowed root: {source_path}")


def _is_plain_directory(path: Path) -> bool:
    """Return False for missing paths and reject links in every path component."""

    candidate = path.expanduser().absolute()
    current = Path(candidate.anchor)
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for index, part in enumerate(parts):
        current /= part
        try:
            result = current.lstat()
        except FileNotFoundError:
            return False
        entry_type = _supported_entry_type(current, result)
        if entry_type != "directory":
            if index == len(parts) - 1:
                return False
            raise OSError(f"path parent is not a directory: {current}")
    return True


def _stat_matches_manifest(result: os.stat_result, entry: _ManifestEntry) -> bool:
    return (
        _identity_from_stat(result) == entry.identity
        and int(result.st_mode) == entry.mode
        and int(result.st_size) == entry.size
        and int(result.st_mtime_ns) == entry.mtime_ns
    )


def _stat_matches_snapshot_entry(
    result: os.stat_result,
    entry: _ManifestEntry,
    *,
    ignore_directory_metadata: bool,
) -> bool:
    return (
        _identity_from_stat(result) == entry.identity
        and int(result.st_mode) == entry.mode
        and (
            ignore_directory_metadata and entry.entry_type == "directory"
            or (
                int(result.st_size) == entry.size
                and int(result.st_mtime_ns) == entry.mtime_ns
            )
        )
    )


def _digest_regular_file(
    path: Path,
    *,
    expected: _ManifestEntry | None = None,
    destination: Path | None = None,
) -> str:
    """Hash (and optionally copy) one file without following a path link.

    The descriptor identity is checked before the first byte is read and again
    after EOF. A swap to a link or another inode therefore fails closed even on
    platforms without ``O_NOFOLLOW``.
    """
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(_ext(path), flags)
    destination_descriptor: int | None = None
    digest = hashlib.sha256()
    try:
        opened_stat = os.fstat(descriptor)
        _supported_entry_type(path, opened_stat)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise OSError(f"source path stopped being a regular file: {path}")
        if expected is not None and not _stat_matches_manifest(opened_stat, expected):
            raise OSError(f"source file changed before it could be copied: {path}")
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            destination_flags |= int(getattr(os, "O_BINARY", 0))
            destination_descriptor = os.open(_ext(destination), destination_flags, 0o600)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if destination_descriptor is not None:
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    view = view[written:]
        closed_stat = os.fstat(descriptor)
        if expected is not None and not _stat_matches_manifest(closed_stat, expected):
            raise OSError(f"source file changed while it was being copied: {path}")
        if destination_descriptor is not None:
            os.fsync(destination_descriptor)
    except Exception:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
            destination_descriptor = None
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)
    if expected is not None:
        try:
            after = path.lstat()
        except OSError as exc:
            if destination is not None:
                destination.unlink(missing_ok=True)
            raise OSError(f"source file disappeared after it was copied: {path}") from exc
        if not _stat_matches_manifest(after, expected):
            if destination is not None:
                destination.unlink(missing_ok=True)
            raise OSError(f"source file changed while it was being copied: {path}")
    return digest.hexdigest()


def _capture_legacy_gateway_authority_file(
    path: Path,
) -> tuple[_ManifestEntry | None, bytes | None]:
    try:
        result = path.lstat()
    except FileNotFoundError:
        return None, None
    if _supported_entry_type(path, result) != "file":
        raise OSError(f"legacy gateway authority is not a regular file: {path}")
    if result.st_size > _GATEWAY_AUTHORITY_MAX_BYTES:
        raise OSError(f"legacy gateway authority is unexpectedly large: {path}")
    entry = _ManifestEntry(
        source=path,
        relative=Path(path.name),
        entry_type="file",
        identity=_identity_from_stat(result),
        mode=int(result.st_mode),
        size=int(result.st_size),
        mtime_ns=int(result.st_mtime_ns),
        digest=None,
    )
    digest = _digest_regular_file(path, expected=entry)
    raw = path.read_bytes()
    if len(raw) != entry.size or _digest_regular_file(path, expected=entry) != digest:
        raise OSError(f"legacy gateway authority changed during inspection: {path}")
    return _ManifestEntry(
        source=entry.source,
        relative=entry.relative,
        entry_type=entry.entry_type,
        identity=entry.identity,
        mode=entry.mode,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        digest=digest,
    ), raw


def _capture_legacy_gateway_authority_file_posix(
    root_descriptor: int,
    root: Path,
    name: str,
) -> tuple[_ManifestEntry | None, bytes | None]:
    path = root / name
    try:
        result = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    if _supported_entry_type(path, result) != "file":
        raise OSError(f"legacy gateway authority is not a regular file: {path}")
    if result.st_size > _GATEWAY_AUTHORITY_MAX_BYTES:
        raise OSError(f"legacy gateway authority is unexpectedly large: {path}")
    entry = _ManifestEntry(
        source=path,
        relative=Path(name),
        entry_type="file",
        identity=_identity_from_stat(result),
        mode=int(result.st_mode),
        size=int(result.st_size),
        mtime_ns=int(result.st_mtime_ns),
        digest=None,
    )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, dir_fd=root_descriptor)
    try:
        if not _stat_matches_manifest(os.fstat(descriptor), entry):
            raise OSError(f"legacy gateway authority changed while opened: {path}")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        remaining = _GATEWAY_AUTHORITY_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != entry.size or not _stat_matches_manifest(os.fstat(descriptor), entry):
            raise OSError(f"legacy gateway authority changed while read: {path}")
    finally:
        os.close(descriptor)
    if not _stat_matches_manifest(
        os.stat(name, dir_fd=root_descriptor, follow_symlinks=False),
        entry,
    ):
        raise OSError(f"legacy gateway authority changed during inspection: {path}")
    return _ManifestEntry(
        source=entry.source,
        relative=entry.relative,
        entry_type=entry.entry_type,
        identity=entry.identity,
        mode=entry.mode,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        digest=digest.hexdigest(),
    ), raw


def _capture_legacy_gateway_authority(
    root: Path,
    *,
    held_lock: LegacyGatewayLockFileSnapshot | None = None,
) -> _LegacyGatewayAuthoritySnapshot:
    if os.name == "nt":  # pragma: no cover - exercised by Windows platform CI
        windows_authority = capture_windows_bounded_files(
            root,
            names=("gateway.pid",) if held_lock is not None else (
                "gateway.pid",
                "gateway.pid.lock",
            ),
            max_bytes=_GATEWAY_AUTHORITY_MAX_BYTES,
        )
        if windows_authority is None:
            return _LegacyGatewayAuthoritySnapshot(
                root=root,
                root_identity=None,
                root_mode=None,
                root_mtime_ns=None,
                pid=None,
                pid_value=None,
                lock=None,
            )

        def convert(name: str) -> tuple[_ManifestEntry | None, bytes | None]:
            captured = windows_authority.file(name)
            if captured is None:
                return None, None
            entry = captured.entry
            return (
                _ManifestEntry(
                    source=entry.source,
                    relative=entry.relative,
                    entry_type=entry.entry_type,
                    identity=_PathIdentity(
                        device=entry.identity.device,
                        inode=entry.identity.inode,
                        file_type=entry.identity.file_type,
                    ),
                    mode=entry.mode,
                    size=entry.size,
                    mtime_ns=entry.mtime_ns,
                    digest=entry.digest,
                ),
                captured.data,
            )

        pid, pid_raw = convert("gateway.pid")
        if held_lock is None:
            lock, _lock_raw = convert("gateway.pid.lock")
        else:
            expected_lock_path = root / "gateway.pid.lock"
            if _normalized_path(held_lock.path) != _normalized_path(expected_lock_path):
                raise OSError("held legacy gateway lock path changed")
            lock = _ManifestEntry(
                source=expected_lock_path,
                relative=Path("gateway.pid.lock"),
                entry_type="file",
                identity=_PathIdentity(
                    device=held_lock.device,
                    inode=held_lock.inode,
                    file_type=stat.S_IFREG,
                ),
                mode=held_lock.mode,
                size=held_lock.size,
                mtime_ns=held_lock.mtime_ns,
                digest=held_lock.digest,
            )
        return _LegacyGatewayAuthoritySnapshot(
            root=windows_authority.root,
            root_identity=_PathIdentity(
                device=windows_authority.identity.device,
                inode=windows_authority.identity.inode,
                file_type=windows_authority.identity.file_type,
            ),
            root_mode=windows_authority.root_mode,
            root_mtime_ns=windows_authority.root_mtime_ns,
            pid=pid,
            pid_value=_parse_pid_bytes(pid_raw) if pid_raw is not None else None,
            lock=lock,
            windows_authority=windows_authority,
        )
    if _supports_posix_handle_walk():
        try:
            descriptor = _open_posix_directory_chain(root)
        except FileNotFoundError:
            return _LegacyGatewayAuthoritySnapshot(
                root=root,
                root_identity=None,
                root_mode=None,
                root_mtime_ns=None,
                pid=None,
                pid_value=None,
                lock=None,
            )
        try:
            result = os.fstat(descriptor)
            if not stat.S_ISDIR(result.st_mode):
                raise OSError(f"legacy gateway state root is not a directory: {root}")
            before_identity = _identity_from_stat(result)
            before_mtime = int(result.st_mtime_ns)
            pid, pid_raw = _capture_legacy_gateway_authority_file_posix(
                descriptor,
                root,
                "gateway.pid",
            )
            lock, _lock_raw = _capture_legacy_gateway_authority_file_posix(
                descriptor,
                root,
                "gateway.pid.lock",
            )
            after = os.fstat(descriptor)
            if (
                _identity_from_stat(after) != before_identity
                or int(after.st_mode) != int(result.st_mode)
                or int(after.st_mtime_ns) != before_mtime
            ):
                raise OSError(f"legacy gateway state root changed during inspection: {root}")
            return _LegacyGatewayAuthoritySnapshot(
                root=root,
                root_identity=before_identity,
                root_mode=int(result.st_mode),
                root_mtime_ns=before_mtime,
                pid=pid,
                pid_value=_parse_pid_bytes(pid_raw) if pid_raw is not None else None,
                lock=lock,
            )
        finally:
            os.close(descriptor)
    try:
        result = root.lstat()
    except FileNotFoundError:
        return _LegacyGatewayAuthoritySnapshot(
            root=root,
            root_identity=None,
            root_mode=None,
            root_mtime_ns=None,
            pid=None,
            pid_value=None,
            lock=None,
        )
    if _supported_entry_type(root, result) != "directory":
        raise OSError(f"legacy gateway state root is not a directory: {root}")
    before_identity = _identity_from_stat(result)
    before_mtime = int(result.st_mtime_ns)
    pid, pid_raw = _capture_legacy_gateway_authority_file(root / "gateway.pid")
    lock, _lock_raw = _capture_legacy_gateway_authority_file(root / "gateway.pid.lock")
    after = root.lstat()
    if (
        _identity_from_stat(after) != before_identity
        or int(after.st_mode) != int(result.st_mode)
        or int(after.st_mtime_ns) != before_mtime
    ):
        raise OSError(f"legacy gateway state root changed during inspection: {root}")
    return _LegacyGatewayAuthoritySnapshot(
        root=root,
        root_identity=before_identity,
        root_mode=int(result.st_mode),
        root_mtime_ns=before_mtime,
        pid=pid,
        pid_value=_parse_pid_bytes(pid_raw) if pid_raw is not None else None,
        lock=lock,
    )


def _open_posix_directory_chain(path: Path) -> int:
    """Open an absolute directory one no-follow component at a time."""

    candidate = Path(os.path.abspath(os.fspath(path.expanduser())))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    anchor = Path(candidate.anchor or os.sep)
    descriptor = os.open(anchor, flags)
    try:
        parts = candidate.parts[1:] if candidate.anchor else candidate.parts
        for part in parts:
            child = os.open(part, flags, dir_fd=descriptor)
            try:
                value = os.fstat(child)
                if not stat.S_ISDIR(value.st_mode):
                    raise OSError(f"source path component is not a directory: {part}")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _digest_open_file(
    descriptor: int,
    *,
    expected: _ManifestEntry,
    destination: Path | None = None,
) -> str:
    """Digest/copy an already no-follow-opened regular file."""

    opened = os.fstat(descriptor)
    if not _stat_matches_manifest(opened, expected):
        raise OSError(f"source file changed while it was opened: {expected.source}")
    destination_descriptor: int | None = None
    digest = hashlib.sha256()
    try:
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= int(getattr(os, "O_BINARY", 0))
            destination_descriptor = os.open(_ext(destination), flags, 0o600)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if destination_descriptor is not None:
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    view = view[written:]
        if not _stat_matches_manifest(os.fstat(descriptor), expected):
            raise OSError(f"source file changed while it was read: {expected.source}")
        if destination_descriptor is not None:
            os.fsync(destination_descriptor)
    except BaseException:
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
    return digest.hexdigest()


def _scan_source_tree_posix(
    root: Path,
    *,
    destination_prefix: Path,
    role: str,
    excluded: set[Path] | frozenset[Path],
    excluded_leaf_suffixes: tuple[str, ...] = (),
    allowed_symlink_roots: set[Path] | frozenset[Path] = frozenset(),
) -> _SourceSnapshot:
    root_descriptor = _open_posix_directory_chain(root)
    try:
        root_stat = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise OSError(f"import root is not a directory: {root}")
        root_identity = _identity_from_stat(root_stat)
        entries: list[_ManifestEntry] = []

        def visit(directory_descriptor: int, relative_directory: Path) -> None:
            before = os.fstat(directory_descriptor)
            try:
                with os.scandir(directory_descriptor) as iterator:
                    names = sorted(entry.name for entry in iterator)
            except OSError as exc:
                raise OSError(
                    f"could not enumerate source directory: {root / relative_directory}"
                ) from exc
            for name in names:
                relative = relative_directory / name
                if relative in excluded or any(
                    parent in excluded for parent in relative.parents
                ):
                    continue
                if name.endswith(excluded_leaf_suffixes):
                    continue
                source_path = root / relative
                value = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
                policy_root = _allowed_symlink_root(relative, allowed_symlink_roots)
                link_target: str | None = None
                if stat.S_ISLNK(value.st_mode) and policy_root is not None:
                    entry_type = "symlink"
                    link_target = os.readlink(name, dir_fd=directory_descriptor)
                    _validate_relative_symlink_target(
                        source_path=source_path,
                        relative=relative,
                        target=link_target,
                        policy_root=policy_root,
                    )
                else:
                    entry_type = _supported_entry_type(source_path, value)
                entry = _ManifestEntry(
                    source=source_path,
                    relative=relative,
                    entry_type=entry_type,
                    identity=_identity_from_stat(value),
                    mode=int(value.st_mode),
                    size=int(value.st_size),
                    mtime_ns=int(value.st_mtime_ns),
                    digest=None,
                    link_target=link_target,
                )
                digest: str | None = None
                if entry_type == "file":
                    flags = (
                        os.O_RDONLY
                        | getattr(os, "O_BINARY", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                    )
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    file_descriptor = os.open(
                        name,
                        flags,
                        dir_fd=directory_descriptor,
                    )
                    try:
                        digest = _digest_open_file(file_descriptor, expected=entry)
                    finally:
                        os.close(file_descriptor)
                    if not _stat_matches_manifest(
                        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False),
                        entry,
                    ):
                        raise OSError(f"source file changed during enumeration: {source_path}")
                entries.append(
                    _ManifestEntry(
                        source=entry.source,
                        relative=entry.relative,
                        entry_type=entry.entry_type,
                        identity=entry.identity,
                        mode=entry.mode,
                        size=entry.size,
                        mtime_ns=entry.mtime_ns,
                        digest=digest,
                        link_target=entry.link_target,
                    )
                )
                if entry_type == "symlink":
                    if (
                        not _stat_matches_manifest(
                            os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False),
                            entry,
                        )
                        or os.readlink(name, dir_fd=directory_descriptor) != link_target
                    ):
                        raise OSError(
                            f"source link changed during enumeration: {source_path}"
                        )
                if entry_type == "directory":
                    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                    child_descriptor = os.open(
                        name,
                        flags,
                        dir_fd=directory_descriptor,
                    )
                    try:
                        if _identity_from_stat(os.fstat(child_descriptor)) != entry.identity:
                            raise OSError(
                                f"source directory changed while opened: {source_path}"
                            )
                        visit(child_descriptor, relative)
                    finally:
                        os.close(child_descriptor)
            after = os.fstat(directory_descriptor)
            if (
                _identity_from_stat(after) != _identity_from_stat(before)
                or int(after.st_mode) != int(before.st_mode)
                or (
                    not excluded_leaf_suffixes
                    and int(after.st_mtime_ns) != int(before.st_mtime_ns)
                )
            ):
                raise OSError(
                    f"source directory changed during enumeration: {root / relative_directory}"
                )

        visit(root_descriptor, Path())
        after_root = os.fstat(root_descriptor)
        if (
            _identity_from_stat(after_root) != root_identity
            or int(after_root.st_mode) != int(root_stat.st_mode)
            or (
                not excluded_leaf_suffixes
                and int(after_root.st_mtime_ns) != int(root_stat.st_mtime_ns)
            )
        ):
            raise OSError(f"source root changed during enumeration: {root}")
        entries.sort(key=lambda entry: (len(entry.relative.parts), entry.relative.as_posix()))
        return _SourceSnapshot(
            root=root,
            destination_prefix=destination_prefix,
            identity=root_identity,
            root_mode=int(root_stat.st_mode),
            root_mtime_ns=int(root_stat.st_mtime_ns),
            entries=tuple(entries),
            role=role,
            excluded=frozenset(excluded),
            excluded_leaf_suffixes=tuple(excluded_leaf_suffixes),
            allowed_symlink_roots=frozenset(allowed_symlink_roots),
        )
    finally:
        os.close(root_descriptor)


def _scan_source_tree_path(
    root: Path,
    *,
    destination_prefix: Path,
    role: str,
    excluded: set[Path] | frozenset[Path] = frozenset(),
    excluded_leaf_suffixes: tuple[str, ...] = (),
) -> _SourceSnapshot:
    """Create a stable, no-follow manifest with temporary content digests."""
    if destination_prefix.is_absolute() or ".." in destination_prefix.parts:
        raise OSError(f"import destination prefix is unsafe: {destination_prefix}")
    root_stat = root.lstat()
    if _supported_entry_type(root, root_stat) != "directory":
        raise OSError(f"import root is not a directory: {root}")
    root_identity = _identity_from_stat(root_stat)
    entries: list[_ManifestEntry] = []
    pending: list[tuple[Path, Path]] = [(root, Path())]
    while pending:
        directory, relative_directory = pending.pop()
        before_directory = directory.lstat()
        if _supported_entry_type(directory, before_directory) != "directory":
            raise OSError(f"source directory changed type during enumeration: {directory}")
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda child: child.name)
        except OSError as exc:
            raise OSError(f"could not enumerate source directory: {directory}") from exc
        for child in children:
            relative = relative_directory / child.name
            if relative in excluded or any(parent in excluded for parent in relative.parents):
                continue
            if child.name.endswith(excluded_leaf_suffixes):
                continue
            path = Path(child.path)
            result = child.stat(follow_symlinks=False)
            entry_type = _supported_entry_type(path, result)
            entry = _ManifestEntry(
                source=path,
                relative=relative,
                entry_type=entry_type,
                identity=_identity_from_stat(result),
                mode=int(result.st_mode),
                size=int(result.st_size),
                mtime_ns=int(result.st_mtime_ns),
                digest=None,
            )
            digest = _digest_regular_file(path, expected=entry) if entry_type == "file" else None
            entries.append(
                _ManifestEntry(
                    source=entry.source,
                    relative=entry.relative,
                    entry_type=entry.entry_type,
                    identity=entry.identity,
                    mode=entry.mode,
                    size=entry.size,
                    mtime_ns=entry.mtime_ns,
                    digest=digest,
                )
            )
            if entry_type == "directory":
                pending.append((path, relative))
        after_directory = directory.lstat()
        if (
            _identity_from_stat(after_directory) != _identity_from_stat(before_directory)
            or (
                not excluded_leaf_suffixes
                and int(after_directory.st_mtime_ns) != int(before_directory.st_mtime_ns)
            )
        ):
            raise OSError(f"source directory changed during enumeration: {directory}")
    after_root = root.lstat()
    if _identity_from_stat(after_root) != root_identity:
        raise OSError(f"source root changed during enumeration: {root}")
    entries.sort(key=lambda entry: (len(entry.relative.parts), entry.relative.as_posix()))
    return _SourceSnapshot(
        root=root,
        destination_prefix=destination_prefix,
        identity=root_identity,
        root_mode=int(root_stat.st_mode),
        root_mtime_ns=int(root_stat.st_mtime_ns),
        entries=tuple(entries),
        role=role,
        excluded=frozenset(excluded),
        excluded_leaf_suffixes=tuple(excluded_leaf_suffixes),
    )


def _scan_source_tree(
    root: Path,
    *,
    destination_prefix: Path,
    role: str,
    excluded: set[Path] | frozenset[Path] = frozenset(),
    excluded_leaf_suffixes: tuple[str, ...] = (),
    allowed_symlink_roots: set[Path] | frozenset[Path] = frozenset(),
) -> _SourceSnapshot:
    """Create a stable manifest without path-following parent races."""

    if destination_prefix.is_absolute() or ".." in destination_prefix.parts:
        raise OSError(f"import destination prefix is unsafe: {destination_prefix}")
    if os.name == "nt":  # pragma: no cover - exercised by Windows platform CI
        windows_snapshot = scan_windows_source_tree(
            root,
            destination_prefix=destination_prefix,
            role=role,
            excluded=excluded,
            excluded_leaf_suffixes=excluded_leaf_suffixes,
        )
        return _SourceSnapshot(
            root=windows_snapshot.root,
            destination_prefix=windows_snapshot.destination_prefix,
            identity=_PathIdentity(
                device=windows_snapshot.identity.device,
                inode=windows_snapshot.identity.inode,
                file_type=windows_snapshot.identity.file_type,
            ),
            root_mode=windows_snapshot.root_mode,
            root_mtime_ns=windows_snapshot.root_mtime_ns,
            entries=tuple(
                _ManifestEntry(
                    source=entry.source,
                    relative=entry.relative,
                    entry_type=entry.entry_type,
                    identity=_PathIdentity(
                        device=entry.identity.device,
                        inode=entry.identity.inode,
                        file_type=entry.identity.file_type,
                    ),
                    mode=entry.mode,
                    size=entry.size,
                    mtime_ns=entry.mtime_ns,
                    digest=entry.digest,
                )
                for entry in windows_snapshot.entries
            ),
            role=windows_snapshot.role,
            excluded=windows_snapshot.excluded,
            excluded_leaf_suffixes=windows_snapshot.excluded_leaf_suffixes,
            allowed_symlink_roots=frozenset(allowed_symlink_roots),
            windows_snapshot=windows_snapshot,
        )
    if _supports_posix_handle_walk():
        return _scan_source_tree_posix(
            root,
            destination_prefix=destination_prefix,
            role=role,
            excluded=excluded,
            excluded_leaf_suffixes=excluded_leaf_suffixes,
            allowed_symlink_roots=allowed_symlink_roots,
        )
    # There is no safe path-based fallback for source profile data. Platforms
    # without openat-style traversal or the native Windows handle primitive
    # must leave the target untouched.
    raise OSError(
        "safe handle-relative source traversal is unavailable on this platform"
    )


def _source_snapshots_match(
    current: _SourceSnapshot,
    expected: _SourceSnapshot,
) -> bool:
    if not expected.excluded_leaf_suffixes:
        return current == expected
    if (
        current.root != expected.root
        or current.destination_prefix != expected.destination_prefix
        or current.identity != expected.identity
        or current.root_mode != expected.root_mode
        or current.role != expected.role
        or current.excluded != expected.excluded
        or current.excluded_leaf_suffixes != expected.excluded_leaf_suffixes
        or current.allowed_symlink_roots != expected.allowed_symlink_roots
        or len(current.entries) != len(expected.entries)
    ):
        return False
    for current_entry, expected_entry in zip(current.entries, expected.entries, strict=True):
        if expected_entry.entry_type in {"file", "symlink"}:
            if current_entry != expected_entry:
                return False
            continue
        if (
            current_entry.source != expected_entry.source
            or current_entry.relative != expected_entry.relative
            or current_entry.entry_type != expected_entry.entry_type
            or current_entry.identity != expected_entry.identity
            or current_entry.mode != expected_entry.mode
            or current_entry.digest != expected_entry.digest
        ):
            return False
    return True


def _copy_snapshot_file_posix(
    snapshot: _SourceSnapshot,
    entry: _ManifestEntry,
    destination: Path,
) -> str:
    root_descriptor = _open_posix_directory_chain(snapshot.root)
    directory_descriptors = [root_descriptor]
    directory_entries = {
        item.relative: item for item in snapshot.entries if item.entry_type == "directory"
    }
    try:
        root_stat = os.fstat(root_descriptor)
        if (
            _identity_from_stat(root_stat) != snapshot.identity
            or int(root_stat.st_mode) != snapshot.root_mode
            or (
                not snapshot.excluded_leaf_suffixes
                and int(root_stat.st_mtime_ns) != snapshot.root_mtime_ns
            )
        ):
            raise OSError(f"source root changed before copy: {snapshot.root}")
        relative_parent = Path()
        for part in entry.relative.parent.parts:
            relative_parent /= part
            expected_directory = directory_entries.get(relative_parent)
            if expected_directory is None:
                raise OSError(f"source manifest parent is missing: {relative_parent}")
            parent_descriptor = directory_descriptors[-1]
            before = os.stat(part, dir_fd=parent_descriptor, follow_symlinks=False)
            if not _stat_matches_snapshot_entry(
                before,
                expected_directory,
                ignore_directory_metadata=bool(snapshot.excluded_leaf_suffixes),
            ):
                raise OSError(
                    f"source directory changed before copy: {expected_directory.source}"
                )
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            child_descriptor = os.open(part, flags, dir_fd=parent_descriptor)
            if not _stat_matches_snapshot_entry(
                os.fstat(child_descriptor),
                expected_directory,
                ignore_directory_metadata=bool(snapshot.excluded_leaf_suffixes),
            ):
                os.close(child_descriptor)
                raise OSError(
                    f"source directory changed while opened: {expected_directory.source}"
                )
            directory_descriptors.append(child_descriptor)
        parent_descriptor = directory_descriptors[-1]
        before_file = os.stat(
            entry.relative.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _stat_matches_manifest(before_file, entry):
            raise OSError(f"source file changed before copy: {entry.source}")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(
            entry.relative.name,
            flags,
            dir_fd=parent_descriptor,
        )
        try:
            digest = _digest_open_file(
                file_descriptor,
                expected=entry,
                destination=destination,
            )
        finally:
            os.close(file_descriptor)
        if not _stat_matches_manifest(
            os.stat(
                entry.relative.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            ),
            entry,
        ):
            destination.unlink(missing_ok=True)
            raise OSError(f"source file changed during copy: {entry.source}")
        for index, descriptor in enumerate(directory_descriptors):
            current = os.fstat(descriptor)
            if index == 0:
                if (
                    _identity_from_stat(current) != snapshot.identity
                    or int(current.st_mode) != snapshot.root_mode
                    or (
                        not snapshot.excluded_leaf_suffixes
                        and int(current.st_mtime_ns) != snapshot.root_mtime_ns
                    )
                ):
                    destination.unlink(missing_ok=True)
                    raise OSError(f"source root changed during copy: {snapshot.root}")
                continue
            relative = Path(*entry.relative.parent.parts[:index])
            expected_directory = directory_entries[relative]
            if not _stat_matches_snapshot_entry(
                current,
                expected_directory,
                ignore_directory_metadata=bool(snapshot.excluded_leaf_suffixes),
            ):
                destination.unlink(missing_ok=True)
                raise OSError(
                    f"source directory changed during copy: {expected_directory.source}"
                )
        return digest
    finally:
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)


def _copy_snapshot_symlink_posix(
    snapshot: _SourceSnapshot,
    entry: _ManifestEntry,
    destination: Path,
) -> str:
    """Recreate one frozen workspace link without opening its target."""

    if entry.entry_type != "symlink" or entry.link_target is None:
        raise OSError(f"source manifest link payload is missing: {entry.source}")
    root_descriptor = _open_posix_directory_chain(snapshot.root)
    directory_descriptors = [root_descriptor]
    directory_entries = {
        item.relative: item for item in snapshot.entries if item.entry_type == "directory"
    }
    try:
        root_stat = os.fstat(root_descriptor)
        if (
            _identity_from_stat(root_stat) != snapshot.identity
            or int(root_stat.st_mode) != snapshot.root_mode
            or (
                not snapshot.excluded_leaf_suffixes
                and int(root_stat.st_mtime_ns) != snapshot.root_mtime_ns
            )
        ):
            raise OSError(f"source root changed before link copy: {snapshot.root}")
        relative_parent = Path()
        for part in entry.relative.parent.parts:
            relative_parent /= part
            expected_directory = directory_entries.get(relative_parent)
            if expected_directory is None:
                raise OSError(f"source manifest parent is missing: {relative_parent}")
            parent_descriptor = directory_descriptors[-1]
            before = os.stat(part, dir_fd=parent_descriptor, follow_symlinks=False)
            if not _stat_matches_snapshot_entry(
                before,
                expected_directory,
                ignore_directory_metadata=bool(snapshot.excluded_leaf_suffixes),
            ):
                raise OSError(
                    f"source directory changed before link copy: {expected_directory.source}"
                )
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            child_descriptor = os.open(part, flags, dir_fd=parent_descriptor)
            if not _stat_matches_snapshot_entry(
                os.fstat(child_descriptor),
                expected_directory,
                ignore_directory_metadata=bool(snapshot.excluded_leaf_suffixes),
            ):
                os.close(child_descriptor)
                raise OSError(
                    f"source directory changed while opened: {expected_directory.source}"
                )
            directory_descriptors.append(child_descriptor)

        parent_descriptor = directory_descriptors[-1]
        before_link = os.stat(
            entry.relative.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not _stat_matches_manifest(before_link, entry)
            or os.readlink(entry.relative.name, dir_fd=parent_descriptor)
            != entry.link_target
        ):
            raise OSError(f"source link changed before copy: {entry.source}")

        destination.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(entry.link_target, destination)

        if (
            not _stat_matches_manifest(
                os.stat(
                    entry.relative.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                ),
                entry,
            )
            or os.readlink(entry.relative.name, dir_fd=parent_descriptor)
            != entry.link_target
        ):
            destination.unlink(missing_ok=True)
            raise OSError(f"source link changed during copy: {entry.source}")
        for index, descriptor in enumerate(directory_descriptors):
            current = os.fstat(descriptor)
            if index == 0:
                if (
                    _identity_from_stat(current) != snapshot.identity
                    or int(current.st_mode) != snapshot.root_mode
                    or (
                        not snapshot.excluded_leaf_suffixes
                        and int(current.st_mtime_ns) != snapshot.root_mtime_ns
                    )
                ):
                    destination.unlink(missing_ok=True)
                    raise OSError(f"source root changed during link copy: {snapshot.root}")
                continue
            relative = Path(*entry.relative.parent.parts[:index])
            expected_directory = directory_entries[relative]
            if not _stat_matches_snapshot_entry(
                current,
                expected_directory,
                ignore_directory_metadata=bool(snapshot.excluded_leaf_suffixes),
            ):
                destination.unlink(missing_ok=True)
                raise OSError(
                    f"source directory changed during link copy: "
                    f"{expected_directory.source}"
                )
        return entry.link_target
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)


def _copy_snapshot_file(
    snapshot: _SourceSnapshot,
    entry: _ManifestEntry,
    destination: Path,
) -> str:
    if os.name == "nt":  # pragma: no cover - exercised by Windows platform CI
        windows_snapshot = snapshot.windows_snapshot
        if windows_snapshot is None:
            raise OSError("Windows source snapshot authority is missing")
        windows_entry = next(
            (
                candidate
                for candidate in windows_snapshot.entries
                if candidate.relative == entry.relative
            ),
            None,
        )
        if windows_entry is None or (
            windows_entry.source != entry.source
            or windows_entry.entry_type != entry.entry_type
            or windows_entry.identity.device != entry.identity.device
            or windows_entry.identity.inode != entry.identity.inode
            or windows_entry.identity.file_type != entry.identity.file_type
            or windows_entry.mode != entry.mode
            or windows_entry.size != entry.size
            or windows_entry.mtime_ns != entry.mtime_ns
            or windows_entry.digest != entry.digest
        ):
            raise OSError(f"Windows source manifest entry changed: {entry.source}")
        return copy_windows_snapshot_file(windows_snapshot, windows_entry, destination)
    if _supports_posix_handle_walk():
        return _copy_snapshot_file_posix(snapshot, entry, destination)
    raise OSError(
        "safe handle-relative source copying is unavailable on this platform"
    )


def _supports_posix_handle_walk() -> bool:
    return (
        os.name != "nt"
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.scandir in os.supports_fd
        and os.readlink in os.supports_dir_fd
    )


def _snapshot_file_entry(
    snapshot: _SourceSnapshot,
    relative: Path,
) -> _ManifestEntry | None:
    return next(
        (
            entry
            for entry in snapshot.entries
            if entry.relative == relative and entry.entry_type == "file"
        ),
        None,
    )


def _read_snapshot_file_bytes(
    snapshot: _SourceSnapshot,
    relative: Path,
    *,
    limit: int,
) -> bytes | None:
    entry = _snapshot_file_entry(snapshot, relative)
    if entry is None:
        return None
    if entry.size > limit:
        raise OSError(f"source file exceeds bounded read limit: {entry.source}")
    with tempfile.TemporaryDirectory(prefix="opensquilla-profile-read-") as temporary:
        destination = Path(temporary) / "value"
        digest = _copy_snapshot_file(snapshot, entry, destination)
        data = destination.read_bytes()
    if digest != entry.digest or len(data) != entry.size:
        raise OSError(f"source file changed during bounded read: {entry.source}")
    return data


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(_ext(temporary), _ext(path))
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _cas_publish_bytes(snapshot: Any, data: bytes, *, mode: int) -> Any:
    """Publish bytes only while a no-follow, temporary-digest snapshot matches."""
    from opensquilla.recovery import AtomicStateUnknownError
    from opensquilla.recovery.atomic import _chmod_open_file
    from opensquilla.recovery.config_patch import ConfigSnapshot

    path = snapshot.path
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot.assert_current()
    published_visible = False
    if snapshot.identity is None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(_ext(path), flags, mode)
        published_visible = True
        try:
            _write_all(descriptor, data)
            _chmod_open_file(descriptor, mode)
            os.fsync(descriptor)
        except BaseException as exc:
            os.close(descriptor)
            raise AtomicStateUnknownError(
                f"CAS publication state is unknown after creating {path}"
            ) from exc
        os.close(descriptor)
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            raise AtomicStateUnknownError(
                f"CAS publication durability is unknown for {path}"
            ) from exc
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            _chmod_open_file(descriptor, mode)
            _write_all(descriptor, data)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            snapshot.assert_current()
            os.replace(_ext(temporary), _ext(path))
            published_visible = True
            _fsync_directory(path.parent)
        except BaseException as exc:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            if published_visible:
                raise AtomicStateUnknownError(
                    f"CAS replacement state is unknown after publishing {path}"
                ) from exc
            raise
    try:
        published = ConfigSnapshot.capture(path)
    except (OSError, RuntimeError) as exc:
        raise AtomicStateUnknownError(
            f"CAS publication could not be read back for {path}"
        ) from exc
    if published.data != data:
        raise AtomicStateUnknownError(
            f"CAS-published bytes could not be proven for {path}"
        )
    return published


def _cas_publish_json(snapshot: Any, payload: dict[str, Any]) -> Any:
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return _cas_publish_bytes(snapshot, data, mode=0o600)


def _rollback_history_publication(publication: _HistoryPublication) -> None:
    publication.after.assert_current()
    if publication.before.identity is None:
        publication.path.unlink()
        _fsync_directory(publication.path.parent)
        return
    restored = _cas_publish_bytes(
        publication.after,
        publication.before.data,
        mode=publication.before.mode,
    )
    if restored.digest != publication.before.digest:
        raise OSError("replacement history rollback did not restore the prior bytes")


@dataclass
class PortableCandidate:
    """Privacy-narrow metadata for one explicitly selectable profile home."""

    path: Path
    last_used: float
    size_bytes: int | None
    era_hint: str
    previously_imported: bool
    kind: str = "windows-portable"
    session_count: int | None = None

    @property
    def version(self) -> str | None:
        return self.era_hint or None

    @property
    def estimated_activity_at(self) -> str | None:
        if self.last_used <= 0:
            return None
        return datetime.fromtimestamp(self.last_used, UTC).isoformat()

    def as_payload(self) -> dict[str, Any]:
        """Return display metadata without session ids, titles, or content."""

        return {
            "kind": self.kind,
            "path": str(self.path),
            "version": self.version,
            "estimated_activity_at": self.estimated_activity_at,
            "session_count": self.session_count,
            "size_bytes": self.size_bytes,
            "previously_imported": self.previously_imported,
        }


def enumerate_portable_homes(
    bases: list[Path] | None = None,
    *,
    target: Path | None = None,
) -> list[PortableCandidate]:
    """Enumerate ``<base>/OpenSquilla/portable/*`` homes, newest-first.

    Default bases come from the ``LOCALAPPDATA`` and ``TEMP`` environment
    variables (unset ones are skipped), matching where every portable
    launcher ever placed its data dir.
    """
    if bases is None:
        bases = []
        for env_name in ("LOCALAPPDATA", "TEMP"):
            raw = os.environ.get(env_name, "").strip()
            if raw:
                bases.append(Path(raw))
    candidates: list[PortableCandidate] = []
    seen_identities: set[_PathIdentity] = set()
    for base in bases:
        if len(candidates) >= _CANDIDATE_ENUMERATION_MAX_CANDIDATES:
            break
        portable_root = base / "OpenSquilla" / "portable"
        try:
            if _supported_entry_type(portable_root, portable_root.lstat()) != "directory":
                continue
        except OSError:
            continue
        try:
            with os.scandir(portable_root) as iterator:
                entries = []
                for index, entry in enumerate(iterator):
                    if index >= _CANDIDATE_ENUMERATION_MAX_ENTRIES:
                        break
                    entries.append(entry)
                entries.sort(key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            candidate_path = Path(entry.path)
            try:
                result = entry.stat(follow_symlinks=False)
                if _supported_entry_type(candidate_path, result) != "directory":
                    continue
            except OSError:
                continue
            # Re-stat by path. On Windows ``DirEntry.stat()`` may omit the
            # directory file index even when a direct stat can provide it.
            identity = _advisory_identity(candidate_path.lstat())
            if (
                identity is not None
                and identity in seen_identities
            ) or not is_valid_opensquilla_home(candidate_path):
                continue
            if identity is not None:
                seen_identities.add(identity)
            candidate = inspect_opensquilla_home_candidate(
                candidate_path,
                kind="windows-portable",
                target=target,
            )
            if candidate is not None:
                candidates.append(candidate)
            if len(candidates) >= _CANDIDATE_ENUMERATION_MAX_CANDIDATES:
                break
    candidates.sort(
        key=lambda candidate: (
            candidate.last_used,
            os.path.normcase(os.path.normpath(str(candidate.path))),
        ),
        reverse=True,
    )
    return candidates


def detect_desktop_home(target: Path | None = None) -> Path | None:
    """Return the platform Electron home when distinct from ``target``.

    ``target`` defaults to the ambient active home for legacy CLI callers.
    Gateway discovery supplies its config-derived profile home explicitly so a
    shell environment cannot hide or misclassify the Desktop source.
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "OpenSquilla"
    elif sys.platform == "win32":  # pragma: no cover - Windows-only path
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            base = Path(appdata) / "OpenSquilla"
        else:
            base = Path.home() / "AppData" / "Roaming" / "OpenSquilla"
    else:
        base = Path.home() / ".config" / "OpenSquilla"
    candidate = base / "opensquilla"
    if not is_valid_opensquilla_home(candidate):
        return None
    comparison_target = target or default_opensquilla_home()
    if _same_path(candidate, comparison_target):
        return None
    return candidate


def _home_last_used(home: Path) -> float:
    """Return a clearly advisory activity estimate from metadata-only probes."""
    latest = 0.0
    for probe in (
        home / "config.toml",
        home / "state" / "sessions.db",
        home / "state" / "scheduler.db",
        home / "workspace" / "MEMORY.md",
        home,
    ):
        try:
            result = probe.lstat()
            _supported_entry_type(probe, result)
            latest = max(latest, float(result.st_mtime))
        except OSError:
            continue
    return latest


def _bounded_tree_size_bytes(root: Path) -> int | None:
    """Measure a candidate without following links or allowing an unbounded walk."""

    try:
        root_result = root.lstat()
        if _supported_entry_type(root, root_result) != "directory":
            return None
    except OSError:
        return None
    pending: list[tuple[Path, Path, int]] = [(root, Path(), 0)]
    root_identity = _advisory_identity(root_result)
    seen = {root_identity} if root_identity is not None else set()
    entries = 0
    total = 0
    while pending:
        directory, relative_directory, depth = pending.pop()
        if depth > _CANDIDATE_METADATA_MAX_DEPTH:
            return None
        try:
            before = directory.lstat()
            if _supported_entry_type(directory, before) != "directory":
                return None
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda child: child.name)
        except OSError:
            return None
        for child in children:
            if depth == 0 and child.name in (
                _EXCLUDED_TOP_LEVEL_DIRS
                | _EXCLUDED_NONPORTABLE_TOP_LEVEL_DIRS
                | _EXCLUDED_AUTHORITY_NAMES
            ):
                continue
            entries += 1
            if entries > _CANDIDATE_METADATA_MAX_ENTRIES:
                return None
            path = Path(child.path)
            relative = relative_directory / child.name
            try:
                result = child.stat(follow_symlinks=False)
                if os.name != "nt" and stat.S_ISLNK(result.st_mode):
                    policy_root = _allowed_symlink_root(
                        relative,
                        _INITIAL_INSPECTION_LINK_ROOTS,
                    )
                    if policy_root is None:
                        return None
                    target = os.readlink(path)
                    _validate_relative_symlink_target(
                        source_path=path,
                        relative=relative,
                        target=target,
                        policy_root=policy_root,
                    )
                    if (
                        _identity_from_stat(path.lstat()) != _identity_from_stat(result)
                        or os.readlink(path) != target
                    ):
                        return None
                    # Link payloads occupy no copied file bytes and are never
                    # dereferenced by this advisory metadata walk.
                    continue
                entry_type = _supported_entry_type(path, result)
            except OSError:
                return None
            if entry_type == "file":
                total += int(result.st_size)
                continue
            identity = _advisory_identity(result)
            if identity is not None and identity in seen:
                return None
            if identity is not None:
                seen.add(identity)
            pending.append((path, relative, depth + 1))
        try:
            after = directory.lstat()
        except OSError:
            return None
        if (
            _identity_from_stat(before) != _identity_from_stat(after)
            or int(before.st_mtime_ns) != int(after.st_mtime_ns)
        ):
            return None
    return total


def _read_small_regular_bytes(path: Path, *, limit: int) -> bytes | None:
    """Read a bounded regular file through a no-follow descriptor."""

    try:
        result = path.lstat()
        if _supported_entry_type(path, result) != "file" or result.st_size > limit:
            return None
        entry = _ManifestEntry(
            source=path,
            relative=Path(path.name),
            entry_type="file",
            identity=_identity_from_stat(result),
            mode=int(result.st_mode),
            size=int(result.st_size),
            mtime_ns=int(result.st_mtime_ns),
            digest=None,
        )
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(_ext(path), flags)
        try:
            if not _stat_matches_manifest(os.fstat(descriptor), entry):
                return None
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > limit or not _stat_matches_manifest(os.fstat(descriptor), entry):
                return None
        finally:
            os.close(descriptor)
        if not _stat_matches_manifest(path.lstat(), entry):
            return None
        return data
    except OSError:
        return None


def _tree_size_bytes(root: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(dirpath)
        safe_dirnames: list[str] = []
        for name in dirnames:
            path = directory / name
            try:
                if _supported_entry_type(path, path.lstat()) == "directory":
                    safe_dirnames.append(name)
            except OSError:
                continue
        dirnames[:] = safe_dirnames
        for name in filenames:
            try:
                path = directory / name
                result = path.lstat()
                if _supported_entry_type(path, result) == "file":
                    total += result.st_size
            except OSError:
                continue
    return total


def _era_hint(home: Path) -> str:
    receipt = home / "install-receipt.json"
    receipt_bytes = _read_small_regular_bytes(receipt, limit=_LAYOUT_RECEIPT_MAX_BYTES)
    if receipt_bytes is not None:
        try:
            data = json.loads(receipt_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            version = data.get("version")
            if isinstance(version, str) and version.strip():
                return version.strip()[:80]
    config_bytes = _read_small_regular_bytes(
        home / "config.toml",
        limit=_CANDIDATE_METADATA_MAX_CONFIG_BYTES,
    )
    if config_bytes is not None:
        try:
            config = tomllib.loads(config_bytes.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            config = {}
        version = config.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()[:80]
    update_checks = (
        home / "state" / "update_check.json",
        home / "state" / "update_check_rc.json",
    )
    has_update_check = False
    for update_check in update_checks:
        try:
            if _supported_entry_type(update_check, update_check.lstat()) == "file":
                has_update_check = True
                break
        except OSError:
            continue
    if has_update_check:
        return "0.5.0rc2+"
    return ""


def _candidate_state_roots(home: Path) -> list[Path] | None:
    roots = [home / "state"]
    config_bytes = _read_small_regular_bytes(
        home / "config.toml",
        limit=_CANDIDATE_METADATA_MAX_CONFIG_BYTES,
    )
    if config_bytes is not None:
        try:
            config = tomllib.loads(config_bytes.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            config = {}
        configured = config.get("state_dir")
        if isinstance(configured, str) and configured.strip():
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                candidate = home / candidate
            candidate = candidate.absolute()
            try:
                candidate.relative_to(home)
            except ValueError:
                # Candidate display happens before the user has approved any
                # external roots named by this profile. Do not inspect an
                # arbitrary outside database at this stage.
                return None
            roots.append(candidate)
    unique: list[Path] = []
    identities: set[_PathIdentity] = set()
    for root in roots:
        try:
            result = root.lstat()
            if _supported_entry_type(root, result) != "directory":
                continue
            identity = _advisory_identity(result)
        except OSError:
            continue
        if identity is not None and identity in identities:
            continue
        if identity is not None:
            identities.add(identity)
        unique.append(root)
    return unique


def inspect_opensquilla_home_candidate(
    home: Path,
    *,
    kind: str,
    target: Path | None = None,
) -> PortableCandidate | None:
    """Inspect one candidate for display without reading chat or Markdown content."""

    home = home.expanduser().absolute()
    if target is not None:
        target = target.expanduser().absolute()
    if kind not in OPENSQUILLA_SOURCE_KINDS or not is_valid_opensquilla_home(home):
        return None
    state_roots = _candidate_state_roots(home)
    session_count: int | None = 0 if state_roots is not None else None
    for state_root in state_roots or []:
        sessions_db = state_root / "sessions.db"
        bundle_size = 0
        bundle_safe = True
        for suffix in ("", *_SQLITE_DURABLE_SIDECAR_SUFFIXES):
            member = sessions_db.with_name(sessions_db.name + suffix)
            try:
                member_stat = member.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                bundle_safe = False
                break
            try:
                if _supported_entry_type(member, member_stat) != "file":
                    bundle_safe = False
                    break
            except OSError:
                bundle_safe = False
                break
            bundle_size += int(member_stat.st_size)
            if bundle_size > _CANDIDATE_METADATA_MAX_SQLITE_BYTES:
                bundle_safe = False
                break
        count = _read_session_count(sessions_db) if bundle_safe else None
        if count is None:
            session_count = None
            break
        assert session_count is not None
        session_count += count
    return PortableCandidate(
        path=home,
        last_used=_home_last_used(home),
        size_bytes=_bounded_tree_size_bytes(home),
        era_hint=_era_hint(home),
        previously_imported=(
            target is not None
            and _source_was_previously_imported(home, target, source_kind=kind)
        ),
        kind=kind,
        session_count=session_count,
    )


# ---------------------------------------------------------------------------
# Gateway liveness (mirrors the gateway pidlock semantics without importing
# its private helpers: JSON pid payload + signal-0 style liveness probe).
# ---------------------------------------------------------------------------


def _parse_pid_bytes(raw: bytes) -> int | None:
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        try:
            return int(payload["pid"])
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(payload, int) and not isinstance(payload, bool):
        return payload
    try:
        return int(raw.decode("utf-8", errors="replace").strip())
    except ValueError:
        return None


def _read_pid_file(path: Path) -> int | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return _parse_pid_bytes(raw)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows-only path
        return _pid_is_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not owned by us
    except OSError:
        return False
    return True


def _pid_is_alive_windows(pid: int) -> bool:  # pragma: no cover - Windows-only path
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == still_active
        finally:
            try:
                kernel32.CloseHandle(handle)
            except OSError:
                pass
    except Exception:  # noqa: BLE001 - liveness probe must never raise
        return False


def _gateway_running(home: Path) -> bool:
    pid = _read_pid_file(home / "state" / "gateway.pid")
    return pid is not None and _pid_is_alive(pid)


# ---------------------------------------------------------------------------
# Schema-ahead pre-flight (read-only ledger inspection; never runs migrations)
# ---------------------------------------------------------------------------


def _migration_dir_candidates() -> list[Path]:
    """Mirror gateway boot's migrations-dir resolution order."""
    candidates: list[Path] = []
    env_dir = os.environ.get("OPENSQUILLA_MIGRATIONS_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    try:
        from importlib import resources as importlib_resources

        package_dir = importlib_resources.files("opensquilla").joinpath("_migrations")
        if package_dir.is_dir():
            candidates.append(Path(str(package_dir)))
    except Exception:  # noqa: BLE001 - packaged resources are best-effort here
        pass
    candidates.append(Path(__file__).resolve().parents[3] / "migrations")
    return candidates


def _known_migration_ids() -> set[str]:
    """Return the migration ids shipped with this binary (yoyo id == file stem)."""
    for candidate in _migration_dir_candidates():
        try:
            ids = {entry.stem for entry in candidate.glob("V*.py")}
        except OSError:
            continue
        if ids:
            return ids
    return set()


def _read_applied_migration_ids(db_path: Path) -> set[str] | None:
    """Read the yoyo ledger read-only; ``None`` when the db cannot be inspected."""
    try:
        with tempfile.TemporaryDirectory(prefix="opensquilla-sqlite-inspect-") as temporary:
            copied_db = _copy_sqlite_bundle(
                db_path,
                Path(temporary),
                verify_stable_bundle=True,
            )
            connection = sqlite3.connect(
                f"{copied_db.resolve().as_uri()}?mode=ro",
                uri=True,
            )
            try:
                table_rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE '%yoyo_migration'"
                ).fetchall()
                table = next(
                    (
                        name
                        for (name,) in table_rows
                        if isinstance(name, str) and name.endswith("yoyo_migration")
                    ),
                    None,
                )
                if table is None:
                    return set()
                rows = connection.execute(f'SELECT migration_id FROM "{table}"').fetchall()
            finally:
                connection.close()
    except (OSError, sqlite3.Error):
        return None
    return {str(migration_id) for (migration_id,) in rows if migration_id}


def _read_session_count(db_path: Path) -> int | None:
    """Return a privacy-safe session count from a stable read-only snapshot.

    Candidate cards need a useful size/activity signal without opening the live
    source database in Electron. No title, transcript, or session identifier is
    read or returned.
    """

    try:
        db_stat = db_path.lstat()
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    try:
        if _supported_entry_type(db_path, db_stat) != "file":
            return None
        with tempfile.TemporaryDirectory(prefix="opensquilla-sqlite-inspect-") as temporary:
            copied_db = _copy_sqlite_bundle(
                db_path,
                Path(temporary),
                verify_stable_bundle=True,
            )
            bundle_connection = sqlite3.connect(
                f"{copied_db.resolve().as_uri()}?mode=ro",
                uri=True,
            )
            try:
                snapshot_path = Path(temporary) / "candidate-session-snapshot.db"
                connection = sqlite3.connect(snapshot_path)
                try:
                    bundle_connection.backup(connection)
                    integrity = connection.execute("PRAGMA quick_check").fetchone()
                    if not integrity or str(integrity[0]).lower() != "ok":
                        return None
                    table = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
                    ).fetchone()
                    if table is None:
                        return 0
                    count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()
                finally:
                    connection.close()
            finally:
                bundle_connection.close()
    except (OSError, sqlite3.Error):
        return None
    if not count or isinstance(count[0], bool) or not isinstance(count[0], int):
        return None
    return max(0, int(count[0]))


def _copy_sqlite_bundle(
    source_db: Path,
    destination_dir: Path,
    *,
    verify_stable_bundle: bool = False,
) -> Path:
    """Copy a SQLite database and present sidecars for mutation-free inspection."""
    destination_db = destination_dir / source_db.name
    captured: dict[str, tuple[_ManifestEntry, str]] = {}
    for suffix in ("", *_SQLITE_DURABLE_SIDECAR_SUFFIXES):
        source_file = source_db.with_name(source_db.name + suffix)
        try:
            result = source_file.lstat()
        except FileNotFoundError:
            continue
        if _supported_entry_type(source_file, result) != "file":
            raise OSError(f"SQLite bundle member is not a regular file: {source_file}")
        entry = _ManifestEntry(
            source=source_file,
            relative=Path(source_file.name),
            entry_type="file",
            identity=_identity_from_stat(result),
            mode=int(result.st_mode),
            size=int(result.st_size),
            mtime_ns=int(result.st_mtime_ns),
            digest=None,
        )
        destination_file = destination_db.with_name(destination_db.name + suffix)
        digest = _digest_regular_file(source_file, expected=entry, destination=destination_file)
        if not digest:
            raise OSError(f"could not snapshot SQLite bundle member: {source_file}")
        captured[suffix] = (entry, digest)
    if verify_stable_bundle:
        for suffix in ("", *_SQLITE_DURABLE_SIDECAR_SUFFIXES):
            source_file = source_db.with_name(source_db.name + suffix)
            expected = captured.get(suffix)
            if expected is None:
                try:
                    source_file.lstat()
                except FileNotFoundError:
                    continue
                raise OSError(f"SQLite bundle changed during snapshot: {source_file}")
            entry, copied_digest = expected
            if _digest_regular_file(source_file, expected=entry) != copied_digest:
                raise OSError(f"SQLite bundle changed during snapshot: {source_file}")
    return destination_db


# ---------------------------------------------------------------------------
# Secret env-key naming
# ---------------------------------------------------------------------------


def _provider_env_key(provider_id: str) -> str:
    """Return the provider's conventional key env var, or "" when unknown."""
    normalized = provider_id.strip().lower()
    if not normalized:
        return ""
    try:
        registry = importlib.import_module("opensquilla.provider.registry")
        spec = registry.get_provider_spec(normalized)
    except Exception:  # noqa: BLE001 - unknown providers fall back to a generic key
        return ""
    env_key = str(getattr(spec, "env_key", "") or "")
    if not env_key or env_key == "OAuth":
        return ""
    return env_key


def _fallback_profile_env_key(profile_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", profile_id).strip("_").upper() or "UNKNOWN"
    return f"OPENSQUILLA_PROFILE_{slug}_API_KEY"


@dataclass(frozen=True)
class OpenSquillaMigrationOptions:
    """Options for the OpenSquilla-to-OpenSquilla home import."""

    source: Path | str | None = None
    kind: str = "cli-home"
    config_path: Path | None = None
    apply: bool = False
    replace_target: bool = False
    confirm_replace_target: Path | str | None = None
    #: Deprecated compatibility alias for ``replace_target``. It never waives
    #: the exact ``confirm_replace_target`` check.
    overwrite: bool = False
    #: Test override for the target home; defaults to the active home.
    target: Path | str | None = None
    #: Internal settings-preview capability. It permits a dry-run to inspect
    #: the active target while its gateway is running; apply must never use it.
    allow_running_target_preview: bool = False


class OpenSquillaHomeMigrator:
    """Import a legacy OpenSquilla home into the current home.

    Protocol: validate -> pre-flight -> (dry-run stop) -> staged copy ->
    transforms on the staged copy -> journaled commit renames -> report.
    User errors are recorded as ``error`` items in the report; they never
    raise.
    """

    def __init__(self, options: OpenSquillaMigrationOptions) -> None:
        self.options = options
        self.kind = options.kind
        self.source: Path | None = _as_path(options.source)
        self.target = _as_path(options.target) or default_opensquilla_home()
        self.timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        self.transaction_id = str(uuid.uuid4())
        self.output_dir = (
            self.target / "migration" / "opensquilla" / self.transaction_id
        )
        self.items: list[ItemResult] = []
        self.candidates: list[PortableCandidate] = []
        self.config_transforms: list[str] = []
        self.secret_relocations: list[dict[str, Any]] = []
        self.paused_jobs: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.preflight: dict[str, Any] = {
            "source_gateway_running": False,
            "target_gateway_running": False,
            "schema_ahead": False,
            "disk_required_bytes": 0,
            "disk_free_bytes": 0,
            "session_count": None,
        }
        self._env_additions: dict[str, str] = {}
        self._config_payload: dict[str, Any] | None = None
        self._raw_config_payload: dict[str, Any] | None = None
        self._source_config_bytes: bytes | None = None
        self._dotenv_data_root_values: dict[str, list[str]] = {
            "state": [],
            "workspace": [],
            "media": [],
        }
        self._dotenv_keys_to_remove: dict[Path, set[str]] = {}
        self._data_roots: dict[str, list[Path]] = {
            "state": [],
            "workspace": [],
            "media": [],
        }
        self._agent_workspace_roots: dict[str, Path] = {}
        self._sqlite_stores_cache: dict[Path, Path] | None = None
        self._sqlite_logical_members: set[Path] = set()
        self._source_snapshots: tuple[_SourceSnapshot, ...] = ()
        self._initial_source_snapshot: _SourceSnapshot | None = None
        self._source_gateway_authority: tuple[_LegacyGatewayAuthoritySnapshot, ...] = ()
        self._target_had_real_data = False
        self._target_preflight_identity: _PathIdentity | None = None
        self._target_preflight_present = False
        self._blocked = False
        self._wrote_output_dir = False
        self._committed = False
        self._recovered_report: dict[str, Any] | None = None
        self._recovered_transaction_id = ""

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @property
    def target_had_real_data(self) -> bool:
        """Whether pre-flight classified the target as an established profile."""

        return self._target_had_real_data

    def migrate(self) -> dict[str, Any]:
        if self.options.apply and self.options.allow_running_target_preview:
            self._record(
                "options",
                None,
                self.target,
                "error",
                "allow_running_target_preview is valid for dry-run only",
            )
            self._blocked = True
            return self._report()
        if self.kind not in OPENSQUILLA_SOURCE_KINDS:
            self._record(
                "options",
                None,
                None,
                "error",
                f"Unknown source kind: {self.kind} "
                f"(known: {', '.join(OPENSQUILLA_SOURCE_KINDS)})",
            )
            return self._report()
        if self.options.config_path is not None:
            self._record(
                "options",
                self.options.config_path,
                None,
                "error",
                "config_path is not supported for OpenSquilla self-migration; "
                "select the target home through the active OpenSquilla environment",
            )
            self._blocked = True
            return self._report()
        self._resolve_source()
        if self.source is None:
            return self._report()
        if not self.options.apply:
            return self._migrate_resolved()
        from opensquilla.recovery import (
            RecoveryError,
            acquire_legacy_gateway_locks,
            acquire_profile_locks,
        )
        from opensquilla.recovery.locking import replacement_history_lock_scope

        lock_stack = ExitStack()
        try:
            lock_stack.enter_context(
                acquire_profile_locks(
                    self.source,
                    self.target,
                    replacement_history_lock_scope(self.target),
                )
            )
            if (
                not os.path.lexists(_commit_journal_path(self.target))
                and not self._precheck_target_before_legacy_lock()
            ):
                lock_stack.close()
                return self._report()
            lock_stack.enter_context(
                acquire_legacy_gateway_locks(
                    self.source,
                    self.target,
                    read_only_homes=(self.source,),
                )
            )
            from opensquilla.recovery.transaction import (
                finalize_committed_profile_transaction,
            )

            with contextlib.suppress(RecoveryError):
                finalize_committed_profile_transaction(self.target)
        except (RecoveryError, OSError) as exc:
            lock_stack.close()
            self._record(
                "preflight/lock",
                self.source,
                self.target,
                "error",
                f"could not acquire the required profile operation locks: {exc}",
            )
            self._blocked = True
            return self._report()
        try:
            return self._migrate_resolved()
        finally:
            lock_stack.close()

    def _migrate_resolved(self) -> dict[str, Any]:
        source = self.source
        assert source is not None
        if not self._recover_interrupted_commit():
            return self._report()
        if self._recovered_report is not None:
            return self._recovered_report
        if not self._validate_paths():
            return self._report()
        self._validate_source_tree_no_follow(source)
        if self._blocked:
            return self._report()
        self._sandbox_config_pass(source)
        self._inspect_profile_dotenv(source)
        self._plan_data_roots()
        self._run_preflight()
        if self._blocked:
            return self._report()

        entries = self._collect_entries()
        self._plan_config_transforms()

        if not self.options.apply:
            for entry in entries:
                details: dict[str, Any] = {}
                if entry.name == "state":
                    details["excluded"] = [f"state/{name}" for name in _EXCLUDED_STATE_FILES]
                self._record(
                    "home-entry", entry, self.target / entry.name, "planned", details=details
                )
            planned_data_entries = {entry.name for entry in entries}
            for name, roots in self._data_roots.items():
                for root in roots:
                    if name not in planned_data_entries or root != source / name:
                        self._record(
                            "data-root", root, self.target / name, "planned"
                        )
            scheduler_db = self._source_sqlite_stores().get(Path("scheduler.db"))
            if scheduler_db is not None:
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="opensquilla-scheduler-preview-"
                    ) as temporary:
                        copied_db = self._materialize_sqlite_bundle(
                            scheduler_db,
                            Path(temporary),
                        )
                        jobs = self._read_scheduler_jobs(copied_db)
                except OSError:
                    jobs = None
                if jobs is not None:
                    self.paused_jobs = jobs
            return self._report()

        self._apply(entries)
        return self._report()

    def _validate_source_tree_no_follow(self, source: Path) -> None:
        """Capture the inspection-only source snapshot before reading config."""

        try:
            self._initial_source_snapshot = _scan_source_tree(
                source,
                destination_prefix=Path(),
                role="initial-profile-safety",
                excluded=frozenset(
                    {
                        *(Path("state") / name for name in _EXCLUDED_STATE_FILES),
                        *(
                            Path(name)
                            for name in (
                                _EXCLUDED_TOP_LEVEL_DIRS
                                | _EXCLUDED_NONPORTABLE_TOP_LEVEL_DIRS
                                | _EXCLUDED_AUTHORITY_NAMES
                            )
                        ),
                    }
                ),
                excluded_leaf_suffixes=_SQLITE_TRANSIENT_SIDECAR_SUFFIXES,
                allowed_symlink_roots=_INITIAL_INSPECTION_LINK_ROOTS,
            )
        except (OSError, RuntimeError) as exc:
            self._record(
                "preflight/manifest",
                source,
                self.target,
                "error",
                f"source profile cannot be inspected without following links: {exc}",
                details=_source_snapshot_failure_details(
                    exc,
                    fallback_code="source_snapshot_unreadable",
                ),
            )
            self._blocked = True

    def _rollback_interrupted_import_paths(
        self,
        *,
        staging: Path,
        backup: Path,
        target_existed: bool,
        original_identity: object,
        staging_identity: object,
        backup_identity: object,
        candidate_identity: object,
    ) -> None:
        """Finish a pre-commit rollback from any identity-proven crash window."""

        from opensquilla.recovery import move_profile_no_replace, native_move_no_replace

        candidate = candidate_identity or staging_identity
        original = backup_identity or original_identity
        if not isinstance(candidate, dict):
            raise ValueError("candidate identity is missing from the import journal")
        if target_existed and not isinstance(original, dict):
            raise ValueError("original target identity is missing from the import journal")

        target_present = os.path.lexists(self.target)
        staging_present = os.path.lexists(staging)
        backup_present = os.path.lexists(backup)
        target_is_candidate = target_present and _object_identity_matches(
            self.target,
            candidate,
        )
        target_is_original = (
            target_present
            and target_existed
            and _object_identity_matches(self.target, original)
        )
        staging_is_candidate = staging_present and _object_identity_matches(
            staging,
            candidate,
        )
        backup_is_original = (
            backup_present
            and target_existed
            and _object_identity_matches(backup, original)
        )
        if target_present and not (target_is_candidate or target_is_original):
            raise ValueError("target identity is neither the candidate nor the original")
        if staging_present and not staging_is_candidate:
            raise ValueError("staging identity no longer matches the candidate")
        if backup_present and not backup_is_original:
            raise ValueError("backup identity no longer matches the original target")
        if target_is_candidate:
            if staging_present:
                raise ValueError("candidate exists at both target and staging paths")
            move_profile_no_replace(
                self.target,
                staging,
                move=native_move_no_replace,
            )
            staging_present = True
            staging_is_candidate = True
            target_present = False
            target_is_original = False

        if target_existed:
            if target_is_original:
                if backup_present:
                    raise ValueError("original target exists at both target and backup paths")
            elif not target_present and backup_is_original:
                move_profile_no_replace(
                    backup,
                    self.target,
                    move=native_move_no_replace,
                )
                target_present = True
                target_is_original = True
                backup_present = False
            else:
                raise ValueError("original target cannot be restored from the recorded paths")
        elif target_present or backup_present:
            raise ValueError("first-import rollback paths contain an unexpected target or backup")

        if staging_is_candidate:
            _remove_matching_staging(staging, candidate)
            staging_present = False
        if staging_present or backup_present:
            raise ValueError("import rollback left an unexpected transaction path")
        if target_existed and not target_is_original:
            raise ValueError("import rollback did not restore the original target")

    def _recover_interrupted_commit(self) -> bool:
        from opensquilla.recovery import RecoveryError
        from opensquilla.recovery.config_patch import ConfigSnapshot

        source = self.source
        assert source is not None
        journal = _commit_journal_path(self.target)
        try:
            journal_snapshot = ConfigSnapshot.capture(journal)
        except (OSError, RuntimeError) as exc:
            self._record(
                "preflight/recovery",
                journal,
                self.target,
                "error",
                f"could not safely inspect the interrupted import: {exc}",
            )
            self._blocked = True
            return False
        if journal_snapshot.identity is None:
            return True
        if not self.options.apply:
            self._record(
                "preflight/recovery",
                journal,
                self.target,
                "error",
                "an interrupted import transaction needs recovery; rerun with --apply",
            )
            self._blocked = True
            return False
        try:
            from opensquilla.recovery.transaction import _load_typed_transaction

            journal_snapshot, payload = _load_typed_transaction(self.target)
            if payload.get("operation") != "profile-import":
                raise ValueError("replacement journal is not a profile import")
            recorded_target = Path(str(payload.get("target", "")))
            staging = Path(str(payload.get("staging", "")))
            backup = Path(str(payload.get("backup", "")))
            recorded_source = Path(str(payload.get("source", "")))
            phase = str(payload.get("phase", ""))
            transaction_id = str(payload.get("transaction_id", ""))
            try:
                if str(uuid.UUID(transaction_id)) != transaction_id:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("journal transaction id is not a canonical UUID") from exc
            if not _same_path(recorded_target, self.target):
                raise ValueError("journal target does not match the active target")
            if not _same_path(recorded_source, source):
                raise ValueError("journal source does not match the selected source")
            expected_staging = self.target.parent / (
                f".{self.target.name}.profile-staging.{transaction_id}"
            )
            expected_backup = self.target.with_name(
                f"{self.target.name}.backup.{transaction_id}"
            )
            if not _same_path(staging, expected_staging):
                raise ValueError("journal staging path is outside the target parent")
            if not _same_path(backup, expected_backup):
                raise ValueError("journal backup path is outside the target parent")
            if phase not in _JOURNAL_PHASES:
                raise ValueError(f"unknown journal phase: {phase}")
            identities = payload.get("identities")
            if not isinstance(identities, dict):
                raise ValueError("journal identities are missing")
            original_target = identities.get("original_target")
            staging_identity = identities.get("staging")
            backup_identity = identities.get("backup")
            candidate_identity = identities.get("candidate")
            target_existed = bool(payload.get("target_existed"))
            journal_snapshot.assert_current()

            completed = None
            if phase == "committed" and _identity_payload_matches(
                self.target, candidate_identity
            ):
                completed = _matching_import_receipt(
                    source,
                    self.target,
                    transaction_id=transaction_id,
                    source_kind=self.kind,
                )
            if completed is not None:
                recovered_transaction_id, receipt = completed
                if target_existed:
                    if not _identity_payload_matches(backup, backup_identity):
                        raise ValueError("committed target backup identity no longer matches")
                    history_state, _snapshot, _history = self._import_history_state(
                        backup,
                        payload,
                    )
                    if history_state != "matching":
                        raise ValueError("committed target replacement history is incomplete")
                else:
                    history_state, _snapshot, _history = self._import_history_state(
                        backup,
                        payload,
                    )
                    if history_state != "absent":
                        raise ValueError("first-import replacement history is ambiguous")
                from opensquilla.recovery.transaction import (
                    finalize_committed_profile_transaction,
                )

                if not finalize_committed_profile_transaction(self.target):
                    raise ValueError("committed import journal could not be finalized")
                self._recovered_transaction_id = recovered_transaction_id
                self._recovered_report = _report_from_layout_receipt(receipt)
                return True

            if phase == "committed":
                raise ValueError(
                    "committed journal has no valid target receipt; preserving all paths"
                )
            if phase == "validated":
                self._remove_interrupted_import_history(backup, payload)
            self._rollback_interrupted_import_paths(
                staging=staging,
                backup=backup,
                target_existed=target_existed,
                original_identity=original_target,
                staging_identity=staging_identity,
                backup_identity=backup_identity,
                candidate_identity=candidate_identity,
            )
            recovery_reason = f"rolled back the pre-commit {phase} import transaction"

            _unlink_matching_journal(journal, payload)
            self._record(
                "recovery",
                journal,
                self.target,
                "migrated",
                recovery_reason,
            )
            return True
        except (
            OSError,
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
            RecoveryError,
        ) as exc:
            self._record(
                "preflight/recovery",
                journal,
                self.target,
                "error",
                f"could not safely recover the interrupted import: {exc}",
            )
            self._blocked = True
            return False

    # ------------------------------------------------------------------
    # Source resolution and validation
    # ------------------------------------------------------------------

    def _resolve_source(self) -> None:
        if self.kind == "windows-portable":
            self.candidates = enumerate_portable_homes(target=self.target)
        if self.source is not None:
            return
        if self.kind == "cli-home":
            self.source = Path.home() / ".opensquilla"
            return
        if self.kind == "desktop-home":
            detected = detect_desktop_home()
            if detected is None:
                self._record(
                    "source",
                    None,
                    None,
                    "error",
                    "No desktop OpenSquilla home was found on this machine",
                )
                return
            self.source = detected
            return
        # Portable homes are never auto-selected, even when only one exists.
        # The candidate list is display data; callers must pass --home/--source.
        if not self.candidates:
            self._record(
                "source",
                None,
                None,
                "error",
                "No portable OpenSquilla homes were found; pass --source <path>",
            )
            return
        listing = "; ".join(str(candidate.path) for candidate in self.candidates)
        self._record(
            "source",
            None,
            None,
            "error",
            "Portable OpenSquilla homes were found; explicitly confirm one with "
            f"--home <path>. Candidates: {listing}",
        )

    def _validate_paths(self) -> bool:
        source = self.source
        assert source is not None
        profile_kind = (
            os.environ.get("OPENSQUILLA_DESKTOP_PROFILE_KIND")
            or os.environ.get("OPENSQUILLA_PROFILE_KIND")
            or ""
        ).strip().lower()
        if profile_kind in {"recovery", "desktop-recovery"} or (
            "recovery-profiles" in self.target.parts
        ):
            self._record(
                "target",
                source,
                self.target,
                "error",
                "complete profile imports cannot target a recovery profile",
            )
            return False
        try:
            source_is_plain = _is_plain_directory(source)
            target_parent_is_plain = _is_plain_directory(self.target.parent)
        except OSError:
            source_is_plain = False
            target_parent_is_plain = False
        if not source_is_plain:
            self._record("source", source, None, "error", "source home does not exist")
            return False
        if not target_parent_is_plain:
            self._record(
                "target",
                source,
                self.target,
                "error",
                "target parent contains a link, reparse point, or unsafe component",
            )
            return False
        if not is_valid_opensquilla_home(source):
            self._record(
                "source",
                source,
                None,
                "error",
                "source is not an OpenSquilla home (no config.toml, state/, or workspace/)",
            )
            return False
        try:
            resolved_source = source.resolve(strict=False)
            resolved_target = self.target.resolve(strict=False)
        except OSError:
            resolved_source, resolved_target = source, self.target
        if resolved_source == resolved_target:
            self._record(
                "source",
                source,
                self.target,
                "error",
                "source and target are the same OpenSquilla home",
            )
            return False
        if resolved_source.is_relative_to(resolved_target) or resolved_target.is_relative_to(
            resolved_source
        ):
            self._record(
                "source",
                source,
                self.target,
                "error",
                "source and target homes are nested within each other",
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Pre-flight
    # ------------------------------------------------------------------

    def _run_preflight(self) -> None:
        source = self.source
        assert source is not None

        self._capture_source_gateway_authority()
        self._build_source_snapshots()
        if self._blocked:
            return

        source_running = any(
            authority.pid_value is not None and _pid_is_alive(authority.pid_value)
            for authority in self._source_gateway_authority
        )
        target_running = _gateway_running(self.target)
        self.preflight["source_gateway_running"] = source_running
        self.preflight["target_gateway_running"] = target_running
        if source_running:
            self._record(
                "preflight/gateway",
                source,
                None,
                "error",
                "a gateway is running on the source home; stop it and re-run",
            )
            self._blocked = True
        if target_running and not self.options.allow_running_target_preview:
            self._record(
                "preflight/gateway",
                self.target,
                None,
                "error",
                "a gateway is running on the target home; stop it and re-run",
            )
            self._blocked = True
        if target_running and self.options.allow_running_target_preview:
            self._record(
                "preflight/gateway",
                self.target,
                self.target,
                "skipped",
                "the active target gateway is allowed for this read-only preview",
            )
        elif not source_running and not target_running:
            self._record(
                "preflight/gateway",
                source,
                self.target,
                "skipped",
                "no gateway is running on the source or target home",
            )

        schema_ahead = self._check_schema_ahead(source)
        self.preflight["schema_ahead"] = schema_ahead
        if schema_ahead:
            self._blocked = True
        if self._check_sqlite_integrity():
            self._blocked = True

        session_count: int | None = 0
        sessions_db = self._source_sqlite_stores().get(Path("sessions.db"))
        if sessions_db is not None:
            try:
                with tempfile.TemporaryDirectory(
                    prefix="opensquilla-session-count-"
                ) as temporary:
                    copied_db = self._materialize_sqlite_bundle(
                        sessions_db,
                        Path(temporary),
                    )
                    session_count = _read_session_count(copied_db)
            except OSError:
                session_count = None
        self.preflight["session_count"] = session_count

        # The preview must discover split-root conflicts before the user
        # approves an apply. This walk is read-only and also re-runs during
        # staging to catch a source that changed after preview.
        for name, roots in self._data_roots.items():
            if len(roots) < 2:
                continue
            try:
                self._validate_data_root_conflicts(name, roots)
            except OSError:
                self._blocked = True

        required = self._required_disk_bytes()
        free = self._target_free_bytes()
        self.preflight["disk_required_bytes"] = required
        self.preflight["disk_free_bytes"] = free
        if free < required:
            self._record(
                "preflight/disk",
                source,
                self.target,
                "error",
                f"not enough free disk space on the target volume: {required} bytes "
                f"required (source size plus margin), {free} bytes free",
            )
            self._blocked = True
        else:
            self._record("preflight/disk", source, self.target, "skipped", "ok")

        try:
            target_has_data = self._target_has_real_data()
        except OSError as exc:
            self._record(
                "preflight/target",
                None,
                self.target,
                "error",
                f"target home cannot be replaced safely: {exc}",
            )
            self._blocked = True
            target_has_data = False
        self._target_had_real_data = target_has_data
        replace_requested = self.options.replace_target or self.options.overwrite
        target_external_authority = (
            self._target_external_authority()
            if target_has_data and replace_requested
            else None
        )
        if target_external_authority is not None:
            reason, path = target_external_authority
            self._record(
                "preflight/target",
                path,
                self.target,
                "error",
                f"{reason}; a sibling-directory backup would not contain all current "
                "profile data, so replacement was blocked",
            )
            self._blocked = True
        if target_has_data:
            if not replace_requested:
                self._record(
                    "preflight/target",
                    None,
                    self.target,
                    "error",
                    "target home contains real data; cancel to preserve it, or pass "
                    "--replace-target with --confirm-replace-target "
                    f"{_normalized_path(self.target)} to create a complete backup "
                    "and replace the whole profile",
                )
                self._blocked = True
            elif self.options.apply and not self._replace_confirmation_matches():
                self._record(
                    "preflight/target",
                    None,
                    self.target,
                    "error",
                    "whole-profile replacement requires an exact confirmation: "
                    f"--confirm-replace-target {_normalized_path(self.target)}",
                )
                self._blocked = True
            elif target_external_authority is None:
                alias_note = " (deprecated --overwrite alias)" if self.options.overwrite else ""
                self._record(
                    "preflight/target",
                    None,
                    self.target,
                    "skipped",
                    "the entire target will be backed up and replaced; no files are merged"
                    f"{alias_note}",
                )
        else:
            self._record("preflight/target", None, self.target, "skipped", "ok")

    def _build_source_snapshots(self) -> None:
        """Freeze every source root used by apply without following links."""
        source = self.source
        assert source is not None
        if self._blocked:
            return
        snapshots: list[_SourceSnapshot] = []
        allowed_data_root_symlinks = {
            "workspace": frozenset({Path()}),
            "state": frozenset({Path("workspace")}),
        }
        top_level_excluded = {
            Path(name)
            for name in (
                _EXCLUDED_TOP_LEVEL_DIRS
                | _EXCLUDED_NONPORTABLE_TOP_LEVEL_DIRS
                | _EXCLUDED_AUTHORITY_NAMES
            )
        }
        top_level_excluded.update(Path(name) for name in self._data_roots)
        for roots in self._data_roots.values():
            for root in roots:
                try:
                    relative_root = root.resolve(strict=False).relative_to(
                        source.resolve(strict=False)
                    )
                except (OSError, ValueError):
                    continue
                if relative_root != Path():
                    top_level_excluded.add(relative_root)
        for root in self._agent_workspace_roots.values():
            try:
                relative_root = root.resolve(strict=False).relative_to(
                    source.resolve(strict=False)
                )
            except (OSError, ValueError):
                continue
            if relative_root != Path():
                top_level_excluded.add(relative_root)
        try:
            snapshots.append(
                _scan_source_tree(
                    source,
                    destination_prefix=Path(),
                    role="profile",
                    excluded=frozenset(top_level_excluded),
                    excluded_leaf_suffixes=_SQLITE_TRANSIENT_SIDECAR_SUFFIXES,
                )
            )
            for name, roots in self._data_roots.items():
                for index, root in enumerate(roots):
                    exclusions: set[Path] = set()
                    if name == "state":
                        exclusions.update(Path(item) for item in _EXCLUDED_STATE_FILES)
                    snapshots.append(
                        _scan_source_tree(
                            root,
                            destination_prefix=Path(name),
                            role=f"{name}:{index}",
                            excluded=frozenset(exclusions),
                            excluded_leaf_suffixes=_SQLITE_TRANSIENT_SIDECAR_SUFFIXES,
                            allowed_symlink_roots=allowed_data_root_symlinks.get(
                                name,
                                frozenset(),
                            ),
                        )
                    )
            for agent_id, root in sorted(self._agent_workspace_roots.items()):
                snapshots.append(
                    _scan_source_tree(
                        root,
                        destination_prefix=Path("workspace") / "agents" / agent_id,
                        role=f"agent-workspace:{agent_id}",
                        excluded_leaf_suffixes=_SQLITE_TRANSIENT_SIDECAR_SUFFIXES,
                        allowed_symlink_roots=frozenset({Path()}),
                    )
                )
        except OSError as exc:
            self._record(
                "preflight/manifest",
                source,
                self.target,
                "error",
                f"could not create a stable no-follow source manifest: {exc}",
                details=_source_snapshot_failure_details(
                    exc,
                    fallback_code="source_snapshot_unreadable",
                ),
            )
            self._blocked = True
            return
        self._source_snapshots = tuple(snapshots)
        self._sqlite_stores_cache = None
        sqlite_members: set[Path] = set()
        for relative in self._source_sqlite_stores():
            sqlite_members.update(
                Path("state") / relative.with_name(relative.name + suffix)
                for suffix in ("", *_SQLITE_DURABLE_SIDECAR_SUFFIXES)
            )
        self._sqlite_logical_members = sqlite_members
        self._record(
            "preflight/manifest",
            source,
            self.target,
            "skipped",
            "stable no-follow manifest captured",
        )

    def _capture_source_gateway_authority(self) -> None:
        """Freeze excluded old-gateway PID/lock paths without writing the source."""

        source = self.source
        assert source is not None
        if self._blocked:
            return
        roots = {
            _normalized_path(root): root
            for root in (self._data_roots.get("state") or [source / "state"])
        }
        try:
            authority_lock = LegacyGatewayLock(
                source,
                state_roots=(roots[key] for key in sorted(roots)),
                create_if_missing=False,
            )
            with authority_lock:
                self._source_gateway_authority = tuple(
                    _capture_legacy_gateway_authority(
                        roots[key],
                        held_lock=authority_lock.snapshot_state_root(roots[key]),
                    )
                    for key in sorted(roots)
                )
        except OSError as exc:
            self._record(
                "preflight/gateway-authority",
                source,
                self.target,
                "error",
                f"could not safely inspect the source gateway authority: {exc}",
            )
            self._blocked = True

    def _verify_source_gateway_authority(
        self,
        final_source_lock: LegacyGatewayLock,
    ) -> None:
        """Detect PID/lock appearance, disappearance, replacement, or mutation."""

        for expected in self._source_gateway_authority:
            try:
                held_before = final_source_lock.snapshot_state_root(expected.root)
                current = _capture_legacy_gateway_authority(
                    expected.root,
                    held_lock=held_before,
                )
                held_after = final_source_lock.snapshot_state_root(expected.root)
            except OSError as exc:
                raise OSError(
                    "source legacy gateway authority changed during import; "
                    "publication cancelled"
                ) from exc
            if current != expected or held_after != held_before:
                raise OSError(
                    "source legacy gateway authority changed during import; "
                    "publication cancelled"
                )

    def _source_gateway_authority_is_no_longer_stable(self) -> bool:
        """Recheck excluded authority after an apply error releases its leases.

        Some Windows lock-file mutations can invalidate the compatibility
        lease before the normal bracketed verifier produces its stable error.
        The outer apply handler calls this only after context-manager teardown,
        so a fresh read-only capture can preserve the more useful fail-closed
        classification without obscuring an unchanged source's real error.
        """

        source = self.source
        assert source is not None
        final_source_lock = LegacyGatewayLock(
            source,
            state_roots=(item.root for item in self._source_gateway_authority),
            create_if_missing=False,
        )
        try:
            with final_source_lock:
                self._verify_source_gateway_authority(final_source_lock)
        except OSError:
            return True
        return False

    def _verify_source_snapshots(self) -> None:
        """Second full scan: any add/remove/change aborts before publication."""
        for expected in self._source_snapshots:
            current = _scan_source_tree(
                expected.root,
                destination_prefix=expected.destination_prefix,
                role=expected.role,
                excluded=expected.excluded,
                excluded_leaf_suffixes=expected.excluded_leaf_suffixes,
                allowed_symlink_roots=expected.allowed_symlink_roots,
            )
            if not _source_snapshots_match(current, expected):
                raise OSError(
                    f"source changed during import ({expected.role}); publication cancelled"
                )

    def _verify_source_still_stable(
        self,
        final_source_lock: LegacyGatewayLock,
    ) -> None:
        """Bracket a complete source rescan with old-gateway authority checks."""

        self._verify_source_gateway_authority(final_source_lock)
        self._verify_source_snapshots()
        self._verify_source_gateway_authority(final_source_lock)

    def _copy_source_snapshots(self, staging: Path) -> None:
        """Materialize the frozen manifest into a source-only staging tree."""

        def ensure_snapshot_destination(snapshot: _SourceSnapshot) -> Path:
            current = staging
            for component in snapshot.destination_prefix.parts:
                current /= component
                try:
                    value = current.lstat()
                except FileNotFoundError:
                    current.mkdir()
                    continue
                try:
                    entry_type = _supported_entry_type(current, value)
                except OSError as exc:
                    raise OSError(
                        f"source roots collide on an unsafe destination: {current}"
                    ) from exc
                if entry_type != "directory":
                    raise OSError(
                        f"source roots collide on a non-directory: {current}"
                    )
            return current

        copied_files: dict[Path, str] = {}
        copied_links: dict[Path, str] = {}
        for snapshot in self._source_snapshots:
            snapshot_destination = staging / snapshot.destination_prefix
            if snapshot.destination_prefix != Path():
                snapshot_destination = ensure_snapshot_destination(snapshot)
                os.chmod(
                    snapshot_destination,
                    (snapshot.root_mode & 0o777) | 0o700,
                )
            for entry in snapshot.entries:
                destination = staging / snapshot.destination_prefix / entry.relative
                if entry.entry_type == "directory":
                    if destination.exists() or destination.is_symlink():
                        try:
                            destination_stat = destination.lstat()
                            destination_type = _supported_entry_type(
                                destination, destination_stat
                            )
                        except OSError as exc:
                            raise OSError(
                                f"staging destination is unsafe: {destination}"
                            ) from exc
                        if destination_type != "directory":
                            raise OSError(
                                f"source roots collide on a non-directory: {destination}"
                            )
                    else:
                        destination.mkdir(parents=True, exist_ok=False)
                    os.chmod(destination, (entry.mode & 0o777) | 0o700)
                    continue

                if entry.entry_type == "symlink":
                    assert entry.link_target is not None
                    previous_target = copied_links.get(destination)
                    if previous_target is not None:
                        if previous_target != entry.link_target:
                            raise OSError(
                                f"source roots contain conflicting links for {destination}"
                            )
                        continue
                    if destination.exists() or destination.is_symlink():
                        raise OSError(f"staging destination already exists: {destination}")
                    copied_target = _copy_snapshot_symlink_posix(
                        snapshot,
                        entry,
                        destination,
                    )
                    if copied_target != entry.link_target:
                        destination.unlink(missing_ok=True)
                        raise OSError(f"source link changed while copied: {entry.source}")
                    copied_links[destination] = copied_target
                    continue

                assert entry.digest is not None
                previous_digest = copied_files.get(destination)
                if previous_digest is not None:
                    if previous_digest != entry.digest:
                        logical = destination.relative_to(staging)
                        if logical in self._sqlite_logical_members:
                            continue
                        raise OSError(
                            f"source roots contain conflicting files for {destination}"
                        )
                    continue
                if destination.exists() or destination.is_symlink():
                    raise OSError(f"staging destination already exists: {destination}")
                copied_digest = _copy_snapshot_file(snapshot, entry, destination)
                if copied_digest != entry.digest:
                    destination.unlink(missing_ok=True)
                    raise OSError(f"source file changed while copied: {entry.source}")
                os.chmod(destination, (entry.mode & 0o777) | 0o600)
                copied_files[destination] = copied_digest

    def _target_free_bytes(self) -> int:
        probe = self.target.parent
        while not probe.exists():
            parent = probe.parent
            if parent == probe:
                break
            probe = parent
        try:
            return int(shutil.disk_usage(probe).free)
        except OSError:
            return 0

    def _precheck_target_before_legacy_lock(self) -> bool:
        """Reject known target hazards before a compatibility lock can touch H."""

        try:
            target_has_data = self._target_has_real_data()
        except OSError as exc:
            self._record(
                "preflight/target",
                None,
                self.target,
                "error",
                f"target home cannot be replaced safely: {exc}",
            )
            self._blocked = True
            return False
        if not target_has_data:
            external = self._target_external_authority()
            if external is None:
                return True
            reason, path = external
            self._record(
                "preflight/target",
                path,
                self.target,
                "error",
                f"{reason}; remove the active override before importing so the new "
                "profile cannot share another live data root",
            )
            self._blocked = True
            return False
        if not (self.options.replace_target or self.options.overwrite):
            self._record(
                "preflight/target",
                None,
                self.target,
                "error",
                "target home contains real data; cancel to preserve it, or pass "
                "--replace-target with --confirm-replace-target "
                f"{_normalized_path(self.target)} to create a complete backup "
                "and replace the whole profile",
            )
            self._blocked = True
            return False
        if not self._replace_confirmation_matches():
            self._record(
                "preflight/target",
                None,
                self.target,
                "error",
                "whole-profile replacement requires an exact confirmation: "
                f"--confirm-replace-target {_normalized_path(self.target)}",
            )
            self._blocked = True
            return False
        external = self._target_external_authority()
        if external is None:
            return True
        reason, path = external
        self._record(
            "preflight/target",
            path,
            self.target,
            "error",
            f"{reason}; a sibling-directory backup would not contain all current "
            "profile data, so replacement was blocked",
        )
        self._blocked = True
        return False

    def _target_external_authority(self) -> tuple[str, Path | None] | None:
        """Return a root that a sibling-directory target backup cannot contain."""

        from opensquilla.recovery.config_patch import ConfigSnapshot, _parse_dotenv_value
        from opensquilla.recovery.errors import RecoveryError

        def configured_path(raw: object) -> Path | None:
            if not isinstance(raw, str) or not raw.strip():
                return None
            value = Path(raw).expanduser()
            return value if value.is_absolute() else self.target / value

        roots: list[tuple[str, Path]] = []
        config_path = self.target / "config.toml"
        if os.path.lexists(config_path):
            try:
                snapshot = ConfigSnapshot.capture(config_path)
                if (
                    snapshot.identity is None
                    or snapshot.identity.size > _CANDIDATE_METADATA_MAX_CONFIG_BYTES
                ):
                    raise OSError("target config is unavailable or too large")
                payload = tomllib.loads(snapshot.data.decode("utf-8"))
            except (
                OSError,
                RecoveryError,
                UnicodeDecodeError,
                tomllib.TOMLDecodeError,
            ) as exc:
                return f"target config cannot be inspected safely ({exc})", config_path
            for role, raw in (
                ("state", payload.get("state_dir")),
                ("workspace", payload.get("workspace_dir")),
            ):
                if (path := configured_path(raw)) is not None:
                    roots.append((role, path))
            attachments = payload.get("attachments")
            if isinstance(attachments, dict):
                path = configured_path(attachments.get("media_root"))
                if path is not None:
                    roots.append(("media", path))
            agents = payload.get("agents")
            if isinstance(agents, list):
                for entry in agents:
                    if not isinstance(entry, dict):
                        continue
                    path = configured_path(entry.get("workspace"))
                    if path is not None:
                        roots.append((f"agent {entry.get('id')!s} workspace", path))

        for dotenv_path in (self.target / ".env", self.target / "state" / ".env"):
            if not os.path.lexists(dotenv_path):
                continue
            try:
                snapshot = ConfigSnapshot.capture(dotenv_path)
                if snapshot.identity is None or snapshot.identity.size > _PROFILE_DOTENV_MAX_BYTES:
                    raise OSError("target dotenv is unavailable or too large")
                parsed: dict[str, str] = {}
                for line in snapshot.data.decode("utf-8").splitlines():
                    key = env_line_key(line)
                    if key not in _DOTENV_PROFILE_SCOPED_KEYS:
                        continue
                    _name, separator, raw_value = line.partition("=")
                    if separator:
                        parsed[key] = _parse_dotenv_value(
                            raw_value,
                            label=key,
                            error_type=RecoveryError,
                            stable_code="target_dotenv_unsafe",
                        )
            except (OSError, RecoveryError, UnicodeDecodeError) as exc:
                return f"target dotenv cannot be inspected safely ({exc})", dotenv_path
            for role, keys in _DOTENV_DATA_ROOT_KEYS.items():
                for key in keys:
                    path = configured_path(parsed.get(key))
                    if path is not None:
                        roots.append((f"dotenv {role}", path))
            state_home = configured_path(parsed.get("OPENSQUILLA_STATE_DIR"))
            if state_home is not None:
                roots.append(("dotenv profile home", state_home))
            config_override = configured_path(
                parsed.get("OPENSQUILLA_GATEWAY_CONFIG_PATH")
            )
            if config_override is not None:
                roots.append(("dotenv config", config_override.parent))
            profile_root = configured_path(parsed.get("OPENSQUILLA_HOME"))
            profile_name = parsed.get("OPENSQUILLA_PROFILE", "").strip()
            if profile_root is not None or profile_name:
                root = profile_root or (Path.home() / ".opensquilla" / "profiles")
                roots.append(("dotenv selected profile", root / (profile_name or "default")))

        for role, keys in _DOTENV_DATA_ROOT_KEYS.items():
            for key in keys:
                path = configured_path(os.environ.get(key))
                if path is not None:
                    roots.append((f"process {role}", path))
        process_config = configured_path(os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))
        if process_config is not None:
            roots.append(("process config", process_config.parent))

        target_absolute = self.target.absolute()
        for role, root in roots:
            try:
                lexical_inside = root.absolute().is_relative_to(target_absolute)
                resolved_inside = root.resolve(strict=False).is_relative_to(
                    self.target.resolve(strict=False)
                )
            except OSError:
                return f"{role} cannot be resolved safely", root
            if not lexical_inside or not resolved_inside:
                return f"{role} is outside the target profile", root
        return None

    def _required_disk_bytes(self) -> int:
        """Return frozen source bytes plus staging headroom without reopening paths."""

        logical_files: dict[Path, tuple[str | None, int]] = {}
        physical_files: dict[_PathIdentity, int] = {}
        # Only role snapshots authorize bytes in staging.  The broader initial
        # snapshot exists solely for bounded config/dotenv inspection.
        for snapshot in self._source_snapshots:
            for entry in snapshot.entries:
                if entry.entry_type != "file":
                    continue
                physical_files[entry.identity] = max(
                    entry.size,
                    physical_files.get(entry.identity, 0),
                )
                logical = snapshot.destination_prefix / entry.relative
                previous = logical_files.get(logical)
                if previous is None or previous[0] != entry.digest:
                    logical_files[logical] = (
                        entry.digest,
                        max(entry.size, previous[1] if previous else 0),
                    )
        physical_size = sum(physical_files.values())
        logical_size = sum(size for _digest, size in logical_files.values())
        return max(physical_size, logical_size) + _DISK_MARGIN_BYTES

    def _replace_confirmation_matches(self) -> bool:
        confirmation = _as_path(self.options.confirm_replace_target)
        return confirmation is not None and _normalized_path(confirmation) == _normalized_path(
            self.target
        )

    def _target_has_real_data(self) -> bool:
        try:
            result = self.target.lstat()
        except FileNotFoundError:
            self._target_preflight_present = False
            self._target_preflight_identity = None
            return False
        if _supported_entry_type(self.target, result) != "directory":
            raise OSError("target exists but is not a plain directory")
        self._target_preflight_present = True
        self._target_preflight_identity = _identity_from_stat(result)
        try:
            with os.scandir(self.target) as iterator:
                return next(iterator, None) is not None
        except OSError as exc:
            raise OSError("target directory cannot be enumerated") from exc

    def _check_schema_ahead(self, source: Path) -> bool:
        sessions_db = self._source_sqlite_stores().get(Path("sessions.db"))
        if sessions_db is None:
            db_path = source / "state" / "sessions.db"
            self._record("preflight/schema", db_path, None, "skipped", "no sessions.db in source")
            return False
        known = _known_migration_ids()
        if not known:
            self._note("no migration set was found for this binary")
            self._record(
                "preflight/schema",
                sessions_db,
                None,
                "error",
                "no migration set found for this binary; refusing an unverifiable import",
            )
            return True
        with tempfile.TemporaryDirectory(prefix="opensquilla-schema-inspect-") as temporary:
            try:
                copied_db = self._materialize_sqlite_bundle(
                    sessions_db,
                    Path(temporary),
                )
            except OSError as exc:
                self._record(
                    "preflight/schema",
                    sessions_db,
                    None,
                    "error",
                    f"source sessions.db could not be snapshotted safely ({exc})",
                )
                return True
            applied = _read_applied_migration_ids(copied_db)
            if applied is None:
                self._record(
                    "preflight/schema",
                    sessions_db,
                    None,
                    "error",
                    "source sessions.db could not be inspected read-only; "
                    "refusing an unverifiable import",
                )
                return True
            unknown = sorted(applied - known)
            if unknown:
                self._record(
                    "preflight/schema",
                    sessions_db,
                    None,
                    "error",
                    "source home was written by a newer OpenSquilla "
                    f"(unknown migrations: {', '.join(unknown)}); update OpenSquilla first",
                )
                return True
            self._record("preflight/schema", sessions_db, None, "skipped", "ok")
        return False

    def _source_sqlite_stores(self) -> dict[Path, Path]:
        if self._sqlite_stores_cache is not None:
            return dict(self._sqlite_stores_cache)
        candidates: dict[Path, list[Path]] = {}
        fixed_stores = {relative.relative_to("state") for relative in _SQLITE_STORES}
        state_snapshots = tuple(
            snapshot
            for snapshot in self._source_snapshots
            if snapshot.role.startswith("state:")
        )
        if self._data_roots.get("state") and not state_snapshots:
            raise OSError("stable state manifests are unavailable")
        for snapshot in state_snapshots:
            for entry in snapshot.entries:
                if entry.entry_type != "file":
                    continue
                relative = entry.relative
                is_agent_memory = (
                    len(relative.parts) == 3
                    and relative.parts[0] == "agents"
                    and relative.parts[2] == "memory.db"
                )
                if relative in fixed_stores or is_agent_memory:
                    candidates.setdefault(relative, []).append(entry.source)
        stores = {
            relative: self._select_sqlite_bundle(relative, paths)
            for relative, paths in candidates.items()
        }
        self._sqlite_stores_cache = stores
        return dict(stores)

    def _select_sqlite_bundle(self, relative: Path, candidates: list[Path]) -> Path:
        if len(candidates) == 1:
            return candidates[0]
        try:
            fingerprints = {
                candidate: self._sqlite_logical_fingerprint(candidate)
                for candidate in candidates
            }
        except (OSError, sqlite3.Error) as exc:
            self._record(
                "preflight/sqlite",
                candidates[-1],
                self.target / "state" / relative,
                "error",
                f"could not compare duplicate SQLite bundles for state/{relative}: {exc}",
            )
            self._blocked = True
            return candidates[-1]

        wal_candidates = [
            candidate
            for candidate in candidates
            if self._sqlite_wal_has_frames(candidate)
        ]
        if len(set(fingerprints.values())) == 1:
            return wal_candidates[-1] if wal_candidates else candidates[-1]

        first = candidates[0]
        first_digest = self._sqlite_bundle_entries(first)[1][""].digest
        main_files_match = all(
            self._sqlite_bundle_entries(candidate)[1][""].digest == first_digest
            for candidate in candidates[1:]
        )
        if main_files_match and len(wal_candidates) == 1:
            # The roots hold the same checkpointed database, but one bundle has
            # additional committed WAL frames. That bundle is the complete store.
            return wal_candidates[0]

        rendered = ", ".join(str(candidate) for candidate in candidates)
        self._record(
            "preflight/data-root",
            candidates[-1],
            self.target / "state" / relative,
            "error",
            f"conflicting logical SQLite stores exist in multiple state roots: {rendered}",
        )
        self._blocked = True
        return candidates[-1]

    def _sqlite_bundle_entries(
        self,
        source_db: Path,
    ) -> tuple[_SourceSnapshot, dict[str, _ManifestEntry]]:
        for snapshot in self._source_snapshots:
            if not snapshot.role.startswith("state:"):
                continue
            members = {
                suffix: entry
                for suffix in ("", *_SQLITE_DURABLE_SIDECAR_SUFFIXES)
                for entry in snapshot.entries
                if entry.entry_type == "file"
                and entry.source == source_db.with_name(source_db.name + suffix)
            }
            if "" in members:
                return snapshot, members
        raise OSError(f"SQLite bundle is absent from the frozen source manifest: {source_db}")

    def _materialize_sqlite_bundle(
        self,
        source_db: Path,
        destination_dir: Path,
    ) -> Path:
        snapshot, members = self._sqlite_bundle_entries(source_db)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination_db = destination_dir / source_db.name
        for suffix in ("", *_SQLITE_DURABLE_SIDECAR_SUFFIXES):
            entry = members.get(suffix)
            if entry is None:
                continue
            destination = destination_db.with_name(destination_db.name + suffix)
            copied_digest = _copy_snapshot_file(snapshot, entry, destination)
            if copied_digest != entry.digest:
                destination.unlink(missing_ok=True)
                raise OSError(f"SQLite bundle member changed during copy: {entry.source}")
        return destination_db

    def _sqlite_wal_has_frames(self, source_db: Path) -> bool:
        _snapshot, members = self._sqlite_bundle_entries(source_db)
        wal = members.get("-wal")
        return wal is not None and wal.size > 32

    def _sqlite_logical_fingerprint(self, source_db: Path) -> str:
        with tempfile.TemporaryDirectory(prefix="opensquilla-sqlite-compare-") as temporary:
            copied_db = self._materialize_sqlite_bundle(source_db, Path(temporary))
            connection = sqlite3.connect(
                f"{copied_db.resolve().as_uri()}?mode=ro",
                uri=True,
            )
            try:
                result = connection.execute("PRAGMA quick_check").fetchone()
                if result != ("ok",):
                    raise sqlite3.DatabaseError(f"quick_check returned {result!r}")
                digest = hashlib.sha256()
                for statement in connection.iterdump():
                    digest.update(statement.encode("utf-8", errors="surrogatepass"))
                    digest.update(b"\n")
                return digest.hexdigest()
            finally:
                connection.close()

    def _check_sqlite_integrity(self) -> bool:
        failed = False
        for relative, source_db in sorted(self._source_sqlite_stores().items()):
            try:
                with tempfile.TemporaryDirectory(
                    prefix="opensquilla-sqlite-inspect-"
                ) as temporary:
                    copied_db = self._materialize_sqlite_bundle(
                        source_db,
                        Path(temporary),
                    )
                    connection = sqlite3.connect(
                        f"{copied_db.resolve().as_uri()}?mode=ro",
                        uri=True,
                    )
                    try:
                        result = connection.execute("PRAGMA quick_check").fetchone()
                    finally:
                        connection.close()
                if result != ("ok",):
                    raise sqlite3.DatabaseError(f"quick_check returned {result!r}")
            except (OSError, sqlite3.Error) as exc:
                failed = True
                self._record(
                    "preflight/sqlite",
                    source_db,
                    self.target / "state" / relative,
                    "error",
                    f"source SQLite store state/{relative} failed integrity check: {exc}",
                )
        return failed

    def _sandbox_config_pass(self, source: Path) -> None:
        config_path = source / "config.toml"
        snapshot = self._initial_source_snapshot
        if snapshot is None:
            raise RuntimeError("initial source snapshot is unavailable")
        try:
            config_bytes = _read_snapshot_file_bytes(
                snapshot,
                Path("config.toml"),
                limit=_CANDIDATE_METADATA_MAX_CONFIG_BYTES,
            )
            if config_bytes is None:
                self._record(
                    "preflight/config",
                    config_path,
                    None,
                    "skipped",
                    "no config.toml",
                )
                return
            payload = tomllib.loads(config_bytes.decode("utf-8"))
        except (
            OSError,
            tomllib.TOMLDecodeError,
            UnicodeDecodeError,
        ) as exc:
            self._record(
                "preflight/config",
                config_path,
                None,
                "error",
                f"source config.toml could not be parsed ({exc}); import blocked",
            )
            self._blocked = True
            return
        self._raw_config_payload = payload
        self._source_config_bytes = config_bytes
        candidate = migrate_config_payload(payload, emit_diagnostics=False).payload
        errors = self._validate_config_payload(candidate)
        if errors is not None:
            extra_locs = [
                error.get("loc", ())
                for error in errors
                if error.get("type") == "extra_forbidden"
            ]
            for loc in extra_locs:
                dotted = ".".join(str(part) for part in loc) or "<root>"
                self._record(
                    "preflight/config",
                    config_path,
                    None,
                    "error",
                    f"unknown config key {dotted} cannot be preserved losslessly; "
                    "the complete profile import was blocked without changing it",
                )
            if extra_locs:
                self._blocked = True
                return
        if errors is None:
            agent_id_error = self._canonicalize_agent_ids(candidate)
            if agent_id_error is not None:
                self._record(
                    "preflight/config",
                    config_path,
                    None,
                    "error",
                    f"source config.toml has unsafe agent ids ({agent_id_error}); import blocked",
                )
                self._blocked = True
                return
            self._config_payload = candidate
            self._record("preflight/config", config_path, None, "skipped", "ok")
            return
        summary = "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', '')}"
            for error in errors[:3]
        )
        self._record(
            "preflight/config",
            config_path,
            None,
            "error",
            "source config.toml does not validate against the current schema "
            f"({summary}); import blocked",
        )
        self._blocked = True

    def _canonicalize_agent_ids(self, payload: dict[str, Any]) -> str | None:
        """Reject agent ids that cannot name one unambiguous staging directory.

        Runtime validation normalizes ids (for example ``Research`` becomes
        ``research``).  Whole-profile import cannot silently perform that
        rename: the source may already contain either spelling, and choosing
        one would be a merge.  Requiring the on-disk spelling to already be
        canonical keeps the config path and copied workspace path identical.
        """

        agents = payload.get("agents")
        if not isinstance(agents, list):
            return None
        try:
            validated = GatewayConfig(**payload)
        except ValidationError:
            return "agent ids could not be validated"
        if len(validated.agents) != len(agents):
            return "agent list changed during validation"
        seen: set[str] = set()
        for index, (entry, normalized_entry) in enumerate(
            zip(agents, validated.agents, strict=True)
        ):
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                return f"agents.{index}.id is not a string"
            raw = str(entry["id"]).strip()
            if not raw or _SAFE_RAW_AGENT_ID_RE.fullmatch(raw) is None:
                return f"agents.{index}.id is path-like or contains unsafe characters"
            canonical = normalized_entry.id
            if canonical in seen:
                return f"agents.{index}.id collides with canonical id {canonical}"
            seen.add(canonical)
            if raw != canonical:
                return (
                    f"agents.{index}.id must already use canonical id {canonical}; "
                    "automatic agent workspace renames are not safe"
                )
        return None

    def _inspect_profile_dotenv(self, source: Path) -> None:
        """Discover and neutralize profile/data selectors without reading secrets.

        Only the small allowlist of path-bearing keys is parsed. Other lines
        (including provider credentials) remain opaque bytes until the source
        manifest copies the file into staging.
        """

        if self._blocked:
            return
        from opensquilla.paths import is_valid_profile_name
        from opensquilla.recovery.config_patch import _parse_dotenv_value
        from opensquilla.recovery.errors import RecoveryError

        snapshot = self._initial_source_snapshot
        if snapshot is None:
            raise RuntimeError("initial source snapshot is unavailable")
        dotenvs = (
            (source / ".env", Path(".env")),
            (source / "state" / ".env", Path("state/.env")),
        )
        for dotenv_path, destination in dotenvs:
            try:
                dotenv_bytes = _read_snapshot_file_bytes(
                    snapshot,
                    destination,
                    limit=_PROFILE_DOTENV_MAX_BYTES,
                )
            except OSError as exc:
                self._record(
                    "preflight/env",
                    dotenv_path,
                    None,
                    "error",
                    f"profile dotenv is not a plain stable file ({exc}); import blocked",
                )
                self._blocked = True
                continue
            if dotenv_bytes is None:
                continue
            try:
                text = dotenv_bytes.decode("utf-8")
            except UnicodeDecodeError:
                self._record(
                    "preflight/env",
                    dotenv_path,
                    None,
                    "error",
                    "profile dotenv is not valid UTF-8; import blocked without changing it",
                )
                self._blocked = True
                continue
            parsed: dict[str, str] = {}
            present: set[str] = set()
            try:
                for line in text.splitlines():
                    key = env_line_key(line)
                    if key not in _DOTENV_PROFILE_SCOPED_KEYS:
                        continue
                    present.add(key)
                    _name, separator, raw_value = line.partition("=")
                    if not separator:
                        continue
                    parsed[key] = _parse_dotenv_value(
                        raw_value,
                        label=key,
                        error_type=RecoveryError,
                        stable_code="profile_import_dotenv_unsafe",
                    )
            except RecoveryError as exc:
                self._record(
                    "preflight/env",
                    dotenv_path,
                    None,
                    "error",
                    f"profile dotenv path override is not safely parseable ({exc})",
                )
                self._blocked = True
                continue
            if not present:
                continue
            self._dotenv_keys_to_remove.setdefault(destination, set()).update(present)

            state_home = parsed.get("OPENSQUILLA_STATE_DIR", "").strip()
            profile_root = parsed.get("OPENSQUILLA_HOME", "").strip()
            profile_name = parsed.get("OPENSQUILLA_PROFILE", "").strip()
            config_path = parsed.get("OPENSQUILLA_GATEWAY_CONFIG_PATH", "").strip()
            if state_home:
                selected_home = Path(state_home).expanduser()
                if not selected_home.is_absolute() or not _same_path(selected_home, source):
                    self._record(
                        "preflight/env",
                        dotenv_path,
                        None,
                        "error",
                        "OPENSQUILLA_STATE_DIR selects another live profile; import blocked",
                    )
                    self._blocked = True
            elif profile_root or profile_name:
                selected_name = profile_name or "default"
                if not is_valid_profile_name(selected_name):
                    self._record(
                        "preflight/env",
                        dotenv_path,
                        None,
                        "error",
                        "OPENSQUILLA_PROFILE is invalid; import blocked",
                    )
                    self._blocked = True
                else:
                    root = (
                        Path(profile_root).expanduser()
                        if profile_root
                        else Path.home() / ".opensquilla" / "profiles"
                    )
                    selected_home = root / selected_name
                    if not root.is_absolute() or not _same_path(selected_home, source):
                        self._record(
                            "preflight/env",
                            dotenv_path,
                            None,
                            "error",
                            "OPENSQUILLA_HOME/PROFILE selects another live profile; "
                            "import blocked",
                        )
                        self._blocked = True
            if config_path:
                selected_config = Path(config_path).expanduser()
                if not selected_config.is_absolute() or not _same_path(
                    selected_config,
                    source / "config.toml",
                ):
                    self._record(
                        "preflight/env",
                        dotenv_path,
                        None,
                        "error",
                        "OPENSQUILLA_GATEWAY_CONFIG_PATH selects an external config; "
                        "import blocked",
                    )
                    self._blocked = True

            for role, keys in _DOTENV_DATA_ROOT_KEYS.items():
                for key in keys:
                    value = parsed.get(key, "").strip()
                    if value and value not in self._dotenv_data_root_values[role]:
                        self._dotenv_data_root_values[role].append(value)
            self.config_transforms.extend(
                f"removed profile-scoped dotenv selector {key} after snapshot/rebase"
                for key in sorted(present)
            )

    def _plan_data_roots(self) -> None:
        """Discover canonical and configured data roots before path pins are dropped."""
        source = self.source
        assert source is not None
        payload = self._raw_config_payload or {}
        configured_values: dict[str, object] = {
            "state": payload.get("state_dir"),
            "workspace": payload.get("workspace_dir"),
            "media": None,
        }
        attachments = payload.get("attachments")
        if isinstance(attachments, dict):
            configured_values["media"] = attachments.get("media_root")
        configured = {
            name: self._configured_path(value)
            for name, value in configured_values.items()
        }
        legacy_internal = {
            name: self._is_proven_legacy_internal_pin(configured_values[name], name)
            for name in configured
        }
        dotenv_configured: dict[str, list[tuple[str, Path, bool]]] = {
            name: [
                (
                    value,
                    path,
                    self._is_proven_legacy_internal_pin(value, name),
                )
                for value in self._dotenv_data_root_values[name]
                if (path := self._configured_path(value)) is not None
            ]
            for name in ("state", "workspace", "media")
        }

        for name in ("state", "workspace", "media"):
            roots: list[Path] = []
            canonical = source / name
            try:
                if _is_plain_directory(canonical):
                    roots.append(canonical)
            except OSError as exc:
                self._record(
                    "preflight/data-root",
                    canonical,
                    self.target / name,
                    "error",
                    f"canonical {name} root is unsafe ({exc})",
                )
                self._blocked = True
            explicit = configured[name]
            configured_candidates = [
                ("config.toml", explicit, legacy_internal[name])
            ] if explicit is not None else []
            configured_candidates.extend(
                ("profile dotenv", path, is_legacy)
                for _value, path, is_legacy in dotenv_configured[name]
            )
            for origin, candidate, is_legacy in configured_candidates:
                if is_legacy:
                    continue
                try:
                    if _is_plain_directory(candidate):
                        roots.append(candidate)
                except OSError as exc:
                    self._record(
                        "preflight/data-root",
                        candidate,
                        self.target / name,
                        "error",
                        f"{origin} {name} root is unsafe ({exc})",
                    )
                    self._blocked = True
            if name == "media" and explicit is None:
                state_root = configured["state"]
                if state_root is not None and not legacy_internal["state"]:
                    for candidate in (state_root.parent / "media", state_root / "media"):
                        try:
                            if _is_plain_directory(candidate):
                                roots.append(candidate)
                        except OSError as exc:
                            self._record(
                                "preflight/data-root",
                                candidate,
                                self.target / name,
                                "error",
                                f"derived media root is unsafe ({exc})",
                            )
                            self._blocked = True
            unique: list[Path] = []
            seen: set[Path] = set()
            for root in roots:
                resolved = root.resolve(strict=False)
                if resolved in seen:
                    continue
                seen.add(resolved)
                if _path_is_relative_to_lexical(root, source / "code-task"):
                    self._record(
                        "preflight/data-root",
                        root,
                        self.target / name,
                        "error",
                        f"configured {name} root points inside source code-task; "
                        "machine-local code-task runs are not portable",
                        {"stable_code": "configured_code_task_root"},
                    )
                    self._blocked = True
                    continue
                # Target overlap is the publication hazard with the narrowest,
                # most actionable diagnosis. A broad configured root can contain
                # both source and target; report the target hazard first instead
                # of obscuring it as only a recursive-source problem.
                if _paths_overlap(root, self.target):
                    self._record(
                        "preflight/data-root",
                        root,
                        self.target / name,
                        "error",
                        f"configured {name} root overlaps the target home; "
                        "refusing a recursive import",
                    )
                    self._blocked = True
                    continue
                try:
                    contains_profile = source.resolve(strict=False).is_relative_to(resolved)
                except OSError:
                    contains_profile = False
                if contains_profile:
                    self._record(
                        "preflight/data-root",
                        root,
                        self.target / name,
                        "error",
                        f"configured {name} root contains the source profile; "
                        "refusing a recursive import",
                    )
                    self._blocked = True
                    continue
                unique.append(root)
            self._data_roots[name] = unique

        for name in ("state", "workspace", "media"):
            explicit = configured[name]
            try:
                canonical_exists = _is_plain_directory(source / name)
            except OSError:
                canonical_exists = False
            if legacy_internal[name] and not canonical_exists:
                self._record(
                    "preflight/data-root",
                    explicit,
                    self.target / name,
                    "error",
                    f"legacy internal {name} pin was recognized, but the source "
                    f"{name} directory is missing",
                )
                self._blocked = True
                continue
            try:
                explicit_is_directory = (
                    explicit is not None and _is_plain_directory(explicit)
                )
            except OSError:
                explicit_is_directory = False
            if explicit is None or legacy_internal[name] or explicit_is_directory:
                continue
            reason = (
                f"configured {name} directory does not exist"
                if not explicit.exists()
                else f"configured {name} path is not a directory"
            )
            self._record(
                "preflight/data-root",
                explicit,
                self.target / name,
                "error",
                f"{reason}; refusing to drop its config pin",
            )
            self._blocked = True

            # Continue checking dotenv roots as independent profile authority.
        for name, entries in dotenv_configured.items():
            for _value, explicit, is_legacy in entries:
                if is_legacy:
                    try:
                        canonical_exists = _is_plain_directory(source / name)
                    except OSError:
                        canonical_exists = False
                    if canonical_exists:
                        continue
                else:
                    try:
                        if _is_plain_directory(explicit):
                            continue
                    except OSError:
                        pass
                reason = (
                    f"dotenv-configured {name} directory does not exist"
                    if not os.path.lexists(explicit)
                    else f"dotenv-configured {name} path is not a plain directory"
                )
                self._record(
                    "preflight/data-root",
                    explicit,
                    self.target / name,
                    "error",
                    f"{reason}; refusing to remove its profile dotenv selector",
                )
                self._blocked = True

        normalized_payload = self._config_payload or {}
        agents = normalized_payload.get("agents")
        if isinstance(agents, list):
            for entry in agents:
                if not isinstance(entry, dict):
                    continue
                raw_id = entry.get("id")
                raw_workspace = entry.get("workspace")
                if not isinstance(raw_id, str) or not isinstance(raw_workspace, str):
                    continue
                agent_id = raw_id.strip()
                if not agent_id or agent_id == "main" or not raw_workspace.strip():
                    continue
                configured_workspace = self._configured_path(raw_workspace)
                if configured_workspace is None:
                    continue
                canonical_agent = source / "workspace" / "agents" / agent_id
                try:
                    canonical_agent_exists = _is_plain_directory(canonical_agent)
                except OSError:
                    canonical_agent_exists = False
                if (
                    self._is_proven_agent_workspace_pin(raw_workspace, agent_id)
                    and canonical_agent_exists
                ):
                    continue
                try:
                    configured_workspace_exists = _is_plain_directory(
                        configured_workspace
                    )
                except OSError:
                    configured_workspace_exists = False
                if not configured_workspace_exists:
                    self._record(
                        "preflight/data-root",
                        configured_workspace,
                        self.target / "workspace" / "agents" / agent_id,
                        "error",
                        f"configured workspace for agent {agent_id} is missing or unreadable",
                    )
                    self._blocked = True
                    continue
                if _path_is_relative_to_lexical(configured_workspace, source / "code-task"):
                    self._record(
                        "preflight/data-root",
                        configured_workspace,
                        self.target / "workspace" / "agents" / agent_id,
                        "error",
                        f"configured workspace for agent {agent_id} points inside "
                        "source code-task; machine-local code-task runs are not portable",
                        {"stable_code": "configured_code_task_root"},
                    )
                    self._blocked = True
                    continue
                try:
                    contains_profile = source.resolve(strict=False).is_relative_to(
                        configured_workspace.resolve(strict=False)
                    )
                except OSError:
                    contains_profile = False
                if contains_profile:
                    self._record(
                        "preflight/data-root",
                        configured_workspace,
                        self.target / "workspace" / "agents" / agent_id,
                        "error",
                        f"configured workspace for agent {agent_id} contains the source profile",
                    )
                    self._blocked = True
                    continue
                if _paths_overlap(configured_workspace, self.target):
                    self._record(
                        "preflight/data-root",
                        configured_workspace,
                        self.target / "workspace" / "agents" / agent_id,
                        "error",
                        f"configured workspace for agent {agent_id} overlaps the target home",
                    )
                    self._blocked = True
                    continue
                try:
                    inside_profile = configured_workspace.resolve(strict=False).is_relative_to(
                        (source / "workspace").resolve(strict=False)
                    )
                except OSError:
                    inside_profile = False
                if not inside_profile:
                    self._agent_workspace_roots[agent_id] = configured_workspace

    def _is_proven_legacy_internal_pin(self, value: object, role: str) -> bool:
        if not isinstance(value, str) or not _path_pin_is_absolute(value):
            return False
        source = self.source
        assert source is not None
        configured = Path(value).expanduser()
        expected = [source / role]
        if role in {"workspace", "media"}:
            # Desktop through RC2 passed H/state as the Python home, so its
            # automatically-derived workspace/media could carry this one-level
            # nested absolute pin.
            expected.append(source / "state" / role)
        if any(_same_path(configured, candidate) for candidate in expected):
            return True

        # A path that exists somewhere else is user data, even when a directory
        # happens to be named `.opensquilla`. Only use historical spelling as
        # proof for a stale cross-platform pin that cannot be resolved here.
        if configured.exists():
            return False
        normalized = value.strip().replace("\\", "/").rstrip("/").lower()
        if normalized.startswith("//"):
            # UNC/network roots were never OpenSquilla's automatically selected
            # CLI home; treat them as external and require a complete snapshot.
            return False
        suffix = re.escape(role)
        nested = rf"(?:/state)?/{suffix}" if role in {"workspace", "media"} else rf"/{suffix}"
        return bool(
            re.fullmatch(
                rf"[a-z]:/users/[^/]+/\.opensquilla(?:/profiles/[^/]+)?{nested}",
                normalized,
            )
            or re.fullmatch(
                rf"/(?:home|users)/[^/]+/\.opensquilla(?:/profiles/[^/]+)?{nested}",
                normalized,
            )
            or re.fullmatch(
                rf"/root/\.opensquilla(?:/profiles/[^/]+)?{nested}",
                normalized,
            )
            or re.fullmatch(
                rf"[a-z]:/.+/opensquilla/portable/[^/]+{nested}",
                normalized,
            )
            or re.fullmatch(
                rf"[a-z]:/users/[^/]+/appdata/(?:roaming|local)/opensquilla/opensquilla{nested}",
                normalized,
            )
            or re.fullmatch(
                rf"/users/[^/]+/library/application support/opensquilla/opensquilla{nested}",
                normalized,
            )
        )

    def _is_proven_agent_workspace_pin(self, value: str, agent_id: str) -> bool:
        if not _path_pin_is_absolute(value):
            return False
        source = self.source
        assert source is not None
        configured = Path(value).expanduser()
        expected = source / "workspace" / "agents" / agent_id
        if _same_path(configured, expected):
            return True
        if configured.exists():
            return False
        normalized = value.strip().replace("\\", "/").rstrip("/").lower()
        suffix = f"/workspace/agents/{agent_id.lower()}"
        if normalized.startswith("//") or not normalized.endswith(suffix):
            return False
        return bool(
            re.fullmatch(
                rf"[a-z]:/users/[^/]+/\.opensquilla(?:/profiles/[^/]+)?{re.escape(suffix)}",
                normalized,
            )
            or re.fullmatch(
                rf"/(?:home|users)/[^/]+/\.opensquilla(?:/profiles/[^/]+)?{re.escape(suffix)}",
                normalized,
            )
            or re.fullmatch(
                rf"/root/\.opensquilla(?:/profiles/[^/]+)?{re.escape(suffix)}",
                normalized,
            )
            or re.fullmatch(
                rf"[a-z]:/.+/opensquilla/portable/[^/]+{re.escape(suffix)}",
                normalized,
            )
            or re.fullmatch(
                rf"[a-z]:/users/[^/]+/appdata/(?:roaming|local)/opensquilla/opensquilla{re.escape(suffix)}",
                normalized,
            )
        )

    def _configured_path(self, value: object) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value).expanduser()
        if _path_pin_is_absolute(value):
            return path
        source = self.source
        assert source is not None
        return source / path

    def _validate_config_payload(self, payload: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Return ``None`` when the payload validates, else pydantic error dicts."""
        try:
            GatewayConfig(**payload)
        except ValidationError as exc:
            return [dict(error) for error in exc.errors()]
        except Exception as exc:  # noqa: BLE001 - sandbox validation is advisory
            return [{"type": "unexpected", "loc": (), "msg": str(exc)}]
        return None

    # ------------------------------------------------------------------
    # Planning: entries, config transforms, secret relocation
    # ------------------------------------------------------------------

    def _collect_entries(self) -> list[Path]:
        source = self.source
        assert source is not None
        entries: list[Path] = []
        for entry in sorted(source.iterdir()):
            if entry.name in _EXCLUDED_AUTHORITY_NAMES:
                self._record(
                    "home-entry",
                    entry,
                    None,
                    "skipped",
                    "source layout/import/recovery authority is not copied",
                )
                continue
            if entry.name in _EXCLUDED_TOP_LEVEL_DIRS:
                self._record(
                    "home-entry",
                    entry,
                    None,
                    "skipped",
                    "nested profile or migration/recovery authority is not imported",
                )
                continue
            if entry.name in _EXCLUDED_NONPORTABLE_TOP_LEVEL_DIRS:
                self._record(
                    "home-entry",
                    entry,
                    None,
                    "skipped",
                    "machine-local code-task runs remain unchanged in the source profile",
                )
                continue
            entries.append(entry)
        return entries

    def _plan_config_transforms(self) -> None:
        payload = self._config_payload
        if payload is None:
            return
        for key in ("state_dir", "workspace_dir"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                payload.pop(key)
                self.config_transforms.append(
                    f"rebased {key} into the imported profile; the default "
                    "re-derives under the new home"
                )
        attachments = payload.get("attachments")
        if isinstance(attachments, dict):
            media_root = attachments.get("media_root")
            if (
                isinstance(media_root, str)
                and media_root.strip()
            ):
                attachments.pop("media_root")
                self.config_transforms.append(
                    "rebased attachments.media_root into the imported profile; "
                    "the default re-derives under the new home"
                )
        agents = payload.get("agents")
        if isinstance(agents, list):
            for entry in agents:
                if not isinstance(entry, dict):
                    continue
                agent_id = entry.get("id")
                workspace = entry.get("workspace")
                if (
                    not isinstance(agent_id, str)
                    or not agent_id.strip()
                    or agent_id.strip() == "main"
                    or not isinstance(workspace, str)
                    or not workspace.strip()
                ):
                    continue
                destination = self.target / "workspace" / "agents" / agent_id.strip()
                entry["workspace"] = str(destination)
                self.config_transforms.append(
                    f"rebased agents.{agent_id.strip()}.workspace into the imported profile"
                )
        if payload.get("port") == _LEGACY_DEFAULT_PORT:
            payload["port"] = _CURRENT_DEFAULT_PORT
            self.config_transforms.append(
                f"port: {_LEGACY_DEFAULT_PORT} -> {_CURRENT_DEFAULT_PORT} "
                "(legacy default gateway port)"
            )
        self._plan_secret_relocations(payload)

    def _plan_secret_relocations(self, payload: dict[str, Any]) -> None:
        llm = payload.get("llm")
        if isinstance(llm, dict):
            provider = llm.get("provider")
            env_key = (
                _provider_env_key(provider) if isinstance(provider, str) else ""
            ) or _FALLBACK_LLM_ENV_KEY
            self._relocate_secret(llm, "api_key", "llm.api_key", env_key)
        profiles = payload.get("llm_profiles")
        if isinstance(profiles, dict):
            for profile_id, profile in profiles.items():
                if not isinstance(profile, dict):
                    continue
                profile_env = _provider_env_key(str(profile_id)) or _fallback_profile_env_key(
                    str(profile_id)
                )
                self._relocate_secret(
                    profile, "api_key", f"llm_profiles.{profile_id}.api_key", profile_env
                )
        audio = payload.get("audio")
        providers = audio.get("providers") if isinstance(audio, dict) else None
        elevenlabs = providers.get("elevenlabs") if isinstance(providers, dict) else None
        if isinstance(elevenlabs, dict):
            self._relocate_secret(
                elevenlabs,
                "api_key",
                "audio.providers.elevenlabs.api_key",
                _ELEVENLABS_ENV_KEY,
            )

    def _relocate_secret(
        self, section: dict[str, Any], key: str, config_path: str, env_key: str
    ) -> None:
        value = section.get(key)
        if not isinstance(value, str) or not value.strip():
            return
        self._env_additions[env_key] = value.strip()
        section["api_key_env"] = env_key
        section.pop(key, None)
        # Never the value: the report shape is a pinned, redaction-guaranteed
        # contract covered by the wire-shape tests.
        self.secret_relocations.append(
            {"config_path": config_path, "env_key": env_key, "moved": True}
        )
        self.config_transforms.append(f"moved {config_path} to .env as {env_key}")

    # ------------------------------------------------------------------
    # Apply: staged copy, transforms, journaled commit
    # ------------------------------------------------------------------

    def _apply(self, entries: list[Path]) -> None:
        staging = self.target.parent / (
            f".{self.target.name}.profile-staging.{self.transaction_id}"
        )
        try:
            staging.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            self._record(
                "apply", self.source, self.target, "error", f"could not create staging dir: {exc}"
            )
            return
        staging_created_identity = _path_identity_payload(staging)
        try:
            self._copy_source_snapshots(staging)
            for entry in entries:
                if entry.name in self._data_roots:
                    continue
                self._record("home-entry", entry, self.target / entry.name, "migrated")
            for name, roots in self._data_roots.items():
                for root in roots:
                    self._record("data-root", root, self.target / name, "migrated")
            for agent_id, root in sorted(self._agent_workspace_roots.items()):
                self._record(
                    "data-root",
                    root,
                    self.target / "workspace" / "agents" / agent_id,
                    "migrated",
                )
            self._snapshot_sqlite_stores(staging)
            self._transform_staged_config(staging)
            self._write_staged_env(staging)
            staged_scheduler = staging / "state" / "scheduler.db"
            if staged_scheduler.is_file():
                self._pause_scheduler_jobs(staged_scheduler)
            if self._has_error():
                raise OSError("one or more staged migration transforms failed")
            # Seed and lock the candidate's canonical state before publication.
            # The open lock inode travels with staging -> target, so an RC3 or
            # older gateway cannot enter the newly-published profile during the
            # validation/receipt window. If an old target is parked, its own
            # outer compatibility lease remains held at the backup path too.
            candidate_state = staging / "state"
            candidate_state.mkdir(mode=0o700, exist_ok=True)
            candidate_lock = LegacyGatewayLock(
                staging,
                state_roots=(candidate_state,),
                create_if_missing=True,
            )
            # A source lock that was absent at preflight must never be created
            # by the importer. Immediately before publication, acquire every
            # lock that now exists in existing-only mode, then bracket the last
            # complete source rescan with explicit checks for the excluded PID
            # and lock paths. Appearance alone is treated as a source change,
            # even when the newly-created lock is currently unlocked.
            source = self.source
            assert source is not None
            final_source_lock = LegacyGatewayLock(
                source,
                state_roots=(item.root for item in self._source_gateway_authority),
                create_if_missing=False,
            )
            with candidate_lock:
                with final_source_lock:
                    self._verify_source_still_stable(final_source_lock)
                    self._commit(staging, final_source_lock)
        except (OSError, RuntimeError) as exc:
            apply_error = exc
            if self._source_gateway_authority_is_no_longer_stable():
                apply_error = OSError(
                    "source legacy gateway authority changed during import; "
                    "publication cancelled"
                )
            if (
                not self._committed
                and not os.path.lexists(_commit_journal_path(self.target))
                and _object_identity_matches(staging, staging_created_identity)
            ):
                with contextlib.suppress(OSError, RuntimeError):
                    _remove_matching_staging(staging, staging_created_identity)
            log.error(
                "opensquilla_home_migration.apply_failed",
                source=str(self.source),
                target=str(self.target),
                error=str(apply_error),
            )
            self._record(
                "apply",
                self.source,
                self.target,
                "error",
                f"import failed before completion: {apply_error}; "
                "the transaction was not completed",
                details=_source_snapshot_failure_details(
                    apply_error,
                    fallback_code="migration_apply_failed",
                ),
            )
            if not self._committed:
                self._wrote_output_dir = False

    def _validate_data_root_conflicts(self, name: str, roots: list[Path]) -> None:
        del roots  # The frozen snapshots are the only conflict authority.
        seen: dict[Path, _ManifestEntry] = {}
        sqlite_bundle_members: set[Path] = set()
        if name == "state":
            for relative in self._source_sqlite_stores():
                sqlite_bundle_members.update(
                    relative.with_name(relative.name + suffix)
                    for suffix in ("", *_SQLITE_DURABLE_SIDECAR_SUFFIXES)
                )
        snapshots = sorted(
            (
                snapshot
                for snapshot in self._source_snapshots
                if snapshot.role.startswith(f"{name}:")
            ),
            key=lambda snapshot: snapshot.role,
        )
        for snapshot in snapshots:
            for entry in snapshot.entries:
                relative = entry.relative
                if name == "state" and relative.name in _EXCLUDED_STATE_FILES:
                    continue
                if entry.entry_type == "file" and relative in sqlite_bundle_members:
                    continue
                previous = seen.get(relative)
                if previous is None:
                    seen[relative] = entry
                    continue
                same_entry = (
                    previous.entry_type == entry.entry_type == "directory"
                    or (
                        previous.entry_type == entry.entry_type == "file"
                        and previous.digest == entry.digest
                    )
                    or (
                        previous.entry_type == entry.entry_type == "symlink"
                        and previous.link_target == entry.link_target
                    )
                )
                if same_entry:
                    continue
                self._record(
                    "preflight/data-root",
                    entry.source,
                    self.target / name / relative,
                    "error",
                    f"conflicting {name} entries exist in multiple source roots: "
                    f"{previous.source} and {entry.source}",
                )
                raise OSError(f"conflicting {name} source roots")

    @staticmethod
    def _unlink_staged_file_for_replacement(path: Path) -> None:
        """Remove a copied staging file without changing its read-only source."""
        try:
            result = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(result.st_mode):
            raise OSError(f"staged SQLite path is not a regular file: {path}")
        try:
            os.chmod(path, result.st_mode | stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Some filesystems reject chmod even when the containing directory
            # still permits unlinking. Let unlink remain the authoritative gate.
            pass
        path.unlink(missing_ok=True)

    def _snapshot_sqlite_stores(self, staging: Path) -> None:
        """Create consistent WAL-aware SQLite snapshots and validate every store."""
        for relative, source_db in sorted(self._source_sqlite_stores().items()):
            destination = staging / "state" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _snapshot, bundle_entries = self._sqlite_bundle_entries(source_db)
            source_mode = bundle_entries[""].mode
            temporary = destination.with_name(
                f".{destination.name}.snapshot-{self.timestamp}.tmp"
            )
            temporary.unlink(missing_ok=True)
            try:
                with tempfile.TemporaryDirectory(
                    prefix="opensquilla-sqlite-snapshot-"
                ) as inspection_dir:
                    copied_db = self._materialize_sqlite_bundle(
                        source_db,
                        Path(inspection_dir),
                    )
                    source_connection = sqlite3.connect(
                        f"{copied_db.resolve().as_uri()}?mode=ro",
                        uri=True,
                    )
                    try:
                        target_connection = sqlite3.connect(temporary)
                        try:
                            source_connection.backup(target_connection)
                            result = target_connection.execute("PRAGMA quick_check").fetchone()
                            if result != ("ok",):
                                raise sqlite3.DatabaseError(
                                    f"quick_check failed for {relative}: {result!r}"
                                )
                        finally:
                            target_connection.close()
                    finally:
                        source_connection.close()
            except (OSError, sqlite3.Error) as exc:
                temporary.unlink(missing_ok=True)
                self._record(
                    "sqlite",
                    source_db,
                    self.target / "state" / relative,
                    "error",
                    f"could not create a consistent snapshot for state/{relative}: {exc}",
                )
                raise OSError(f"sqlite snapshot failed for state/{relative}") from exc

            self._unlink_staged_file_for_replacement(destination)
            for suffix in _SQLITE_ALL_SIDECAR_SUFFIXES:
                self._unlink_staged_file_for_replacement(
                    destination.with_name(destination.name + suffix)
                )
            os.replace(_ext(temporary), _ext(destination))
            try:
                os.chmod(destination, (source_mode & 0o777) | 0o600)
            except OSError:
                pass
            self._record(
                "sqlite",
                source_db,
                self.target / "state" / relative,
                "migrated",
                "consistent snapshot verified",
            )

    def _transform_staged_config(self, staging: Path) -> None:
        staged_config = staging / "config.toml"
        if not staged_config.is_file():
            return
        payload = self._config_payload
        original_payload = self._raw_config_payload
        source_bytes = self._source_config_bytes
        if payload is None or original_payload is None or source_bytes is None:
            raise OSError("validated source config is unavailable")
        try:
            from opensquilla.migration.lossless_toml import patch_import_config

            patched = patch_import_config(source_bytes, original_payload, payload)
            staged_config.write_bytes(patched)
            os.chmod(staged_config, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            self._record(
                "config",
                staged_config,
                self.target / "config.toml",
                "error",
                f"could not losslessly patch the transformed config ({exc}); import blocked",
            )
            raise OSError("could not losslessly patch transformed config") from exc
        source = self.source
        assert source is not None
        self._record(
            "config",
            source / "config.toml",
            self.target / "config.toml",
            "migrated",
            details={"transforms": list(self.config_transforms)},
        )

    def _write_staged_env(self, staging: Path) -> None:
        destinations = set(self._dotenv_keys_to_remove)
        if self._env_additions or (staging / ".env").is_file():
            destinations.add(Path(".env"))
        for relative in sorted(destinations, key=lambda value: value.as_posix()):
            env_path = staging / relative
            existing_lines = (
                env_path.read_text(encoding="utf-8").splitlines()
                if env_path.is_file()
                else []
            )
            removed = self._dotenv_keys_to_remove.get(relative, set())
            filtered = [
                line for line in existing_lines if env_line_key(line) not in removed
            ]
            additions = self._env_additions if relative == Path(".env") else {}
            if not removed and not additions:
                if env_path.is_file():
                    os.chmod(env_path, 0o600)
                continue
            lines = merge_env_lines(filtered, additions)
            write_secret_env_file(env_path, lines)
            self._record(
                "env",
                env_path,
                self.target / relative,
                "migrated",
                details={
                    "env_keys": sorted(additions),
                    "removed_path_keys": sorted(removed),
                },
            )

    def _pause_scheduler_jobs(self, staged_db: Path) -> None:
        # The source remains read-only and the canonical candidate database is
        # still unpublished. A failed pause discards/retains only that staging
        # transaction for recovery; report directories must never contain a
        # second database copy with chats, memory, or approval state.
        try:
            connection = sqlite3.connect(staged_db)
            try:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(scheduler_jobs)")
                }
                if not columns:
                    self._record(
                        "scheduler", staged_db, None, "skipped", "no scheduler_jobs table"
                    )
                    return
                if "enabled" in columns:
                    connection.execute("UPDATE scheduler_jobs SET enabled = 0")
                else:
                    # Pre-seeding the column at 0 wins over JobStore's later
                    # conditional add (which defaults to 1), so every imported
                    # job arrives paused.
                    connection.execute(
                        "ALTER TABLE scheduler_jobs "
                        "ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0"
                    )
                rows = connection.execute(
                    "SELECT id, name, cron_expr FROM scheduler_jobs"
                ).fetchall()
                connection.commit()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            self._record(
                "scheduler", staged_db, None, "error", f"could not pause scheduler jobs: {exc}"
            )
            raise OSError("could not pause imported scheduler jobs") from exc
        self.paused_jobs = [
            {"id": row[0], "name": row[1], "cron_expr": row[2]} for row in rows
        ]
        self._record(
            "scheduler",
            staged_db,
            self.target / "state" / "scheduler.db",
            "migrated",
            f"paused {len(self.paused_jobs)} imported scheduler job(s)",
        )

    def _read_scheduler_jobs(self, db_path: Path) -> list[dict[str, Any]] | None:
        """Read the (id, name, cron_expr) job rows read-only for the dry-run preview."""
        if not db_path.is_file():
            return None
        try:
            with tempfile.TemporaryDirectory(prefix="opensquilla-sqlite-inspect-") as temporary:
                copied_db = _copy_sqlite_bundle(db_path, Path(temporary))
                connection = sqlite3.connect(
                    f"{copied_db.resolve().as_uri()}?mode=ro",
                    uri=True,
                )
                try:
                    columns = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(scheduler_jobs)")
                    }
                    if not columns:
                        return None
                    rows = connection.execute(
                        "SELECT id, name, cron_expr FROM scheduler_jobs"
                    ).fetchall()
                finally:
                    connection.close()
        except (OSError, sqlite3.Error):
            return None
        return [{"id": row[0], "name": row[1], "cron_expr": row[2]} for row in rows]

    def _commit(
        self,
        staging: Path,
        final_source_lock: LegacyGatewayLock,
    ) -> None:
        from opensquilla.recovery import AtomicStateUnknownError, RecoveryError
        from opensquilla.recovery.config_patch import ConfigSnapshot

        self.target.parent.mkdir(parents=True, exist_ok=True)
        backup = self.target.with_name(
            f"{self.target.name}.backup.{self.transaction_id}"
        )
        journal = _commit_journal_path(self.target)
        journal_snapshot = ConfigSnapshot.capture(journal)
        if journal_snapshot.identity is not None:
            raise OSError("replacement journal appeared after preflight")
        target_existed = self._target_preflight_present
        target_had_real_data = self._target_had_real_data
        target_was_empty = target_existed and not target_had_real_data
        original_target_identity = (
            _path_identity_payload(self.target)
            if self._target_preflight_present
            else None
        )
        source = self.source
        assert source is not None
        journal_payload: dict[str, Any] = {
            "schema_version": 1,
            "operation": "profile-import",
            "transaction_id": self.transaction_id,
            "source": _normalized_path(source),
            "source_kind": self.kind,
            "target": _normalized_path(self.target),
            "staging": _normalized_path(staging),
            "backup": _normalized_path(backup),
            "phase": "prepared",
            "target_existed": target_existed,
            "target_had_real_data": target_had_real_data,
            "target_was_empty": target_was_empty,
            "identities": {
                "source": _path_identity_payload(source),
                "original_target": original_target_identity,
                "staging": None,
                # A no-replace move preserves the directory identity. Recording
                # it before the move closes the crash window between parking
                # the target and persisting phase=target_parked.
                "backup": original_target_identity,
                "candidate": None,
            },
        }
        backup_item: ItemResult | None = None
        if target_existed:
            self._record(
                "backup",
                self.target,
                backup,
                "migrated",
                "complete previous target home backup retained for rollback",
            )
            backup_item = self.items[-1]

        target_parked = False
        published = False
        history_publication: _HistoryPublication | None = None
        try:
            self._assert_target_unchanged_before_commit()
            self._write_report_files(staging)
            candidate_identity = _path_identity_payload(staging)
            self._write_layout_receipt(staging, candidate_identity)
            candidate_identity = _path_identity_payload(staging)
            identities = journal_payload["identities"]
            assert isinstance(identities, dict)
            identities["staging"] = candidate_identity
            journal_snapshot = _cas_publish_json(journal_snapshot, journal_payload)
            if target_existed:
                from opensquilla.recovery import move_profile_no_replace, native_move_no_replace

                move_profile_no_replace(
                    self.target,
                    backup,
                    move=native_move_no_replace,
                )
                target_parked = True
                _fsync_directory(self.target.parent)
                identities["backup"] = _path_identity_payload(backup)
                journal_payload["phase"] = "target_parked"
                journal_snapshot = _cas_publish_json(journal_snapshot, journal_payload)
            from opensquilla.recovery import move_profile_no_replace, native_move_no_replace

            move_profile_no_replace(
                staging,
                self.target,
                move=native_move_no_replace,
            )
            published = True
            identities["candidate"] = _path_identity_payload(self.target)
            _fsync_directory(self.target.parent)
            journal_payload["phase"] = "candidate_published_unvalidated"
            journal_snapshot = _cas_publish_json(journal_snapshot, journal_payload)
            self._verify_source_still_stable(final_source_lock)
            final_candidate_identity = self._validate_published_target(
                journal_snapshot,
                journal_payload,
            )
            self._verify_source_still_stable(final_source_lock)
            identities["candidate"] = final_candidate_identity
            journal_payload["phase"] = "validated"
            journal_snapshot = _cas_publish_json(journal_snapshot, journal_payload)
            if target_existed:
                history_publication = self._write_replacement_history(
                    backup,
                    journal_payload,
                    allow_existing=False,
                )
            # This is the final source-stability linearization point. A source
            # change after history publication still rolls back before the
            # durable journal can claim committed.
            self._verify_source_still_stable(final_source_lock)
            journal_payload["phase"] = "committed"
            journal_snapshot = _cas_publish_json(journal_snapshot, journal_payload)
            self._committed = True
        except AtomicStateUnknownError:
            # A native no-replace move can complete and then lose the ability
            # to prove its post-state.  Flags maintained after the call are no
            # longer authoritative in that case.  Preserve the journal and
            # every path exactly as observed for offline recovery; attempting
            # rollback here could move or delete the wrong profile.
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            rollback_error: Exception | None = None
            try:
                from opensquilla.recovery import move_profile_no_replace, native_move_no_replace

                identities = journal_payload["identities"]
                assert isinstance(identities, dict)
                if history_publication is not None:
                    _rollback_history_publication(history_publication)
                if published:
                    if not _object_identity_matches(
                        self.target, identities.get("candidate")
                    ):
                        raise OSError(
                            "published candidate identity changed; automatic rollback stopped"
                        )
                    move_profile_no_replace(
                        self.target,
                        staging,
                        move=native_move_no_replace,
                    )
                if target_parked:
                    if not _identity_payload_matches(backup, identities.get("backup")):
                        raise OSError(
                            "complete target backup identity changed during rollback"
                        )
                    move_profile_no_replace(
                        backup,
                        self.target,
                        move=native_move_no_replace,
                    )
                staging_expected = identities.get("candidate") or identities.get("staging")
                if staging_expected is not None and _object_identity_matches(
                    staging, staging_expected
                ):
                    _remove_matching_staging(staging, staging_expected)
                _fsync_directory(self.target.parent)
            except (OSError, RuntimeError, ValueError) as rollback_exc:
                rollback_error = rollback_exc
            if rollback_error is None:
                try:
                    try:
                        journal.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        persisted_payload = json.loads(
                            journal_snapshot.data.decode("utf-8")
                        )
                        if not isinstance(persisted_payload, dict):
                            raise OSError("persisted replacement journal is invalid")
                        _unlink_matching_journal(journal, persisted_payload)
                except OSError as cleanup_exc:
                    rollback_error = cleanup_exc
            if backup_item is not None:
                self.items.remove(backup_item)
            if rollback_error is not None:
                raise OSError(f"{exc}; rollback failed: {rollback_error}") from exc
            raise

        try:
            from opensquilla.recovery.transaction import (
                finalize_committed_profile_transaction,
            )

            if not finalize_committed_profile_transaction(self.target):
                raise OSError("committed import journal could not be finalized")
        except (OSError, RecoveryError):
            self._note(
                f"committed import journal could not be finalized: {journal}; "
                "a later apply can clean it safely"
            )
        if os.environ.get("OPENSQUILLA_PROFILE_KIND", "").strip().lower() == "desktop-primary":
            # Marker finalization deliberately happens only after the profile
            # transaction and replacement-history CAS are committed. A retained
            # committed journal is accepted only when the recovery engine can
            # validate its target, backup/history (when present), and receipt.
            try:
                from opensquilla.recovery import reconcile_profile

                finalized = reconcile_profile(
                    self.target,
                    profile_kind="desktop-primary",
                )
            except (OSError, RuntimeError) as exc:
                self._note(f"desktop layout marker finalization was deferred: {exc}")
            else:
                if finalized.outcome == "recovery_required":
                    self._note(
                        "desktop layout marker finalization was deferred: "
                        f"{finalized.stable_code}"
                    )

    def _assert_target_unchanged_before_commit(self) -> None:
        try:
            current = self.target.lstat()
        except FileNotFoundError:
            if self._target_preflight_present:
                raise OSError("target disappeared after preflight")
            return
        if not self._target_preflight_present:
            raise OSError("target appeared after preflight")
        if _identity_from_stat(current) != self._target_preflight_identity:
            raise OSError("target identity changed after preflight")
        current_has_data = self._target_has_real_data()
        if current_has_data != self._target_had_real_data:
            raise OSError("target contents changed after preflight")

    def _write_layout_receipt(
        self, staging: Path, candidate_identity: dict[str, int]
    ) -> None:
        source = self.source
        assert source is not None
        receipt = {
            "schema_version": _LAYOUT_RECEIPT_SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "imported_at": datetime.now(UTC).isoformat(),
            "source": _normalized_path(source),
            "source_identity": _path_identity_payload(source),
            "source_kind": self.kind,
            "source_version": _era_hint(source),
            "target": _normalized_path(self.target),
            "candidate_identity": candidate_identity,
            "recovery_outcome": "pending",
            "recovery_stable_code": "",
            "layout": _LAYOUT_CONTRACT,
        }
        _atomic_write_json(
            self._staged_output_dir(staging) / _LAYOUT_RECEIPT_FILENAME,
            receipt,
        )

    def _assert_active_journal_is_ours(
        self,
        journal_snapshot: Any,
        journal_payload: dict[str, Any],
    ) -> None:
        """Prove the transaction ignored by recovery inspection is this writer's.

        Published-target validation necessarily runs while the transaction is
        in ``candidate_published_unvalidated`` and its journal is present.  A
        blanket recovery-inspection bypass would also hide an unrelated,
        replaced, or tampered journal.  Keep the bypass local to this call and
        bracket it with CAS identity/content checks against the journal handle
        returned by our own exclusive publication.
        """

        source = self.source
        assert source is not None
        expected_journal = _commit_journal_path(self.target)
        identities = journal_payload.get("identities")
        if (
            journal_snapshot.identity is None
            or not _same_path(journal_snapshot.path, expected_journal)
            or journal_payload.get("transaction_id") != self.transaction_id
            or journal_payload.get("phase") != "candidate_published_unvalidated"
            or not _same_path(Path(str(journal_payload.get("source", ""))), source)
            or not _same_path(Path(str(journal_payload.get("target", ""))), self.target)
            or not isinstance(identities, dict)
            or not _object_identity_matches(self.target, identities.get("candidate"))
        ):
            raise OSError("active replacement journal is not owned by this transaction")
        try:
            persisted = json.loads(journal_snapshot.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OSError("active replacement journal is unreadable") from exc
        if persisted != journal_payload:
            raise OSError("active replacement journal does not match this transaction")
        journal_snapshot.assert_current()

    def _validate_published_target(
        self,
        journal_snapshot: Any,
        journal_payload: dict[str, Any],
    ) -> dict[str, int]:
        source = self.source
        assert source is not None
        self._assert_active_journal_is_ours(journal_snapshot, journal_payload)
        if not is_valid_opensquilla_home(self.target):
            raise OSError("published target is not a valid OpenSquilla home")
        receipt_path = self.output_dir / _LAYOUT_RECEIPT_FILENAME
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OSError("published layout receipt is unreadable") from exc
        if not _valid_layout_import_receipt(
            receipt,
            source=source,
            target=self.target,
            transaction_id=self.transaction_id,
            require_validated_recovery=False,
        ):
            raise OSError("published layout receipt does not match the transaction")
        from opensquilla.recovery import inspect_profile, reconcile_profile

        profile_kind = os.environ.get("OPENSQUILLA_PROFILE_KIND", "").strip().lower()
        if profile_kind == "desktop-primary":
            # Desktop imports must validate and reconcile historical nested
            # roles before commit. Ordinary CLI targets deliberately never run
            # the Desktop reconciler or receive its downgrade marker.
            recovery_report = reconcile_profile(
                self.target,
                profile_kind=profile_kind,
                _ignore_replace_transaction=True,
            )
            if recovery_report.outcome in {"recovery_required", "recovery_profile"}:
                raise OSError(
                    "published target requires recovery and cannot be committed "
                    f"({recovery_report.stable_code})"
                )
            recovery_outcome = recovery_report.outcome
            recovery_stable_code = recovery_report.stable_code
        elif profile_kind == "desktop-recovery":
            raise OSError("complete profile import into a recovery profile is not allowed")
        else:
            # Ordinary CLI imports do not opt into Desktop reconciliation or
            # marker writes, but the just-published canonical target must still
            # pass the same read-only safety inspection before commit.  The
            # only ignored transaction is bracketed by identity/content checks
            # against this writer's current journal.
            recovery_report = inspect_profile(
                self.target,
                profile_kind="",
                _ignore_transaction=True,
            )
            if recovery_report.outcome == "recovery_required":
                raise OSError(
                    "published target requires recovery and cannot be committed "
                    f"({recovery_report.stable_code})"
                )
            recovery_outcome = recovery_report.outcome
            recovery_stable_code = recovery_report.stable_code
        # Close the TOCTOU window around the narrowly-scoped recovery bypass.
        # A replaced journal causes the transaction to fail closed before it
        # can be marked validated or committed.
        self._assert_active_journal_is_ours(journal_snapshot, journal_payload)
        final_identity = _path_identity_payload(self.target)
        receipt["candidate_identity"] = final_identity
        receipt["recovery_outcome"] = recovery_outcome
        receipt["recovery_stable_code"] = recovery_stable_code
        _atomic_write_json(receipt_path, receipt)
        if not _identity_payload_matches(self.target, final_identity):
            raise OSError("published target identity changed during layout validation")
        if _matching_import_receipt(
            source,
            self.target,
            transaction_id=self.transaction_id,
            source_kind=self.kind,
        ) is None:
            raise OSError("published target receipt did not validate after reconciliation")
        return final_identity

    def _write_replacement_history(
        self,
        backup: Path,
        journal_payload: dict[str, Any],
        *,
        allow_existing: bool,
    ) -> _HistoryPublication | None:
        from opensquilla.recovery.config_patch import ConfigSnapshot

        source = self.source
        assert source is not None
        history_path = self.target.parent / _REPLACEMENT_HISTORY
        snapshot = ConfigSnapshot.capture(history_path)
        if snapshot.identity is None:
            history: dict[str, Any] = {"schema_version": 1, "backups": []}
        else:
            try:
                loaded = json.loads(snapshot.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("replacement history is unreadable") from exc
            schema_version = loaded.get("schema_version") if isinstance(loaded, dict) else None
            if (
                not isinstance(loaded, dict)
                or set(loaded) != _HISTORY_FIELDS
                or schema_version != 1
                or not isinstance(loaded.get("backups"), list)
                or not all(
                    _valid_history_record_payload(record)
                    for record in loaded.get("backups", [])
                )
            ):
                if isinstance(schema_version, int) and schema_version > 1:
                    raise ValueError("replacement history schema is newer than this binary")
                raise ValueError("replacement history has a malformed or unsupported schema")
            history = loaded
        identities = journal_payload.get("identities")
        if not isinstance(identities, dict):
            raise ValueError("replacement journal identities are missing")
        entry = {
            "transaction_id": str(journal_payload.get("transaction_id", "")),
            "committed_at": datetime.now(UTC).isoformat(),
            "source": str(journal_payload.get("source") or _normalized_path(source)),
            "target": _normalized_path(self.target),
            "backup": _normalized_path(backup),
            "source_identity": identities.get("source"),
            "target_identity": _path_identity_payload(self.target),
            "backup_identity": _path_identity_payload(backup),
        }
        backups = history["backups"]
        assert isinstance(backups, list)
        for item in backups:
            assert isinstance(item, dict)
            existing_id = item.get("transaction_id")
            if existing_id != entry["transaction_id"]:
                continue
            if (
                item.get("target") != entry["target"]
                or item.get("backup") != entry["backup"]
                or item.get("backup_identity") != entry["backup_identity"]
            ):
                raise ValueError("replacement history transaction conflicts with the journal")
            if not allow_existing:
                raise ValueError("replacement history already contains this transaction")
            snapshot.assert_current()
            return None
        backups.append(entry)
        data = (json.dumps(history, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        published = _cas_publish_bytes(snapshot, data, mode=0o600)
        return _HistoryPublication(
            path=history_path,
            before=snapshot,
            after=published,
        )

    def _import_history_state(
        self,
        backup: Path,
        journal_payload: dict[str, Any],
    ) -> tuple[str, Any | None, dict[str, Any] | None]:
        """Return absent/matching/ambiguous for one import history record."""

        from opensquilla.recovery.config_patch import ConfigSnapshot

        history_path = self.target.parent / _REPLACEMENT_HISTORY
        snapshot = ConfigSnapshot.capture(history_path)
        if snapshot.identity is None:
            return "absent", None, None
        try:
            history = json.loads(snapshot.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "ambiguous", snapshot, None
        if (
            not isinstance(history, dict)
            or set(history) != _HISTORY_FIELDS
            or history.get("schema_version") != 1
            or not isinstance(history.get("backups"), list)
        ):
            return "ambiguous", snapshot, None
        backups = history["backups"]
        assert isinstance(backups, list)
        for record in backups:
            if not _valid_history_record_payload(record):
                return "ambiguous", snapshot, history

        transaction_id = str(journal_payload.get("transaction_id", ""))
        matches = [
            record
            for record in backups
            if isinstance(record, dict) and record.get("transaction_id") == transaction_id
        ]
        if not matches:
            return "absent", snapshot, history
        if len(matches) != 1 or set(matches[0]) != _HISTORY_RECORD_FIELDS:
            return "ambiguous", snapshot, history
        identities = journal_payload.get("identities")
        if not isinstance(identities, dict):
            return "ambiguous", snapshot, history
        record = matches[0]
        if (
            record.get("source") != journal_payload.get("source")
            or record.get("target") != journal_payload.get("target")
            or record.get("backup") != _normalized_path(backup)
            or record.get("source_identity") != identities.get("source")
            or record.get("target_identity") != identities.get("candidate")
            or record.get("backup_identity") != identities.get("backup")
        ):
            return "ambiguous", snapshot, history
        return "matching", snapshot, history

    def _remove_interrupted_import_history(
        self,
        backup: Path,
        journal_payload: dict[str, Any],
    ) -> None:
        state, snapshot, history = self._import_history_state(backup, journal_payload)
        if state == "absent":
            return
        if state != "matching" or snapshot is None or history is None:
            raise ValueError("validated import replacement history is ambiguous")
        transaction_id = str(journal_payload.get("transaction_id", ""))
        backups = history["backups"]
        assert isinstance(backups, list)
        history["backups"] = [
            record
            for record in backups
            if not isinstance(record, dict) or record.get("transaction_id") != transaction_id
        ]
        _cas_publish_json(snapshot, history)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _record(
        self,
        kind: str,
        source: Path | str | None,
        destination: Path | str | None,
        status: str,
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.items.append(
            ItemResult(
                kind=kind,
                source=str(source) if source is not None else None,
                destination=str(destination) if destination is not None else None,
                status=status,
                reason=reason,
                details=dict(details or {}),
            )
        )

    def _note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def _has_error(self) -> bool:
        return any(item.status == "error" for item in self.items)

    def _report(self) -> dict[str, Any]:
        return {
            "source": str(self.source) if self.source is not None else "",
            "source_kind": self.kind,
            "target": str(self.target),
            "output_dir": str(self.output_dir) if self._wrote_output_dir else "",
            "apply": self.options.apply,
            "items": [asdict(item) for item in self.items],
            "candidates": [
                {
                    **candidate.as_payload(),
                    # Compatibility aliases for RC3-era consumers. New clients
                    # use the explicitly advisory ``estimated_activity_at`` and
                    # concrete ``version`` names above.
                    "last_used_iso": candidate.estimated_activity_at,
                    "era_hint": candidate.era_hint,
                }
                for candidate in self.candidates
            ],
            "config_transforms": list(self.config_transforms),
            "secret_relocations": [dict(entry) for entry in self.secret_relocations],
            "paused_jobs": [dict(job) for job in self.paused_jobs],
            "preflight": dict(self.preflight),
            "notes": list(self.notes),
        }

    def _staged_output_dir(self, staging: Path) -> Path:
        return staging / "migration" / "opensquilla" / self.transaction_id

    def _persisted_report_summary(self) -> dict[str, Any]:
        """Return a privacy-narrow diagnostic summary, never commit authority."""

        source = self.source
        assert source is not None
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return {
            "schema_version": 1,
            "transaction_id": self.transaction_id,
            "created_at": datetime.now(UTC).isoformat(),
            "source": _normalized_path(source),
            "source_kind": self.kind,
            "target": _normalized_path(self.target),
            "apply": True,
            "item_counts": counts,
            "paused_job_count": len(self.paused_jobs),
            "authority": False,
        }

    def _write_report_files(self, staging: Path) -> None:
        output_dir = self._staged_output_dir(staging)
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._note("could not create the migration report directory")
            raise OSError("could not create migration report directory") from exc
        self._wrote_output_dir = True
        report = self._persisted_report_summary()
        (output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        counts = report["item_counts"]
        assert isinstance(counts, dict)
        lines = [
            "# OpenSquilla Home Import Summary",
            "",
            f"- Source: `{self.source}` ({self.kind})",
            f"- Target home: `{self.target}`",
            f"- Apply: `{self.options.apply}`",
            f"- Paused scheduler jobs: {len(self.paused_jobs)}",
            "",
            "## Counts",
            "",
        ]
        for status, count in sorted(counts.items()):
            lines.append(f"- {status}: {count}")
        lines.append("")
        (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def recover_interrupted_profile_import(
    target: Path,
    journal_payload: dict[str, Any],
) -> None:
    """Recover exactly one typed import journal without starting a new import.

    This deliberately calls only the existing identity-gated recovery branch.
    A successful rollback must return to the recovery UI; it must never fall
    through into snapshotting or publishing the recorded source again.
    """

    if journal_payload.get("operation") != "profile-import":
        raise ValueError("replacement journal is not a profile import")
    source_raw = journal_payload.get("source")
    source_kind = journal_payload.get("source_kind")
    transaction_id = journal_payload.get("transaction_id")
    if (
        not isinstance(source_raw, str)
        or not source_raw
        or source_kind not in OPENSQUILLA_SOURCE_KINDS
        or not isinstance(transaction_id, str)
    ):
        raise ValueError("profile import journal metadata is incomplete")
    try:
        if str(uuid.UUID(transaction_id)) != transaction_id:
            raise ValueError
    except ValueError as exc:
        raise ValueError("profile import journal transaction id is invalid") from exc

    migrator = OpenSquillaHomeMigrator(
        OpenSquillaMigrationOptions(
            source=Path(source_raw),
            kind=str(source_kind),
            apply=True,
            target=target,
        )
    )
    migrator.transaction_id = transaction_id
    migrator.output_dir = target / "migration" / "opensquilla" / transaction_id
    if not migrator._recover_interrupted_commit():
        raise ValueError("profile import transaction cannot be recovered automatically")
