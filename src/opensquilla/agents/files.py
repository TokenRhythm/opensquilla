"""Shared, bounded Agent-file I/O for the registry, management RPC and tools."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from opensquilla.identity.bootstrap import (
    CORE_BOOTSTRAP_TEMPLATE_FILENAMES,
    RETIRED_WORKSPACE_FILENAMES,
)

AGENT_FILE_NAMES = (*CORE_BOOTSTRAP_TEMPLATE_FILENAMES, "memory.md")
PROFILE_FILE_NAMES = frozenset({"USER.md", "IDENTITY.md", "SOUL.md"})
ABSENT_REVISION = "absent"
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


class AgentFileConflictError(ValueError):
    """The file no longer matches the revision the caller read."""


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("params.name is required")
    if name != Path(name).name or "/" in name or "\\" in name:
        raise ValueError("workspace file name must not contain path separators")
    if name not in AGENT_FILE_NAMES:
        raise ValueError(
            f"Retired workspace agent file: {name}; use ordinary file tools to access it"
            if name in RETIRED_WORKSPACE_FILENAMES else f"Unsupported workspace agent file: {name}"
        )
    return name


def resolve_file(root: Path, name: str) -> tuple[str, Path]:
    safe_name = validate_name(name)
    path = root / safe_name
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("workspace file escapes workspace root") from exc
    return safe_name, path


def validate_file_stat(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("workspace agent file must be a regular file")
    if getattr(file_stat, "st_nlink", 1) > 1:
        raise ValueError("workspace agent file must not be hardlinked")


def _path_stat(path: Path) -> os.stat_result:
    file_stat = path.lstat()
    if stat.S_ISLNK(file_stat.st_mode) or getattr(path, "is_junction", lambda: False)():
        raise ValueError("workspace agent file must not be a symlink")
    validate_file_stat(file_stat)
    return file_stat


def read_bytes(path: Path) -> bytes:
    before = _path_stat(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError("workspace agent file must not be a symlink") from exc
    try:
        current = os.fstat(fd)
        validate_file_stat(current)
        if not os.path.samestat(before, current):
            raise AgentFileConflictError("Agent file changed while being read; read it again.")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd != -1:
            os.close(fd)


def read_text(path: Path) -> str:
    return read_bytes(path).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def file_entry(root: Path, name: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name, "path": name, "exists": False, "missing": True,
        "status": "missing", "revision": ABSENT_REVISION,
    }
    try:
        file_stat = (root / name).lstat()
    except FileNotFoundError:
        return entry
    entry.update({"exists": True, "missing": False})
    entry.pop("revision")
    if stat.S_ISLNK(file_stat.st_mode):
        entry.update({"status": "unsafe", "unsafeReason": "symlink"})
    elif not stat.S_ISREG(file_stat.st_mode):
        entry.update({"status": "unsafe", "unsafeReason": "not-regular-file"})
    elif getattr(file_stat, "st_nlink", 1) > 1:
        entry.update({"status": "unsafe", "unsafeReason": "hardlink"})
    else:
        entry.update({"status": "present", "size": file_stat.st_size})
    return entry


def _lock_for(path: Path) -> asyncio.Lock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _revision(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


async def read_file(root: Path, name: str, *, missing_ok: bool = False) -> dict[str, Any]:
    safe_name, path = resolve_file(root, name)
    async with _lock_for(path):
        try:
            data = read_bytes(path)
        except FileNotFoundError:
            if not missing_ok:
                raise
            return {"name": safe_name, "content": "", "exists": False, "revision": ABSENT_REVISION}
        return {
            "name": safe_name,
            "content": data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"),
            "exists": True, "revision": _revision(data),
        }


async def replace_file(
    root: Path,
    name: str,
    content: Any,
    *,
    expected_revision: str | None = None,
    on_write: Callable[[], None] | None = None,
) -> dict[str, Any]:
    safe_name, path = resolve_file(root, name)
    if expected_revision is not None and (
        not isinstance(expected_revision, str) or not expected_revision
    ):
        raise ValueError("expectedRevision must be a non-empty revision")
    data = (content if isinstance(content, str) else str(content)).encode("utf-8")
    async with _lock_for(path):
        try:
            previous = read_bytes(path)
            observed = _revision(previous)
        except FileNotFoundError:
            observed = ABSENT_REVISION
        if expected_revision is not None and expected_revision != observed:
            raise AgentFileConflictError("Agent file changed since it was read; read it again.")
        root.mkdir(parents=True, exist_ok=True)
        nofollow = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        if observed == ABSENT_REVISION:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
            except FileExistsError as exc:
                raise AgentFileConflictError("Agent file appeared; read it again.") from exc
        else:
            before = _path_stat(path)
            fd = os.open(path, os.O_WRONLY | nofollow)
            try:
                if not os.path.samestat(before, os.fstat(fd)):
                    raise AgentFileConflictError("Agent file changed; read it again.")
            except BaseException:
                os.close(fd)
                raise
        mutated = False
        try:
            validate_file_stat(os.fstat(fd))
            os.ftruncate(fd, 0)
            mutated = True
            pending = memoryview(data)
            while pending:
                written = os.write(fd, pending)
                if written <= 0:
                    raise OSError("Agent file write did not complete")
                pending = pending[written:]
        finally:
            os.close(fd)
            if mutated and on_write is not None:
                on_write()
        if read_bytes(path) != data:
            raise RuntimeError("Agent file write could not be confirmed; read it again.")
        return {
            "name": safe_name, "path": safe_name, "size": len(data), "revision": _revision(data),
        }
