"""Strict importer for Electron's local early-event spool.

Electron never sends these events.  The Python runtime atomically claims each
file, validates the closed wire contract, rechecks current scope consent, and
only then hands the model to its scope-bound recorder.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opensquilla.telemetry.consent import (
    ConsentDecision,
    TelemetryScope,
    resolve_scope_consent,
)
from opensquilla.telemetry.contracts import (
    TelemetryWireError,
    TelemetryWireTarget,
    parse_telemetry_wire,
)
from opensquilla.telemetry.contracts.common import EventBase
from opensquilla.telemetry.recorder import RecordStatus, TelemetryRecorder

EARLY_SPOOL_MAX_FILES = 512
EARLY_SPOOL_MAX_BYTES = 4 * 1024 * 1024
EARLY_SPOOL_MAX_AGE = timedelta(days=7)
DEFAULT_PROCESSING_STALE_AFTER = timedelta(minutes=5)
_MAX_SCAN_ENTRIES_PER_SCOPE = EARLY_SPOOL_MAX_FILES * 4

_UUID4_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_READY_NAME_RE = re.compile(rf"(?P<event_id>{_UUID4_PATTERN})\.ready\Z", re.IGNORECASE)
_PROCESSING_NAME_RE = re.compile(
    rf"(?P<event_id>{_UUID4_PATTERN})\.processing\.(?P<owner_pid>[1-9][0-9]*)\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DesktopIngressStats:
    discovered: int = 0
    claimed: int = 0
    enqueued: int = 0
    rejected: int = 0
    retried: int = 0
    recovered_stale: int = 0


@dataclass(frozen=True)
class _ReadyCandidate:
    scope: TelemetryScope
    path: Path
    event_id: str
    modified_at: float


class _RejectedIngressError(ValueError):
    pass


class _RetryableIngressError(OSError):
    pass


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_real_directory(path: Path) -> bool:
    metadata = _lstat(path)
    return (
        metadata is not None
        and stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
    )


def _unlink_best_effort(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _iter_scope_entries(directory: Path) -> list[os.DirEntry[str]]:
    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                entries.append(entry)
                if len(entries) >= _MAX_SCAN_ENTRIES_PER_SCOPE:
                    break
    except OSError:
        return []
    return entries


def _restore_processing_claim(processing: Path, ready: Path, now_timestamp: float) -> bool:
    """Restore a claim without overwriting another claimant's ready file."""

    try:
        os.link(processing, ready, follow_symlinks=False)
    except FileExistsError:
        return _unlink_best_effort(processing)
    except OSError:
        return False
    try:
        os.utime(ready, (now_timestamp, now_timestamp), follow_symlinks=False)
    except OSError:
        pass
    return _unlink_best_effort(processing)


def _recover_stale_processing(
    scope_directory: Path,
    *,
    now_timestamp: float,
    stale_after_seconds: float,
) -> tuple[int, int]:
    recovered = 0
    rejected = 0
    cutoff = now_timestamp - stale_after_seconds
    for entry in _iter_scope_entries(scope_directory):
        if ".processing." not in entry.name:
            continue
        match = _PROCESSING_NAME_RE.fullmatch(entry.name)
        path = scope_directory / entry.name
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if match is None or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            if stat.S_ISLNK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
                _unlink_best_effort(path)
            rejected += 1
            continue
        if metadata.st_mtime > cutoff:
            continue
        ready = scope_directory / f"{match.group('event_id').lower()}.ready"
        if _restore_processing_claim(path, ready, now_timestamp):
            recovered += 1
    return recovered, rejected


def _collect_ready_candidates(
    scope_directory: Path,
    scope: TelemetryScope,
) -> tuple[list[_ReadyCandidate], int]:
    candidates: list[_ReadyCandidate] = []
    rejected = 0
    for entry in _iter_scope_entries(scope_directory):
        if not entry.name.endswith(".ready"):
            continue
        path = scope_directory / entry.name
        match = _READY_NAME_RE.fullmatch(entry.name)
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if match is None:
            if stat.S_ISLNK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
                _unlink_best_effort(path)
            rejected += 1
            continue
        candidates.append(
            _ReadyCandidate(
                scope=scope,
                path=path,
                event_id=match.group("event_id").lower(),
                modified_at=metadata.st_mtime,
            )
        )
    return candidates, rejected


def _claim_ready(candidate: _ReadyCandidate) -> tuple[Path, os.stat_result] | None:
    processing = candidate.path.with_name(f"{candidate.event_id}.processing.{os.getpid()}")
    if _lstat(processing) is not None:
        raise _RetryableIngressError("early telemetry claim path is already occupied")
    try:
        os.rename(candidate.path, processing)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _RetryableIngressError("early telemetry event could not be claimed") from exc
    metadata = _lstat(processing)
    if metadata is None:
        return None
    return processing, metadata


def _read_claimed_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _RetryableIngressError("claimed telemetry event could not be opened") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise _RejectedIngressError("claimed telemetry event is not a regular file")
        if metadata.st_size > EARLY_SPOOL_MAX_BYTES:
            raise _RejectedIngressError("claimed telemetry event exceeds its size limit")
        chunks: list[bytes] = []
        remaining = EARLY_SPOOL_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > EARLY_SPOOL_MAX_BYTES:
            raise _RejectedIngressError("claimed telemetry event exceeds its size limit")
        return raw
    finally:
        os.close(descriptor)


def _validate_claimed_event(
    raw: bytes,
    *,
    expected_scope: TelemetryScope,
    expected_event_id: str,
) -> EventBase:
    try:
        event: EventBase
        if expected_scope is TelemetryScope.RELIABILITY:
            event = parse_telemetry_wire(
                raw,
                target=TelemetryWireTarget.RELIABILITY_EVENT,
            )
        else:
            event = parse_telemetry_wire(
                raw,
                target=TelemetryWireTarget.GROWTH_EVENT,
            )
    except TelemetryWireError as exc:
        raise _RejectedIngressError("telemetry event contract validation failed") from exc
    if str(event.consent_scope) != expected_scope.value:
        raise _RejectedIngressError("telemetry event is in the wrong scope directory")
    if str(event.event_id).lower() != expected_event_id:
        raise _RejectedIngressError("telemetry event id does not match its spool filename")
    return event


async def drain_desktop_early_spool(
    spool_root: str | Path,
    *,
    config: Any | None,
    recorders: Mapping[TelemetryScope, TelemetryRecorder],
    env: Mapping[str, str | None] | None = None,
    now: datetime | None = None,
    processing_stale_after: timedelta = DEFAULT_PROCESSING_STALE_AFTER,
    retention: timedelta = EARLY_SPOOL_MAX_AGE,
    max_events: int = EARLY_SPOOL_MAX_FILES,
    transient_forced_off: bool = False,
) -> DesktopIngressStats:
    """Claim, validate, consent-check, and enqueue bounded early events."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("desktop ingress time must be timezone-aware")
    if processing_stale_after.total_seconds() <= 0 or retention.total_seconds() <= 0:
        raise ValueError("desktop ingress retention intervals must be positive")
    if max_events <= 0:
        raise ValueError("desktop ingress max_events must be positive")
    if config is None:
        raise ValueError("desktop ingress requires the live telemetry config")
    if not recorders or not set(recorders).issubset(set(TelemetryScope)):
        raise ValueError("desktop ingress requires at least one scoped recorder")
    for scope, recorder in recorders.items():
        if (
            not isinstance(recorder, TelemetryRecorder)
            or recorder.scope is not scope
            or not recorder.is_bound_to_config(config)
        ):
            raise ValueError("desktop ingress recorder binding is invalid")

    root = Path(spool_root).expanduser()
    root_metadata = _lstat(root)
    if root_metadata is None:
        return DesktopIngressStats()
    if not _is_real_directory(root):
        return DesktopIngressStats(rejected=1)

    now_timestamp = current.timestamp()
    recovered_stale = 0
    rejected = 0
    candidates: list[_ReadyCandidate] = []
    for scope in recorders:
        scope_directory = root / scope.value
        metadata = _lstat(scope_directory)
        if metadata is None:
            continue
        if not _is_real_directory(scope_directory):
            rejected += 1
            continue
        recovered, recovery_rejected = _recover_stale_processing(
            scope_directory,
            now_timestamp=now_timestamp,
            stale_after_seconds=processing_stale_after.total_seconds(),
        )
        recovered_stale += recovered
        rejected += recovery_rejected
        scoped_candidates, collection_rejected = _collect_ready_candidates(
            scope_directory,
            scope,
        )
        candidates.extend(scoped_candidates)
        rejected += collection_rejected

    candidates.sort(key=lambda item: (item.modified_at, item.scope.value, item.path.name))
    discovered = min(len(candidates), max_events)
    claimed = 0
    enqueued = 0
    retried = 0
    retention_seconds = retention.total_seconds()

    for candidate in candidates[:max_events]:
        try:
            claimed_value = _claim_ready(candidate)
        except _RetryableIngressError:
            retried += 1
            continue
        if claimed_value is None:
            continue
        processing, metadata = claimed_value
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            _unlink_best_effort(processing)
            rejected += 1
            continue
        if now_timestamp - metadata.st_mtime > retention_seconds:
            _unlink_best_effort(processing)
            rejected += 1
            continue
        if metadata.st_size > EARLY_SPOOL_MAX_BYTES:
            _unlink_best_effort(processing)
            rejected += 1
            continue
        try:
            os.utime(processing, (now_timestamp, now_timestamp), follow_symlinks=False)
        except OSError:
            if _restore_processing_claim(processing, candidate.path, now_timestamp):
                retried += 1
            continue
        claimed += 1

        try:
            raw = _read_claimed_file(processing)
            event = _validate_claimed_event(
                raw,
                expected_scope=candidate.scope,
                expected_event_id=candidate.event_id,
            )
            consent = resolve_scope_consent(
                candidate.scope,
                config=config,
                env=env,
                transient_forced_off=transient_forced_off,
            )
            if (
                consent.decision is not ConsentDecision.GRANTED
                or not consent.record_complete
                or not consent.notice_current
                or event.notice_version != consent.notice_version
            ):
                raise _RejectedIngressError("telemetry scope consent is not currently valid")
            if consent.forced_off:
                raise _RetryableIngressError("telemetry scope is temporarily forced off")
            if not consent.enabled:
                raise _RejectedIngressError("telemetry scope consent is not currently valid")
        except _RejectedIngressError:
            _unlink_best_effort(processing)
            rejected += 1
            continue
        except _RetryableIngressError:
            if _restore_processing_claim(processing, candidate.path, now_timestamp):
                retried += 1
            continue

        try:
            record_result = await recorders[candidate.scope].record(event)
        except Exception:
            if _restore_processing_claim(processing, candidate.path, now_timestamp):
                retried += 1
            continue
        if record_result.status is RecordStatus.CONSENT_BLOCKED:
            consent = resolve_scope_consent(
                candidate.scope,
                config=config,
                env=env,
                transient_forced_off=transient_forced_off,
            )
            if consent.forced_off:
                if _restore_processing_claim(processing, candidate.path, now_timestamp):
                    retried += 1
            else:
                _unlink_best_effort(processing)
                rejected += 1
            continue
        if record_result.status is RecordStatus.NOTICE_MISMATCH:
            _unlink_best_effort(processing)
            rejected += 1
            continue
        if record_result.status is RecordStatus.EVICTED:
            if _restore_processing_claim(processing, candidate.path, now_timestamp):
                retried += 1
            continue
        enqueued += 1
        _unlink_best_effort(processing)

    return DesktopIngressStats(
        discovered=discovered,
        claimed=claimed,
        enqueued=enqueued,
        rejected=rejected,
        retried=retried,
        recovered_stale=recovered_stale,
    )


__all__ = [
    "DEFAULT_PROCESSING_STALE_AFTER",
    "DesktopIngressStats",
    "EARLY_SPOOL_MAX_AGE",
    "EARLY_SPOOL_MAX_BYTES",
    "EARLY_SPOOL_MAX_FILES",
    "drain_desktop_early_spool",
]
