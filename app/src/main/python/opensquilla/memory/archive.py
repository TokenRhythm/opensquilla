"""Internal transcript archive writer for compaction safety sidecars."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class RawArchiveWriteResult:
    relative_path: str
    content_hash: str
    byte_count: int


def _safe_path_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    safe = re.sub(r"\.{2,}", ".", safe).strip(".-")
    if safe in {"", ".", ".."}:
        return "unknown"
    return safe[:96]


def _validate_sha256_hex(value: str) -> str:
    normalized = str(value or "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", normalized):
        raise ValueError("content_hash must be a 64-character SHA-256 hex string")
    return normalized.lower()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY

    try:
        fd = os.open(path, flags)
    except OSError:
        return

    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)


def raw_fallback_relative_path(
    *,
    reason: str,
    session_key: str | None,
    content_hash: str,
    now: datetime | None = None,
) -> Path:
    content_hash = _validate_sha256_hex(content_hash)
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    session_part = _safe_path_component(session_key or "unknown")
    reason_part = _safe_path_component(reason)
    filename = f"{stamp}-{session_part}-{reason_part}-{content_hash[:16]}.md"
    return Path("memory") / ".raw_fallbacks" / filename


def write_raw_fallback_archive(
    workspace: str | Path,
    *,
    content: str,
    reason: str,
    session_key: str | None = None,
    now: datetime | None = None,
) -> RawArchiveWriteResult:
    root = Path(workspace).expanduser().resolve()
    body = str(content or "")
    encoded = body.encode("utf-8")
    content_hash = hashlib.sha256(encoded).hexdigest()
    relative_path = raw_fallback_relative_path(
        reason=reason,
        session_key=session_key,
        content_hash=content_hash,
        now=now,
    )
    target = (root / relative_path).resolve()
    target.relative_to(root)

    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == content_hash:
        return RawArchiveWriteResult(
            relative_path=relative_path.as_posix(),
            content_hash=content_hash,
            byte_count=len(encoded),
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        _fsync_directory(target.parent)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    return RawArchiveWriteResult(
        relative_path=relative_path.as_posix(),
        content_hash=content_hash,
        byte_count=len(encoded),
    )
