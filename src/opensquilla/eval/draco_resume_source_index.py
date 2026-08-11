"""Bound, lazy access to historical DRACO resume result rows.

The resume runner has to classify every historical row, but only scheduled
repair work needs the full winning row later.  This module keeps compact byte
locators and authenticated source snapshots so classification does not turn
the whole archive into resident Python objects.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import resource
import stat
import tempfile
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO

from opensquilla.eval.draco_artifact_integrity import (
    seal_result_row,
    verify_result_row_evidence,
)
from opensquilla.eval.draco_artifact_io import (
    DRACO_RUN_MANIFEST_SCHEMA_V2,
    DracoArtifactDurabilityError,
    durable_artifact_capability_contract,
    verify_durable_artifact_path_snapshots,
    verify_durable_draco_artifacts,
)
from opensquilla.eval.draco_selection_plan_evidence import (
    SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD,
    SELECTION_PLAN_EVIDENCE_ROW_FIELD,
    SELECTION_PLAN_PACK_ARTIFACT_FIELD,
    SelectionPlanEvidenceError,
    SelectionPlanPackIndex,
    SelectionPlanPackReader,
    lazy_selection_plan_row_view,
    materialize_selection_plan_row_view,
    selection_plan_evidence_capability_contract,
    selection_plan_reference_signal,
    selection_plan_row_capability_signal,
    validate_compact_selection_plan_evidence_row_structure,
    validate_selection_plan_evidence_manifest_binding,
)


class DracoResumeSourceError(ValueError):
    """A resume source or one of its indexed rows changed unexpectedly."""


_SIGNATURE_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)
_HASH_CHUNK_BYTES = 1024 * 1024
_SOURCE_READ_CHUNK_BYTES = 1024 * 1024
_UNIVERSAL_NEWLINE_RE = re.compile(rb"\r\n?|\n")
_STANDARD_RESULT_NAME_RE = re.compile(
    r"^draco_ensemble_(?P<stamp>[0-9]{8}-[0-9]{6})\.jsonl$"
)
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_COMPACT_TERMINAL_STATUSES = frozenset(
    {
        "aborted",
        "complete",
        "judge_incomplete",
        "metadata_incomplete",
        "result_incomplete",
    }
)
DRACO_RESUME_SOURCE_ARTIFACT_EVIDENCE_SCHEMA = (
    "opensquilla.draco-resume-source-artifact-evidence/v1"
)


def _file_signature(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return tuple(int(getattr(value, field)) for field in _SIGNATURE_FIELDS)  # type: ignore[return-value]


def _sha256(value: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(value).hexdigest()


def _pack_identity(index: SelectionPlanPackIndex) -> tuple[int, ...]:
    return (
        index.device,
        index.inode,
        index.mode,
        index.pack_bytes,
        index.mtime_ns,
        index.ctime_ns,
    )


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


def _open_regular(path: Path, *, label: str = "resume JSONL") -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DracoResumeSourceError(f"cannot open {label}: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DracoResumeSourceError(f"{label} is not a regular file: {path}")
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


def _read_json_object_snapshot(
    path: Path,
    *,
    label: str,
) -> tuple[dict[str, Any], tuple[int, ...], str]:
    """Read one regular JSON object and bind its pathname identity."""

    fd = _open_regular(path, label=label)
    try:
        before = os.fstat(fd)
        signature = _file_signature(before)
        size = int(before.st_size)
        if size <= 0 or size > _MAX_MANIFEST_BYTES:
            raise DracoResumeSourceError(f"{label} is outside its byte bound")
        payload = bytes(_read_exact(fd, offset=0, length=size))
        if _file_signature(os.fstat(fd)) != signature:
            raise DracoResumeSourceError(f"{label} changed while it was read")
        path_fd = _open_regular(path, label=label)
        try:
            if _file_signature(os.fstat(path_fd)) != signature:
                raise DracoResumeSourceError(
                    f"{label} path was replaced while it was read"
                )
        finally:
            os.close(path_fd)
    finally:
        os.close(fd)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DracoResumeSourceError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise DracoResumeSourceError(f"{label} is not a JSON object")
    return value, signature, _sha256(payload)


def _verify_json_object_snapshot(
    path: Path,
    *,
    label: str,
    expected_signature: tuple[int, ...],
    expected_sha256: str,
) -> None:
    fd = _open_regular(path, label=label)
    try:
        file_stat = os.fstat(fd)
        if _file_signature(file_stat) != expected_signature:
            raise DracoResumeSourceError(f"{label} path changed after binding")
        if _hash_fd(fd, expected_size=int(file_stat.st_size)) != expected_sha256:
            raise DracoResumeSourceError(f"{label} content changed after binding")
        if _file_signature(os.fstat(fd)) != expected_signature:
            raise DracoResumeSourceError(f"{label} changed during verification")
        path_fd = _open_regular(path, label=label)
        try:
            if _file_signature(os.fstat(path_fd)) != expected_signature:
                raise DracoResumeSourceError(
                    f"{label} path was replaced during verification"
                )
        finally:
            os.close(path_fd)
    finally:
        os.close(fd)


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
    parsed_row: dict[str, Any] | None = None


@dataclass(slots=True)
class _CompactSourceBundle:
    results_path: Path
    trace_path: Path
    checkpoint_path: Path
    manifest_path: Path
    pack_path: Path
    manifest_signature: tuple[int, ...]
    manifest_sha256: str
    artifact_path_snapshots: dict[str, tuple[int, ...]]
    durable_verification: dict[str, Any]
    binding: dict[str, Any]
    pack_index: SelectionPlanPackIndex
    reader: SelectionPlanPackReader | None


@dataclass(slots=True)
class _SourceSnapshot:
    source_id: int
    path: Path
    source_index: int
    signature: tuple[int, int, int, int, int, int]
    sha256: str
    fd: int | None
    compact_bundle: _CompactSourceBundle | None


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
        self._active_line_identity: tuple[int, int, str] | None = None
        self._sources: dict[int, _SourceSnapshot] = {}
        self._consumed: set[ResumeRowLocator] = set()
        self._materialized_row_count = 0
        self._peeked_row_count = 0
        self._attempt_payload_load_count = 0
        self._selection_plan_classification_materialization_count = 0
        self._selection_plan_materialized_row_count = 0
        use_spool = (
            bool(force_spool)
            if force_spool is not None
            else len(resume_paths) * 2 > _safe_bound_source_limit()
        )
        self._backing = "spool" if use_spool else "source"
        self._spool: BinaryIO | None = tempfile.TemporaryFile(mode="w+b") if use_spool else None
        self._spool_fd = self._spool.fileno() if self._spool is not None else None
        if self._spool_fd is not None:
            os.fchmod(self._spool_fd, 0o600)
        self._spool_offset = 0
        self._spool_digest = hashlib.sha256()
        self._spool_signature: tuple[int, int, int, int, int, int] | None = None
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
    def selection_plan_classification_materialization_count(self) -> int:
        return self._selection_plan_classification_materialization_count

    @property
    def selection_plan_materialized_row_count(self) -> int:
        return self._selection_plan_materialized_row_count

    @property
    def closed(self) -> bool:
        return self._closed

    def source_is_compact_authenticated(self, *, source_index: int) -> bool:
        """Report a completed source's bound compact state without doing I/O."""

        self._require_open()
        if (
            isinstance(source_index, bool)
            or not isinstance(source_index, int)
            or source_index < 0
        ):
            raise DracoResumeSourceError("resume source index must be non-negative")
        if self._active_source_id is not None:
            raise DracoResumeSourceError(
                "cannot inspect compact authentication during a source scan"
            )
        matches = [
            snapshot
            for snapshot in self._sources.values()
            if snapshot.source_index == source_index
        ]
        if not matches:
            raise DracoResumeSourceError(
                f"resume source index has no completed source {source_index}"
            )
        if len(matches) != 1:
            raise DracoResumeSourceError(
                f"resume source index is not unique for source {source_index}"
            )
        return matches[0].compact_bundle is not None

    def source_artifact_evidence(self, *, source_index: int) -> dict[str, Any]:
        """Return bounded metadata already authenticated by a sealed scan.

        This accessor performs no I/O, expansion, hashing, or pack scan.  Its
        detached primitive values let downstream consumers bind their own
        reads to this exact source lifecycle without exposing descriptors,
        readers, object locations, or unbounded row identities.
        """

        self._require_open()
        if (
            isinstance(source_index, bool)
            or not isinstance(source_index, int)
            or source_index < 0
        ):
            raise DracoResumeSourceError("resume source index must be non-negative")
        if self._active_source_id is not None:
            raise DracoResumeSourceError(
                "cannot inspect artifact evidence during a source scan"
            )
        if not self._sealed:
            raise DracoResumeSourceError(
                "resume source index must be sealed before reading artifact evidence"
            )
        matches = [
            snapshot
            for snapshot in self._sources.values()
            if snapshot.source_index == source_index
        ]
        if not matches:
            raise DracoResumeSourceError(
                f"resume source index has no completed source {source_index}"
            )
        if len(matches) != 1:
            raise DracoResumeSourceError(
                f"resume source index is not unique for source {source_index}"
            )
        snapshot = matches[0]
        if not snapshot.sha256:
            raise DracoResumeSourceError(
                f"resume source index has no completed digest for source {source_index}"
            )
        bundle = snapshot.compact_bundle
        compact_evidence: dict[str, Any] | None = None
        if bundle is not None:
            durable_hashes: dict[str, str] = {}
            for key in (
                "results_sha256",
                "trace_sha256",
                "checkpoint_sha256",
            ):
                value = bundle.durable_verification.get(key)
                if not isinstance(value, str):
                    raise DracoResumeSourceError(
                        "compact resume durable hash evidence is malformed"
                    )
                durable_hashes[key] = value
            path_signatures: dict[str, tuple[int, ...]] = {}
            for key in (
                "results_jsonl",
                "trace_jsonl",
                "checkpoint_json",
            ):
                signature = bundle.artifact_path_snapshots.get(key)
                if not isinstance(signature, tuple):
                    raise DracoResumeSourceError(
                        "compact resume path signature evidence is malformed"
                    )
                path_signatures[key] = tuple(int(item) for item in signature)
            compact_evidence = {
                "durable_hashes": durable_hashes,
                "path_signatures": path_signatures,
                "manifest_snapshot": {
                    "signature": tuple(
                        int(item) for item in bundle.manifest_signature
                    ),
                    "sha256": str(bundle.manifest_sha256),
                },
            }
        return {
            "schema": DRACO_RESUME_SOURCE_ARTIFACT_EVIDENCE_SCHEMA,
            "source_index": source_index,
            "result_snapshot": {
                "signature": tuple(int(item) for item in snapshot.signature),
                "sha256": str(snapshot.sha256),
            },
            "compact_authenticated": bundle is not None,
            "compact_artifact_evidence": compact_evidence,
        }

    def _require_open(self) -> None:
        if self._closed:
            raise DracoResumeSourceError("resume source index is closed")

    @staticmethod
    def _standard_manifest_path(source_path: Path) -> tuple[str, Path] | None:
        match = _STANDARD_RESULT_NAME_RE.fullmatch(source_path.name)
        if match is None:
            return None
        stamp = match.group("stamp")
        parent = Path(os.path.abspath(source_path.parent))
        return stamp, parent / f"draco_run_{stamp}.manifest.json"

    def _bind_compact_bundle(
        self,
        source_path: Path,
        *,
        source_signature: tuple[int, ...],
    ) -> _CompactSourceBundle | None:
        standard = self._standard_manifest_path(source_path)
        if standard is None:
            return None
        stamp, manifest_path = standard
        if not os.path.lexists(manifest_path):
            return None
        manifest, manifest_signature, manifest_sha256 = (
            _read_json_object_snapshot(
                manifest_path,
                label="resume sibling manifest",
            )
        )
        artifacts = manifest.get("artifacts")
        capability_present = SELECTION_PLAN_EVIDENCE_ROW_FIELD in manifest
        binding_present = SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD in manifest
        pack_present = bool(
            isinstance(artifacts, Mapping)
            and SELECTION_PLAN_PACK_ARTIFACT_FIELD in artifacts
        )
        if not (capability_present or binding_present or pack_present):
            return None
        expected_durable_capability = durable_artifact_capability_contract()
        compatibility = manifest.get("run_compatibility")
        contracts = (
            compatibility.get("contracts")
            if isinstance(compatibility, Mapping)
            else None
        )
        groups = manifest.get("groups")
        if (
            manifest.get("schema") != DRACO_RUN_MANIFEST_SCHEMA_V2
            or manifest.get("stamp") != stamp
            or manifest.get("durable_artifact_capability")
            != expected_durable_capability
            or not isinstance(groups, list)
            or not isinstance(contracts, Mapping)
            or any(
                not isinstance(contracts.get(str(group)), Mapping)
                or contracts[str(group)].get("durable_artifact_capability")
                != expected_durable_capability
                for group in groups
            )
        ):
            raise DracoResumeSourceError(
                "compact resume manifest lacks its durable v2 contract"
            )
        if not isinstance(artifacts, Mapping):
            raise DracoResumeSourceError(
                "compact resume manifest lacks its artifact map"
            )
        status = manifest.get("status")
        if status not in _COMPACT_TERMINAL_STATUSES:
            raise DracoResumeSourceError(
                "compact resume manifest status is not an allowed terminal state"
            )
        if manifest.get(SELECTION_PLAN_EVIDENCE_ROW_FIELD) != (
            selection_plan_evidence_capability_contract()
        ):
            raise DracoResumeSourceError(
                "compact resume manifest capability is incomplete"
            )
        declared_binding = manifest.get(SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD)
        if not isinstance(declared_binding, Mapping):
            raise DracoResumeSourceError(
                "compact resume manifest lacks its terminal binding"
            )

        parent = manifest_path.parent
        expected_paths = {
            "results_jsonl": parent / f"draco_ensemble_{stamp}.jsonl",
            "trace_jsonl": parent / f"draco_run_{stamp}.trace.jsonl",
            "checkpoint_json": parent / f"draco_run_{stamp}.checkpoint.json",
            "manifest_json": manifest_path,
            SELECTION_PLAN_PACK_ARTIFACT_FIELD: (
                parent / f"draco_run_{stamp}.selection-plan.pack.jsonl"
            ),
        }
        for key, expected_path in expected_paths.items():
            raw_path = artifacts.get(key)
            if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                raise DracoResumeSourceError(
                    f"compact resume manifest artifacts.{key} is not absolute"
                )
            if Path(os.path.abspath(raw_path)) != expected_path:
                raise DracoResumeSourceError(
                    f"compact resume manifest artifacts.{key} is not stamp-bound"
                )
        if expected_paths["results_jsonl"] != Path(os.path.abspath(source_path)):
            raise DracoResumeSourceError(
                "compact resume result path is not bound to its sibling manifest"
            )
        artifact_recovery = manifest.get("artifact_recovery")
        if artifact_recovery is not None and (
            not isinstance(artifact_recovery, Mapping)
            or artifact_recovery.get(SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD)
            != declared_binding
        ):
            raise DracoResumeSourceError(
                "compact resume recovery binding differs from its terminal manifest"
            )

        reader: SelectionPlanPackReader | None = None
        compact_row_count = 0
        try:
            reader = SelectionPlanPackReader(
                expected_paths[SELECTION_PLAN_PACK_ARTIFACT_FIELD],
                owner_only=True,
            )

            def validate_compact_row(row: Mapping[str, Any]) -> None:
                nonlocal compact_row_count
                compact_row_count += int(
                    validate_compact_selection_plan_evidence_row_structure(
                        row,
                        pack_index=reader.index,
                    )
                )

            artifact_path_snapshots: dict[str, tuple[int, ...]] = {}
            durable_verification = verify_durable_draco_artifacts(
                results_path=expected_paths["results_jsonl"],
                trace_path=expected_paths["trace_jsonl"],
                checkpoint_path=expected_paths["checkpoint_json"],
                result_row_observer=validate_compact_row,
                path_snapshot_out=artifact_path_snapshots,
            )
            validate_selection_plan_evidence_manifest_binding(
                declared_binding,
                pack_index=reader.index,
                durable_artifact_verification=durable_verification,
                compact_row_count=compact_row_count,
            )
            if artifact_path_snapshots.get("results_jsonl") != source_signature:
                raise DracoResumeSourceError(
                    "compact resume verifier and source index bound different results"
                )
            verify_durable_artifact_path_snapshots(
                artifact_path_snapshots,
                results_path=expected_paths["results_jsonl"],
                trace_path=expected_paths["trace_jsonl"],
                checkpoint_path=expected_paths["checkpoint_json"],
            )
            _verify_json_object_snapshot(
                manifest_path,
                label="resume sibling manifest",
                expected_signature=manifest_signature,
                expected_sha256=manifest_sha256,
            )
            return _CompactSourceBundle(
                results_path=expected_paths["results_jsonl"],
                trace_path=expected_paths["trace_jsonl"],
                checkpoint_path=expected_paths["checkpoint_json"],
                manifest_path=manifest_path,
                pack_path=expected_paths[SELECTION_PLAN_PACK_ARTIFACT_FIELD],
                manifest_signature=manifest_signature,
                manifest_sha256=manifest_sha256,
                artifact_path_snapshots=artifact_path_snapshots,
                durable_verification=durable_verification,
                binding=dict(declared_binding),
                pack_index=reader.index,
                reader=reader,
            )
        except DracoResumeSourceError:
            if reader is not None:
                try:
                    reader.close()
                except BaseException:
                    pass
            raise
        except (DracoArtifactDurabilityError, OSError, SelectionPlanEvidenceError) as exc:
            if reader is not None:
                try:
                    reader.close()
                except BaseException:
                    pass
            raise DracoResumeSourceError(
                "compact resume evidence binding failed"
            ) from exc

    @contextmanager
    def _bundle_reader(
        self,
        bundle: _CompactSourceBundle,
    ) -> Iterator[SelectionPlanPackReader]:
        reader = bundle.reader
        close_reader = reader is None
        primary_error = False
        try:
            if reader is None:
                reader = SelectionPlanPackReader.from_index(
                    bundle.pack_path,
                    bundle.pack_index,
                    owner_only=True,
                )
            reader.verify_identity()
            compact_row_count = bundle.binding.get("compact_row_count")
            if isinstance(compact_row_count, bool) or not isinstance(
                compact_row_count,
                int,
            ):
                raise DracoResumeSourceError(
                    "compact resume binding row count is malformed"
                )
            validate_selection_plan_evidence_manifest_binding(
                bundle.binding,
                pack_index=reader.index,
                durable_artifact_verification=bundle.durable_verification,
                compact_row_count=compact_row_count,
            )
            yield reader
            reader.verify_identity()
        except DracoResumeSourceError:
            primary_error = True
            raise
        except (OSError, SelectionPlanEvidenceError) as exc:
            primary_error = True
            raise DracoResumeSourceError(
                "compact resume pack changed after binding"
            ) from exc
        except BaseException:
            primary_error = True
            raise
        finally:
            if close_reader and reader is not None:
                try:
                    reader.close()
                except BaseException:
                    if not primary_error:
                        raise

    def _verify_compact_bundle(self, bundle: _CompactSourceBundle) -> None:
        _verify_json_object_snapshot(
            bundle.manifest_path,
            label="resume sibling manifest",
            expected_signature=bundle.manifest_signature,
            expected_sha256=bundle.manifest_sha256,
        )
        verify_durable_artifact_path_snapshots(
            bundle.artifact_path_snapshots,
            results_path=bundle.results_path,
            trace_path=bundle.trace_path,
            checkpoint_path=bundle.checkpoint_path,
        )
        compact_row_count = bundle.binding.get("compact_row_count")
        if isinstance(compact_row_count, bool) or not isinstance(
            compact_row_count,
            int,
        ):
            raise DracoResumeSourceError(
                "compact resume binding row count is malformed"
            )
        retained_reader = bundle.reader
        if retained_reader is not None:
            validate_selection_plan_evidence_manifest_binding(
                bundle.binding,
                pack_index=retained_reader.index,
                durable_artifact_verification=bundle.durable_verification,
                compact_row_count=compact_row_count,
            )
            retained_reader.verify_snapshot()
            return
        reader: SelectionPlanPackReader | None = None
        primary_error = False
        try:
            # A spooled source released its pack descriptor after indexing.
            # Construction is the one final whole-pack scan for this source.
            reader = SelectionPlanPackReader(bundle.pack_path, owner_only=True)
            if _pack_identity(reader.index) != _pack_identity(bundle.pack_index):
                raise DracoResumeSourceError(
                    "compact resume pack path no longer names its bound inode"
                )
            validate_selection_plan_evidence_manifest_binding(
                bundle.binding,
                pack_index=reader.index,
                durable_artifact_verification=bundle.durable_verification,
                compact_row_count=compact_row_count,
            )
            reader.verify_identity()
        except BaseException:
            primary_error = True
            raise
        finally:
            if reader is not None:
                try:
                    reader.close()
                except BaseException:
                    if not primary_error:
                        raise

    @staticmethod
    def _row_has_compact_signal(row: Mapping[str, Any]) -> bool:
        return bool(
            selection_plan_row_capability_signal(row)
            or selection_plan_reference_signal(row)
        )

    def _parse_legacy_line_and_reject_compact_signal(
        self,
        line: bytes,
        *,
        source_path: Path,
        line_number: int,
    ) -> dict[str, Any] | None:
        """Parse a normal legacy row once and retain it for its streaming caller."""

        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(value, Mapping) and self._row_has_compact_signal(value):
            raise DracoResumeSourceError(
                "resume row contains undeclared compact selection-plan evidence at "
                f"{source_path}:{line_number}"
            )
        return value if isinstance(value, dict) else None

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
        try:
            compact_bundle = self._bind_compact_bundle(
                source_path,
                source_signature=start_signature,
            )
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        snapshot = _SourceSnapshot(
            source_id=source_id,
            path=source_path,
            source_index=source_index,
            signature=start_signature,
            sha256="",
            fd=fd,
            compact_bundle=compact_bundle,
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
                    parsed_row = None
                    if compact_bundle is None:
                        parsed_row = self._parse_legacy_line_and_reject_compact_signal(
                            line,
                            source_path=source_path,
                            line_number=line_number,
                        )
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
                    line_identity = (source_id, line_number, line_sha256)
                    self._active_line_identity = line_identity
                    try:
                        yield IndexedResumeLine(
                            payload=line,
                            locator=locator,
                            parsed_row=parsed_row,
                        )
                    finally:
                        if self._active_line_identity == line_identity:
                            self._active_line_identity = None
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
            if compact_bundle is not None and snapshot.sha256 != str(
                compact_bundle.durable_verification.get("results_sha256") or ""
            ):
                raise DracoResumeSourceError(
                    "compact resume source digest differs from its terminal binding"
                )
            completed = True
        finally:
            self._active_source_id = None
            self._active_line_identity = None
            if self._backing == "spool" or not completed:
                try:
                    os.close(fd)
                finally:
                    snapshot.fd = None
            if (
                compact_bundle is not None
                and compact_bundle.reader is not None
                and (self._backing == "spool" or not completed)
            ):
                try:
                    compact_bundle.reader.close()
                except BaseException:
                    if completed:
                        raise
                compact_bundle.reader = None
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
        materialize_selection_plans: bool,
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
        self._validate_row_for_locator(
            locator,
            value,
            verify_evidence=verify_evidence,
        )
        if materialize_selection_plans:
            return self._selection_plan_row_view(
                locator,
                value,
                lazy=False,
            )
        return value

    def _validate_row_for_locator(
        self,
        locator: ResumeRowLocator,
        value: Mapping[str, Any],
        *,
        verify_evidence: bool,
    ) -> None:
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

    def _selection_plan_row_view(
        self,
        locator: ResumeRowLocator,
        value: Mapping[str, Any],
        *,
        lazy: bool,
    ) -> dict[str, Any]:
        snapshot = self._sources.get(locator.source_id)
        if snapshot is None:
            raise DracoResumeSourceError(
                "resume row locator has no selection-plan source snapshot"
            )
        bundle = snapshot.compact_bundle
        if bundle is None:
            if self._row_has_compact_signal(value):
                raise DracoResumeSourceError(
                    "resume row contains compact selection-plan evidence without "
                    "an authenticated sibling manifest"
                )
            return dict(value)
        if value.get(SELECTION_PLAN_EVIDENCE_ROW_FIELD) != (
            selection_plan_evidence_capability_contract()
        ):
            raise DracoResumeSourceError(
                "compact resume row lost its exact capability marker"
            )
        try:
            with self._bundle_reader(bundle) as reader:
                if lazy:
                    expected_line_identity = (
                        locator.source_id,
                        locator.line_number,
                        locator.line_sha256,
                    )
                    index_ref = weakref.ref(self)

                    def require_active_scan_scope() -> None:
                        index = index_ref()
                        if (
                            index is None
                            or index._closed
                            or index._active_line_identity
                            != expected_line_identity
                        ):
                            raise SelectionPlanEvidenceError(
                                "lazy selection-plan view escaped its source scan scope"
                            )

                    def record_materialization() -> None:
                        index = index_ref()
                        if index is None:
                            raise SelectionPlanEvidenceError(
                                "lazy selection-plan owner is unavailable"
                            )
                        index._selection_plan_classification_materialization_count += 1

                    view = lazy_selection_plan_row_view(
                        value,
                        reader=reader,
                        require_references=True,
                        on_materialize=record_materialization,
                        access_guard=require_active_scan_scope,
                    )
                else:
                    view = materialize_selection_plan_row_view(
                        value,
                        reader=reader,
                        require_references=True,
                    )
        except (OSError, SelectionPlanEvidenceError) as exc:
            raise DracoResumeSourceError(
                "compact resume row could not be materialized"
            ) from exc
        detached = dict(view)
        marker = detached.pop(SELECTION_PLAN_EVIDENCE_ROW_FIELD, None)
        if marker != selection_plan_evidence_capability_contract():
            raise DracoResumeSourceError(
                "compact resume row capability disappeared during materialization"
            )
        if not lazy and (
            selection_plan_row_capability_signal(detached)
            or selection_plan_reference_signal(detached)
        ):
            raise DracoResumeSourceError(
                "materialized resume row retained compact selection-plan evidence"
            )
        if not lazy and selection_plan_reference_signal(value):
            self._selection_plan_materialized_row_count += 1
        return seal_result_row(detached) if not lazy else detached

    def classification_row(
        self,
        locator: ResumeRowLocator,
        value: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Verify the sealed raw row, then expose a transient lazy plan view."""

        self._require_open()
        if not locator.group or not locator.task_id:
            raise DracoResumeSourceError("resume row locator is not identity-bound")
        self._locator_fd(locator)
        if self._active_line_identity != (
            locator.source_id,
            locator.line_number,
            locator.line_sha256,
        ):
            raise DracoResumeSourceError(
                "resume classification view is outside its active source scan"
            )
        if not isinstance(value, dict):
            raise DracoResumeSourceError("resume classification row is not an object")
        snapshot = self._sources.get(locator.source_id)
        compact = bool(
            snapshot is not None and snapshot.compact_bundle is not None
        )
        self._validate_row_for_locator(
            locator,
            value,
            verify_evidence=compact,
        )
        return (
            self._selection_plan_row_view(
                locator,
                value,
                lazy=True,
            ),
            compact,
        )

    def load_attempts(
        self,
        requests: list[tuple[ResumeRowLocator, int, str]],
    ) -> list[dict[str, Any]]:
        """Load each strict-attempt parent once for one bounded caller batch."""

        grouped: dict[
            ResumeRowLocator,
            list[tuple[int, int, str]],
        ] = {}
        for output_index, (locator, attempt_index, attempt_id) in enumerate(requests):
            grouped.setdefault(locator, []).append(
                (output_index, attempt_index, attempt_id)
            )
        loaded: dict[int, dict[str, Any]] = {}
        for locator, requested_attempts in grouped.items():
            snapshot = self._sources.get(locator.source_id)
            compact = bool(
                snapshot is not None and snapshot.compact_bundle is not None
            )
            row = self._load_row(
                locator,
                # Historical strict-attempt replay intentionally accepted a
                # parent row whose result seal was stale. Preserve that legacy
                # contract; compact rows require their authenticated raw seal.
                verify_evidence=compact,
                materialize_selection_plans=False,
            )
            execution = row.get("execution")
            attempts = (
                execution.get("generation_attempts")
                if isinstance(execution, Mapping)
                else None
            )
            if not isinstance(attempts, list):
                raise DracoResumeSourceError(
                    "strict attempt parent has no generation history"
                )
            selected_by_index: dict[int, Mapping[str, Any]] = {}
            for output_index, attempt_index, attempt_id in requested_attempts:
                if not 0 <= attempt_index < len(attempts):
                    raise DracoResumeSourceError(
                        "strict attempt locator is out of range"
                    )
                attempt = attempts[attempt_index]
                if (
                    not isinstance(attempt, Mapping)
                    or attempt.get("attempt_id") != attempt_id
                ):
                    raise DracoResumeSourceError(
                        "strict attempt locator changed identity"
                    )
                selected_by_index.setdefault(attempt_index, attempt)
            materialized_by_index: dict[int, dict[str, Any]]
            if compact:
                snapshot = self._sources.get(locator.source_id)
                bundle = snapshot.compact_bundle if snapshot is not None else None
                if bundle is None:  # pragma: no cover - compact derived above.
                    raise DracoResumeSourceError(
                        "compact strict attempt lost its authenticated bundle"
                    )
                selected_indices = list(selected_by_index)
                selected_wrapper = {
                    "generation_attempts": [
                        selected_by_index[index] for index in selected_indices
                    ]
                }
                try:
                    with self._bundle_reader(bundle) as reader:
                        materialized_wrapper = materialize_selection_plan_row_view(
                            selected_wrapper,
                            reader=reader,
                            require_references=True,
                        )
                except (OSError, SelectionPlanEvidenceError) as exc:
                    raise DracoResumeSourceError(
                        "compact strict attempt could not be materialized"
                    ) from exc
                selected_attempts = materialized_wrapper.get("generation_attempts")
                if not isinstance(selected_attempts, list) or len(
                    selected_attempts
                ) != len(selected_indices):
                    raise DracoResumeSourceError(
                        "compact strict attempt materialization changed shape"
                    )
                materialized_by_index = {}
                for index, attempt in zip(
                    selected_indices,
                    selected_attempts,
                    strict=True,
                ):
                    if not isinstance(attempt, Mapping):
                        raise DracoResumeSourceError(
                            "compact strict attempt materialization is not an object"
                        )
                    materialized_by_index[index] = dict(attempt)
            else:
                materialized_by_index = {
                    index: dict(attempt)
                    for index, attempt in selected_by_index.items()
                }
            for output_index, attempt_index, _ in requested_attempts:
                loaded[output_index] = copy.deepcopy(
                    materialized_by_index[attempt_index]
                )
            self._attempt_payload_load_count += 1
            del row
        return [loaded[index] for index in range(len(requests))]

    def load_attempt(
        self,
        locator: ResumeRowLocator,
        *,
        attempt_index: int,
        attempt_id: str,
    ) -> dict[str, Any]:
        """Compatibility wrapper for one strict-attempt request."""

        return self.load_attempts(
            [(locator, attempt_index, attempt_id)]
        )[0]

    def consume_row(self, locator: ResumeRowLocator) -> dict[str, Any]:
        """Validate and deliver one scheduled row exactly once."""

        if locator in self._consumed:
            raise DracoResumeSourceError(
                f"resume row was already consumed for {locator.group}/{locator.task_id}"
            )
        value = self._load_row(
            locator,
            verify_evidence=True,
            materialize_selection_plans=True,
        )
        # Mark only after every byte, identity, JSON, and evidence check passed.
        self._consumed.add(locator)
        self._materialized_row_count += 1
        return value

    def peek_row(self, locator: ResumeRowLocator) -> dict[str, Any]:
        """Non-consuming inspection hook for compatibility tests only."""

        value = self._load_row(
            locator,
            verify_evidence=True,
            materialize_selection_plans=True,
        )
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
            if snapshot.compact_bundle is not None:
                try:
                    self._verify_compact_bundle(snapshot.compact_bundle)
                except DracoResumeSourceError:
                    raise
                except (
                    DracoArtifactDurabilityError,
                    OSError,
                    SelectionPlanEvidenceError,
                ) as exc:
                    raise DracoResumeSourceError(
                        "compact resume artifact paths changed after binding"
                    ) from exc

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
            bundle = snapshot.compact_bundle
            if bundle is not None and bundle.reader is not None:
                try:
                    bundle.reader.close()
                except BaseException as exc:
                    if verify and error is None:
                        error = exc
                bundle.reader = None
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
