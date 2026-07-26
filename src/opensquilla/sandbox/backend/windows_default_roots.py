"""Path planning for the Windows default sandbox."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from opensquilla.sandbox.platform_permissions import WINDOWS_PLATFORM_READ_ROOTS


@dataclass(frozen=True)
class WorkspaceWriteRoots:
    workspace: Path
    cache_root: Path
    rwx_roots: tuple[Path, ...]


def normalize_windows_path(path: str | Path) -> Path:
    return Path(path).expanduser()


def workspace_cache_root(workspace: Path) -> Path:
    return workspace / ".opensquilla-cache"


def workspace_write_roots(workspace: Path) -> WorkspaceWriteRoots:
    cache = workspace_cache_root(workspace)
    return WorkspaceWriteRoots(
        workspace=workspace,
        cache_root=cache,
        rwx_roots=(workspace, cache),
    )


def runtime_rx_roots(
    python_executable: Path,
    *,
    base_prefix: Path | None = None,
) -> tuple[Path, ...]:
    roots: list[Path] = []
    exe_dir = python_executable.parent
    roots.append(exe_dir)
    if exe_dir.name.lower() == "scripts":
        roots.append(exe_dir.parent)
    base = base_prefix if base_prefix is not None else Path(sys.base_prefix)
    if base:
        roots.append(base)
    return tuple(dict.fromkeys(roots))


def windows_system_root(env: Mapping[str, str] | None = None) -> Path:
    source = env or {}
    raw = (
        source.get("SystemRoot") or source.get("SYSTEMROOT") or str(WINDOWS_PLATFORM_READ_ROOTS[0])
    )
    return Path(raw)


def windows_program_data_root(env: Mapping[str, str] | None = None) -> Path:
    source = env or {}
    return Path(source.get("ProgramData") or str(WINDOWS_PLATFORM_READ_ROOTS[3]))


def _program_files_roots_from_env(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    source = env or {}
    roots = [
        Path(source.get("ProgramFiles") or str(WINDOWS_PLATFORM_READ_ROOTS[1])),
        Path(source.get("ProgramFiles(x86)") or str(WINDOWS_PLATFORM_READ_ROOTS[2])),
    ]
    return tuple(dict.fromkeys(root for root in roots if str(root)))


def windows_platform_rx_roots(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    system_root = windows_system_root(env)
    roots = [
        system_root,
        system_root / "System32",
        windows_program_data_root(env),
        *_program_files_roots_from_env(env),
    ]
    return tuple(dict.fromkeys(root for root in roots if str(root)))


def process_executable_rx_roots(
    argv: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    if not argv:
        return ()
    executable = Path(argv[0])
    roots: list[Path] = []
    if executable.is_absolute():
        roots.append(executable.parent)
        roots.append(executable.parent.parent)
    roots.extend(windows_platform_rx_roots(env))
    return tuple(dict.fromkeys(root for root in roots if str(root)))


def opensquilla_state_root(home: Path) -> Path:
    return home / ".opensquilla"


def opensquilla_protected_roots(home: Path) -> tuple[Path, ...]:
    root = opensquilla_state_root(home)
    return (
        root / "sandbox",
        root / "sandbox-secrets",
    )


def windows_sensitive_marker(path: str | Path, *, home: Path | None = None) -> str | None:
    candidate = Path(path)
    base_home = home if home is not None else Path.home()
    for root in _sensitive_user_roots(base_home):
        if _is_relative_to_casefold(candidate, root):
            return "user_secret"
    if _has_sensitive_user_part(candidate):
        return "user_secret"
    protected_roots = list(opensquilla_protected_roots(base_home))
    if home is None:
        # The active profile may live outside ``Path.home()/.opensquilla``
        # (Desktop uses OPENSQUILLA_STATE_DIR). Protect its sandbox marker and
        # secrets with the same classification as the legacy default profile.
        from opensquilla.paths import default_opensquilla_home

        active_root = default_opensquilla_home()
        protected_roots.extend(
            (
                active_root / "sandbox",
                active_root / "sandbox-secrets",
            )
        )
    for root in dict.fromkeys(protected_roots):
        if _is_relative_to_casefold(candidate, root):
            return "opensquilla_sandbox_state"
    text = str(candidate).replace("\\", "/").lower()
    if text.startswith("c:/windows"):
        return "windows_system"
    if text.startswith("c:/program files"):
        return "windows_system"
    return None


def _sensitive_user_roots(home: Path) -> tuple[Path, ...]:
    return (
        home / ".ssh",
        home / ".aws",
        home / ".azure",
        home / ".kube",
        home / ".docker",
        home / ".gnupg",
        home / ".config" / "gh",
    )


def _has_sensitive_user_part(path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    sensitive_parts = {".ssh", ".aws", ".azure", ".kube", ".docker", ".gnupg"}
    if any(part in sensitive_parts for part in parts):
        return True
    return any(
        parts[index : index + 2] == (".config", "gh") for index in range(max(0, len(parts) - 1))
    )


def _is_relative_to_casefold(candidate: Path, root: Path) -> bool:
    c = str(candidate).replace("\\", "/").rstrip("/").lower()
    r = str(root).replace("\\", "/").rstrip("/").lower()
    return c == r or c.startswith(r + "/")


__all__ = [
    "WorkspaceWriteRoots",
    "normalize_windows_path",
    "opensquilla_protected_roots",
    "opensquilla_state_root",
    "process_executable_rx_roots",
    "runtime_rx_roots",
    "windows_sensitive_marker",
    "windows_platform_rx_roots",
    "windows_program_data_root",
    "windows_system_root",
    "workspace_cache_root",
    "workspace_write_roots",
]
