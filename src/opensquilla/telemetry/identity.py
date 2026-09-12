"""Random, purpose-isolated identity for optional growth analytics.

Identifiers are generated from UUIDv4 randomness only.  This module never
reads network interfaces, addresses, host names, platform serials, or another
telemetry scope's state.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from opensquilla.paths import default_opensquilla_home
from opensquilla.profile_operation_lock import ProfileOperationLock

IDENTITY_SCHEMA_VERSION = 1
_MAX_IDENTITY_FILE_BYTES = 4096
_IDENTITY_LOCK_TIMEOUT_SECONDS = 5.0
_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS = (0.02, 0.05, 0.1, 0.2)
_WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32, 33})


class TelemetryIdentityKind(StrEnum):
    ANALYTICS_USER = "analytics_user_id"


class IdentityStateError(ValueError):
    """Raised when persisted identity state is present but not trustworthy."""


@dataclass(frozen=True)
class RandomTelemetryIdentity:
    kind: TelemetryIdentityKind
    value: str
    created_at_utc: str
    schema_version: int = IDENTITY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "value": self.value,
            "created_at_utc": self.created_at_utc,
        }


def identity_state_path(
    kind: TelemetryIdentityKind | str,
    *,
    config: Any | None = None,
) -> Path:
    """Return the private state path for exactly one identity scope."""

    TelemetryIdentityKind(kind)
    configured_state_dir = getattr(config, "state_dir", None)
    root = (
        Path(configured_state_dir.strip()).expanduser()
        if isinstance(configured_state_dir, str) and configured_state_dir.strip()
        else default_opensquilla_home() / "state"
    )
    return root / "telemetry" / "growth_identity.json"


def generate_random_identity(
    kind: TelemetryIdentityKind | str,
    *,
    now: datetime | None = None,
    uuid_factory: Callable[[], uuid.UUID] | None = None,
) -> RandomTelemetryIdentity:
    """Create one canonical UUIDv4 identity without consulting device state."""

    normalized_kind = TelemetryIdentityKind(kind)
    generated = (uuid_factory or uuid.uuid4)()
    if (
        not isinstance(generated, uuid.UUID)
        or generated.version != 4
        or generated.variant != uuid.RFC_4122
    ):
        raise ValueError("telemetry identity factory must return a UUIDv4 value")
    created_at = now or datetime.now(UTC)
    if created_at.tzinfo is None:
        raise ValueError("telemetry identity creation time must be timezone-aware")
    created_at = created_at.astimezone(UTC).replace(microsecond=0)
    return RandomTelemetryIdentity(
        kind=normalized_kind,
        value=str(generated),
        created_at_utc=created_at.isoformat().replace("+00:00", "Z"),
    )


def read_identity(
    path: str | Path,
    *,
    expected_kind: TelemetryIdentityKind | str | None = None,
) -> RandomTelemetryIdentity | None:
    """Read strict identity state; absence is distinct from corrupt state."""

    target = Path(path).expanduser()
    if target.is_symlink():
        raise IdentityStateError("telemetry identity path must not be a symlink")
    try:
        with target.open("rb") as stream:
            raw = stream.read(_MAX_IDENTITY_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IdentityStateError("telemetry identity state could not be read") from exc
    if len(raw) > _MAX_IDENTITY_FILE_BYTES:
        raise IdentityStateError("telemetry identity state exceeds its size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityStateError("telemetry identity state is not valid JSON") from exc
    return _identity_from_payload(payload, expected_kind=expected_kind)


def load_or_create_identity(
    path: str | Path,
    kind: TelemetryIdentityKind | str,
    *,
    now: datetime | None = None,
    uuid_factory: Callable[[], uuid.UUID] | None = None,
) -> RandomTelemetryIdentity:
    """Return one stable identity, atomically creating it only when absent.

    Invalid existing state fails closed instead of silently rotating an
    identifier and double-counting the same analytics subject.
    """

    target = Path(path).expanduser()
    normalized_kind = TelemetryIdentityKind(kind)
    with ProfileOperationLock(target, timeout=_IDENTITY_LOCK_TIMEOUT_SECONDS):
        existing = read_identity(target, expected_kind=normalized_kind)
        if existing is not None:
            return existing
        identity = generate_random_identity(
            normalized_kind,
            now=now,
            uuid_factory=uuid_factory,
        )
        _write_identity(target, identity)
        return identity


def delete_identity(path: str | Path) -> bool:
    """Delete the local growth-analysis identity."""

    target = Path(path).expanduser()
    with ProfileOperationLock(target, timeout=_IDENTITY_LOCK_TIMEOUT_SECONDS):
        if target.is_symlink():
            raise IdentityStateError("telemetry identity path must not be a symlink")
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise IdentityStateError("telemetry identity state could not be deleted") from exc
        return True


def _identity_from_payload(
    payload: object,
    *,
    expected_kind: TelemetryIdentityKind | str | None,
) -> RandomTelemetryIdentity:
    if not isinstance(payload, dict):
        raise IdentityStateError("telemetry identity state must be an object")
    expected_keys = {"schema_version", "kind", "value", "created_at_utc"}
    if set(payload) != expected_keys:
        raise IdentityStateError("telemetry identity state has unknown or missing fields")
    if payload.get("schema_version") != IDENTITY_SCHEMA_VERSION:
        raise IdentityStateError("unsupported telemetry identity schema version")
    kind_value = payload.get("kind")
    if not isinstance(kind_value, str):
        raise IdentityStateError("unknown telemetry identity kind")
    try:
        kind = TelemetryIdentityKind(kind_value)
    except ValueError as exc:
        raise IdentityStateError("unknown telemetry identity kind") from exc
    if expected_kind is not None and kind is not TelemetryIdentityKind(expected_kind):
        raise IdentityStateError("telemetry identity belongs to another scope")

    value = payload.get("value")
    if not isinstance(value, str):
        raise IdentityStateError("telemetry identity value must be a UUIDv4 string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise IdentityStateError("telemetry identity value must be a UUIDv4 string") from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122 or str(parsed) != value:
        raise IdentityStateError("telemetry identity value must be a canonical UUIDv4 string")

    created_at_utc = payload.get("created_at_utc")
    if not isinstance(created_at_utc, str) or not _is_utc_timestamp(created_at_utc):
        raise IdentityStateError("telemetry identity creation time must be UTC")
    return RandomTelemetryIdentity(
        kind=kind,
        value=value,
        created_at_utc=created_at_utc,
    )


def _write_identity(path: Path, identity: RandomTelemetryIdentity) -> None:
    if path.is_symlink():
        raise IdentityStateError("telemetry identity path must not be a symlink")
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
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(identity.to_dict(), stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary_name, 0o600)
        except OSError:
            pass
        _replace_identity_file(temporary_name, path)
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


def _replace_identity_file(source: str | Path, destination: Path) -> None:
    for delay in (*_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS, None):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None) not in _WINDOWS_TRANSIENT_REPLACE_ERRORS
                or delay is None
            ):
                raise
            time.sleep(delay)


def _is_utc_timestamp(value: str) -> bool:
    candidate = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)
