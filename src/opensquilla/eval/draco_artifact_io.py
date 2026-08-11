"""Crash-consistent writes for paid DRACO result artifacts.

Result and trace rows are append-only.  A compact checkpoint is advanced only
after both lines have been fully written and fsynced.  JSON serialization stays
in the runner's historical format so existing byte and evidence contracts do
not change.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensquilla.eval.draco_artifact_integrity import (
    RESULT_EVIDENCE_SHA256_FIELD,
    trace_row_from_result,
    verify_result_row_evidence,
)

DRACO_ARTIFACT_CHECKPOINT_SCHEMA = "opensquilla.draco-artifact-checkpoint/v1"
DRACO_RUN_MANIFEST_SCHEMA_V2 = "opensquilla.draco-run-manifest/v2"
DRACO_DURABLE_RESULT_ROW_FIELD = "durable_artifact_capability"
DRACO_DURABLE_ARTIFACT_CAPABILITY_SCHEMA = "opensquilla.draco-durable-artifact-capability/v1"
DRACO_DURABLE_ARTIFACT_FORMAT_VERSION = 1

_write_once = os.write
_replace = os.replace


class DracoArtifactDurabilityError(RuntimeError):
    """Raised when an artifact cannot be written or recovered safely."""


def durable_artifact_capability_contract() -> dict[str, Any]:
    """Return the immutable contract that prevents durable-format downgrade."""

    return {
        "schema": DRACO_DURABLE_ARTIFACT_CAPABILITY_SCHEMA,
        "format_version": DRACO_DURABLE_ARTIFACT_FORMAT_VERSION,
        "checkpoint_schema": DRACO_ARTIFACT_CHECKPOINT_SCHEMA,
        "commit_order": "result_then_trace_then_checkpoint",
        "trace_binding": "deterministic_result_projection",
    }


class DracoArtifactRunLock:
    """Process-scoped advisory lock shared by runners and offline recovery."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DracoArtifactDurabilityError(
                    "DRACO artifacts are locked by an active runner or recovery"
                ) from exc
            os.ftruncate(fd, 0)
            _write_all(fd, f"owner_pid={os.getpid()}\n".encode())
            os.fsync(fd)
        except BaseException:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
            raise
        self._fd = fd

    def close(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> DracoArtifactRunLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte, including when the operating system short-writes."""

    remaining = memoryview(payload)
    while remaining:
        written = _write_once(fd, remaining)
        if written <= 0:
            raise OSError("artifact write made no forward progress")
        remaining = remaining[written:]


def fsync_directory(path: Path) -> None:
    """Persist directory-entry changes for ``path``."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _target_mode(path: Path) -> int:
    try:
        target_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return 0o600
    if stat.S_ISREG(target_stat.st_mode):
        return stat.S_IMODE(target_stat.st_mode)
    return 0o600


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Atomically publish bytes and fsync both the file and parent directory."""

    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd: int | None = None
    created = False
    try:
        fd = os.open(temporary, flags, _target_mode(path))
        created = True
        _write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        _replace(temporary, path)
        created = False
        fsync_directory(path.parent)
    except BaseException:
        if fd is not None:
            os.close(fd)
        if created:
            removed = False
            try:
                os.unlink(temporary)
                removed = True
            except FileNotFoundError:
                pass
            if removed:
                fsync_directory(path.parent)
        raise


def atomic_write_text(path: Path, document: str) -> None:
    atomic_write_bytes(path, document.encode("utf-8"))


def _line_sha256(line: bytes) -> str:
    return f"sha256:{hashlib.sha256(line).hexdigest()}"


def _row_identity(row: dict[str, Any], *, artifact: str) -> tuple[str, str, str]:
    group = row.get("group")
    task_id = row.get("task_id")
    evidence = row.get(RESULT_EVIDENCE_SHA256_FIELD)
    if not isinstance(group, str) or not group:
        raise DracoArtifactDurabilityError(f"{artifact} row has no string group")
    if not isinstance(task_id, str) or not task_id:
        raise DracoArtifactDurabilityError(f"{artifact} row has no string task_id")
    if not isinstance(evidence, str) or not evidence:
        raise DracoArtifactDurabilityError(f"{artifact} row has no {RESULT_EVIDENCE_SHA256_FIELD}")
    return group, task_id, evidence


@dataclass(frozen=True)
class _ArtifactLine:
    identity: tuple[str, str, str]
    sha256: str
    start_offset: int
    end_offset: int


def _serialized_line(row: dict[str, Any]) -> bytes:
    return (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _update_checkpoint_prefix_digest(
    digest: Any,
    result: _ArtifactLine,
    trace: _ArtifactLine,
) -> None:
    """Extend the v1 paired-row digest with one compact line pair."""

    payload = json.dumps(
        [*result.identity, result.sha256, trace.sha256],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest.update(len(payload).to_bytes(8, byteorder="big"))
    digest.update(payload)


def _checkpoint_prefix_digest(
    results: list[_ArtifactLine], traces: list[_ArtifactLine], count: int
) -> str:
    digest = hashlib.sha256()
    for result, trace in zip(results[:count], traces[:count], strict=True):
        _update_checkpoint_prefix_digest(digest, result, trace)
    return f"sha256:{digest.hexdigest()}"


def _checkpoint_payload_for(
    *,
    results_path: Path,
    trace_path: Path,
    results: list[_ArtifactLine],
    traces: list[_ArtifactLine],
    count: int,
    paired_rows_sha256: str | None = None,
) -> dict[str, Any]:
    last_row: dict[str, Any] | None = None
    if count:
        result = results[count - 1]
        trace = traces[count - 1]
        last_row = {
            "group": result.identity[0],
            "task_id": result.identity[1],
            RESULT_EVIDENCE_SHA256_FIELD: result.identity[2],
            "result_line_sha256": result.sha256,
            "trace_line_sha256": trace.sha256,
        }
    return {
        "schema": DRACO_ARTIFACT_CHECKPOINT_SCHEMA,
        "results_file": results_path.name,
        "trace_file": trace_path.name,
        "rows_written": count,
        "results_bytes": results[count - 1].end_offset if count else 0,
        "trace_bytes": traces[count - 1].end_offset if count else 0,
        "paired_rows_sha256": (
            paired_rows_sha256
            if paired_rows_sha256 is not None
            else _checkpoint_prefix_digest(results, traces, count)
        ),
        "last_row": last_row,
    }


def _open_readonly_artifact(path: Path, *, artifact: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DracoArtifactDurabilityError(
            f"{artifact} artifact is not a regular non-symlink file"
        ) from exc
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except BaseException:
        os.close(fd)
        raise
    if not regular:
        os.close(fd)
        raise DracoArtifactDurabilityError(
            f"{artifact} artifact is not a regular non-symlink file"
        )
    return fd


def _parse_readonly_result_line(
    line: bytes,
    *,
    line_number: int,
) -> dict[str, Any]:
    if not line.endswith(b"\n"):
        raise DracoArtifactDurabilityError(
            f"unterminated result row at line {line_number}"
        )
    if line == b"\n":
        raise DracoArtifactDurabilityError(f"blank result row at line {line_number}")
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DracoArtifactDurabilityError(
            f"invalid result JSON at line {line_number}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise DracoArtifactDurabilityError(
            f"result line {line_number} is not a JSON object"
        )
    if not verify_result_row_evidence(value):
        raise DracoArtifactDurabilityError(
            f"result evidence verification failed at line {line_number}"
        )
    return value


def iter_verified_result_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Yield sealed result rows without retaining prior payloads in memory."""

    path = Path(path)
    fd = _open_readonly_artifact(path, artifact="result")
    seen: set[tuple[str, str, str]] = set()
    with os.fdopen(fd, "rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            value = _parse_readonly_result_line(line, line_number=line_number)
            identity = _row_identity(value, artifact="result")
            if identity in seen:
                raise DracoArtifactDurabilityError(
                    f"duplicate sealed result row for {identity!r}"
                )
            seen.add(identity)
            del line
            yield value


def verify_durable_draco_artifacts(
    *,
    results_path: Path,
    trace_path: Path,
    checkpoint_path: Path,
) -> dict[str, Any]:
    """Verify a finalized result/trace/checkpoint set without mutating it."""

    results_path = Path(results_path)
    trace_path = Path(trace_path)
    checkpoint_path = Path(checkpoint_path)
    results_fd = _open_readonly_artifact(results_path, artifact="result")
    try:
        trace_fd = _open_readonly_artifact(trace_path, artifact="trace")
    except BaseException:
        os.close(results_fd)
        raise
    try:
        checkpoint_fd = _open_readonly_artifact(
            checkpoint_path,
            artifact="checkpoint",
        )
    except BaseException:
        os.close(trace_fd)
        os.close(results_fd)
        raise
    results: list[_ArtifactLine] = []
    traces: list[_ArtifactLine] = []
    result_identities: set[tuple[str, str, str]] = set()
    trace_identities: set[tuple[str, str, str]] = set()
    durable_result_keys: list[tuple[str, str]] = []
    durable_capability = durable_artifact_capability_contract()
    results_digest = hashlib.sha256()
    trace_digest = hashlib.sha256()
    paired_digest = hashlib.sha256()
    results_offset = 0
    trace_offset = 0
    with os.fdopen(results_fd, "rb") as results_handle:
        with os.fdopen(trace_fd, "rb") as trace_handle:
            with os.fdopen(checkpoint_fd, "rb") as checkpoint_handle:
                for line_number, result_line in enumerate(results_handle, start=1):
                    result_start = results_offset
                    results_offset += len(result_line)
                    results_digest.update(result_line)
                    result_row = _parse_readonly_result_line(
                        result_line,
                        line_number=line_number,
                    )
                    result_identity = _row_identity(result_row, artifact="result")
                    if result_identity in result_identities:
                        raise DracoArtifactDurabilityError(
                            f"duplicate sealed result row for {result_identity!r}"
                        )
                    result_identities.add(result_identity)
                    if result_row.get(DRACO_DURABLE_RESULT_ROW_FIELD) == durable_capability:
                        durable_result_keys.append(
                            (result_identity[0], result_identity[1])
                        )
                    result = _ArtifactLine(
                        identity=result_identity,
                        sha256=_line_sha256(result_line),
                        start_offset=result_start,
                        end_offset=results_offset,
                    )
                    results.append(result)
                    del result_line

                    trace_line = trace_handle.readline()
                    if not trace_line:
                        raise DracoArtifactDurabilityError(
                            "finalized result and trace artifacts are not fully paired"
                        )
                    trace_start = trace_offset
                    trace_offset += len(trace_line)
                    trace_digest.update(trace_line)
                    if not trace_line.endswith(b"\n"):
                        raise DracoArtifactDurabilityError(
                            f"unterminated trace row at line {line_number}"
                        )
                    if trace_line == b"\n":
                        raise DracoArtifactDurabilityError(
                            f"blank trace row at line {line_number}"
                        )
                    expected = _serialized_line(trace_row_from_result(result_row))
                    del result_row
                    if trace_line != expected:
                        raise DracoArtifactDurabilityError(
                            f"trace projection mismatch at row {line_number}"
                        )
                    trace_identity = result_identity
                    if trace_identity in trace_identities:
                        raise DracoArtifactDurabilityError(
                            f"duplicate sealed trace row for {trace_identity!r}"
                        )
                    trace_identities.add(trace_identity)
                    trace = _ArtifactLine(
                        identity=trace_identity,
                        sha256=_line_sha256(trace_line),
                        start_offset=trace_start,
                        end_offset=trace_offset,
                    )
                    traces.append(trace)
                    _update_checkpoint_prefix_digest(
                        paired_digest,
                        result,
                        trace,
                    )
                if trace_handle.read(1):
                    raise DracoArtifactDurabilityError(
                        "trace artifact is ahead of results artifact"
                    )
                checkpoint_payload = checkpoint_handle.read()
                checkpoint_digest = hashlib.sha256(checkpoint_payload).hexdigest()
    try:
        checkpoint = json.loads(checkpoint_payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DracoArtifactDurabilityError(f"invalid artifact checkpoint: {exc}") from exc
    expected = _checkpoint_payload_for(
        results_path=results_path,
        trace_path=trace_path,
        results=results,
        traces=traces,
        count=len(results),
        paired_rows_sha256=f"sha256:{paired_digest.hexdigest()}",
    )
    if checkpoint != expected:
        raise DracoArtifactDurabilityError(
            "artifact checkpoint conflicts with finalized durable rows"
        )
    return {
        **expected,
        "results_sha256": results_digest.hexdigest(),
        "trace_sha256": trace_digest.hexdigest(),
        "checkpoint_sha256": checkpoint_digest,
        "durable_result_keys": tuple(durable_result_keys),
    }


class DurableDracoArtifactWriter:
    """Append and recover paired result/trace rows without duplicating evidence."""

    def __init__(
        self,
        *,
        results_path: Path,
        trace_path: Path,
        checkpoint_path: Path,
        create: bool = True,
    ) -> None:
        self.results_path = Path(results_path)
        self.trace_path = Path(trace_path)
        self.checkpoint_path = Path(checkpoint_path)
        self._results_fd: int | None = None
        self._trace_fd: int | None = None
        self._results: list[_ArtifactLine] = []
        self._traces: list[_ArtifactLine] = []
        self._result_by_identity: dict[tuple[str, str, str], _ArtifactLine] = {}
        self._trace_by_identity: dict[tuple[str, str, str], _ArtifactLine] = {}
        self._paired_digest = hashlib.sha256()
        self._paired_prefix_digests: list[str] = []
        self._broken = False
        self._closed = False
        try:
            if create:
                self._open_new_files()
            else:
                self._open_existing_files()
            self._validate_pair_order()
            self._rebuild_paired_prefix_digests()
            checkpoint_count = self._load_checkpoint_count()
            if checkpoint_count != self.paired_row_count:
                self._publish_checkpoint()
            elif not create:
                # A prior checkpoint replace may have become visible before its
                # parent-directory fsync failed.  Reopening an exact checkpoint
                # must complete that pending durability barrier.
                fsync_directory(self.checkpoint_path.parent)
        except BaseException:
            self.close()
            raise

    @property
    def paired_row_count(self) -> int:
        return len(self._traces)

    @property
    def paired_result_rows(self) -> list[dict[str, Any]]:
        """Return detached result objects for every fully paired durable row."""

        return list(self.iter_paired_result_rows())

    def iter_paired_result_rows(self) -> Iterator[dict[str, Any]]:
        """Yield paired result objects while retaining only compact line metadata."""

        if self._closed or self._results_fd is None:
            raise DracoArtifactDurabilityError("artifact writer is closed")
        for line in self._results[: self.paired_row_count]:
            yield self._read_indexed_row(
                self._results_fd,
                line,
                artifact="result",
            )

    def __enter__(self) -> DurableDracoArtifactWriter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        for name in ("_results_fd", "_trace_fd"):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
        self._closed = True

    def _open_new_files(self) -> None:
        flags = (
            os.O_CREAT
            | os.O_EXCL
            | os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        created: list[Path] = []
        try:
            self._results_fd = os.open(self.results_path, flags, 0o600)
            created.append(self.results_path)
            self._trace_fd = os.open(self.trace_path, flags, 0o600)
            created.append(self.trace_path)
            if hasattr(os, "fchmod"):
                os.fchmod(self._results_fd, 0o600)
                os.fchmod(self._trace_fd, 0o600)
            os.fsync(self._results_fd)
            os.fsync(self._trace_fd)
            for directory in {self.results_path.parent, self.trace_path.parent}:
                fsync_directory(directory)
            self._publish_checkpoint()
        except BaseException:
            self.close()
            for path in reversed(created):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            for directory in {path.parent for path in created}:
                fsync_directory(directory)
            raise

    def _open_existing_files(self) -> None:
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        self._results_fd = os.open(self.results_path, flags)
        self._trace_fd = os.open(self.trace_path, flags)
        self._results = self._scan_file(self._results_fd, artifact="result")
        self._traces = self._scan_file(self._trace_fd, artifact="trace")
        self._result_by_identity = self._index_lines(self._results, artifact="result")
        self._trace_by_identity = self._index_lines(self._traces, artifact="trace")

    def _scan_file(self, fd: int, *, artifact: str) -> list[_ArtifactLine]:
        lines: list[_ArtifactLine] = []
        offset = 0
        with os.fdopen(os.dup(fd), "rb") as handle:
            for line_number, line in enumerate(handle, start=1):
                start_offset = offset
                offset += len(line)
                terminated = line.endswith(b"\n")
                candidate = line if terminated else line + b"\n"
                if candidate == b"\n":
                    raise DracoArtifactDurabilityError(
                        f"blank {artifact} row at line {line_number}"
                    )
                try:
                    value = json.loads(candidate)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    if not terminated:
                        self._truncate_torn_tail(
                            fd,
                            start_offset=start_offset,
                            artifact=artifact,
                        )
                        break
                    raise DracoArtifactDurabilityError(
                        f"invalid {artifact} JSON at line {line_number}: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    if not terminated:
                        self._truncate_torn_tail(
                            fd,
                            start_offset=start_offset,
                            artifact=artifact,
                        )
                        break
                    raise DracoArtifactDurabilityError(
                        f"{artifact} line {line_number} is not a JSON object"
                    )
                if artifact == "result" and not verify_result_row_evidence(value):
                    if not terminated:
                        self._truncate_torn_tail(
                            fd,
                            start_offset=start_offset,
                            artifact=artifact,
                        )
                        break
                    raise DracoArtifactDurabilityError(
                        f"result evidence verification failed at line {line_number}"
                    )
                try:
                    identity = _row_identity(value, artifact=artifact)
                except DracoArtifactDurabilityError:
                    if not terminated:
                        self._truncate_torn_tail(
                            fd,
                            start_offset=start_offset,
                            artifact=artifact,
                        )
                        break
                    raise
                if artifact == "trace":
                    if len(lines) >= len(self._results):
                        if not terminated:
                            self._truncate_torn_tail(
                                fd,
                                start_offset=start_offset,
                                artifact=artifact,
                            )
                            break
                        raise DracoArtifactDurabilityError(
                            "trace artifact is ahead of results artifact"
                        )
                    result = self._results[len(lines)]
                    assert self._results_fd is not None
                    result_row = self._read_indexed_row(
                        self._results_fd,
                        result,
                        artifact="result",
                    )
                    expected = _serialized_line(trace_row_from_result(result_row))
                    if identity != result.identity or candidate != expected:
                        if not terminated:
                            self._truncate_torn_tail(
                                fd,
                                start_offset=start_offset,
                                artifact=artifact,
                            )
                            break
                        raise DracoArtifactDurabilityError(
                            f"trace projection mismatch at row {line_number}"
                        )
                if not terminated:
                    os.lseek(fd, 0, os.SEEK_END)
                    _write_all(fd, b"\n")
                    os.fsync(fd)
                    offset += 1
                    fsync_directory(
                        self.results_path.parent if artifact == "result" else self.trace_path.parent
                    )
                    line = candidate
                lines.append(
                    _ArtifactLine(
                        identity=identity,
                        sha256=_line_sha256(line),
                        start_offset=start_offset,
                        end_offset=offset,
                    )
                )
        os.lseek(fd, 0, os.SEEK_END)
        return lines

    @staticmethod
    def _read_indexed_payload(fd: int, line: _ArtifactLine) -> bytes:
        remaining = line.end_offset - line.start_offset
        offset = line.start_offset
        chunks: list[bytes] = []
        while remaining:
            chunk = os.pread(fd, remaining, offset)
            if not chunk:
                raise DracoArtifactDurabilityError(
                    "artifact row changed after it was indexed"
                )
            chunks.append(chunk)
            offset += len(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if _line_sha256(payload) != line.sha256:
            raise DracoArtifactDurabilityError(
                "artifact row changed after it was indexed"
            )
        return payload

    @classmethod
    def _read_indexed_row(
        cls,
        fd: int,
        line: _ArtifactLine,
        *,
        artifact: str,
    ) -> dict[str, Any]:
        payload = cls._read_indexed_payload(fd, line)
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DracoArtifactDurabilityError(
                f"indexed {artifact} row is no longer valid JSON"
            ) from exc
        if not isinstance(value, dict) or _row_identity(value, artifact=artifact) != line.identity:
            raise DracoArtifactDurabilityError(
                f"indexed {artifact} row changed identity"
            )
        if artifact == "result" and not verify_result_row_evidence(value):
            raise DracoArtifactDurabilityError(
                "indexed result row failed evidence verification"
            )
        return value

    def _truncate_torn_tail(
        self,
        fd: int,
        *,
        start_offset: int,
        artifact: str,
    ) -> None:
        os.ftruncate(fd, start_offset)
        os.fsync(fd)
        fsync_directory(
            self.results_path.parent if artifact == "result" else self.trace_path.parent
        )

    @staticmethod
    def _index_lines(
        lines: list[_ArtifactLine], *, artifact: str
    ) -> dict[tuple[str, str, str], _ArtifactLine]:
        indexed: dict[tuple[str, str, str], _ArtifactLine] = {}
        for line in lines:
            if line.identity in indexed:
                raise DracoArtifactDurabilityError(
                    f"duplicate sealed {artifact} row for {line.identity!r}"
                )
            indexed[line.identity] = line
        return indexed

    def _validate_pair_order(self) -> None:
        if len(self._traces) > len(self._results):
            raise DracoArtifactDurabilityError("trace artifact is ahead of results artifact")
        for index, trace in enumerate(self._traces):
            result = self._results[index]
            if trace.identity != result.identity:
                raise DracoArtifactDurabilityError(
                    f"result/trace projection mismatch at row {index + 1}"
                )
        if len(self._results) - len(self._traces) > 1:
            raise DracoArtifactDurabilityError(
                "results artifact contains more than one unpaired row"
            )

    def _rebuild_paired_prefix_digests(self) -> None:
        digest = hashlib.sha256()
        prefixes: list[str] = []
        for result, trace in zip(
            self._results[: self.paired_row_count],
            self._traces,
            strict=True,
        ):
            _update_checkpoint_prefix_digest(digest, result, trace)
            prefixes.append(f"sha256:{digest.hexdigest()}")
        self._paired_digest = digest
        self._paired_prefix_digests = prefixes

    def _record_paired_prefix_digest(
        self,
        result: _ArtifactLine,
        trace: _ArtifactLine,
    ) -> None:
        if len(self._paired_prefix_digests) + 1 != self.paired_row_count:
            raise DracoArtifactDurabilityError(
                "paired digest state is not aligned with durable rows"
            )
        _update_checkpoint_prefix_digest(self._paired_digest, result, trace)
        self._paired_prefix_digests.append(
            f"sha256:{self._paired_digest.hexdigest()}"
        )

    def _paired_prefix_digest(self, count: int) -> str:
        if count == 0:
            return f"sha256:{hashlib.sha256().hexdigest()}"
        try:
            return self._paired_prefix_digests[count - 1]
        except IndexError as exc:
            raise DracoArtifactDurabilityError(
                "paired digest prefix is outside durable rows"
            ) from exc

    def _checkpoint_payload(self, count: int | None = None) -> dict[str, Any]:
        if count is None:
            count = self.paired_row_count
        return _checkpoint_payload_for(
            results_path=self.results_path,
            trace_path=self.trace_path,
            results=self._results,
            traces=self._traces,
            count=count,
            paired_rows_sha256=self._paired_prefix_digest(count),
        )

    def _load_checkpoint_count(self) -> int:
        try:
            document = self.checkpoint_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        try:
            payload = json.loads(document)
        except json.JSONDecodeError as exc:
            raise DracoArtifactDurabilityError(f"invalid artifact checkpoint: {exc}") from exc
        if not isinstance(payload, dict):
            raise DracoArtifactDurabilityError("artifact checkpoint is not a JSON object")
        count = payload.get("rows_written")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise DracoArtifactDurabilityError("artifact checkpoint rows_written is invalid")
        if count > self.paired_row_count:
            raise DracoArtifactDurabilityError("artifact checkpoint is ahead of durable rows")
        expected = self._checkpoint_payload(count)
        if payload != expected:
            raise DracoArtifactDurabilityError("artifact checkpoint conflicts with durable rows")
        return count

    def _publish_checkpoint(self) -> None:
        document = (
            json.dumps(self._checkpoint_payload(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        atomic_write_text(self.checkpoint_path, document)

    @staticmethod
    def _append_line(fd: int, payload: bytes) -> int:
        start_offset = os.lseek(fd, 0, os.SEEK_END)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        except BaseException:
            try:
                os.ftruncate(fd, start_offset)
                os.fsync(fd)
            except BaseException:
                pass
            raise
        return start_offset + len(payload)

    def append(self, result: dict[str, Any], trace: dict[str, Any]) -> bool:
        """Durably append a pair; return false when identical evidence is present."""

        if self._closed:
            raise DracoArtifactDurabilityError("artifact writer is closed")
        if self._broken:
            raise DracoArtifactDurabilityError(
                "artifact writer must be closed and reopened after a write failure"
            )
        if not verify_result_row_evidence(result):
            raise DracoArtifactDurabilityError("result row evidence is not valid")
        result_identity = _row_identity(result, artifact="result")
        trace_identity = _row_identity(trace, artifact="trace")
        if trace_identity != result_identity:
            raise DracoArtifactDurabilityError("result and trace identities do not match")
        result_payload = _serialized_line(result)
        trace_payload = _serialized_line(trace)
        expected_trace_payload = _serialized_line(trace_row_from_result(result))
        if trace_payload != expected_trace_payload:
            raise DracoArtifactDurabilityError(
                "trace row is not the deterministic projection of its result row"
            )
        existing_result = self._result_by_identity.get(result_identity)
        existing_trace = self._trace_by_identity.get(result_identity)
        assert self._results_fd is not None
        assert self._trace_fd is not None
        if existing_result is not None and (
            self._read_indexed_payload(self._results_fd, existing_result)
            != result_payload
        ):
            raise DracoArtifactDurabilityError(
                "sealed result evidence conflicts with the existing result bytes"
            )
        if existing_trace is not None and (
            self._read_indexed_payload(self._trace_fd, existing_trace)
            != trace_payload
        ):
            raise DracoArtifactDurabilityError(
                "sealed result evidence conflicts with the existing trace bytes"
            )
        if existing_trace is not None:
            if existing_result is None:
                raise DracoArtifactDurabilityError("trace row exists without its result row")
            if self._load_checkpoint_count() != self.paired_row_count:
                try:
                    self._publish_checkpoint()
                except BaseException:
                    self._broken = True
                    raise
            return False
        if existing_result is None and len(self._results) != len(self._traces):
            raise DracoArtifactDurabilityError(
                "an unpaired result must be repaired before appending another row"
            )

        try:
            result_line = existing_result
            if existing_result is None:
                result_line = _ArtifactLine(
                    identity=result_identity,
                    sha256=_line_sha256(result_payload),
                    start_offset=os.lseek(self._results_fd, 0, os.SEEK_END),
                    end_offset=self._append_line(self._results_fd, result_payload),
                )
                self._results.append(result_line)
                self._result_by_identity[result_identity] = result_line
            assert result_line is not None
            trace_line = _ArtifactLine(
                identity=trace_identity,
                sha256=_line_sha256(trace_payload),
                start_offset=os.lseek(self._trace_fd, 0, os.SEEK_END),
                end_offset=self._append_line(self._trace_fd, trace_payload),
            )
            self._traces.append(trace_line)
            self._trace_by_identity[trace_identity] = trace_line
            self._record_paired_prefix_digest(result_line, trace_line)
            self._publish_checkpoint()
        except BaseException:
            self._broken = True
            raise
        return True

    def repair_unpaired_result(self) -> bool:
        """Complete the sole durable result whose trace was interrupted."""

        if len(self._results) == len(self._traces):
            return False
        assert self._results_fd is not None
        result = self._read_indexed_row(
            self._results_fd,
            self._results[-1],
            artifact="result",
        )
        return self.append(result, trace_row_from_result(result))


@dataclass(frozen=True)
class DracoArtifactAppendOutcome:
    committed: bool
    paired_row_count: int


class AsyncDurableDracoArtifactWriter:
    """Cancellation-safe, event-loop-friendly single-flight artifact writer."""

    def __init__(self, writer: DurableDracoArtifactWriter) -> None:
        self._writer = writer
        self._lock = asyncio.Lock()
        self._inflight: asyncio.Task[bool] | None = None
        self._state = "idle"

    @property
    def state(self) -> str:
        return self._state

    @property
    def paired_row_count(self) -> int:
        return self._writer.paired_row_count

    @property
    def paired_result_rows(self) -> list[dict[str, Any]]:
        return self._writer.paired_result_rows

    async def __aenter__(self) -> AsyncDurableDracoArtifactWriter:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def append(
        self,
        result: dict[str, Any],
        trace: dict[str, Any],
    ) -> DracoArtifactAppendOutcome:
        async with self._lock:
            if self._state == "closed":
                raise DracoArtifactDurabilityError("artifact writer is closed")
            if self._state == "broken":
                raise DracoArtifactDurabilityError(
                    "artifact writer must be reopened after a write failure"
                )
            self._state = "in_flight"
            task = asyncio.create_task(asyncio.to_thread(self._writer.append, result, trace))
            self._inflight = task
            cancellation: asyncio.CancelledError | None = None
            try:
                while True:
                    try:
                        committed = await asyncio.shield(task)
                        break
                    except asyncio.CancelledError as exc:
                        if task.done() and task.cancelled():
                            raise DracoArtifactDurabilityError(
                                "artifact write worker was cancelled before settlement"
                            ) from exc
                        cancellation = exc
                        self._state = "settling_cancel"
                        continue
            except BaseException:
                self._state = "broken"
                raise
            finally:
                self._inflight = None
            self._state = "idle"
            outcome = DracoArtifactAppendOutcome(
                committed=committed,
                paired_row_count=self._writer.paired_row_count,
            )
            if cancellation is not None:
                raise cancellation
            return outcome

    async def repair_unpaired_result(self) -> DracoArtifactAppendOutcome:
        async with self._lock:
            if self._state != "idle":
                raise DracoArtifactDurabilityError(
                    f"artifact writer cannot repair while {self._state}"
                )
            self._state = "in_flight"
            task = asyncio.create_task(asyncio.to_thread(self._writer.repair_unpaired_result))
            self._inflight = task
            cancellation: asyncio.CancelledError | None = None
            try:
                while True:
                    try:
                        committed = await asyncio.shield(task)
                        break
                    except asyncio.CancelledError as exc:
                        if task.done() and task.cancelled():
                            raise DracoArtifactDurabilityError(
                                "artifact repair worker was cancelled before settlement"
                            ) from exc
                        cancellation = exc
                        self._state = "settling_cancel"
                        continue
            except BaseException:
                self._state = "broken"
                raise
            finally:
                self._inflight = None
            self._state = "idle"
            outcome = DracoArtifactAppendOutcome(
                committed=committed,
                paired_row_count=self._writer.paired_row_count,
            )
            if cancellation is not None:
                raise cancellation
            return outcome

    async def aclose(self) -> None:
        async with self._lock:
            if self._state == "closed":
                return
            if self._inflight is not None:
                raise DracoArtifactDurabilityError(
                    "artifact writer close raced an unsettled append"
                )
            self._state = "in_flight"
            task = asyncio.create_task(asyncio.to_thread(self._writer.close))
            self._inflight = task
            cancellation: asyncio.CancelledError | None = None
            try:
                while True:
                    try:
                        await asyncio.shield(task)
                        break
                    except asyncio.CancelledError as exc:
                        if task.done() and task.cancelled():
                            raise DracoArtifactDurabilityError(
                                "artifact close worker was cancelled before settlement"
                            ) from exc
                        cancellation = exc
                        self._state = "settling_cancel"
                        continue
            except BaseException:
                self._state = "broken"
                raise
            finally:
                self._inflight = None
            self._state = "closed"
            if cancellation is not None:
                raise cancellation
