"""Narrow, recoverable replacement of the defaults retired at 4d181161.

Gateway composition owns this explicit startup operation across identity and
recovery; missing-only seeding and RPC reads never invoke it. The profile lease
and pre-publication snapshot check exclude cooperating
writers and detect changes during preparation; os.replace is not a content CAS.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from opensquilla.identity.bootstrap import _template_text
from opensquilla.paths import native_io_path
from opensquilla.profile_operation_lock import profile_operation_lock_held_by_current_thread
from opensquilla.recovery.atomic import is_path_redirecting_stat
from opensquilla.recovery.config_patch import ConfigSnapshot
from opensquilla.recovery.errors import ConfigChangedError, RecoveryError, UnsafePathError

# Only the defaults replaced by this change, not every historical template.
# Test fixtures retain their source text; production needs only fingerprints.
_OLD_DEFAULT_SHA256 = {
    "AGENTS.md": "c65e9ae77f824ac0c1eb320031d511338d316040eba998902054a0a544302c8e",
    "SOUL.md": "a0061fa01720975c8810da552e02418b5cd0f6d041279199c1d3062e6587526e",
}
_OLD_IMPORT_SHA256 = {
    "AGENTS.md": "b3359ec2b92a18878b992e106d4555d9bb1fbe3ae5af231b32d51d3e10d534c8",
    "SOUL.md": "a84152e93dc0b925703ad7cfaf53fe95f98bca5ccf445372d12fd036f8c7a9bd",
}
_BACKUP_PARTS = (".opensquilla", "template-backups", "md-retirement-v1")


def is_pre_retirement_default(filename: str, data: bytes, *, importing: bool = False) -> bool:
    """Match only BOM/CRLF variants; imports retain their existing rstrip policy."""
    expected = (_OLD_IMPORT_SHA256 if importing else _OLD_DEFAULT_SHA256).get(filename)
    if expected is None:
        return False
    try:
        text = data.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return False
    if importing:
        text = text.rstrip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() == expected


@dataclass(frozen=True)
class TemplateUpgradeResult:
    filename: str
    status: str
    reason: str
    backup_path: Path | None = None


def _directory_identity(path: Path) -> tuple[int, int]:
    value = os.lstat(native_io_path(path))
    if is_path_redirecting_stat(value) or not stat.S_ISDIR(value.st_mode):
        raise UnsafePathError("template upgrade requires non-redirecting directories")
    return value.st_dev, value.st_ino


def _capture_directories(root: Path) -> dict[Path, tuple[int, int]]:
    # Do not resolve away symlinks before checking them, including ancestors.
    return {path: _directory_identity(path) for path in (*reversed(root.parents), root)}


def _assert_directories(directories: dict[Path, tuple[int, int]]) -> None:
    for path, expected in directories.items():
        if _directory_identity(path) != expected:
            raise ConfigChangedError("template upgrade directory changed")


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(
        native_io_path(path),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _backup_directory(root: Path, directories: dict[Path, tuple[int, int]]) -> Path:
    current = root
    for part in _BACKUP_PARTS:
        _assert_directories(directories)
        current = current / part
        try:
            os.mkdir(native_io_path(current), 0o700)
        except FileExistsError:
            pass
        directories[current] = _directory_identity(current)
        _sync_directory(current.parent)
    return current


def _write_backup(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(native_io_path(path), flags, 0o600)
    except FileExistsError:
        existing = ConfigSnapshot.capture(path)
        if existing.identity is None or existing.data != data:
            raise UnsafePathError("template backup exists with different content")
        existing.assert_current()
        return
    # Leave an incomplete backup in place on failure, rather than deleting a
    # path which might have been changed by another writer. Retry fails closed.
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


def _upgrade_file(root: Path, filename: str) -> TemplateUpgradeResult:
    backup: Path | None = None
    temporary: str | None = None
    published = False
    try:
        directories = _capture_directories(root)
        snapshot = ConfigSnapshot.capture(root / filename)
        if snapshot.identity is None:
            return TemplateUpgradeResult(filename, "unchanged", "missing")
        if not is_pre_retirement_default(filename, snapshot.data):
            return TemplateUpgradeResult(filename, "unchanged", "not-old-default")
        if not snapshot.mode & 0o222:
            return TemplateUpgradeResult(filename, "skipped", "read-only")
        replacement = _template_text(filename).encode("utf-8")
        backup_dir = _backup_directory(root, directories)
        backup = backup_dir / f"{filename}.{hashlib.sha256(snapshot.data).hexdigest()}.bak"
        _assert_directories(directories)
        _write_backup(backup, snapshot.data)
        _assert_directories(directories)
        fd, temporary = tempfile.mkstemp(prefix=f".{filename}.upgrade-", dir=native_io_path(root))
        with os.fdopen(fd, "wb") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), snapshot.mode)
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_directories(directories)
        snapshot.assert_current()
        os.replace(temporary, native_io_path(snapshot.path))
        temporary = None
        published = True
        _sync_directory(root)
        return TemplateUpgradeResult(filename, "updated", "old-default", backup)
    except (OSError, RecoveryError) as exc:
        # Do not log exception prose or document bytes. A post-publication
        # durability error must not blindly roll back over subsequent edits.
        return TemplateUpgradeResult(
            filename,
            "updated" if published else "skipped",
            "durability-unconfirmed" if published else type(exc).__name__,
            backup,
        )
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def upgrade_workspace_defaults(
    workspace_dir: str | Path, *, profile_home: str | Path
) -> tuple[TemplateUpgradeResult, ...]:
    """Upgrade two known defaults only while the caller owns the profile lease."""
    if not profile_operation_lock_held_by_current_thread(profile_home):
        return tuple(
            TemplateUpgradeResult(name, "skipped", "profile-lease-required")
            for name in _OLD_DEFAULT_SHA256
        )
    root = Path(os.path.abspath(Path(workspace_dir).expanduser()))
    return tuple(_upgrade_file(root, name) for name in _OLD_DEFAULT_SHA256)
