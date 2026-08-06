"""Offline recovery of a corrupt profile ``config.toml``.

The repair is deliberately conservative: the corrupt file is preserved by an
atomic no-replace rename to ``config.toml.corrupt.<stamp>`` before any byte is
written, and the replacement comes from the newest sibling backup whose bytes
still validate. Without a usable backup a minimal default config is written so
startup can proceed. A crash between the rename and the write leaves no config
at all, which the inspector already treats as a fresh profile with defaults —
never a replaced or half-written file.
"""

from __future__ import annotations

import datetime
import os
import tomllib
from pathlib import Path

from opensquilla.recovery.atomic import _native_io_path, native_move_no_replace
from opensquilla.recovery.config_patch import ConfigSnapshot
from opensquilla.recovery.errors import (
    DestinationExistsError,
    RecoveryError,
    UnsafePathError,
)
from opensquilla.recovery.locking import (
    LegacyGatewayLock,
    ProfileOperationLock,
    resolve_home_link,
)
from opensquilla.recovery.models import RecoveryReport

_RECOVERABLE_CONFIG_CODES = frozenset({"config_invalid", "config_unreadable"})
_DEFAULT_CONFIG = b"config_version = 1\n"
_BACKUP_PREFIX = "config.toml.backup."


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


def _backup_is_restorable(data: bytes) -> bool:
    """Restoring bytes the inspector would reject only trades one bad config
    for another (or, worse, raises the schema-too-new startup gate), so a
    backup must pass the inspector's validation before it is trusted.
    """
    from opensquilla.recovery.engine import SUPPORTED_CONFIG_VERSION

    try:
        payload = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    config_version = payload.get("config_version", 0)
    if isinstance(config_version, bool) or not isinstance(config_version, int):
        return False
    if config_version > SUPPORTED_CONFIG_VERSION:
        return False
    return all(
        payload.get(key) is None or isinstance(payload.get(key), str)
        for key in ("state_dir", "workspace_dir")
    )


def _newest_valid_backup_bytes(home_path: Path) -> bytes | None:
    # Backup names embed a lexically sortable timestamp, so reverse name order
    # is newest-first (the same rule ``make_config_backup`` prunes by).
    try:
        with os.scandir(_native_io_path(home_path)) as iterator:
            names = [entry.name for entry in iterator if entry.name.startswith(_BACKUP_PREFIX)]
    except OSError:
        return None
    for name in sorted(names, reverse=True):
        try:
            snapshot = ConfigSnapshot.capture(home_path / name)
        except RecoveryError:
            # A link-shaped or concurrently changing backup is never trusted.
            continue
        if snapshot.identity is not None and _backup_is_restorable(snapshot.data):
            return snapshot.data
    return None


def _park_corrupt_config(config_path: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
    for attempt in range(1000):
        suffix = "" if attempt == 0 else f".{attempt}"
        destination = config_path.with_name(f"{config_path.name}.corrupt.{stamp}{suffix}")
        try:
            native_move_no_replace(config_path, destination)
        except DestinationExistsError:
            continue
        _fsync_directory(config_path.parent)
        return destination
    raise RecoveryError(
        f"could not park the corrupt config next to {config_path}",
        stable_code="config_recovery_failed",
    )


def _write_config_no_replace(path: Path, data: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(_native_io_path(path), flags, 0o600)
    try:
        view = memoryview(data)
        while len(view):
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(_native_io_path(path))
        except OSError:
            pass
        raise
    os.close(fd)
    _fsync_directory(path.parent)


def _legacy_gateway_lock(home_path: Path, lock_timeout: float) -> LegacyGatewayLock:
    try:
        return LegacyGatewayLock(home_path, timeout=lock_timeout)
    except UnsafePathError:
        # A corrupt config can name an unlockable state_dir (wrong type or an
        # empty string). The canonical root must still be covered so an old
        # gateway cannot race the repair; that gateway cannot use the garbage
        # configured root either.
        return LegacyGatewayLock(
            home_path,
            state_roots=(home_path / "state",),
            timeout=lock_timeout,
        )


def recover_config(home: str | Path, *, lock_timeout: float = 0.0) -> RecoveryReport:
    """Replace a corrupt ``config.toml`` after preserving it beside itself.

    Inspection runs inside the profile locks and gates the repair, so pending
    crash-recovery journals keep their priority and only
    ``config_invalid``/``config_unreadable`` may proceed.
    ``config_schema_too_new`` is a downgrade scenario, not corruption, and is
    never rewritten.
    """

    from opensquilla.recovery.engine import inspect_profile

    home_path = resolve_home_link(Path(home).expanduser().absolute())
    config_path = home_path / "config.toml"
    with ProfileOperationLock(home_path, timeout=lock_timeout):
        with _legacy_gateway_lock(home_path, lock_timeout):
            report = inspect_profile(home_path)
            if report.stable_code not in _RECOVERABLE_CONFIG_CODES:
                if report.outcome == "recovery_required":
                    raise RecoveryError(
                        "config recovery does not apply to this profile state",
                        stable_code="config_recovery_not_applicable",
                    )
                return report
            restored = _newest_valid_backup_bytes(home_path)
            _park_corrupt_config(config_path)
            _write_config_no_replace(
                config_path,
                restored if restored is not None else _DEFAULT_CONFIG,
            )
    return inspect_profile(home_path)
