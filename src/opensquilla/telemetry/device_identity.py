"""Derive an application-specific device identifier without exporting OS identifiers.

Callers must check upload policy before resolving identity. Profile directories,
user accounts and network interfaces are deliberately not identity inputs.
An unavailable OS identity leaves device-based metrics unattributed.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

_DOMAIN = "opensquilla.telemetry.device.v1\0"
_MACHINE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_IO_PLATFORM_UUID_RE = re.compile(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"')
_LOOKUP_TIMEOUT_SECONDS = 1.0


def derive_device_id(platform: str, machine_id: str) -> str | None:
    """Return a purpose-separated SHA-256 token from a valid OS machine ID."""

    if platform not in {"macos", "windows", "linux"}:
        return None
    normalized = machine_id.strip().replace("-", "").lower()
    if (
        not _MACHINE_ID_RE.fullmatch(normalized)
        or normalized == "0" * 32
        or normalized == "f" * 32
    ):
        return None
    return hashlib.sha256(f"{_DOMAIN}{platform}\0{normalized}".encode()).hexdigest()


@lru_cache(maxsize=1)
def get_device_id() -> str | None:
    """Resolve once per process; never substitute a random or network identity."""

    try:
        if sys.platform == "darwin":
            raw = _macos_machine_id()
            return derive_device_id("macos", raw) if raw is not None else None
        if sys.platform == "win32":
            raw = _windows_machine_id()
            return derive_device_id("windows", raw) if raw is not None else None
        if sys.platform.startswith("linux"):
            for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                raw = _read_machine_id_file(Path(path))
                device_id = derive_device_id("linux", raw) if raw is not None else None
                if device_id is not None:
                    return device_id
    except Exception:
        # Never propagate an OS error that could contain the original value.
        return None
    return None


def _macos_machine_id() -> str | None:
    result = subprocess.run(
        ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        text=True,
        timeout=_LOOKUP_TIMEOUT_SECONDS,
    )
    if len(result.stdout) > 256 * 1024:
        return None
    match = _IO_PLATFORM_UUID_RE.search(result.stdout)
    return match.group(1) if match is not None else None


def _windows_machine_id() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography",
        0,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        value, value_type = winreg.QueryValueEx(key, "MachineGuid")
    return value if value_type == winreg.REG_SZ and isinstance(value, str) else None


def _read_machine_id_file(path: Path) -> str | None:
    try:
        with path.open("r", encoding="ascii") as stream:
            value = stream.read(129)
        return value if len(value) <= 128 else None
    except (OSError, UnicodeError):
        return None


__all__ = ["derive_device_id", "get_device_id"]
