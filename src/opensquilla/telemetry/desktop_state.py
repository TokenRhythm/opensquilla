"""Narrow, profile-scoped state shared with the Electron telemetry producer."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from opensquilla.telemetry.consent import (
    ConsentDecision,
    ScopeConsentState,
    TelemetryScope,
)

_MIRROR_SCHEMA_VERSION = 1
_MIRROR_NAME = "desktop-consent-mirror.json"
_EARLY_SPOOL_NAME = "desktop-early-spool"
_REVOKED_SCOPE_RE = re.compile(
    r"^\.revoked-(reliability|growth)-"
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SAFE_NOTICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


class DesktopTelemetryStateError(RuntimeError):
    """Desktop telemetry state could not be updated safely."""


@dataclass(frozen=True)
class EarlySpoolCleanupResult:
    removed: int = 0
    failed: int = 0
    unsafe: bool = False

    @property
    def complete(self) -> bool:
        return not self.unsafe and self.failed == 0


def desktop_consent_mirror_path(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / "telemetry" / _MIRROR_NAME


def desktop_early_spool_root(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / "telemetry" / _EARLY_SPOOL_NAME


def _require_real_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DesktopTelemetryStateError(
            "desktop telemetry directory could not be inspected"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise DesktopTelemetryStateError("desktop telemetry directory must not be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise DesktopTelemetryStateError("desktop telemetry directory must be a real directory")


def _ensure_telemetry_directory(state_dir: str | Path) -> Path:
    directory = desktop_consent_mirror_path(state_dir).parent
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise DesktopTelemetryStateError(
            "desktop telemetry directory could not be created"
        ) from exc
    _require_real_directory(directory)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def _valid_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() is not None


def _mirrored_scope(
    state: ScopeConsentState,
    *,
    expected_scope: TelemetryScope,
) -> dict[str, object]:
    if state.scope is not expected_scope:
        raise ValueError("telemetry consent state belongs to the wrong scope")
    forced_off = bool(state.forced_off_reasons)
    if state.decision is ConsentDecision.DECLINED:
        enabled: bool | None = False
    elif state.decision is ConsentDecision.UNSET:
        enabled = None
    elif (
        state.record_complete
        and isinstance(state.notice_version, str)
        and _SAFE_NOTICE_RE.fullmatch(state.notice_version) is not None
        and _valid_utc_timestamp(state.consented_at_utc)
    ):
        enabled = True
    else:
        # An incomplete/invalid grant is never mirrored as a usable grant.
        enabled = None
    return {
        "enabled": enabled,
        "notice_version": state.notice_version if enabled is True else None,
        "consented_at_utc": state.consented_at_utc if enabled is True else None,
        "forced_off": forced_off,
    }


def _fsync_directory_best_effort(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def write_desktop_consent_mirror(
    state_dir: str | Path,
    *,
    reliability: ScopeConsentState,
    growth: ScopeConsentState,
) -> Path:
    """Atomically publish the closed consent snapshot Electron reads."""

    payload: dict[str, Any] = {
        "schema_version": _MIRROR_SCHEMA_VERSION,
        "reliability": _mirrored_scope(
            reliability,
            expected_scope=TelemetryScope.RELIABILITY,
        ),
        "growth": _mirrored_scope(growth, expected_scope=TelemetryScope.GROWTH),
    }
    directory = _ensure_telemetry_directory(state_dir)
    target = desktop_consent_mirror_path(state_dir)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise DesktopTelemetryStateError("desktop consent mirror could not be inspected") from exc
    else:
        if stat.S_ISLNK(metadata.st_mode):
            raise DesktopTelemetryStateError("desktop consent mirror must not be a symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise DesktopTelemetryStateError("desktop consent mirror must be a regular file")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=directory,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass

        # Refuse a target swapped to a link/non-file after the initial check.
        try:
            target_metadata = target.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(target_metadata.st_mode):
                raise DesktopTelemetryStateError("desktop consent mirror must not be a symlink")
            if not stat.S_ISREG(target_metadata.st_mode):
                raise DesktopTelemetryStateError("desktop consent mirror must be a regular file")
        os.replace(temporary, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        _fsync_directory_best_effort(directory)
        return target
    except DesktopTelemetryStateError:
        raise
    except OSError as exc:
        raise DesktopTelemetryStateError("desktop consent mirror could not be written") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _is_managed_spool_name(name: str) -> bool:
    return (
        name.endswith(".ready")
        or ".processing." in name
        or (name.startswith(".") and name.endswith(".tmp"))
    )


def _is_scope_quarantine(name: str, scope: TelemetryScope) -> bool:
    match = _REVOKED_SCOPE_RE.fullmatch(name)
    return match is not None and match.group(1) == scope.value


def _optional_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DesktopTelemetryStateError("desktop early spool could not be inspected") from exc


def clear_desktop_early_spool_scope(
    state_dir: str | Path,
    scope: TelemetryScope | str,
) -> EarlySpoolCleanupResult:
    """Delete only managed files in one exact Desktop early-spool scope."""

    normalized_scope = TelemetryScope(scope)
    root = desktop_early_spool_root(state_dir)
    telemetry_directory = root.parent
    try:
        telemetry_metadata = _optional_lstat(telemetry_directory)
    except DesktopTelemetryStateError:
        return EarlySpoolCleanupResult(unsafe=True)
    if telemetry_metadata is None:
        return EarlySpoolCleanupResult()
    if stat.S_ISLNK(telemetry_metadata.st_mode) or not stat.S_ISDIR(telemetry_metadata.st_mode):
        return EarlySpoolCleanupResult(unsafe=True)
    try:
        root_metadata = _optional_lstat(root)
    except DesktopTelemetryStateError:
        return EarlySpoolCleanupResult(unsafe=True)
    if root_metadata is None:
        return EarlySpoolCleanupResult()
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        return EarlySpoolCleanupResult(unsafe=True)

    scope_directory = root / normalized_scope.value
    try:
        scope_metadata = _optional_lstat(scope_directory)
    except DesktopTelemetryStateError:
        return EarlySpoolCleanupResult(unsafe=True)
    if scope_metadata is not None and (
        stat.S_ISLNK(scope_metadata.st_mode) or not stat.S_ISDIR(scope_metadata.st_mode)
    ):
        return EarlySpoolCleanupResult(unsafe=True)
    if scope_metadata is not None:
        quarantine = root / f".revoked-{normalized_scope.value}-{uuid.uuid4()}"
        try:
            os.replace(scope_directory, quarantine)
        except OSError:
            return EarlySpoolCleanupResult(failed=1)
        _fsync_directory_best_effort(root)

    removed = 0
    failed = 0
    unsafe = False
    try:
        quarantines = tuple(
            entry for entry in root.iterdir() if _is_scope_quarantine(entry.name, normalized_scope)
        )
    except OSError:
        return EarlySpoolCleanupResult(failed=1)
    for quarantine in quarantines:
        try:
            metadata = quarantine.lstat()
        except OSError:
            failed += 1
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            unsafe = True
            continue
        try:
            entries = tuple(quarantine.iterdir())
        except OSError:
            failed += 1
            continue
        for entry in entries:
            if not _is_managed_spool_name(entry.name):
                unsafe = True
                continue
            try:
                entry_metadata = entry.lstat()
                if stat.S_ISLNK(entry_metadata.st_mode):
                    unsafe = True
                    continue
                if not stat.S_ISREG(entry_metadata.st_mode):
                    failed += 1
                    continue
                entry.unlink()
            except OSError:
                failed += 1
                continue
            removed += 1
        try:
            quarantine.rmdir()
        except OSError:
            try:
                if any(quarantine.iterdir()):
                    unsafe = True
                else:
                    failed += 1
            except OSError:
                failed += 1
    _fsync_directory_best_effort(root)
    return EarlySpoolCleanupResult(removed=removed, failed=failed, unsafe=unsafe)


__all__ = [
    "DesktopTelemetryStateError",
    "EarlySpoolCleanupResult",
    "clear_desktop_early_spool_scope",
    "desktop_consent_mirror_path",
    "desktop_early_spool_root",
    "write_desktop_consent_mirror",
]
