"""Protected metadata creation cleanup for Linux sandbox runs."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SyntheticMountCleanupTarget:
    path: Path
    kind: str
    pre_existing_identity: tuple[int, int] | None = None

    @staticmethod
    def identity_for_path(path: Path) -> tuple[int, int] | None:
        try:
            stat_result = path.stat()
        except OSError:
            return None
        return (stat_result.st_dev, stat_result.st_ino)


@dataclass(frozen=True)
class SyntheticMountRegistration:
    target: SyntheticMountCleanupTarget
    marker_file: Path
    marker_dir: Path


@dataclass(frozen=True)
class ProtectedCreateRegistration:
    target: Path
    marker_file: Path
    marker_dir: Path


_SYNTHETIC_MARKER_ROOT = Path(tempfile.gettempdir()) / "opensquilla-bwrap-markers"


def register_synthetic_mount_targets(
    targets: Iterable[SyntheticMountCleanupTarget],
) -> tuple[SyntheticMountRegistration, ...]:
    registrations: list[SyntheticMountRegistration] = []
    for target in targets:
        marker_dir = _marker_dir(target.path)
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_file = marker_dir / _marker_name()
        marker_file.write_text("synthetic\n", encoding="utf-8")
        registrations.append(
            SyntheticMountRegistration(
                target=target,
                marker_file=marker_file,
                marker_dir=marker_dir,
            )
        )
    return tuple(registrations)


def register_protected_create_targets(
    targets: Iterable[Path],
) -> tuple[ProtectedCreateRegistration, ...]:
    registrations: list[ProtectedCreateRegistration] = []
    for target in targets:
        marker_dir = _marker_dir(target)
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_file = marker_dir / _marker_name()
        marker_file.write_text("protected-create\n", encoding="utf-8")
        registrations.append(
            ProtectedCreateRegistration(
                target=target,
                marker_file=marker_file,
                marker_dir=marker_dir,
            )
        )
    return tuple(registrations)


def cleanup_synthetic_mount_registrations(
    registrations: Iterable[SyntheticMountRegistration],
) -> None:
    items = tuple(registrations)
    for item in reversed(items):
        _remove_marker_file(item.marker_file)
    for item in reversed(items):
        if _marker_dir_has_active_process(item.marker_dir):
            continue
        cleanup_synthetic_mount_targets((item.target,))
        _remove_marker_dir(item.marker_dir)


def cleanup_protected_create_registrations(
    registrations: Iterable[ProtectedCreateRegistration],
) -> list[str]:
    items = tuple(registrations)
    for item in reversed(items):
        _remove_marker_file(item.marker_file)
    messages: list[str] = []
    for item in reversed(items):
        if _marker_dir_has_active_process(item.marker_dir):
            if item.target.exists() or item.target.is_symlink():
                messages.append(_protected_create_message(item.target))
            continue
        messages.extend(cleanup_protected_create_targets((item.target,)))
        _remove_marker_dir(item.marker_dir)
    messages.reverse()
    return messages


def cleanup_synthetic_mount_targets(
    targets: Iterable[SyntheticMountCleanupTarget],
) -> None:
    for target in reversed(tuple(targets)):
        path = target.path
        try:
            if target.pre_existing_identity is not None:
                current_identity = SyntheticMountCleanupTarget.identity_for_path(path)
                if current_identity == target.pre_existing_identity:
                    continue
            if target.kind == "empty_directory":
                path.rmdir()
            elif target.kind == "empty_file":
                if path.stat().st_size == 0:
                    path.unlink()
            else:
                continue
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            continue
        except OSError:
            continue


def cleanup_protected_create_targets(targets: tuple[Path, ...]) -> list[str]:
    messages: list[str] = []
    for target in reversed(targets):
        try:
            if not target.exists() and not target.is_symlink():
                continue
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except FileNotFoundError:
            continue
        messages.append(_protected_create_message(target))
    messages.reverse()
    return messages


def _protected_create_message(target: Path) -> str:
    return f"sandbox blocked creation of protected workspace metadata path {target}"


def _marker_dir(target: Path) -> Path:
    digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()
    return _SYNTHETIC_MARKER_ROOT / digest


def _marker_name() -> str:
    return f"{os.getpid()}-{uuid.uuid4().hex}"


def _remove_marker_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _remove_marker_dir(path: Path) -> None:
    try:
        path.rmdir()
    except (FileNotFoundError, OSError):
        return


def _marker_dir_has_active_process(marker_dir: Path) -> bool:
    try:
        entries = tuple(marker_dir.iterdir())
    except FileNotFoundError:
        return False
    active = False
    for marker in entries:
        pid = _pid_from_marker(marker)
        if pid is None:
            continue
        if _process_is_active(pid):
            active = True
            continue
        _remove_marker_file(marker)
    return active


def _pid_from_marker(marker: Path) -> int | None:
    raw = marker.name.split("-", 1)[0]
    try:
        return int(raw)
    except ValueError:
        return None


def _process_is_active(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = [
    "ProtectedCreateRegistration",
    "SyntheticMountCleanupTarget",
    "SyntheticMountRegistration",
    "cleanup_protected_create_targets",
    "cleanup_protected_create_registrations",
    "cleanup_synthetic_mount_registrations",
    "cleanup_synthetic_mount_targets",
    "register_protected_create_targets",
    "register_synthetic_mount_targets",
]
