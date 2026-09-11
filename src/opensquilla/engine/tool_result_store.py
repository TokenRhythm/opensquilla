"""Persistent raw tool-result storage for provider-context projections."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from opensquilla.attachment_refs import _atomic_write_bytes
from opensquilla.managed_artifacts import (
    ManagedArtifactInstallLock,
    _release_file_lock,
    _try_file_lock,
)

DEFAULT_TOOL_RESULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TOOL_RESULT_DISK_BUDGET_BYTES = 256 * 1024 * 1024
DEFAULT_TOOL_RESULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
TOOL_RESULT_STORE_SESSION_BUCKET = "s"
TOOL_RESULT_CONTENT_NAME = "content.txt"
TOOL_RESULT_COMPRESSED_CONTENT_NAME = "content.txt.gz"
TOOL_RESULT_META_NAME = "meta.json"
_TOOL_OUTPUT_SPOOL_NAME = "output.spool"
_TOOL_OUTPUT_LEASE_NAME = "output.lease"
# Hex chars of the content sha256 used to derive a deterministic (content-addressed)
# handle. 32 hex chars = 128 bits, which both satisfies the ``tr-<32 hex>`` handle
# format and makes truncated-digest collisions between distinct payloads negligible.
_CONTENT_HANDLE_HEX = 32

_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ToolResultStoreBudgetError(ValueError):
    """Raised when a raw tool-result snapshot exceeds store budgets."""


@dataclass(frozen=True)
class ToolResultRecord:
    handle: str
    tool_use_id: str
    tool_name: str
    session_id: str
    session_key: str
    agent_id: str
    sha256: str
    chars: int
    size_bytes: int
    created_at: str
    content: str
    stored_size_bytes: int | None = None
    storage_encoding: str = "utf-8"


@dataclass(frozen=True)
class _StoredMeta:
    created_at: datetime
    size_bytes: int
    record_dir: Path
    active: bool = False


class ToolResultStore:
    """Store full raw tool results omitted from provider context."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def write(
        self,
        content: str,
        *,
        tool_use_id: str,
        tool_name: str,
        session_id: str,
        session_key: str,
        agent_id: str,
        max_bytes: int | None = DEFAULT_TOOL_RESULT_MAX_BYTES,
        disk_budget_bytes: int | None = DEFAULT_TOOL_RESULT_DISK_BUDGET_BYTES,
        retention_seconds: int | None = DEFAULT_TOOL_RESULT_RETENTION_SECONDS,
        lock_timeout_seconds: float = 5.0,
    ) -> ToolResultRecord:
        with self._budget_lock(timeout=lock_timeout_seconds):
            return self._write(
                content, tool_use_id=tool_use_id, tool_name=tool_name,
                session_id=session_id, session_key=session_key, agent_id=agent_id,
                max_bytes=max_bytes, disk_budget_bytes=disk_budget_bytes,
                retention_seconds=retention_seconds,
            )

    def _write(
        self,
        content: str,
        *,
        tool_use_id: str,
        tool_name: str,
        session_id: str,
        session_key: str,
        agent_id: str,
        max_bytes: int | None = DEFAULT_TOOL_RESULT_MAX_BYTES,
        disk_budget_bytes: int | None = DEFAULT_TOOL_RESULT_DISK_BUDGET_BYTES,
        retention_seconds: int | None = DEFAULT_TOOL_RESULT_RETENTION_SECONDS,
    ) -> ToolResultRecord:
        session_id = _validate_non_empty("session_id", session_id)
        session_key = _validate_non_empty("session_key", session_key)
        agent_id = _validate_non_empty("agent_id", agent_id)
        payload = content.encode("utf-8")
        raw_size_bytes = len(payload)
        if raw_size_bytes == 0:
            raise ToolResultStoreBudgetError("tool result snapshot is empty")
        stored_payload = payload
        content_name = TOOL_RESULT_CONTENT_NAME
        storage_encoding = "utf-8"
        stored_size_bytes = raw_size_bytes
        if max_bytes is not None and raw_size_bytes > max_bytes:
            compressed = gzip.compress(payload, compresslevel=6)
            if len(compressed) < raw_size_bytes:
                stored_payload = compressed
                content_name = TOOL_RESULT_COMPRESSED_CONTENT_NAME
                storage_encoding = "gzip+utf-8"
                stored_size_bytes = len(compressed)
        if max_bytes is not None and stored_size_bytes > max_bytes:
            raise ToolResultStoreBudgetError(
                "tool result snapshot exceeds per-result budget "
                f"(stored={stored_size_bytes}, raw={raw_size_bytes}, max={max_bytes})"
            )

        sha = hashlib.sha256(payload).hexdigest()
        primary_handle = f"tr-{sha[:_CONTENT_HANDLE_HEX]}"

        # One cleanup scan feeds both retention and the budget prune below, so a new
        # write pays a single store walk instead of re-scanning the whole store once
        # per cleanup pass (issue #305). Retention runs first — a deduped write must
        # never bypass cleanup nor reuse a record retention is about to evict (a
        # small/zero retention_seconds would otherwise hand back a handle to an
        # immediately-reaped record) — and the surviving records prune to fit.
        surviving_records = self._remove_expired(
            self._iter_record_stats(), retention_seconds
        )

        # Content-addressed snapshots: identical content that survived retention is
        # reused instead of rewritten — refreshing its access time so a frequently
        # re-projected record stays hot — and only genuinely new content pays the
        # budget prune and the write below, so the store stops re-growing on repeats.
        reused = self._existing_record(
            primary_handle,
            sha=sha,
            content=content,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            session_id=session_id,
            session_key=session_key,
            agent_id=agent_id,
            size_bytes=raw_size_bytes,
        )
        if reused is not None:
            self._touch(primary_handle, session_id=session_id)
            return reused

        if disk_budget_bytes is not None:
            self._prune_to_fit(surviving_records, stored_size_bytes, disk_budget_bytes)

        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        # The deterministic handle is tried first; a random handle is only needed for
        # the negligible chance of a truncated-digest collision with *different*
        # content already occupying that directory.
        candidate_handles = (
            primary_handle,
            *(f"tr-{secrets.token_hex(16)}" for _ in range(4)),
        )
        for handle in candidate_handles:
            record_dir = self._record_dir(handle, session_id=session_id)
            if (record_dir / TOOL_RESULT_CONTENT_NAME).exists() or (
                record_dir / TOOL_RESULT_COMPRESSED_CONTENT_NAME
            ).exists():
                # A concurrent writer may have just stored the same content here; reuse
                # it. Otherwise it is a genuine collision and we try a random handle.
                reused = self._existing_record(
                    handle,
                    sha=sha,
                    content=content,
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    session_id=session_id,
                    session_key=session_key,
                    agent_id=agent_id,
                    size_bytes=raw_size_bytes,
                )
                if reused is not None:
                    self._touch(handle, session_id=session_id)
                    return reused
                continue
            record = ToolResultRecord(
                handle=handle,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                session_id=session_id,
                session_key=session_key,
                agent_id=agent_id,
                sha256=sha,
                chars=len(content),
                size_bytes=raw_size_bytes,
                created_at=created_at,
                content=content,
                stored_size_bytes=stored_size_bytes,
                storage_encoding=storage_encoding,
            )
            try:
                _atomic_write_bytes(
                    record_dir / TOOL_RESULT_META_NAME,
                    json.dumps(
                        {
                            "handle": record.handle,
                            "tool_use_id": record.tool_use_id,
                            "tool_name": record.tool_name,
                            "session_id": record.session_id,
                            "session_key": record.session_key,
                            "agent_id": record.agent_id,
                            "sha256": record.sha256,
                            "chars": record.chars,
                            "size_bytes": record.size_bytes,
                            "stored_size_bytes": record.stored_size_bytes,
                            "storage_encoding": record.storage_encoding,
                            "content_file": content_name,
                            "created_at": record.created_at,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8"),
                )
                # The content file (content.txt or content.txt.gz) is written last so
                # its presence marks a complete record for _existing_record (dedup) and
                # _iter_record_stats (cleanup). (A concurrent cleanup may still delete
                # the meta first; both readers treat a missing meta as "not a usable
                # record", so that race is harmless.)
                _atomic_write_bytes(record_dir / content_name, stored_payload)
            except BaseException:
                _remove_record_dir(record_dir)
                raise
            return record
        raise FileExistsError("could not allocate unique tool result handle")

    @contextmanager
    def _budget_lock(self, *, timeout: float = 5.0) -> Iterator[None]:
        # Reuse the package-neutral cross-process/thread lock. At timeout=0
        # its thread acquire is nonblocking and the file lock is tried exactly
        # once, without sleeping. Synchronous projection can therefore skip a
        # contended write; worker-side writes keep the default five-second wait.
        # This bounds lock contention, not filesystem IO or result encoding.
        (self.root / "locks").mkdir(parents=True, exist_ok=True)
        with ManagedArtifactInstallLock(self.root, "tool-results", timeout=timeout):
            yield

    def open_output_spool(
        self,
        *,
        tool_name: str,
        session_id: str,
        session_key: str,
        agent_id: str,
        max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
        disk_budget_bytes: int | None = DEFAULT_TOOL_RESULT_DISK_BUDGET_BYTES,
        retention_seconds: int | None = DEFAULT_TOOL_RESULT_RETENTION_SECONDS,
    ) -> ToolOutputSpool:
        """Reserve bounded output space in the existing result-store budget.

        The lease is held for the complete job lifetime. Cleanup in another
        process can reclaim abandoned spools, but cannot evict an active writer.
        """
        session_id = _validate_non_empty("session_id", session_id)
        session_key = _validate_non_empty("session_key", session_key)
        agent_id = _validate_non_empty("agent_id", agent_id)
        if max_bytes <= 0:
            raise ToolResultStoreBudgetError("output spool budget must be positive")
        with self._budget_lock():
            records = self._remove_expired(self._iter_record_stats(), retention_seconds)
            if disk_budget_bytes is not None:
                self._prune_to_fit(records, max_bytes, disk_budget_bytes)
            handle = f"tr-{secrets.token_hex(16)}"
            record_dir = self._record_dir(handle, session_id=session_id)
            record_dir.mkdir(parents=True, exist_ok=False)
            lease = (record_dir / _TOOL_OUTPUT_LEASE_NAME).open("w+b")
            try:
                lease.write(b"0" + str(max_bytes).encode("ascii"))
                lease.flush()
                if not _try_file_lock(lease):
                    raise ToolResultStoreBudgetError("could not acquire output spool lease")
                output = (record_dir / _TOOL_OUTPUT_SPOOL_NAME).open("w+b", buffering=0)
                return ToolOutputSpool(
                    self, handle, record_dir, lease, output, max_bytes,
                    tool_name, session_id, session_key, agent_id,
                )
            except BaseException:
                lease.close()
                _remove_record_dir(record_dir)
                raise

    def read(self, handle: str, *, session_id: str) -> ToolResultRecord:
        session_id = _validate_non_empty("session_id", session_id)
        normalized = _validate_handle(handle)
        record_dir = self._record_dir(normalized, session_id=session_id)
        meta_path = record_dir / TOOL_RESULT_META_NAME
        meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
        content_name = str(meta.get("content_file") or TOOL_RESULT_CONTENT_NAME)
        storage_encoding = str(meta.get("storage_encoding") or "utf-8")
        content_path = record_dir / content_name
        if storage_encoding == "gzip+utf-8":
            content = gzip.decompress(content_path.read_bytes()).decode("utf-8")
        else:
            content = content_path.read_text(encoding="utf-8")
        payload = content.encode("utf-8")
        sha = hashlib.sha256(payload).hexdigest()
        if meta.get("session_id") != session_id:
            raise ValueError("tool result session mismatch")
        if sha != meta.get("sha256"):
            raise ValueError("tool result hash mismatch")
        size_bytes = int(meta.get("size_bytes") or 0)
        if size_bytes != len(payload):
            raise ValueError("tool result size mismatch")
        stored_size_bytes = int(meta.get("stored_size_bytes") or content_path.stat().st_size)
        return ToolResultRecord(
            handle=normalized,
            tool_use_id=str(meta.get("tool_use_id") or ""),
            tool_name=str(meta.get("tool_name") or ""),
            session_id=str(meta.get("session_id") or session_id),
            session_key=str(meta.get("session_key") or ""),
            agent_id=str(meta.get("agent_id") or ""),
            sha256=sha,
            chars=len(content),
            size_bytes=len(payload),
            created_at=str(meta.get("created_at") or ""),
            content=content,
            stored_size_bytes=stored_size_bytes,
            storage_encoding=storage_encoding,
        )

    def read_output_preview(
        self, handle: str, *, session_id: str, max_bytes: int,
    ) -> str:
        """Verify a retained output record while reading only bounded chunks.

        Completed background sessions can release their in-memory preview;
        queries reconstruct a head/tail view without loading the whole record.
        """
        session_id = _validate_non_empty("session_id", session_id)
        record_dir = self._record_dir(handle, session_id=session_id)
        meta = self._read_meta(record_dir)
        if meta is None or meta.get("session_id") != session_id:
            raise ValueError("tool output session mismatch or missing record")
        name = str(meta.get("content_file") or TOOL_RESULT_CONTENT_NAME)
        if name not in {TOOL_RESULT_CONTENT_NAME, TOOL_RESULT_COMPRESSED_CONTENT_NAME}:
            raise ValueError("unsupported tool output content file")
        content_path = record_dir / name
        opener = gzip.open if meta.get("storage_encoding") == "gzip+utf-8" else open
        head, tail = bytearray(), bytearray()
        head_limit = max(1, max_bytes // 2)
        tail_limit = max(1, max_bytes - head_limit)
        digest = hashlib.sha256()
        observed = 0
        with opener(content_path, "rb") as stream:
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
                observed += len(chunk)
                take = min(len(chunk), head_limit - len(head))
                head.extend(chunk[:take])
                remaining = chunk[take:]
                tail.extend(remaining)
                if len(tail) > tail_limit:
                    del tail[:-tail_limit]
        if digest.hexdigest() != meta.get("sha256") or observed != meta.get("size_bytes"):
            raise ValueError("retained output integrity mismatch")
        omitted = observed - len(head) - len(tail)
        if omitted <= 0:
            return (head + tail).decode("utf-8", errors="replace")
        return (
            head.decode("utf-8", errors="replace")
            + f"\n[preview omitted {omitted} retained output bytes]\n"
            + tail.decode("utf-8", errors="replace")
        )

    def _existing_record(
        self,
        handle: str,
        *,
        sha: str,
        content: str,
        tool_use_id: str,
        tool_name: str,
        session_id: str,
        session_key: str,
        agent_id: str,
        size_bytes: int,
    ) -> ToolResultRecord | None:
        """Return the already-stored record for ``handle`` iff it holds this exact
        content (full sha256 match). Makes repeated writes idempotent and detects the
        negligible truncated-digest collision. Costs one existence check plus one small
        meta read; never scans the store."""
        record_dir = self._record_dir(handle, session_id=session_id)
        if not (
            (record_dir / TOOL_RESULT_CONTENT_NAME).exists()
            or (record_dir / TOOL_RESULT_COMPRESSED_CONTENT_NAME).exists()
        ):
            return None
        meta = self._read_meta(record_dir)
        if meta is None or meta.get("sha256") != sha:
            return None
        raw_stored_size = meta.get("stored_size_bytes")
        return ToolResultRecord(
            handle=handle,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            session_id=session_id,
            session_key=session_key,
            agent_id=agent_id,
            sha256=sha,
            chars=len(content),
            size_bytes=size_bytes,
            created_at=str(meta.get("created_at") or ""),
            content=content,
            stored_size_bytes=int(raw_stored_size) if raw_stored_size is not None else None,
            storage_encoding=str(meta.get("storage_encoding") or "utf-8"),
        )

    @staticmethod
    def _read_meta(record_dir: Path) -> dict[str, Any] | None:
        try:
            meta = json.loads((record_dir / TOOL_RESULT_META_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return meta if isinstance(meta, dict) else None

    def _touch(self, handle: str, *, session_id: str) -> None:
        """Refresh a record's last-access time so a frequently reused snapshot is not
        evicted by retention while it is still being projected."""
        record_dir = self._record_dir(handle, session_id=session_id)
        for content_name in (
            TOOL_RESULT_CONTENT_NAME,
            TOOL_RESULT_COMPRESSED_CONTENT_NAME,
        ):
            content_path = record_dir / content_name
            if not content_path.exists():
                continue
            try:
                os.utime(content_path, None)
            except OSError:
                pass

    def _record_dir(self, handle: str, *, session_id: str) -> Path:
        normalized = _validate_handle(handle)
        return (
            self.root
            / TOOL_RESULT_STORE_SESSION_BUCKET
            / _safe_token(_validate_non_empty("session_id", session_id))
            / normalized[3:5]
            / normalized
        )

    def _iter_record_stats(self) -> list[_StoredMeta]:
        """Enumerate stored records for cleanup using only filesystem stat — size from
        the content file and age from its mtime — instead of parsing every meta.json.
        Cleanup runs only when genuinely new content is stored, and even then this keeps
        the scan to cheap stat calls rather than O(records) JSON reads."""
        root = self.root / TOOL_RESULT_STORE_SESSION_BUCKET
        if not root.exists():
            return []
        records: list[_StoredMeta] = []
        for pattern in (
            TOOL_RESULT_CONTENT_NAME, TOOL_RESULT_COMPRESSED_CONTENT_NAME, _TOOL_OUTPUT_SPOOL_NAME
        ):
            for content_path in root.rglob(pattern):
                record_dir = content_path.parent
                try:
                    # Only ever consider (and later delete) well-formed tr-<32hex> record
                    # dirs, so cleanup can never touch a stray or foreign file that happens
                    # to live under the shared media root.
                    _validate_handle(record_dir.name)
                    stat = content_path.stat()
                except (OSError, ValueError):
                    continue
                active, reservation = _output_spool_reservation(record_dir)
                records.append(
                    _StoredMeta(
                        created_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                        size_bytes=max(0, stat.st_size, reservation if active else 0),
                        record_dir=record_dir,
                        active=active,
                    )
                )
        return records

    def _remove_expired(
        self, records: list[_StoredMeta], retention_seconds: int | None
    ) -> list[_StoredMeta]:
        """Delete records older than the retention window and return the survivors,
        so the caller can reuse this single scan for the budget prune instead of
        walking the store again."""
        if retention_seconds is None:
            return records
        cutoff = datetime.now(UTC) - timedelta(seconds=max(0, int(retention_seconds)))
        survivors: list[_StoredMeta] = []
        for record in records:
            if not record.active and record.created_at < cutoff:
                _remove_record_dir(record.record_dir)
                if _record_payload_exists(record.record_dir):
                    survivors.append(record)
            else:
                survivors.append(record)
        return survivors

    def _prune_to_fit(
        self,
        records: list[_StoredMeta],
        incoming_bytes: int,
        disk_budget_bytes: int,
    ) -> None:
        budget = max(0, int(disk_budget_bytes))
        records = sorted(records, key=lambda item: item.created_at)
        current = sum(record.size_bytes for record in records)
        if current + incoming_bytes <= budget:
            return
        for record in records:
            if record.active:
                continue
            _remove_record_dir(record.record_dir)
            # Windows readers or filesystem errors can prevent deletion. Do
            # not spend bytes which are still physically retained on disk.
            if _record_payload_exists(record.record_dir):
                continue
            current = max(0, current - record.size_bytes)
            if current + incoming_bytes <= budget:
                return
        if current + incoming_bytes > budget:
            raise ToolResultStoreBudgetError(
                "tool result snapshot exceeds disk budget "
                f"({current} + {incoming_bytes} > {budget})"
            )


def _validate_handle(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("tr-"):
        raise ValueError("tool result handle is invalid")
    suffix = value[3:]
    if len(suffix) != 32 or any(ch not in "0123456789abcdef" for ch in suffix):
        raise ValueError("tool result handle is invalid")
    return value


def _validate_non_empty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _safe_token(value: str) -> str:
    token = _SAFE_TOKEN_RE.sub("-", value.strip()).strip(".-")
    return token[:80] or "session"


def _remove_record_dir(record_dir: Path) -> None:
    for path in sorted(record_dir.glob("*"), reverse=True):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        record_dir.rmdir()
    except OSError:
        pass


@dataclass
class ToolOutputSpool:
    """One bounded raw prefix; completed retained text uses ordinary tr handles."""

    store: ToolResultStore
    handle: str
    record_dir: Path
    lease: BinaryIO
    output: BinaryIO
    max_bytes: int
    tool_name: str
    session_id: str
    session_key: str
    agent_id: str
    size: int = 0

    def append(self, chunk: bytes) -> None:
        remaining = self.max_bytes - self.size
        if remaining > 0:
            retained = chunk[:remaining]
            written = self.output.write(retained)
            self.size += written or 0
            if written != len(retained):
                raise OSError("short output spool write")

    def prefix(self) -> bytes:
        self.output.seek(0)
        return self.output.read(self.size)

    def finish(self, content: str) -> str:
        # Serialize rename with budget scans, so a scan cannot miss the spool
        # while it changes from output.spool to content.txt between glob passes.
        with self.store._budget_lock():
            return self._finish_locked(content)

    def _finish_locked(self, content: str) -> str:
        payload = content.encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ToolResultStoreBudgetError("retained output exceeds spool budget")
        # Replace the spool in place: no second multi-MiB copy on disk. The
        # lease keeps the reservation live until metadata and content settle.
        self.output.seek(0)
        if self.output.write(payload) != len(payload):
            raise OSError("short retained output write")
        self.output.truncate()
        self.output.close()
        meta = {
            "handle": self.handle, "tool_use_id": self.handle,
            "tool_name": self.tool_name, "session_id": self.session_id,
            "session_key": self.session_key, "agent_id": self.agent_id,
            "sha256": hashlib.sha256(payload).hexdigest(), "chars": len(content),
            "size_bytes": len(payload), "stored_size_bytes": len(payload),
            "storage_encoding": "utf-8", "content_file": TOOL_RESULT_CONTENT_NAME,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        _atomic_write_bytes(
            self.record_dir / TOOL_RESULT_META_NAME,
            json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        (self.record_dir / _TOOL_OUTPUT_SPOOL_NAME).replace(
            self.record_dir / TOOL_RESULT_CONTENT_NAME
        )
        self.close()
        return self.handle

    def close(self) -> None:
        with suppress(OSError):
            self.output.close()
        if not self.lease.closed:
            with suppress(OSError):
                _release_file_lock(self.lease)
            self.lease.close()
        # Keep abandoned bounded data for ordinary retention/budget cleanup.


def _output_spool_reservation(record_dir: Path) -> tuple[bool, int]:
    lease_path = record_dir / _TOOL_OUTPUT_LEASE_NAME
    try:
        with lease_path.open("r+b") as lease:
            if _try_file_lock(lease):
                _release_file_lock(lease)
                return False, 0
            # Windows locks byte zero; read the reservation after that byte.
            lease.seek(1)
            reserved = int(lease.read(32))
            return True, max(0, reserved)
    except (OSError, ValueError):
        return False, 0


def _record_payload_exists(record_dir: Path) -> bool:
    return any((record_dir / name).exists() for name in (
        TOOL_RESULT_CONTENT_NAME, TOOL_RESULT_COMPRESSED_CONTENT_NAME, _TOOL_OUTPUT_SPOOL_NAME,
    ))
