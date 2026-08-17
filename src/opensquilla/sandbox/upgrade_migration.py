"""Idempotent direct-update migration for legacy sandbox state."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensquilla.lossless_toml import patch_import_config
from opensquilla.sandbox.legacy_codec import (
    LegacyModeContext,
    decode_legacy_config_mode,
    decode_legacy_run_mode,
)

MIGRATION_VERSION = 2
JOURNAL_NAME = ".sandbox-upgrade-v2.json"
SNAPSHOT_NAME = ".sandbox-upgrade-snapshot"

_WINDOWS_PRIVATE_ACL_SCRIPT = r"""
$ErrorActionPreference = "Stop"
$target = $env:OPENSQUILLA_UPGRADE_ACL_TARGET
$userSid = $env:OPENSQUILLA_UPGRADE_ACL_USER_SID
$isDirectory = $env:OPENSQUILLA_UPGRADE_ACL_IS_DIRECTORY -eq "1"
$acl = if ($isDirectory) {
    [System.Security.AccessControl.DirectorySecurity]::new()
} else {
    [System.Security.AccessControl.FileSecurity]::new()
}
$acl.SetAccessRuleProtection($true, $false)
$inheritance = [System.Security.AccessControl.InheritanceFlags]::None
if ($isDirectory) {
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
}
$propagation = [System.Security.AccessControl.PropagationFlags]::None
$fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
$allowed = @($userSid)
foreach ($trustedSid in @("S-1-5-18", "S-1-5-32-544")) {
    if ($allowed -notcontains $trustedSid) {
        $allowed += $trustedSid
    }
}
foreach ($sidText in $allowed) {
    $sid = [System.Security.Principal.SecurityIdentifier]::new($sidText)
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid,
        $fullControl,
        $inheritance,
        $propagation,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
}
if ($isDirectory) {
    [System.IO.Directory]::SetAccessControl($target, $acl)
    $verified = [System.IO.Directory]::GetAccessControl($target)
} else {
    [System.IO.File]::SetAccessControl($target, $acl)
    $verified = [System.IO.File]::GetAccessControl($target)
}
if (-not $verified.AreAccessRulesProtected) {
    throw "DACL inheritance remains enabled"
}
$rules = @($verified.Access)
if ($rules.Count -ne $allowed.Count) {
    throw "DACL contains an unexpected rule count"
}
foreach ($rule in $rules) {
    $identity = $rule.IdentityReference.Translate(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
    if ($allowed -notcontains $identity -or
        $rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
        $rule.IsInherited -or
        ($rule.FileSystemRights -band $fullControl) -ne $fullControl -or
        $rule.InheritanceFlags -ne $inheritance -or
        $rule.PropagationFlags -ne $propagation) {
        throw "DACL verification failed"
    }
}
foreach ($sidText in $allowed) {
    $matchCount = 0
    foreach ($rule in $rules) {
        $identity = $rule.IdentityReference.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        if ($identity -eq $sidText) {
            $matchCount += 1
        }
    }
    if ($matchCount -ne 1) {
        throw "DACL principal verification failed"
    }
}
"""
_WINDOWS_PRIVATE_ACL_ENCODED = base64.b64encode(
    _WINDOWS_PRIVATE_ACL_SCRIPT.encode("utf-16-le")
).decode("ascii")
_WINDOWS_IDENTITY_TIMEOUT_SECONDS = 10.0
_WINDOWS_ACL_TIMEOUT_SECONDS = 15.0
_WINDOWS_DLL_DIRECTORY_LOCK = threading.Lock()


def _running_on_windows() -> bool:
    return os.name == "nt"


def _set_windows_dll_directory(path: str | None) -> None:
    import ctypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    setter = kernel32.SetDllDirectoryW
    setter.argtypes = [ctypes.c_wchar_p]
    setter.restype = ctypes.c_bool
    if not setter(path):
        error_code = int(getattr(ctypes, "get_last_error")())
        raise OSError(error_code, "SetDllDirectoryW failed")


@contextmanager
def _system_windows_process_context() -> Iterator[None]:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if not _running_on_windows() or not getattr(sys, "frozen", False) or not bundle_root:
        yield
        return

    with _WINDOWS_DLL_DIRECTORY_LOCK:
        _set_windows_dll_directory(None)
        try:
            yield
        finally:
            _set_windows_dll_directory(str(bundle_root))


def _current_windows_user_sid() -> str:
    try:
        with _system_windows_process_context():
            completed = subprocess.run(
                ["whoami", "/user", "/fo", "csv", "/nh"],
                capture_output=True,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                stdin=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=_WINDOWS_IDENTITY_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired as exc:
        raise OSError("Windows user SID lookup timed out") from exc
    if completed.returncode != 0:
        raise OSError("cannot resolve the current Windows user SID")
    try:
        row = next(csv.reader(completed.stdout.splitlines()))
        sid = row[1].strip()
    except (IndexError, StopIteration) as exc:
        raise OSError("cannot resolve the current Windows user SID") from exc
    if not sid.startswith("S-"):
        raise OSError("cannot resolve the current Windows user SID")
    return sid


def _protect_private_path(
    path: Path,
    *,
    directory: bool,
    windows_user_sid: str | None,
) -> None:
    value = path.lstat()
    attributes = int(getattr(value, "st_file_attributes", 0))
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if stat.S_ISLNK(value.st_mode) or attributes & 0x400 or not expected_type(value.st_mode):
        raise OSError(f"snapshot path has an unsafe type: {path}")

    if _running_on_windows():
        if windows_user_sid is None:
            raise OSError("current Windows user SID is unavailable")
        environment = {
            **os.environ,
            "OPENSQUILLA_UPGRADE_ACL_TARGET": str(path),
            "OPENSQUILLA_UPGRADE_ACL_USER_SID": windows_user_sid,
            "OPENSQUILLA_UPGRADE_ACL_IS_DIRECTORY": "1" if directory else "0",
        }
        try:
            with _system_windows_process_context():
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-NoLogo",
                        "-EncodedCommand",
                        _WINDOWS_PRIVATE_ACL_ENCODED,
                    ],
                    env=environment,
                    capture_output=True,
                    creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                    stdin=subprocess.DEVNULL,
                    text=True,
                    check=False,
                    timeout=_WINDOWS_ACL_TIMEOUT_SECONDS,
                )
        except subprocess.TimeoutExpired as exc:
            detail = " ".join(str(exc.stderr or exc.stdout or "").strip().split())
            suffix = f" ({detail[-500:]})" if detail else ""
            raise OSError(f"Windows ACL hardening timed out: {path}{suffix}") from exc
        if completed.returncode != 0:
            detail = " ".join((completed.stderr or completed.stdout).strip().split())
            suffix = f" ({detail[-500:]})" if detail else ""
            raise OSError(f"cannot protect upgrade snapshot path: {path}{suffix}")
        return

    mode = 0o700 if directory else 0o600
    path.chmod(mode)
    if stat.S_IMODE(path.stat().st_mode) != mode:
        raise OSError(f"cannot protect upgrade snapshot path: {path}")


def _create_private_directory(path: Path, *, windows_user_sid: str | None) -> None:
    path.mkdir(mode=0o700)
    _protect_private_path(
        path,
        directory=True,
        windows_user_sid=windows_user_sid,
    )


def _copy_private_file(
    source: Path,
    destination: Path,
    *,
    windows_user_sid: str | None,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
            descriptor = -1
            shutil.copyfileobj(source_handle, target_handle)
            target_handle.flush()
            os.fsync(target_handle.fileno())
            if not _running_on_windows():
                os.fchmod(target_handle.fileno(), 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _protect_private_path(
        destination,
        directory=False,
        windows_user_sid=windows_user_sid,
    )


def _protect_private_tree(root: Path, *, windows_user_sid: str | None) -> None:
    _protect_private_path(root, directory=True, windows_user_sid=windows_user_sid)
    entries = sorted(root.rglob("*"), key=lambda item: item.as_posix())
    for entry in entries:
        _protect_private_path(
            entry,
            directory=entry.is_dir(),
            windows_user_sid=windows_user_sid,
        )


def _remove_failed_snapshot(path: Path, *, original_error: Exception) -> None:
    try:
        if path.exists():
            shutil.rmtree(path)
        if path.exists():
            raise OSError("path still exists after cleanup")
    except Exception:
        raise OSError(f"upgrade snapshot failed and cleanup failed: {path}") from original_error


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


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(
        path,
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n",
    )


def _store_candidates(home: Path) -> tuple[Path, ...]:
    candidates = [
        home / "config.toml",
        home / "desktop-preferences.json",
        home / "preferences.json",
        home / "sessions.db",
        home / "state" / "sessions.db",
        home / "data" / "sessions.db",
    ]
    return tuple(path for path in candidates if path.is_file())


def inventory_sandbox_stores(home: str | Path) -> tuple[Path, ...]:
    root = Path(home).expanduser().absolute()
    return _store_candidates(root)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _config_mode(payload: dict[str, Any]):
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
    return decode_legacy_config_mode(**arguments)


def lossless_patch_sandbox_fields(raw: bytes) -> tuple[bytes, str]:
    original = tomllib.loads(raw.decode("utf-8"))
    transformed = json.loads(json.dumps(original))
    mode = _config_mode(original)
    sandbox = transformed.setdefault("sandbox", {})
    if not isinstance(sandbox, dict):
        raise ValueError("sandbox config must be a table")
    sandbox["run_mode"] = mode.value
    patched = patch_import_config(raw, original, transformed)
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


class SandboxUpgradeCoordinator:
    def __init__(self, home: str | Path) -> None:
        self.home = Path(home).expanduser().absolute()
        self.journal_path = self.home / JOURNAL_NAME
        self.snapshot_path = self.home / SNAPSHOT_NAME

    def _load_journal(self) -> dict[str, Any] | None:
        if not self.journal_path.exists():
            return None
        payload = json.loads(self.journal_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("migrationVersion") != MIGRATION_VERSION:
            raise ValueError("unsupported sandbox upgrade journal")
        return payload

    def _manual_recovery_report(
        self,
        *,
        store_names: tuple[str, ...],
        error: Exception,
    ) -> UpgradeMigrationReport:
        failed = {
            "migrationVersion": MIGRATION_VERSION,
            "status": "prepared",
            "stores": store_names,
            "snapshot": str(self.snapshot_path),
            "error": f"{type(error).__name__}: {error}",
        }
        _write_json(self.journal_path, failed)
        return UpgradeMigrationReport(
            ok=False,
            status="manual_recovery_required",
            canonical_mode=None,
            journal_path=self.journal_path,
            snapshot_path=self.snapshot_path if self.snapshot_path.exists() else None,
            stores=store_names,
            error=str(failed["error"]),
        )

    def _snapshot(self, stores: tuple[Path, ...]) -> None:
        windows_user_sid = _current_windows_user_sid() if _running_on_windows() else None
        if self.snapshot_path.exists():
            _protect_private_tree(
                self.snapshot_path,
                windows_user_sid=windows_user_sid,
            )
            return
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{SNAPSHOT_NAME}.",
                suffix=".tmp",
                dir=self.home,
            )
        )
        promoted = False
        try:
            _protect_private_path(
                staging,
                directory=True,
                windows_user_sid=windows_user_sid,
            )
            if next(staging.iterdir(), None) is not None:
                raise OSError("upgrade snapshot staging is not empty after hardening")
            manifest: list[dict[str, object]] = []
            for source in stores:
                relative = source.relative_to(self.home)
                destination = staging / relative
                current = staging
                for part in relative.parts[:-1]:
                    current = current / part
                    if not current.exists():
                        _create_private_directory(
                            current,
                            windows_user_sid=windows_user_sid,
                        )
                _copy_private_file(
                    source,
                    destination,
                    windows_user_sid=windows_user_sid,
                )
                manifest.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": _digest(destination),
                        "size": destination.stat().st_size,
                    }
                )
            manifest_path = staging / "manifest.json"
            _write_json(manifest_path, {"stores": manifest})
            _protect_private_path(
                manifest_path,
                directory=False,
                windows_user_sid=windows_user_sid,
            )
            _protect_private_tree(staging, windows_user_sid=windows_user_sid)
            os.replace(staging, self.snapshot_path)
            promoted = True
            _protect_private_tree(
                self.snapshot_path,
                windows_user_sid=windows_user_sid,
            )
        except Exception as exc:
            cleanup = self.snapshot_path if promoted else staging
            _remove_failed_snapshot(cleanup, original_error=exc)
            raise

    def run(self) -> UpgradeMigrationReport:
        from opensquilla.recovery.locking import acquire_profile_locks

        self.home.mkdir(parents=True, exist_ok=True)
        with acquire_profile_locks(self.home, timeout=30.0):
            return self._run_locked()

    def _run_locked(self) -> UpgradeMigrationReport:
        self.home.mkdir(parents=True, exist_ok=True)
        stores = inventory_sandbox_stores(self.home)
        store_names = tuple(path.relative_to(self.home).as_posix() for path in stores)
        journal = self._load_journal()
        if journal is not None and journal.get("status") == "committed":
            try:
                if self.snapshot_path.exists():
                    self._snapshot(())
            except Exception as exc:
                return self._manual_recovery_report(
                    store_names=store_names,
                    error=exc,
                )
            return UpgradeMigrationReport(
                ok=True,
                status="committed",
                canonical_mode=journal.get("canonicalMode"),
                journal_path=self.journal_path,
                snapshot_path=self.snapshot_path if self.snapshot_path.exists() else None,
                stores=store_names,
            )
        try:
            self._snapshot(stores)
            prepared = {
                "migrationVersion": MIGRATION_VERSION,
                "status": "prepared",
                "preparedAt": int(time.time()),
                "stores": store_names,
                "snapshot": str(self.snapshot_path),
            }
            _write_json(self.journal_path, prepared)
            canonical_mode: str | None = None
            config_path = self.home / "config.toml"
            if config_path.is_file():
                patched, canonical_mode = lossless_patch_sandbox_fields(
                    config_path.read_bytes()
                )
                if patched != config_path.read_bytes():
                    _atomic_write(config_path, patched)
            for name in ("desktop-preferences.json", "preferences.json"):
                preference_path = self.home / name
                if not preference_path.is_file():
                    continue
                original = json.loads(preference_path.read_text(encoding="utf-8"))
                transformed = _canonicalize_preferences(original)
                if transformed != original:
                    _write_json(preference_path, transformed)
            committed = {
                **prepared,
                "status": "committed",
                "committedAt": int(time.time()),
                "canonicalMode": canonical_mode,
            }
            _write_json(self.journal_path, committed)
            return UpgradeMigrationReport(
                ok=True,
                status="committed",
                canonical_mode=canonical_mode,
                journal_path=self.journal_path,
                snapshot_path=self.snapshot_path,
                stores=store_names,
            )
        except Exception as exc:
            return self._manual_recovery_report(
                store_names=store_names,
                error=exc,
            )


def ensure_sandbox_upgrade_migrated(home: str | Path) -> UpgradeMigrationReport:
    return SandboxUpgradeCoordinator(home).run()


def inspect_sandbox_upgrade(home: str | Path) -> UpgradeMigrationReport:
    coordinator = SandboxUpgradeCoordinator(home)
    try:
        journal = coordinator._load_journal()
    except Exception as exc:
        return UpgradeMigrationReport(
            ok=False,
            status="manual_recovery_required",
            canonical_mode=None,
            journal_path=coordinator.journal_path,
            snapshot_path=(
                coordinator.snapshot_path if coordinator.snapshot_path.exists() else None
            ),
            stores=(),
            error=f"{type(exc).__name__}: {exc}",
        )
    if journal is None:
        return UpgradeMigrationReport(
            ok=True,
            status="not_started",
            canonical_mode=None,
            journal_path=coordinator.journal_path,
            snapshot_path=None,
            stores=(),
        )
    return UpgradeMigrationReport(
        ok=journal.get("status") == "committed",
        status=str(journal.get("status") or "manual_recovery_required"),
        canonical_mode=journal.get("canonicalMode"),
        journal_path=coordinator.journal_path,
        snapshot_path=(
            coordinator.snapshot_path if coordinator.snapshot_path.exists() else None
        ),
        stores=tuple(str(item) for item in journal.get("stores", ())),
        error=journal.get("error"),
    )


__all__ = [
    "SandboxUpgradeCoordinator",
    "UpgradeMigrationReport",
    "ensure_sandbox_upgrade_migrated",
    "inspect_sandbox_upgrade",
    "inventory_sandbox_stores",
    "lossless_patch_sandbox_fields",
]
