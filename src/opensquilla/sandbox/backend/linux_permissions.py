"""Linux runtime permission model for the sandbox helper."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opensquilla.sandbox.backend.linux_paths import canonical_linux_mount
from opensquilla.sandbox.permissions import FileSystemAccess
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    SandboxPolicy,
)

PROTECTED_SUBPATH_NAMES = (".git", ".codex", ".agents")


@dataclass(frozen=True)
class LinuxRoot:
    host_path: Path
    sandbox_path: Path
    required: bool


@dataclass(frozen=True)
class LinuxPermissions:
    read_roots: tuple[LinuxRoot, ...]
    write_roots: tuple[LinuxRoot, ...]
    denied_roots: tuple[Path, ...]
    protected_subpaths: tuple[Path, ...]
    env_allowlist: tuple[str, ...]
    network: NetworkMode
    tmp_writable: bool
    wall_timeout_s: float
    read_all: bool = False
    denied_globs: tuple[str, ...] = ()


def compile_linux_permissions(policy: SandboxPolicy) -> LinuxPermissions:
    if (
        policy.file_system is not None
        and policy.file_system.default_access is FileSystemAccess.WRITE
    ):
        raise ValueError(
            "unrestricted/default-write filesystem profile must bypass Bubblewrap"
        )

    read_roots: list[LinuxRoot] = []
    write_roots: list[LinuxRoot] = []
    denied_roots: list[Path] = []
    profile_read_paths: list[Path] = []
    writable_host_paths = {mount.host_path for mount in policy.mounts if mount.mode == "rw"}
    for mount in policy.mounts:
        root = _linux_root(mount)
        if mount.mode == "rw" or mount.host_path in writable_host_paths:
            _append_unique_root(write_roots, root)
        else:
            _append_unique_root(read_roots, root)

    if policy.file_system is not None:
        for entry in policy.file_system.effective_entries:
            path = Path(entry.path)
            if entry.access is FileSystemAccess.DENY:
                _append_unique_path(denied_roots, path)
                continue
            root = LinuxRoot(
                host_path=path,
                sandbox_path=path,
                required=path == Path("/") or path.exists(),
            )
            if entry.access is FileSystemAccess.WRITE:
                _append_unique_root(write_roots, root)
            else:
                _append_unique_root(read_roots, root)
                _append_unique_path(profile_read_paths, path)

    protected_subpaths: list[Path] = []
    for read_path in profile_read_paths:
        if any(
            read_path != root.host_path and read_path.is_relative_to(root.host_path)
            for root in write_roots
        ):
            _append_unique_path(protected_subpaths, read_path)
    for root in write_roots:
        for base in _protected_subpath_bases(root):
            for path in _protected_subpaths_for_root(base):
                _append_unique_path(protected_subpaths, path)

    return LinuxPermissions(
        read_roots=tuple(read_roots),
        write_roots=tuple(write_roots),
        denied_roots=tuple(denied_roots),
        denied_globs=tuple(
            dict.fromkeys(
                (
                    *getattr(policy, "unreadable_globs", ()),
                    *(
                        policy.file_system.denied_read_globs
                        if policy.file_system is not None
                        else ()
                    ),
                )
            )
        ),
        protected_subpaths=tuple(protected_subpaths),
        env_allowlist=tuple(policy.env_allowlist),
        network=policy.network,
        tmp_writable=policy.tmp_writable,
        wall_timeout_s=policy.limits.wall_timeout_s,
        read_all=(
            policy.file_system.has_full_disk_read_baseline
            if policy.file_system is not None
            else False
        ),
    )


def _linux_root(mount: MountSpec) -> LinuxRoot:
    mount = canonical_linux_mount(mount)
    return LinuxRoot(
        host_path=mount.host_path,
        sandbox_path=Path(str(mount.sandbox_path)),
        required=mount.required,
    )


def _protected_subpaths_for_root(root: Path) -> tuple[Path, ...]:
    return tuple(root / name for name in PROTECTED_SUBPATH_NAMES)


def _protected_subpath_bases(root: LinuxRoot) -> tuple[Path, ...]:
    if root.host_path == root.sandbox_path:
        return (root.host_path,)
    return (root.host_path, root.sandbox_path)


def _append_unique_root(roots: list[LinuxRoot], root: LinuxRoot) -> None:
    if any(
        existing.host_path == root.host_path
        and existing.sandbox_path == root.sandbox_path
        for existing in roots
    ):
        return
    roots.append(root)


def _append_unique_path(paths: list[Path], path: Path) -> None:
    if path not in paths:
        paths.append(path)
