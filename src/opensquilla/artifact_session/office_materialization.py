"""Bounded, staged single-file Office replacement.

The durable authority lives in SQLite, never in Agent-writable workspace JSON.
Only preparation/recovery reads file bytes; the final transaction swaps one file.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from opensquilla.artifacts import ArtifactBudgetError, _read_regular_bundle_file
from opensquilla.office_format import MAX_OFFICE_BYTES, OfficeFormatError
from opensquilla.paths import native_io_path

from .errors import ArtifactConflictError, ArtifactValidationError


def read_office_file(path: Path) -> bytes:
    try:
        return _read_regular_bundle_file(path, max_bytes=MAX_OFFICE_BYTES)
    except ArtifactBudgetError as exc:
        raise OfficeFormatError("office_size_unsupported") from exc


def _fingerprint(path: Path) -> tuple[int, ...] | None:
    try:
        stat = native_io_path(path).stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@dataclass(frozen=True)
class OfficeFilePlan:
    document_id: str
    workspace: str
    target: str
    token: str
    old_sha256: str | None
    new_sha256: str
    fingerprint: tuple[int, ...] | None
    preserve_backup: bool = False
    staged_fingerprint: tuple[int, ...] | None = None

    def path(self, relative: str) -> Path:
        # Share the existing working-files path policy, including Windows junctions.
        from .working_files import checked_path

        return checked_path(Path(self.workspace), relative)

    @property
    def entry(self) -> Path:
        return self.path(self.target)

    @property
    def directory(self) -> Path:
        key = hashlib.sha256(self.document_id.encode()).hexdigest()
        return self.path(f"artifacts/.office-materializations/{key}/{self.token}")

    def member(self, name: str) -> Path:
        relative = (self.directory / name).relative_to(self.workspace).as_posix()
        return self.path(relative)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> OfficeFilePlan:
        plan = cls(**data)
        if (
            not re.fullmatch(r"[0-9a-f]{32}", plan.token)
            or not re.fullmatch(r"[0-9a-f]{64}", plan.new_sha256)
            or (plan.old_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", plan.old_sha256))
        ):
            raise ArtifactValidationError("Invalid Office materialization identity")
        if str(Path(plan.workspace).resolve()) != plan.workspace:
            raise ArtifactValidationError("Invalid Office materialization workspace")
        plan.entry
        plan.directory
        return plan


def _write(path: Path, payload: bytes) -> None:
    with native_io_path(path).open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def matching_office_fingerprint(path: Path, digest: str) -> tuple[int, ...] | None:
    before = _fingerprint(path)
    if before is None:
        return None
    if hashlib.sha256(read_office_file(path)).hexdigest() != digest:
        return None
    return before if _fingerprint(path) == before else None


def assert_office_fingerprint(path: Path, expected: tuple[int, ...]) -> None:
    if _fingerprint(path) != expected:
        raise ArtifactConflictError("Office working file changed before restoration")


def stage_office_file(
    *,
    document_id: str,
    workspace: str,
    target: str,
    payload: bytes,
    expected_sha256: str | None,
    allow_existing: bool,
) -> OfficeFilePlan:
    plan = OfficeFilePlan(
        document_id,
        workspace,
        target,
        secrets.token_hex(16),
        None,
        hashlib.sha256(payload).hexdigest(),
        None,
    )
    before = _fingerprint(plan.entry)
    old = read_office_file(plan.entry) if before is not None else None
    old_hash = hashlib.sha256(old).hexdigest() if old is not None else None
    if (
        expected_sha256 is not None
        and old_hash != expected_sha256
        or expected_sha256 is None
        and old is not None
        and not allow_existing
    ):
        raise ArtifactConflictError("Office working file changed before staging")
    if _fingerprint(plan.entry) != before:
        raise ArtifactConflictError("Office working file changed while staging")
    plan = OfficeFilePlan(
        document_id,
        workspace,
        target,
        plan.token,
        old_hash,
        plan.new_sha256,
        before,
        old is not None and old_hash != expected_sha256,
    )
    # Keep plans and containment checks logical; only filesystem calls use the
    # Windows extended spelling. Existing persisted plans retain their layout.
    native_io_path(plan.directory).mkdir(parents=True, exist_ok=False)
    native_io_path(plan.entry.parent).mkdir(parents=True, exist_ok=True)
    try:
        _write(plan.member("new"), payload)
        if old is not None:
            _write(plan.member("old"), old)
        staged_fingerprint = matching_office_fingerprint(plan.member("new"), plan.new_sha256)
        if staged_fingerprint is None:
            raise ArtifactConflictError("Staged Office file changed while preparing replacement")
    except BaseException:
        cleanup_office_plan(plan, preserve_backup=False)
        raise
    return OfficeFilePlan(**{**plan.to_dict(), "staged_fingerprint": staged_fingerprint})


def apply_office_file(plan: OfficeFilePlan) -> None:
    """No full-file IO: the caller holds the document lock and final SQL CAS."""
    expected = tuple(plan.fingerprint) if plan.fingerprint is not None else None
    if _fingerprint(plan.entry) != expected:
        raise ArtifactConflictError("Office working file changed before replacement")
    if _fingerprint(plan.member("new")) != tuple(plan.staged_fingerprint or ()):
        raise ArtifactConflictError("Staged Office file changed before replacement")
    os.replace(native_io_path(plan.member("new")), native_io_path(plan.entry))


def office_stage_unconsumed(plan: OfficeFilePlan) -> bool:
    """The original staged inode still exists, so replace never consumed it."""
    staged = _fingerprint(plan.member("new"))
    expected = plan.staged_fingerprint
    return staged is not None and expected is not None and staged[:2] == tuple(expected[:2])


def reconcile_office_file(
    plan: OfficeFilePlan,
    *,
    applied: bool,
    committed_payload: Callable[[], bytes] | None = None,
) -> None:
    entry = plan.entry
    before = _fingerprint(entry)
    current = read_office_file(entry) if before is not None else None
    digest = hashlib.sha256(current).hexdigest() if current is not None else None
    if _fingerprint(entry) != before:
        raise ArtifactConflictError("Office working file changed while checking recovery")
    if applied:
        if digest == plan.new_sha256:
            return
        if digest not in (None, plan.old_sha256):
            raise ArtifactConflictError("Committed Office working file has external changes")
        payload = committed_payload() if committed_payload is not None else None
        if payload is None or hashlib.sha256(payload).hexdigest() != plan.new_sha256:
            raise ArtifactValidationError("Office committed recovery material is unavailable")
        # Recovery is outside SQLite. The immutable receipt supplies authority;
        # a second metadata check prevents overwriting an edit made while writing.
        staged = plan.member("new")
        native_io_path(plan.directory).mkdir(parents=True, exist_ok=True)
        native_io_path(staged).unlink(missing_ok=True)
        _write(staged, payload)
        if _fingerprint(entry) != before:
            raise ArtifactConflictError("Office working file changed during recovery")
        os.replace(native_io_path(staged), native_io_path(entry))
        return
    if digest == plan.old_sha256:
        return
    if digest != plan.new_sha256:
        raise ArtifactConflictError("Office recovery retained an externally modified file")
    if plan.old_sha256 is None:
        if _fingerprint(entry) != before:
            raise ArtifactConflictError("Office working file changed during recovery")
        native_io_path(entry).unlink()
        return
    backup = plan.member("old")
    backup_fingerprint = _fingerprint(backup)
    if hashlib.sha256(read_office_file(backup)).hexdigest() != plan.old_sha256:
        raise ArtifactValidationError("Office recovery backup failed its integrity check")
    if _fingerprint(backup) != backup_fingerprint:
        raise ArtifactConflictError("Office recovery backup changed during recovery")
    if _fingerprint(entry) != before:
        raise ArtifactConflictError("Office working file changed during recovery")
    os.replace(native_io_path(backup), native_io_path(entry))


def cleanup_office_plan(plan: OfficeFilePlan, *, preserve_backup: bool) -> None:
    # Never remove unknown siblings or sweep recovered-* directories.
    native_io_path(plan.member("new")).unlink(missing_ok=True)
    if not preserve_backup:
        native_io_path(plan.member("old")).unlink(missing_ok=True)
    try:
        native_io_path(plan.directory).rmdir()
    except OSError:
        pass
