"""Runtime environment shared by skill-owned subprocesses and shell tools.

Managed toolchains are intentionally activation-scoped: without a valid receipt
the operator's environment is unchanged, while an explicitly activated and
verified component may take precedence over an incomplete system installation.
Activation receipts, rather than skill manifests, remain the only source of
managed filesystem paths.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from opensquilla.skills.toolchains import (
    PAPER_FONTS_ENV,
    ToolchainError,
    list_active_components,
    managed_env,
)
from opensquilla.skills.toolchains.manager import toolchains_root

MEDIA_FONTS_DIR_ENV = "OPENSQUILLA_MEDIA_FONTS_DIR"
def managed_skill_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a child environment with activated managed skill resources.

    Corrupt or unreadable managed state must never prevent an otherwise valid
    system command from running.  In that case this helper fails closed by
    returning the unmodified base environment.
    """

    original = dict(os.environ if base_env is None else base_env)
    try:
        return managed_env(original)
    except (OSError, ValueError, ToolchainError):
        return original


def managed_toolchain_readonly_paths() -> tuple[Path, ...]:
    """Return code-owned, validated roots needed by sandboxed toolchains.

    The managed state root is exposed only when a valid activation receipt has
    produced at least one runtime bin directory.  Formula-backed components may
    live outside that root, so their validated bin directory and package parent
    are included too.  Callers must mount these paths read-only.
    """

    try:
        path_value = managed_env({"PATH": ""}).get("PATH", "")
    except (OSError, ValueError, ToolchainError):
        return ()

    bin_dirs: list[Path] = []
    for raw in path_value.split(os.pathsep):
        if not raw:
            continue
        candidate = Path(raw).expanduser().resolve(strict=False)
        if candidate.is_absolute() and candidate.is_dir() and candidate not in bin_dirs:
            bin_dirs.append(candidate)
    if not bin_dirs:
        return ()

    paths: list[Path] = []
    try:
        state_root = toolchains_root().expanduser().resolve(strict=False)
    except (OSError, ValueError):
        state_root = None
    if state_root is not None and state_root.is_dir():
        paths.append(state_root)

    for bin_dir in bin_dirs:
        # The executable directory is sufficient for a standalone archive.
        # Its parent additionally covers formula-local libraries and data.
        for candidate in (bin_dir, bin_dir.parent):
            if candidate.is_dir() and candidate not in paths:
                paths.append(candidate)
    return tuple(paths)


def managed_toolchain_inventory(*, root: Path | None = None) -> list[dict[str, object]]:
    """Return a path-free diagnostics inventory for catalogued components."""

    try:
        return [asdict(status) for status in list_active_components(root=root)]
    except (OSError, ValueError, ToolchainError):
        return []


__all__ = [
    "MEDIA_FONTS_DIR_ENV",
    "PAPER_FONTS_ENV",
    "managed_skill_env",
    "managed_toolchain_inventory",
    "managed_toolchain_readonly_paths",
]
