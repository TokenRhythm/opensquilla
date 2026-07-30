"""Lossless, CAS-protected patches for the top-level workspace setting."""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensquilla.recovery.atomic import (
    PathIdentity,
    _chmod_open_file,
    _native_io_path,
    _windows_extended_path,
    native_move_no_replace,
)
from opensquilla.recovery.errors import (
    AtomicStateUnknownError,
    ConfigChangedError,
    RecoveryError,
    UnsafePathError,
    WorkspaceOverrideError,
)

WORKSPACE_OVERRIDE_ENV_VARS = (
    "OPENSQUILLA_GATEWAY_WORKSPACE_DIR",
    # GatewayConfig keeps the standalone/TUI spelling as a public compatibility
    # alias, with the canonical Gateway spelling taking precedence.
    "OPENSQUILLA_WORKSPACE_DIR",
)
STATE_OVERRIDE_ENV_VARS = ("OPENSQUILLA_GATEWAY_STATE_DIR",)
MEDIA_OVERRIDE_ENV_VARS = (
    "OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT",
    "OPENSQUILLA_ATTACHMENTS_MEDIA_ROOT",
)
_DOTENV_MAX_BYTES = 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_COPYFILE_ACL = 1 << 0
_COPYFILE_XATTR = 1 << 2
_WORKSPACE_PATCH_SCHEMA_VERSION = 1
_WORKSPACE_PATCH_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "transaction_id",
        "home",
        "paths",
        "identities",
    }
)

_TOP_LEVEL_KEY_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>workspace_dir|\"workspace_dir\"|'workspace_dir')(?P<spacing>\s*=\s*)"
)


@dataclass(frozen=True)
class ConfigSnapshot:
    path: Path
    identity: PathIdentity | None
    mode: int
    data: bytes
    digest: bytes

    @classmethod
    def capture(cls, path: str | Path) -> ConfigSnapshot:
        config_path = Path(path)
        try:
            path_stat = os.lstat(_native_io_path(config_path))
        except FileNotFoundError:
            return cls(
                path=config_path,
                identity=None,
                mode=0o600,
                data=b"",
                digest=hashlib.sha256(b"").digest(),
            )
        except OSError as exc:
            raise UnsafePathError(
                f"cannot inspect config without following links: {config_path}"
            ) from exc
        path_attributes = int(getattr(path_stat, "st_file_attributes", 0))
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or path_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_nlink != 1
        ):
            raise UnsafePathError(f"config must be a regular non-reparse file: {config_path}")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            fd = os.open(_native_io_path(config_path), flags)
        except FileNotFoundError as exc:
            raise ConfigChangedError("config disappeared while it was being opened") from exc
        except OSError as exc:
            raise UnsafePathError(
                f"cannot read config without following links: {config_path}"
            ) from exc
        try:
            before = os.fstat(fd)
            before_attributes = int(getattr(before, "st_file_attributes", 0))
            if (
                before_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
            ):
                raise UnsafePathError(f"config must be a regular single-link file: {config_path}")
            if (
                PathIdentity.from_stat(path_stat).metadata_tuple()
                != PathIdentity.from_stat(before).metadata_tuple()
            ):
                raise ConfigChangedError("config identity changed while it was being opened")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        before_identity = PathIdentity.from_stat(before)
        after_identity = PathIdentity.from_stat(after)
        if before_identity.metadata_tuple() != after_identity.metadata_tuple():
            raise ConfigChangedError("config changed while it was being read")
        data = b"".join(chunks)
        return cls(
            path=config_path,
            identity=after_identity,
            mode=stat.S_IMODE(after.st_mode),
            data=data,
            digest=hashlib.sha256(data).digest(),
        )

    def assert_current(self) -> None:
        current = ConfigSnapshot.capture(self.path)
        expected_identity = self.identity.metadata_tuple() if self.identity is not None else None
        current_identity = (
            current.identity.metadata_tuple() if current.identity is not None else None
        )
        if current_identity != expected_identity or current.digest != self.digest:
            raise ConfigChangedError("config changed after recovery preflight")


def _parse_dotenv_value(
    raw: str,
    *,
    label: str,
    error_type: type[RecoveryError],
    stable_code: str,
) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        escaped = False
        end = -1
        for index in range(1, len(value)):
            character = value[index]
            if quote == '"' and escaped:
                escaped = False
                continue
            if quote == '"' and character == "\\":
                escaped = True
                continue
            if character == quote:
                end = index
                break
        tail = value[end + 1 :].strip() if end >= 0 else ""
        if end < 0 or (tail and not tail.startswith("#")):
            raise error_type(
                f"{label} override in profile dotenv is not safely parseable",
                stable_code=stable_code,
            )
        parsed = value[1:end]
        if quote == '"':
            replacements = {
                r"\\": "\\",
                r"\"": '"',
                r"\n": "\n",
                r"\r": "\r",
                r"\t": "\t",
            }
            for encoded, decoded in replacements.items():
                parsed = parsed.replace(encoded, decoded)
        else:
            parsed = parsed.replace(r"\'", "'").replace(r"\\", "\\")
    else:
        # python-dotenv treats a whitespace-prefixed # as an inline comment.
        parsed = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if "$" in parsed:
        # Normal dotenv bootstrap performs interpolation. Recovery deliberately
        # does not evaluate a general dotenv language or ambient substitutions;
        # an operator can remove the override or use a literal path.
        raise error_type(
            f"interpolated {label} override in profile dotenv is not safe offline",
            stable_code=stable_code,
        )
    return parsed


def _profile_dotenv_path(home: Path, *, include_legacy: bool) -> Path | None:
    current = home / ".env"
    if os.path.lexists(_native_io_path(current)):
        return current
    if not include_legacy:
        return None
    legacy = home / "state" / ".env"
    return legacy if os.path.lexists(_native_io_path(legacy)) else None


def _profile_dotenv_override(
    home: Path,
    *,
    include_legacy: bool,
    names: tuple[str, ...],
    label: str,
    error_type: type[RecoveryError],
    stable_code: str,
) -> tuple[str, str] | None:
    path = _profile_dotenv_path(home, include_legacy=include_legacy)
    if path is None:
        return None
    try:
        snapshot = ConfigSnapshot.capture(path)
    except RecoveryError as exc:
        raise error_type(
            "profile dotenv cannot be inspected without following links",
            stable_code=stable_code,
        ) from exc
    if snapshot.identity is None:
        return None
    if snapshot.identity.size > _DOTENV_MAX_BYTES:
        raise error_type(
            "profile dotenv is too large for offline recovery inspection",
            stable_code=stable_code,
        )
    try:
        text = snapshot.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise error_type(
            "profile dotenv is not valid UTF-8",
            stable_code=stable_code,
        ) from exc
    parsed: dict[str, str] = {}
    key_pattern = "|".join(re.escape(name) for name in names)
    assignment = re.compile(rf"^\s*(?:export\s+)?(?P<key>{key_pattern})\s*=\s*(?P<value>.*)$")
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = assignment.match(raw_line)
        if match is None:
            continue
        parsed[match.group("key")] = _parse_dotenv_value(
            match.group("value"),
            label=label,
            error_type=error_type,
            stable_code=stable_code,
        )
    for name in names:
        value = parsed.get(name, "").strip()
        if value:
            return name, value
    return None


def workspace_override(
    home: str | Path | None = None,
    *,
    include_legacy_dotenv: bool = False,
    include_process_environment: bool = True,
    include_standalone_alias: bool = True,
) -> tuple[str, str] | None:
    """Resolve only workspace overrides without loading a general dotenv.

    Process environment keeps normal precedence. When a Desktop home is
    supplied, inspect the current profile dotenv (or the legacy dotenv that a
    proven reconciliation would publish) with a narrow, no-follow parser.
    """

    names = (
        WORKSPACE_OVERRIDE_ENV_VARS
        if include_standalone_alias
        else ("OPENSQUILLA_GATEWAY_WORKSPACE_DIR",)
    )
    if include_process_environment:
        for name in names:
            value = os.environ.get(name, "").strip()
            if value:
                return name, value
    if home is None:
        return None
    return _profile_dotenv_override(
        Path(home).expanduser().absolute(),
        include_legacy=include_legacy_dotenv,
        names=names,
        label="workspace",
        error_type=WorkspaceOverrideError,
        stable_code="workspace_env_override_unsafe",
    )


def state_override(
    home: str | Path | None = None,
    *,
    include_legacy_dotenv: bool = False,
    include_process_environment: bool = True,
) -> tuple[str, str] | None:
    """Resolve the Gateway state root without loading a general dotenv."""

    if include_process_environment:
        for name in STATE_OVERRIDE_ENV_VARS:
            value = os.environ.get(name, "").strip()
            if value:
                return name, value
    if home is None:
        return None
    return _profile_dotenv_override(
        Path(home).expanduser().absolute(),
        include_legacy=include_legacy_dotenv,
        names=STATE_OVERRIDE_ENV_VARS,
        label="state",
        error_type=RecoveryError,
        stable_code="state_env_override_unsafe",
    )


def media_override(
    home: str | Path | None = None,
    *,
    include_legacy_dotenv: bool = False,
    include_process_environment: bool = True,
) -> tuple[str, str] | None:
    """Resolve the attachment media root without loading a general dotenv."""

    if include_process_environment:
        for name in MEDIA_OVERRIDE_ENV_VARS:
            value = os.environ.get(name, "").strip()
            if value:
                return name, value
    if home is None:
        return None
    return _profile_dotenv_override(
        Path(home).expanduser().absolute(),
        include_legacy=include_legacy_dotenv,
        names=MEDIA_OVERRIDE_ENV_VARS,
        label="media",
        error_type=RecoveryError,
        stable_code="media_env_override_unsafe",
    )


def _comment_start(value: str) -> int | None:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if quote == "'":
            if character == quote:
                quote = None
            continue
        if character in ("'", '"'):
            quote = character
        elif character == "#":
            return index
    return None


def _patch_text(raw: str, workspace: Path) -> str:
    replacement = json.dumps(str(workspace), ensure_ascii=False)
    lines = raw.splitlines(keepends=True)
    table_index = len(lines)
    matched_index: int | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("["):
            table_index = index
            break
        match = _TOP_LEVEL_KEY_RE.match(line)
        if match is None:
            continue
        if matched_index is not None:
            raise RecoveryError(
                "config contains duplicate top-level workspace_dir keys",
                stable_code="config_invalid",
            )
        matched_index = index
        suffix = line[match.end() :]
        newline = ""
        if suffix.endswith("\r\n"):
            suffix, newline = suffix[:-2], "\r\n"
        elif suffix.endswith("\n"):
            suffix, newline = suffix[:-1], "\n"
        comment_index = _comment_start(suffix)
        comment = suffix[comment_index:] if comment_index is not None else ""
        spacing_before_comment = " " if comment and not comment.startswith(" ") else ""
        lines[index] = (
            f"{match.group('indent')}{match.group('key')}{match.group('spacing')}"
            f"{replacement}{spacing_before_comment}{comment}{newline}"
        )

    if matched_index is None:
        newline = "\r\n" if "\r\n" in raw else "\n"
        insertion = f"workspace_dir = {replacement}{newline}"
        if table_index > 0 and lines[table_index - 1].strip():
            insertion += newline
        lines.insert(table_index, insertion)
    patched = "".join(lines)
    if not lines:
        patched = f"workspace_dir = {replacement}\n"
    try:
        payload = tomllib.loads(patched)
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise RecoveryError(
            "lossless workspace patch would produce invalid TOML",
            stable_code="config_invalid",
        ) from exc
    if payload.get("workspace_dir") != str(workspace):
        raise RecoveryError(
            "workspace_dir could not be patched as a top-level TOML key",
            stable_code="config_invalid",
        )
    return patched


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


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


def _normalized_path(path: str | Path) -> str:
    value = Path(path).expanduser()
    try:
        value = value.resolve(strict=False)
    except OSError:
        value = value.absolute()
    return os.path.normcase(os.path.normpath(str(value)))


def workspace_patch_journal(home: str | Path) -> Path:
    home_path = Path(home).expanduser().absolute()
    return home_path.parent / f".{home_path.name}.workspace-patch.json"


def workspace_patch_exists(home: str | Path) -> bool:
    try:
        return os.path.lexists(_native_io_path(workspace_patch_journal(home)))
    except OSError:
        return True


def _workspace_patch_paths(home: Path, transaction_id: str) -> dict[str, Path]:
    config = home / "config.toml"
    journal = workspace_patch_journal(home)
    return {
        "config": config,
        "staged": home / f".{config.name}.{transaction_id}.new",
        "backup": home / f"{config.name}.backup.{transaction_id}",
        "journal": journal,
        "committed": journal.with_name(
            f".{home.name}.workspace-patch.{transaction_id}.committed.json"
        ),
    }


def _identity_payload(identity: PathIdentity | None) -> dict[str, int] | None:
    if identity is None:
        return None
    return {
        "device": identity.device,
        "inode": identity.inode,
        "mode": identity.mode,
        "size": identity.size,
        "modified_at_ns": identity.modified_at_ns,
    }


def _valid_identity_payload(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"device", "inode", "mode", "size", "modified_at_ns"}
        and all(type(item) is int and item >= 0 for item in value.values())
    )


def _identity_matches(path: Path, expected: object) -> bool:
    if not _valid_identity_payload(expected):
        return False
    try:
        snapshot = ConfigSnapshot.capture(path)
    except RecoveryError:
        return False
    return snapshot.identity is not None and _identity_payload(snapshot.identity) == expected


def _object_identity_matches(path: Path, expected: object) -> bool:
    if not _valid_identity_payload(expected):
        return False
    assert isinstance(expected, dict)
    try:
        snapshot = ConfigSnapshot.capture(path)
    except RecoveryError:
        return False
    return (
        snapshot.identity is not None
        and snapshot.identity.device == expected["device"]
        and snapshot.identity.inode == expected["inode"]
    )


def _parked_config_matches(path: Path, expected: object) -> bool:
    if not _valid_identity_payload(expected):
        return False
    assert isinstance(expected, dict)
    try:
        snapshot = ConfigSnapshot.capture(path)
    except RecoveryError:
        return False
    if snapshot.identity is None:
        return False
    identity = snapshot.identity
    owner_only_mode = stat.S_IFMT(expected["mode"]) | 0o600
    return (
        identity.device == expected["device"]
        and identity.inode == expected["inode"]
        and identity.size == expected["size"]
        and identity.modified_at_ns == expected["modified_at_ns"]
        and identity.mode in {expected["mode"], owner_only_mode}
    )


def _write_json_no_replace(path: Path, payload: dict[str, Any]) -> ConfigSnapshot:
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
        fd = os.open(_native_io_path(path), flags, 0o600)
    except FileExistsError as exc:
        raise RecoveryError(
            "an unfinished workspace config transaction already exists",
            stable_code="workspace_patch_incomplete",
        ) from exc
    try:
        with contextlib.suppress(OSError):
            _chmod_open_file(fd, 0o600)
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        # The transaction pathname is intentionally retained if publication may
        # have reached disk.  A later inspection must fail closed instead of
        # guessing whether it is safe to start the profile.
        raise
    os.close(fd)
    _fsync_directory(path.parent)
    return ConfigSnapshot.capture(path)


def _load_workspace_patch(
    home: Path,
) -> tuple[ConfigSnapshot, dict[str, Any], dict[str, Path]]:
    journal = workspace_patch_journal(home)
    snapshot = ConfigSnapshot.capture(journal)
    if snapshot.identity is None:
        raise RecoveryError(
            "workspace config transaction does not exist",
            stable_code="workspace_patch_missing",
        )
    try:
        payload = json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            "workspace config transaction journal is invalid",
            stable_code="workspace_patch_invalid",
        ) from exc
    transaction_id = payload.get("transaction_id") if isinstance(payload, dict) else None
    try:
        canonical_id = str(uuid.UUID(str(transaction_id)))
    except ValueError as exc:
        raise RecoveryError(
            "workspace config transaction id is invalid",
            stable_code="workspace_patch_invalid",
        ) from exc
    paths = _workspace_patch_paths(home, canonical_id)
    expected_paths = {
        key: _normalized_path(path)
        for key, path in paths.items()
        if key not in {"journal", "committed"}
    }
    identities = payload.get("identities") if isinstance(payload, dict) else None
    old_identity = identities.get("old_config") if isinstance(identities, dict) else None
    staged_identity = identities.get("staged") if isinstance(identities, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != _WORKSPACE_PATCH_FIELDS
        or payload.get("schema_version") != _WORKSPACE_PATCH_SCHEMA_VERSION
        or payload.get("operation") != "workspace-config-patch"
        or transaction_id != canonical_id
        or payload.get("home") != _normalized_path(home)
        or payload.get("paths") != expected_paths
        or not isinstance(identities, dict)
        or set(identities) != {"old_config", "staged"}
        or (old_identity is not None and not _valid_identity_payload(old_identity))
        or not _valid_identity_payload(staged_identity)
    ):
        raise RecoveryError(
            "workspace config transaction journal is invalid",
            stable_code="workspace_patch_invalid",
        )
    snapshot.assert_current()
    return snapshot, payload, paths


def _make_owner_only(path: Path, expected: object) -> None:
    if not _object_identity_matches(path, expected):
        raise AtomicStateUnknownError("workspace config backup identity is ambiguous")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(_native_io_path(path), flags)
        try:
            opened = PathIdentity.from_stat(os.fstat(fd))
            assert isinstance(expected, dict)
            if opened.device != expected["device"] or opened.inode != expected["inode"]:
                raise AtomicStateUnknownError(
                    "workspace config backup changed before permission hardening"
                )
            _chmod_open_file(fd, 0o600)
            # Windows has no descriptor chmod and rejects fsync on this
            # read-only descriptor. The no-replace move already preserves the
            # source DACL and bytes; POSIX still flushes its mode hardening.
            if os.name != "nt":
                os.fsync(fd)
        finally:
            os.close(fd)
    except AtomicStateUnknownError:
        raise
    except OSError as exc:
        raise AtomicStateUnknownError(
            "workspace config backup could not be restricted to its owner"
        ) from exc


def _park_workspace_config(
    config: Path,
    backup: Path,
    expected_old: object,
) -> None:
    if not _identity_matches(config, expected_old):
        raise AtomicStateUnknownError("workspace config changed before it could be parked")
    native_move_no_replace(config, backup)
    if not _identity_matches(backup, expected_old):
        raise AtomicStateUnknownError("workspace config changed while it was being parked")
    _make_owner_only(backup, expected_old)


def _publish_workspace_config(
    staged: Path,
    config: Path,
    expected_staged: object,
) -> None:
    if not _identity_matches(staged, expected_staged):
        raise AtomicStateUnknownError("workspace config candidate identity is ambiguous")
    if os.path.lexists(_native_io_path(config)):
        raise AtomicStateUnknownError("workspace config destination changed before publication")
    native_move_no_replace(staged, config)
    if not _identity_matches(config, expected_staged):
        raise AtomicStateUnknownError("workspace config publication state is ambiguous")


def _commit_workspace_patch(home: Path) -> None:
    snapshot, payload, paths = _load_workspace_patch(home)
    identities = payload["identities"]
    if not _identity_matches(paths["config"], identities["staged"]):
        raise AtomicStateUnknownError("workspace config is not safely published")
    if os.path.lexists(_native_io_path(paths["staged"])):
        raise AtomicStateUnknownError("workspace config candidate remains after publication")
    old_identity = identities["old_config"]
    if old_identity is None:
        if os.path.lexists(_native_io_path(paths["backup"])):
            raise AtomicStateUnknownError("unexpected workspace config backup exists")
    elif not _parked_config_matches(paths["backup"], old_identity):
        raise AtomicStateUnknownError("workspace config backup identity is ambiguous")
    snapshot.assert_current()
    native_move_no_replace(paths["journal"], paths["committed"])
    _fsync_directory(paths["journal"].parent)


def _finish_workspace_patch(home: Path) -> Path | None:
    _snapshot, payload, paths = _load_workspace_patch(home)
    identities = payload["identities"]
    old_identity = identities["old_config"]
    staged_identity = identities["staged"]

    if _identity_matches(paths["config"], staged_identity):
        if os.path.lexists(_native_io_path(paths["staged"])):
            raise AtomicStateUnknownError("workspace config publication is duplicated")
    else:
        if old_identity is None:
            if os.path.lexists(_native_io_path(paths["backup"])) or os.path.lexists(
                _native_io_path(paths["config"])
            ):
                raise AtomicStateUnknownError(
                    "workspace config destination changed during publication"
                )
        elif _parked_config_matches(paths["backup"], old_identity):
            if os.path.lexists(_native_io_path(paths["config"])):
                raise AtomicStateUnknownError("workspace config destination changed after parking")
            _make_owner_only(paths["backup"], old_identity)
        elif _identity_matches(paths["config"], old_identity) and not os.path.lexists(
            _native_io_path(paths["backup"])
        ):
            _park_workspace_config(paths["config"], paths["backup"], old_identity)
        else:
            raise AtomicStateUnknownError("workspace config transaction state is ambiguous")

        _publish_workspace_config(paths["staged"], paths["config"], staged_identity)

    _commit_workspace_patch(home)
    return paths["backup"] if old_identity is not None else None


def recover_workspace_patch(home: str | Path, *, lock_timeout: float = 0.0) -> Path | None:
    """Finish an identity-proven workspace config publication after a crash."""

    from opensquilla.recovery.locking import LegacyGatewayLock, ProfileOperationLock

    home_path = Path(home).expanduser().absolute()
    with ProfileOperationLock(home_path, timeout=lock_timeout):
        with LegacyGatewayLock(home_path, timeout=lock_timeout):
            return _finish_workspace_patch(home_path)


def _copy_macos_config_metadata(snapshot: ConfigSnapshot, destination_fd: int) -> None:
    """Copy ACLs/xattrs with fcopyfile; copystat alone drops macOS ACL entries."""

    if sys.platform != "darwin" or snapshot.identity is None:
        return
    libc = ctypes.CDLL(None, use_errno=True)
    fcopyfile = getattr(libc, "fcopyfile", None)
    if fcopyfile is None:
        raise RecoveryError(
            "macOS ACL preservation is unavailable",
            stable_code="config_metadata_preservation_failed",
        )
    fcopyfile.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    fcopyfile.restype = ctypes.c_int
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(_native_io_path(snapshot.path), flags)
    except OSError as exc:
        raise ConfigChangedError("config changed before its ACLs could be preserved") from exc
    try:
        current = PathIdentity.from_stat(os.fstat(source_fd))
        if current.metadata_tuple() != snapshot.identity.metadata_tuple():
            raise ConfigChangedError("config changed before its ACLs could be preserved")
        if (
            fcopyfile(
                source_fd,
                destination_fd,
                None,
                _COPYFILE_ACL | _COPYFILE_XATTR,
            )
            != 0
        ):
            error_number = ctypes.get_errno()
            raise RecoveryError(
                f"macOS ACL or extended metadata could not be preserved ({error_number})",
                stable_code="config_metadata_preservation_failed",
            )
    finally:
        os.close(source_fd)


def _copy_windows_config_dacl(snapshot: ConfigSnapshot, destination: Path) -> None:
    """Copy the existing DACL before the old config is parked."""

    if os.name != "nt" or snapshot.identity is None:
        return
    snapshot.assert_current()
    win_dll = getattr(ctypes, "WinDLL")
    advapi32 = win_dll("advapi32", use_last_error=True)
    get_file_security = advapi32.GetFileSecurityW
    set_file_security = advapi32.SetFileSecurityW
    get_file_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    get_file_security.restype = ctypes.c_int
    set_file_security.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p]
    set_file_security.restype = ctypes.c_int
    dacl_information = 0x00000004
    required = ctypes.c_uint32()
    get_file_security(
        _windows_extended_path(snapshot.path),
        dacl_information,
        None,
        0,
        ctypes.byref(required),
    )
    if required.value == 0:
        error_number = getattr(ctypes, "get_last_error")()
        raise RecoveryError(
            f"Windows config DACL could not be read ({error_number})",
            stable_code="config_metadata_preservation_failed",
        )
    buffer = ctypes.create_string_buffer(required.value)
    if not get_file_security(
        _windows_extended_path(snapshot.path),
        dacl_information,
        buffer,
        required.value,
        ctypes.byref(required),
    ) or not set_file_security(
        _windows_extended_path(destination),
        dacl_information,
        buffer,
    ):
        error_number = getattr(ctypes, "get_last_error")()
        raise RecoveryError(
            f"Windows config DACL could not be preserved ({error_number})",
            stable_code="config_metadata_preservation_failed",
        )


def _patch_workspace_dir_locked(home: str | Path, workspace: str | Path) -> Path | None:
    """Patch only top-level ``workspace_dir`` and return the backup path.

    The content digest lives only in this process and is never included in a
    receipt, log, exception, or protocol response.
    """

    home_path = Path(home).expanduser().absolute()
    override = workspace_override(home_path)
    if override is not None:
        name, _value = override
        raise WorkspaceOverrideError(f"remove {name} before changing the persisted workspace path")
    workspace_path = Path(workspace).expanduser().absolute()
    config_path = home_path / "config.toml"
    os.makedirs(_native_io_path(home_path), mode=0o700, exist_ok=True)
    snapshot = ConfigSnapshot.capture(config_path)
    try:
        raw = snapshot.data.decode("utf-8")
        if raw:
            tomllib.loads(raw)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RecoveryError(
            "config.toml is not valid UTF-8 TOML", stable_code="config_invalid"
        ) from exc
    patched = _patch_text(raw, workspace_path).encode("utf-8")
    if patched == snapshot.data:
        snapshot.assert_current()
        return None

    if workspace_patch_exists(home_path):
        raise RecoveryError(
            "an unfinished workspace config transaction must be recovered first",
            stable_code="workspace_patch_incomplete",
        )
    operation_id = str(uuid.uuid4())
    paths = _workspace_patch_paths(home_path, operation_id)
    staged_path = paths["staged"]
    staged_created = False
    journal_created = False
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(_native_io_path(staged_path), flags, 0o600)
        staged_created = True
        try:
            if snapshot.identity is not None:
                try:
                    shutil.copystat(
                        _native_io_path(config_path),
                        _native_io_path(staged_path),
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise RecoveryError(
                        "config permissions, ACLs, or extended metadata could not be preserved",
                        stable_code="config_metadata_preservation_failed",
                    ) from exc
                _copy_macos_config_metadata(snapshot, fd)
            _chmod_open_file(fd, snapshot.mode if snapshot.identity is not None else 0o600)
            _write_all(fd, patched)
            os.fsync(fd)
        finally:
            os.close(fd)
        _copy_windows_config_dacl(snapshot, staged_path)
        snapshot.assert_current()
        staged_snapshot = ConfigSnapshot.capture(staged_path)
        if staged_snapshot.identity is None or staged_snapshot.data != patched:
            raise AtomicStateUnknownError("workspace config candidate could not be verified")
        payload: dict[str, Any] = {
            "schema_version": _WORKSPACE_PATCH_SCHEMA_VERSION,
            "operation": "workspace-config-patch",
            "transaction_id": operation_id,
            "home": _normalized_path(home_path),
            "paths": {
                key: _normalized_path(path)
                for key, path in paths.items()
                if key not in {"journal", "committed"}
            },
            "identities": {
                "old_config": _identity_payload(snapshot.identity),
                "staged": _identity_payload(staged_snapshot.identity),
            },
        }
        try:
            _write_json_no_replace(paths["journal"], payload)
        finally:
            journal_created = os.path.lexists(_native_io_path(paths["journal"]))
        backup = _finish_workspace_patch(home_path)
        journal_created = False
        staged_created = False
        return backup
    finally:
        # Before the durable journal exists the staged file is disposable
        # process output.  Once the journal is visible, every artifact is left in
        # place for deterministic recovery; no failure path guesses or deletes.
        if staged_created and not journal_created:
            with contextlib.suppress(OSError):
                os.unlink(_native_io_path(staged_path))


def patch_workspace_dir(
    home: str | Path,
    workspace: str | Path,
    *,
    lock_timeout: float = 0.0,
) -> Path | None:
    """Patch ``workspace_dir`` under both RC4 and legacy writer exclusions."""

    from opensquilla.recovery.locking import LegacyGatewayLock, ProfileOperationLock

    home_path = Path(home).expanduser().absolute()
    with ProfileOperationLock(home_path, timeout=lock_timeout):
        with LegacyGatewayLock(home_path, timeout=lock_timeout):
            return _patch_workspace_dir_locked(home_path, workspace)


__all__ = [
    "ConfigSnapshot",
    "MEDIA_OVERRIDE_ENV_VARS",
    "STATE_OVERRIDE_ENV_VARS",
    "WORKSPACE_OVERRIDE_ENV_VARS",
    "patch_workspace_dir",
    "media_override",
    "recover_workspace_patch",
    "state_override",
    "workspace_override",
    "workspace_patch_exists",
    "workspace_patch_journal",
]
