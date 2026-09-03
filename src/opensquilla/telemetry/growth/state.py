"""Strict shared state for the explicitly consented new-user cohort.

Electron is the authority that can prove a genuinely fresh desktop profile.
It writes the active-cohort receipt only after the user grants Growth consent.
The Gateway may read that receipt, but absence or malformed state always means
"not eligible"; it must never infer a new user from missing telemetry files.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opensquilla.paths import default_opensquilla_home
from opensquilla.profile_operation_lock import ProfileOperationLock

GROWTH_COHORT_SCHEMA_VERSION = 1
GROWTH_COHORT_STATE_NAME = "growth_cohort.json"
GATEWAY_GROWTH_MILESTONE_STATE_NAME = "growth_gateway_milestones.json"
CLIENT_LAUNCH_STATE_NAME = "growth_client_launches.json"
DESKTOP_GROWTH_MILESTONE_STATE_NAME = "growth_desktop_milestones.json"
_MAX_GROWTH_STATE_BYTES = 16 * 1024
_GROWTH_STATE_LOCK_TIMEOUT_SECONDS = 5.0


class GrowthStateError(ValueError):
    """Raised when persisted growth state is present but not trustworthy."""


@dataclass(frozen=True, slots=True)
class ActiveGrowthCohort:
    schema_version: int
    state: str
    activated_at_utc: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "activated_at_utc": self.activated_at_utc,
        }


def growth_telemetry_directory(*, config: Any | None = None) -> Path:
    configured_state_dir = getattr(config, "state_dir", None)
    root = (
        Path(configured_state_dir.strip()).expanduser()
        if isinstance(configured_state_dir, str) and configured_state_dir.strip()
        else Path(configured_state_dir).expanduser()
        if isinstance(configured_state_dir, Path)
        else default_opensquilla_home() / "state"
    )
    return root / "telemetry"


def growth_cohort_state_path(*, config: Any | None = None) -> Path:
    return growth_telemetry_directory(config=config) / GROWTH_COHORT_STATE_NAME


def gateway_growth_milestone_state_path(*, config: Any | None = None) -> Path:
    return growth_telemetry_directory(config=config) / GATEWAY_GROWTH_MILESTONE_STATE_NAME


def client_launch_state_path(*, config: Any | None = None) -> Path:
    return growth_telemetry_directory(config=config) / CLIENT_LAUNCH_STATE_NAME


def read_active_growth_cohort(path: str | Path) -> ActiveGrowthCohort | None:
    """Read the Electron-issued activation receipt with a closed schema."""

    target = Path(path).expanduser()
    payload = _read_json_object(target, absent_ok=True)
    if payload is None:
        return None
    expected_keys = {"schema_version", "state", "activated_at_utc"}
    if set(payload) != expected_keys:
        raise GrowthStateError("growth cohort state has unknown or missing fields")
    if payload.get("schema_version") != GROWTH_COHORT_SCHEMA_VERSION:
        raise GrowthStateError("unsupported growth cohort schema version")
    if payload.get("state") != "active":
        raise GrowthStateError("growth cohort is not active")
    activated_at_utc = payload.get("activated_at_utc")
    if not isinstance(activated_at_utc, str) or not _is_utc_timestamp(activated_at_utc):
        raise GrowthStateError("growth cohort activation time must be UTC")
    return ActiveGrowthCohort(
        schema_version=GROWTH_COHORT_SCHEMA_VERSION,
        state="active",
        activated_at_utc=activated_at_utc,
    )


def write_active_growth_cohort(
    path: str | Path,
    *,
    activated_at_utc: str,
) -> ActiveGrowthCohort:
    """Atomically write the cross-process receipt using Electron's wire shape.

    Production activation remains Electron-owned.  This writer exists for
    recovery-safe tooling and contract tests; callers must already hold the
    fresh-profile proof and the effective Growth-consent decision.
    """

    if not _is_utc_timestamp(activated_at_utc):
        raise ValueError("growth cohort activation time must be UTC")
    target = Path(path).expanduser()
    receipt = ActiveGrowthCohort(
        schema_version=GROWTH_COHORT_SCHEMA_VERSION,
        state="active",
        activated_at_utc=activated_at_utc,
    )
    with ProfileOperationLock(target, timeout=_GROWTH_STATE_LOCK_TIMEOUT_SECONDS):
        existing = read_active_growth_cohort(target)
        if existing is not None:
            return existing
        _write_json_object(target, receipt.to_dict())
    return receipt


def delete_growth_cohort_state(*, config: Any | None = None) -> tuple[Path, ...]:
    """Delete only the purpose-specific growth cohort and marker files."""

    removed: list[Path] = []
    for target in (
        growth_cohort_state_path(config=config),
        gateway_growth_milestone_state_path(config=config),
        client_launch_state_path(config=config),
        growth_telemetry_directory(config=config) / DESKTOP_GROWTH_MILESTONE_STATE_NAME,
    ):
        with ProfileOperationLock(target, timeout=_GROWTH_STATE_LOCK_TIMEOUT_SECONDS):
            if target.is_symlink():
                raise GrowthStateError("growth state path must not be a symlink")
            try:
                target.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise GrowthStateError("growth state could not be deleted") from exc
            removed.append(target)
    return tuple(removed)


def read_growth_state_object(path: str | Path) -> dict[str, object] | None:
    """Internal closed-size JSON reader shared by the Gateway marker store."""

    return _read_json_object(Path(path).expanduser(), absent_ok=True)


def write_growth_state_object(path: str | Path, payload: dict[str, object]) -> None:
    """Internal durable JSON writer shared by the Gateway marker store."""

    _write_json_object(Path(path).expanduser(), payload)


def _read_json_object(path: Path, *, absent_ok: bool) -> dict[str, object] | None:
    if path.is_symlink():
        raise GrowthStateError("growth state path must not be a symlink")
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_GROWTH_STATE_BYTES + 1)
    except FileNotFoundError:
        if absent_ok:
            return None
        raise
    except OSError as exc:
        raise GrowthStateError("growth state could not be read") from exc
    if len(raw) > _MAX_GROWTH_STATE_BYTES:
        raise GrowthStateError("growth state exceeds its size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GrowthStateError("growth state is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise GrowthStateError("growth state must be an object")
    return payload


def _write_json_object(path: Path, payload: dict[str, object]) -> None:
    if path.is_symlink():
        raise GrowthStateError("growth state path must not be a symlink")
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > _MAX_GROWTH_STATE_BYTES:
        raise GrowthStateError("growth state exceeds its size limit")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary_name, 0o600)
        except OSError:
            pass
        os.replace(temporary_name, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _is_utc_timestamp(value: str) -> bool:
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return (
        value.endswith("Z")
        and parsed.tzinfo is not None
        and parsed.utcoffset() == UTC.utcoffset(parsed)
    )


__all__ = [
    "ActiveGrowthCohort",
    "CLIENT_LAUNCH_STATE_NAME",
    "GATEWAY_GROWTH_MILESTONE_STATE_NAME",
    "DESKTOP_GROWTH_MILESTONE_STATE_NAME",
    "GROWTH_COHORT_SCHEMA_VERSION",
    "GROWTH_COHORT_STATE_NAME",
    "GrowthStateError",
    "delete_growth_cohort_state",
    "client_launch_state_path",
    "gateway_growth_milestone_state_path",
    "growth_cohort_state_path",
    "growth_telemetry_directory",
    "read_active_growth_cohort",
    "write_active_growth_cohort",
]
