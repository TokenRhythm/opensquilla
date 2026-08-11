"""Bound, lazy access to historical DRACO resume result rows.

The resume runner has to classify every historical row, but only scheduled
repair work needs the full winning row later.  This module keeps compact byte
locators and authenticated source snapshots so classification does not turn
the whole archive into resident Python objects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO

from opensquilla.eval.draco_artifact_integrity import verify_result_row_evidence


class DracoResumeSourceError(ValueError):
    """A resume source or one of its indexed rows changed unexpectedly."""


_SIGNATURE_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
_HASH_CHUNK_BYTES = 1024 * 1024
_SOURCE_READ_CHUNK_BYTES = 1024 * 1024
_UNIVERSAL_NEWLINE_RE = re.compile(rb"\r\n?|\n")


def _file_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return tuple(int(getattr(value, field)) for field in _SIGNATURE_FIELDS)  # type: ignore[return-value]


def _sha256(value: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise DracoResumeSourceError("resume source spool write made no progress")
        written += count


def _read_exact(fd: int, *, offset: int, length: int) -> bytearray:
    if offset < 0 or length <= 0:
        raise DracoResumeSourceError("resume row locator has an invalid byte range")
    payload = bytearray(length)
    completed = 0
    if hasattr(os, "preadv"):
        while completed < length:
            count = os.preadv(
                fd,
                [memoryview(payload)[completed:]],
                offset + completed,
            )
            if count <= 0:
                raise DracoResumeSourceError(
                    "resume source row changed after it was indexed"
                )
            completed += count
        return payload
    while completed < length:  # pragma: no cover - Linux production uses preadv.
        chunk = os.pread(fd, length - completed, offset + completed)
        if not chunk:
            raise DracoResumeSourceError(
                "resume source row changed after it was indexed"
            )
        payload[completed : completed + len(chunk)] = chunk
        completed += len(chunk)
    return payload


def _hash_fd(fd: int, *, expected_size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < expected_size:
        chunk = os.pread(fd, min(_HASH_CHUNK_BYTES, expected_size - offset), offset)
        if not chunk:
            raise DracoResumeSourceError(
                "resume source changed while its snapshot was verified"
            )
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _iter_universal_binary_lines(handle: BinaryIO) -> Iterator[bytes]:
    """Yield raw lines for LF, CRLF, or CR like text universal-newline mode."""

    pending = bytearray()
    while chunk := handle.read(_SOURCE_READ_CHUNK_BYTES):
        start = 0
        if pending and pending[-1] == 0x0D:
            if chunk.startswith(b"\n"):
                pending.extend(b"\n")
                yield bytes(pending)
                pending.clear()
                start = 1
            else:
                yield bytes(pending)
                pending.clear()
        for delimiter in _UNIVERSAL_NEWLINE_RE.finditer(chunk, start):
            end = delimiter.end()
            if delimiter.group() == b"\r" and end == len(chunk):
                # Delay a final CR until the next byte tells us whether this
                # is a bare-CR delimiter or a CRLF split across two reads.
                pending.extend(chunk[start:])
                start = len(chunk)
                break
            pending.extend(chunk[start:end])
            yield bytes(pending)
            pending.clear()
            start = end
        if start < len(chunk):
            pending.extend(chunk[start:])
    if pending:
        yield bytes(pending)


def _open_regular(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DracoResumeSourceError(f"cannot open resume JSONL: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DracoResumeSourceError(f"resume JSONL is not a regular file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _safe_bound_source_limit() -> int:
    """Reserve descriptors for model transports, artifacts, and event loops."""

    try:
        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):  # pragma: no cover - supported production OS.
        return 32
    if soft_limit == resource.RLIM_INFINITY:
        return 64
    try:
        open_count = len(os.listdir("/proc/self/fd"))
    except OSError:
        open_count = 16
    return max(1, min(64, int(soft_limit) - open_count - 64))


@dataclass(frozen=True, slots=True)
class ResumeRowLocator:
    """Compact identity-bound locator for one source JSONL line."""

    source_id: int
    source_path: str
    source_index: int
    line_number: int
    offset: int
    length: int
    line_sha256: str
    backing: str
    group: str = ""
    task_id: str = ""

    def bind(self, *, group: str, task_id: str) -> ResumeRowLocator:
        if not group or not task_id:
            raise DracoResumeSourceError("resume row identity cannot be empty")
        return replace(self, group=group, task_id=task_id)


@dataclass(frozen=True, slots=True)
class IndexedResumeLine:
    """One streaming line and the locator that remains after its bytes die."""

    payload: bytes
    locator: ResumeRowLocator


@dataclass(slots=True)
class _SourceSnapshot:
    source_id: int
    path: Path
    source_index: int
    signature: tuple[int, int, int, int, int]
    sha256: str
    fd: int | None


class ResumeSourceIndex:
    """Index resume rows against bound source FDs or one private spool."""

    def __init__(
        self,
        resume_paths: list[Path],
        *,
        force_spool: bool | None = None,
    ) -> None:
        self._closed = False
        self._sealed = False
        self._active_source_id: int | None = None
        self._sources: dict[int, _SourceSnapshot] = {}
        self._consumed: set[ResumeRowLocator] = set()
        self._materialized_row_count = 0
        self._peeked_row_count = 0
        self._attempt_payload_load_count = 0
        use_spool = (
            bool(force_spool)
            if force_spool is not None
            else len(resume_paths) > _safe_bound_source_limit()
        )
        self._backing = "spool" if use_spool else "source"
        self._spool: BinaryIO | None = tempfile.TemporaryFile(mode="w+b") if use_spool else None
        self._spool_fd = self._spool.fileno() if self._spool is not None else None
        if self._spool_fd is not None:
            os.fchmod(self._spool_fd, 0o600)
        self._spool_offset = 0
        self._spool_digest = hashlib.sha256()
        self._spool_signature: tuple[int, int, int, int, int] | None = None
        self._spool_sha256 = ""

    @property
    def backing(self) -> str:
        return self._backing

    @property
    def materialized_row_count(self) -> int:
        return self._materialized_row_count

    @property
    def peeked_row_count(self) -> int:
        return self._peeked_row_count

    @property
    def attempt_payload_load_count(self) -> int:
        return self._attempt_payload_load_count

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        if self._closed:
            raise DracoResumeSourceError("resume source index is closed")

    def iter_source(
        self,
        path: Path,
        *,
        source_index: int,
    ) -> Iterator[IndexedResumeLine]:
        """Scan one source once and retain only authenticated line locators."""

        self._require_open()
        if self._sealed:
            raise DracoResumeSourceError("resume source index is already sealed")
        if self._active_source_id is not None:
            raise DracoResumeSourceError("resume sources must be scanned serially")
        source_path = Path(path)
        fd = _open_regular(source_path)
        source_id = len(self._sources)
        start_stat = os.fstat(fd)
        start_signature = _file_signature(start_stat)
        snapshot = _SourceSnapshot(
            source_id=source_id,
            path=source_path,
            source_index=source_index,
            signature=start_signature,
            sha256="",
            fd=fd,
        )
        self._sources[source_id] = snapshot
        self._active_source_id = source_id
        digest = hashlib.sha256()
        source_offset = 0
        completed = False
        try:
            with os.fdopen(fd, "rb", closefd=False) as handle:
                for line_number, line in enumerate(
                    _iter_universal_binary_lines(handle),
                    start=1,
                ):
                    digest.update(line)
                    length = len(line)
                    line_sha256 = _sha256(line)
                    if self._backing == "spool":
                        assert self._spool_fd is not None
                        backing_offset = self._spool_offset
                        _write_all(self._spool_fd, line)
                        self._spool_digest.update(line)
                        self._spool_offset += length
                    else:
                        backing_offset = source_offset
                    locator = ResumeRowLocator(
                        source_id=source_id,
                        source_path=str(source_path),
                        source_index=source_index,
                        line_number=line_number,
                        offset=backing_offset,
                        length=length,
                        line_sha256=line_sha256,
                        backing=self._backing,
                    )
                    source_offset += length
                    yield IndexedResumeLine(payload=line, locator=locator)
                    del line
            end_stat = os.fstat(fd)
            if (
                _file_signature(end_stat) != start_signature
                or source_offset != int(end_stat.st_size)
            ):
                raise DracoResumeSourceError(
                    f"resume JSONL changed while it was scanned: {source_path}"
                )
            try:
                path_stat = os.stat(source_path)
            except OSError as exc:
                raise DracoResumeSourceError(
                    f"resume JSONL path changed while it was scanned: {source_path}"
                ) from exc
            if _file_signature(path_stat) != start_signature:
                raise DracoResumeSourceError(
                    f"resume JSONL path changed while it was scanned: {source_path}"
                )
            snapshot.sha256 = digest.hexdigest()
            completed = True
        finally:
            self._active_source_id = None
            if self._backing == "spool" or not completed:
                try:
                    os.close(fd)
                finally:
                    snapshot.fd = None
            if not completed:
                self._sources.pop(source_id, None)

    @contextmanager
    def open_source(
        self,
        path: Path,
        *,
        source_index: int,
    ) -> Iterator[Iterator[IndexedResumeLine]]:
        """Close an in-progress scan immediately when classification aborts."""

        rows = self.iter_source(path, source_index=source_index)
        try:
            yield rows
        finally:
            rows.close()

    def seal(self) -> None:
        """Durably bind the optional private spool before any worker consumes it."""

        self._require_open()
        if self._active_source_id is not None:
            raise DracoResumeSourceError("cannot seal while a resume source is active")
        if self._sealed:
            return
        if self._spool_fd is not None:
            os.fsync(self._spool_fd)
            spool_stat = os.fstat(self._spool_fd)
            if int(spool_stat.st_size) != self._spool_offset:
                raise DracoResumeSourceError("resume source spool size changed before seal")
            self._spool_signature = _file_signature(spool_stat)
            self._spool_sha256 = self._spool_digest.hexdigest()
        self._sealed = True

    def _locator_fd(self, locator: ResumeRowLocator) -> int:
        if locator.backing != self._backing:
            raise DracoResumeSourceError("resume row locator belongs to another index")
        snapshot = self._sources.get(locator.source_id)
        if snapshot is None:
            raise DracoResumeSourceError("resume row locator has no source snapshot")
        if (
            snapshot.source_index != locator.source_index
            or str(snapshot.path) != locator.source_path
        ):
            raise DracoResumeSourceError("resume row locator source identity changed")
        if self._backing == "spool":
            if self._spool_fd is None:
                raise DracoResumeSourceError("resume source spool is unavailable")
            if self._sealed and (
                self._spool_signature is None
                or _file_signature(os.fstat(self._spool_fd)) != self._spool_signature
            ):
                raise DracoResumeSourceError("resume source spool changed after seal")
            return self._spool_fd
        if snapshot.fd is None:
            raise DracoResumeSourceError("bound resume source descriptor is unavailable")
        if _file_signature(os.fstat(snapshot.fd)) != snapshot.signature:
            raise DracoResumeSourceError(
                f"resume JSONL changed after indexing: {snapshot.path}"
            )
        return snapshot.fd

    def _load_row(
        self,
        locator: ResumeRowLocator,
        *,
        verify_evidence: bool,
    ) -> dict[str, Any]:
        self._require_open()
        if not locator.group or not locator.task_id:
            raise DracoResumeSourceError("resume row locator is not identity-bound")
        fd = self._locator_fd(locator)
        payload = _read_exact(fd, offset=locator.offset, length=locator.length)
        if _sha256(payload) != locator.line_sha256:
            raise DracoResumeSourceError(
                f"resume row changed after indexing at "
                f"{locator.source_path}:{locator.line_number}"
            )
        try:
            text = payload.decode("utf-8")
            value = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DracoResumeSourceError(
                f"indexed resume row is invalid JSON at "
                f"{locator.source_path}:{locator.line_number}"
            ) from exc
        finally:
            del payload
        if not isinstance(value, dict):
            raise DracoResumeSourceError(
                f"indexed resume row is not an object at "
                f"{locator.source_path}:{locator.line_number}"
            )
        if (
            str(value.get("group") or "") != locator.group
            or str(value.get("task_id") or "") != locator.task_id
        ):
            raise DracoResumeSourceError(
                f"indexed resume row changed identity at "
                f"{locator.source_path}:{locator.line_number}"
            )
        if verify_evidence and not verify_result_row_evidence(value):
            raise DracoResumeSourceError(
                f"indexed resume row failed result evidence verification at "
                f"{locator.source_path}:{locator.line_number}"
            )
        return value

    def load_attempt(
        self,
        locator: ResumeRowLocator,
        *,
        attempt_index: int,
        attempt_id: str,
    ) -> dict[str, Any]:
        """Transiently load one strict attempt; never retain its parent row."""

        row = self._load_row(locator, verify_evidence=False)
        execution = row.get("execution")
        attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
        if not isinstance(attempts, list) or not 0 <= attempt_index < len(attempts):
            raise DracoResumeSourceError("strict attempt locator is out of range")
        attempt = attempts[attempt_index]
        if not isinstance(attempt, Mapping) or attempt.get("attempt_id") != attempt_id:
            raise DracoResumeSourceError("strict attempt locator changed identity")
        self._attempt_payload_load_count += 1
        return dict(attempt)

    def consume_row(self, locator: ResumeRowLocator) -> dict[str, Any]:
        """Validate and deliver one scheduled row exactly once."""

        if locator in self._consumed:
            raise DracoResumeSourceError(
                f"resume row was already consumed for {locator.group}/{locator.task_id}"
            )
        value = self._load_row(locator, verify_evidence=True)
        # Mark only after every byte, identity, JSON, and evidence check passed.
        self._consumed.add(locator)
        self._materialized_row_count += 1
        return value

    def peek_row(self, locator: ResumeRowLocator) -> dict[str, Any]:
        """Non-consuming inspection hook for compatibility tests only."""

        value = self._load_row(locator, verify_evidence=True)
        self._peeked_row_count += 1
        return value

    def verify_snapshot(self) -> None:
        """Verify all bound bytes and current source-path identities."""

        self._require_open()
        if not self._sealed:
            raise DracoResumeSourceError("resume source index is not sealed")
        if self._spool_fd is not None:
            spool_before = os.fstat(self._spool_fd)
            if (
                self._spool_signature is None
                or _file_signature(spool_before) != self._spool_signature
                or _hash_fd(self._spool_fd, expected_size=int(spool_before.st_size))
                != self._spool_sha256
                or _file_signature(os.fstat(self._spool_fd)) != self._spool_signature
            ):
                raise DracoResumeSourceError("resume source spool changed after seal")
        for snapshot in self._sources.values():
            verification_fd = snapshot.fd
            close_verification_fd = False
            if verification_fd is None:
                verification_fd = _open_regular(snapshot.path)
                close_verification_fd = True
            try:
                before = os.fstat(verification_fd)
                if (
                    _file_signature(before) != snapshot.signature
                    or _hash_fd(verification_fd, expected_size=int(before.st_size))
                    != snapshot.sha256
                    or _file_signature(os.fstat(verification_fd)) != snapshot.signature
                ):
                    raise DracoResumeSourceError(
                        f"resume JSONL changed after indexing: {snapshot.path}"
                    )
                path_fd = _open_regular(snapshot.path)
                try:
                    if _file_signature(os.fstat(path_fd)) != snapshot.signature:
                        raise DracoResumeSourceError(
                            f"resume JSONL path was replaced after indexing: {snapshot.path}"
                        )
                finally:
                    os.close(path_fd)
            finally:
                if close_verification_fd:
                    os.close(verification_fd)

    def close(self, *, verify: bool = True) -> None:
        if self._closed:
            return
        error: BaseException | None = None
        if verify and self._sealed:
            try:
                self.verify_snapshot()
            except BaseException as exc:  # release every descriptor before surfacing it.
                error = exc
        for snapshot in self._sources.values():
            if snapshot.fd is not None:
                try:
                    os.close(snapshot.fd)
                except OSError:
                    pass
                snapshot.fd = None
        if self._spool is not None:
            try:
                self._spool.close()
            except OSError:
                pass
            self._spool = None
            self._spool_fd = None
        self._closed = True
        if error is not None:
            raise error

    def __enter__(self) -> ResumeSourceIndex:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        # Snapshot verification must fail a successful operation, but must not
        # replace an exception already unwinding from the protected work.
        self.close(verify=exc_type is None)

    def __del__(self) -> None:
        try:
            self.close(verify=False)
        except BaseException:
            pass


class ResumeGroupTaskStates(dict[tuple[str, str], dict[str, Any]]):
    """Dict-compatible state capsules owning their lazy source index."""

    _LOCATOR_KEY = "_source_row_locator"

    def __init__(
        self,
        values: Mapping[tuple[str, str], dict[str, Any]],
        *,
        source_index: ResumeSourceIndex,
    ) -> None:
        super().__init__(values)
        self._source_index = source_index

    @property
    def source_index(self) -> ResumeSourceIndex:
        return self._source_index

    def consume_row(self, key: tuple[str, str]) -> dict[str, Any]:
        state = self[key]
        locator = state.get(self._LOCATOR_KEY)
        if not isinstance(locator, ResumeRowLocator):
            raise DracoResumeSourceError(
                f"resume state has no consumable source row for {key[0]}/{key[1]}"
            )
        row = self._source_index.consume_row(locator)
        state.pop(self._LOCATOR_KEY)
        return row

    def peek_row(self, key: tuple[str, str]) -> dict[str, Any]:
        locator = self[key].get(self._LOCATOR_KEY)
        if not isinstance(locator, ResumeRowLocator):
            raise DracoResumeSourceError(
                f"resume state has no source row for {key[0]}/{key[1]}"
            )
        return self._source_index.peek_row(locator)

    def close(self, *, verify: bool = True) -> None:
        self._source_index.close(verify=verify)

    def __del__(self) -> None:
        try:
            self.close(verify=False)
        except BaseException:
            pass
