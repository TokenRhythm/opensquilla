"""Lightweight, idempotent normalization for legacy sandbox run modes.

The gateway already understands the released legacy spellings while loading a
profile. This module only canonicalizes those spellings on disk when doing so
is possible. It deliberately has no persistent backup or permission-hardening
dependency: failure to normalize an optional compatibility field must never
make the gateway unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from opensquilla.lossless_toml import patch_import_config
from opensquilla.sandbox.legacy_codec import (
    LegacyModeContext,
    decode_legacy_config_mode,
    decode_legacy_run_mode,
)

MIGRATION_VERSION = 2
JOURNAL_NAME = ".sandbox-upgrade-v2.json"
SNAPSHOT_NAME = ".sandbox-upgrade-snapshot"


@dataclass(frozen=True)
class UpgradeMigrationReport:
    ok: bool
    status: str
    canonical_mode: str | None
    journal_path: Path
    snapshot_path: Path | None
    stores: tuple[str, ...]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": self.status,
            "canonicalMode": self.canonical_mode,
            "journalPath": str(self.journal_path),
            "snapshotPath": str(self.snapshot_path) if self.snapshot_path else None,
            "stores": list(self.stores),
            "error": self.error,
        }


@dataclass(frozen=True)
class _PlannedWrite:
    path: Path
    payload: bytes
    format: Literal["toml", "json"]


def _path_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        # An inaccessible legacy artifact still exists for cleanup purposes,
        # but must not make report construction raise during gateway startup.
        return True
    return True


def _atomic_write(path: Path, payload: bytes) -> None:
    """Durably write *payload* beside *path*, then atomically replace it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _validate_payload(planned: _PlannedWrite) -> None:
    text = planned.payload.decode("utf-8")
    if planned.format == "toml":
        tomllib.loads(text)
    else:
        json.loads(text)


def _config_mode_arguments(payload: dict[str, Any]) -> dict[str, object]:
    sandbox = payload.get("sandbox")
    sandbox_table = sandbox if isinstance(sandbox, dict) else {}
    permissions = payload.get("permissions")
    permissions_table = permissions if isinstance(permissions, dict) else {}
    arguments: dict[str, object] = {}
    if "run_mode" in sandbox_table:
        arguments["run_mode"] = sandbox_table["run_mode"]
    if "default_mode" in permissions_table:
        arguments["permissions_default_mode"] = permissions_table["default_mode"]
    if "sandbox" in sandbox_table:
        arguments["sandbox_enabled"] = sandbox_table["sandbox"]
    elif "enabled" in sandbox_table:
        arguments["sandbox_enabled"] = sandbox_table["enabled"]
    if "security_grading" in sandbox_table:
        arguments["grading_enabled"] = sandbox_table["security_grading"]
    return arguments


def lossless_patch_sandbox_fields(raw: bytes) -> tuple[bytes, str | None]:
    """Canonicalize a released legacy run mode without touching other TOML."""

    original = tomllib.loads(raw.decode("utf-8"))
    arguments = _config_mode_arguments(original)
    if not arguments:
        return raw, None

    mode = decode_legacy_config_mode(**arguments)
    sandbox = original.get("sandbox")
    current = sandbox.get("run_mode") if isinstance(sandbox, dict) else None
    if current == mode.value:
        return raw, mode.value

    transformed = json.loads(json.dumps(original))
    transformed_sandbox = transformed.setdefault("sandbox", {})
    if not isinstance(transformed_sandbox, dict):
        raise ValueError("sandbox config must be a table")
    transformed_sandbox["run_mode"] = mode.value
    patched = patch_import_config(raw, original, transformed)
    tomllib.loads(patched.decode("utf-8"))
    return patched, mode.value


def _canonicalize_preferences(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonicalize_preferences(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, child in value.items():
        if key in {"runMode", "run_mode", "sandboxMode", "sandbox_mode"} and isinstance(
            child, str
        ):
            result[key] = decode_legacy_run_mode(
                child,
                context=LegacyModeContext.STORED_EVENT,
            ).value
        else:
            result[key] = _canonicalize_preferences(child)
    return result


def _preference_payload(value: Any) -> bytes:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    json.loads(payload.decode("utf-8"))
    return payload


def _plan_writes(home: Path) -> tuple[tuple[_PlannedWrite, ...], str | None]:
    planned: list[_PlannedWrite] = []
    canonical_mode: str | None = None

    config_path = home / "config.toml"
    if config_path.is_file():
        original = config_path.read_bytes()
        patched, canonical_mode = lossless_patch_sandbox_fields(original)
        if patched != original:
            planned.append(_PlannedWrite(config_path, patched, "toml"))

    for name in ("desktop-preferences.json", "preferences.json"):
        path = home / name
        if not path.is_file():
            continue
        original_bytes = path.read_bytes()
        original = json.loads(original_bytes.decode("utf-8"))
        transformed = _canonicalize_preferences(original)
        if transformed != original:
            planned.append(_PlannedWrite(path, _preference_payload(transformed), "json"))

    return tuple(planned), canonical_mode


def _legacy_artifacts_present(home: Path) -> bool:
    return any(_path_exists_no_follow(home / name) for name in (SNAPSHOT_NAME, JOURNAL_NAME))


def _remove_legacy_path(path: Path) -> None:
    metadata = path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    is_reparse = bool(attributes & 0x400)
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and not is_reparse:
        shutil.rmtree(path)
    else:
        path.unlink()


def _cleanup_legacy_artifacts(home: Path) -> tuple[str, ...]:
    errors: list[str] = []
    for name in (SNAPSHOT_NAME, JOURNAL_NAME):
        path = home / name
        try:
            if _path_exists_no_follow(path):
                _remove_legacy_path(path)
        except Exception as exc:  # best-effort compatibility artifact cleanup
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return tuple(errors)


def inventory_sandbox_stores(home: str | Path) -> tuple[Path, ...]:
    """Return only configuration files that this migrator may rewrite."""

    root = Path(home).expanduser().absolute()
    return tuple(
        path
        for path in (
            root / "config.toml",
            root / "desktop-preferences.json",
            root / "preferences.json",
        )
        if path.is_file()
    )


class SandboxUpgradeCoordinator:
    def __init__(self, home: str | Path) -> None:
        self.home = Path(home).expanduser().absolute()
        self.journal_path = self.home / JOURNAL_NAME
        self.snapshot_path = self.home / SNAPSHOT_NAME

    def _report(
        self,
        *,
        ok: bool,
        status: str,
        canonical_mode: str | None = None,
        stores: tuple[str, ...] = (),
        error: str | None = None,
    ) -> UpgradeMigrationReport:
        return UpgradeMigrationReport(
            ok=ok,
            status=status,
            canonical_mode=canonical_mode,
            journal_path=self.journal_path,
            snapshot_path=(
                self.snapshot_path if _path_exists_no_follow(self.snapshot_path) else None
            ),
            stores=stores,
            error=error,
        )

    def run(self) -> UpgradeMigrationReport:
        try:
            initial_writes, canonical_mode = _plan_writes(self.home)
        except Exception as exc:
            return self._report(
                ok=False,
                status="retry_required",
                error=f"{type(exc).__name__}: {exc}",
            )

        # The common, already-canonical path is read-only: do not even create
        # profile lock files when there is no migration or legacy cleanup.
        if not initial_writes and not _legacy_artifacts_present(self.home):
            return self._report(
                ok=True,
                status="not_required",
                canonical_mode=canonical_mode,
            )

        try:
            from opensquilla.recovery.locking import acquire_profile_locks

            with acquire_profile_locks(self.home, timeout=0.0):
                # Re-plan under the lock so a concurrent successful migration
                # turns this invocation into a no-op instead of a stale write.
                planned_writes, canonical_mode = _plan_writes(self.home)
                store_names = tuple(item.path.name for item in planned_writes)
                for planned in planned_writes:
                    _validate_payload(planned)
                for planned in planned_writes:
                    _atomic_write(planned.path, planned.payload)
                cleanup_errors = _cleanup_legacy_artifacts(self.home)
        except Exception as exc:
            return self._report(
                ok=False,
                status="retry_required",
                canonical_mode=canonical_mode,
                stores=tuple(item.path.name for item in initial_writes),
                error=f"{type(exc).__name__}: {exc}",
            )

        if cleanup_errors:
            return self._report(
                ok=True,
                status="cleanup_pending",
                canonical_mode=canonical_mode,
                stores=store_names,
                error="; ".join(cleanup_errors),
            )
        return self._report(
            ok=True,
            status="committed" if planned_writes else "not_required",
            canonical_mode=canonical_mode,
            stores=store_names,
        )


def ensure_sandbox_upgrade_migrated(home: str | Path) -> UpgradeMigrationReport:
    return SandboxUpgradeCoordinator(home).run()


def inspect_sandbox_upgrade(home: str | Path) -> UpgradeMigrationReport:
    coordinator = SandboxUpgradeCoordinator(home)
    try:
        planned, canonical_mode = _plan_writes(coordinator.home)
    except Exception as exc:
        return coordinator._report(
            ok=False,
            status="retry_required",
            error=f"{type(exc).__name__}: {exc}",
        )
    stores = tuple(item.path.name for item in planned)
    if planned:
        return coordinator._report(
            ok=True,
            status="pending",
            canonical_mode=canonical_mode,
            stores=stores,
        )
    if _legacy_artifacts_present(coordinator.home):
        return coordinator._report(
            ok=True,
            status="legacy_artifacts_present",
            canonical_mode=canonical_mode,
        )
    return coordinator._report(
        ok=True,
        status="not_required",
        canonical_mode=canonical_mode,
    )


__all__ = [
    "SandboxUpgradeCoordinator",
    "UpgradeMigrationReport",
    "ensure_sandbox_upgrade_migrated",
    "inspect_sandbox_upgrade",
    "inventory_sandbox_stores",
    "lossless_patch_sandbox_fields",
]
