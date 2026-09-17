"""Package-neutral runtime catalog target normalization."""

from __future__ import annotations

import platform as platform_module
import sys

_PLATFORM_NAMES = {
    "win32": "windows",
    "windows": "windows",
    "linux": "linux",
    "darwin": "darwin",
    "macos": "darwin",
}
_ARCH_NAMES = {
    "amd64": "x64",
    "x86_64": "x64",
    "x64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def runtime_target(platform: str | None = None, arch: str | None = None) -> str:
    """Return the normalized ``<platform>-<architecture>`` catalog target."""

    raw_platform = (platform or sys.platform).strip().lower()
    raw_arch = (
        platform_module.machine().strip().lower()
        if arch is None
        else str(arch).strip().lower()
    )
    platform_name = _PLATFORM_NAMES.get(raw_platform, raw_platform)
    arch_name = _ARCH_NAMES.get(raw_arch, raw_arch)
    return f"{platform_name}-{arch_name}"


__all__ = ["runtime_target"]
