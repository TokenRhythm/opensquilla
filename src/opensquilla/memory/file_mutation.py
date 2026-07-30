"""Process-local coordination and atomic publication for memory source files."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

_WORKSPACE_LOCKS: dict[str, asyncio.Lock] = {}


class MemoryWriteConflictError(RuntimeError):
    """The memory file changed after a mutation was prepared."""


def memory_content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_memory_mutation_lock(workspace: Path) -> asyncio.Lock:
    """Return the single mutation lock for one resolved agent workspace."""
    key = str(workspace.resolve()).casefold()
    lock = _WORKSPACE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _WORKSPACE_LOCKS[key] = lock
    return lock


def atomic_write_text(
    path: Path,
    content: str,
    *,
    expected_sha256: str | None = None,
) -> None:
    """Publish UTF-8 text atomically, optionally guarded by a content CAS."""
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    if (
        expected_sha256 is not None
        and memory_content_sha256(current) != expected_sha256
    ):
        raise MemoryWriteConflictError(
            f"memory source changed before publish: {path.name}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=os.fspath(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
