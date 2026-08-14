"""Linux runtime permission model for the sandbox helper."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opensquilla.sandbox.backend.linux_paths import canonical_linux_mount
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionProfile,
)
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
    frozen_write_authority: Path | None = None


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
        raise ValueError("unrestricted/default-write filesystem profile must bypass Bubblewrap")
    retargeted_writable_roots = (
        policy.file_system.retargeted_writable_roots if policy.file_system is not None else ()
    )
    if retargeted_writable_roots:
        roots = ", ".join(str(path) for path in retargeted_writable_roots)
        raise ValueError(f"retargeted writable filesystem root: {roots}")

    read_roots: list[LinuxRoot] = []
    write_roots: list[LinuxRoot] = []
    denied_roots: list[Path] = []
    profile_carveout_variants: list[tuple[Path, ...]] = []
    writable_host_paths = {mount.host_path for mount in policy.mounts if mount.mode == "rw"}
    for mount in policy.mounts:
        writable = mount.mode == "rw" or mount.host_path in writable_host_paths
        root = _linux_root(
            mount,
            writable_profile=policy.file_system if writable else None,
        )
        if writable:
            _append_effective_write_root(
                write_roots,
                read_roots,
                root,
                profile=policy.file_system,
            )
        else:
            _append_unique_root(read_roots, root)

    if policy.file_system is not None:
        for entry in policy.file_system.effective_entries:
            path = Path(entry.path)
            carveout_variants: tuple[Path, ...] = ()
            if entry.access is not FileSystemAccess.WRITE:
                carveout_variants = tuple(
                    Path(variant)
                    for variant in policy.file_system.protected_path_variants(entry.lexical_path)
                )
                profile_carveout_variants.append(carveout_variants)
            if entry.access is FileSystemAccess.DENY:
                for variant in carveout_variants or (path,):
                    _append_unique_path(denied_roots, variant)
                continue
            root = LinuxRoot(
                host_path=path,
                sandbox_path=path,
                required=path == Path("/") or path.exists(),
                frozen_write_authority=(path if entry.access is FileSystemAccess.WRITE else None),
            )
            if entry.access is FileSystemAccess.WRITE:
                _append_effective_write_root(
                    write_roots,
                    read_roots,
                    root,
                    profile=policy.file_system,
                )
            else:
                _append_unique_root(read_roots, root)

    protected_subpaths: list[Path] = []
    for variants in profile_carveout_variants:
        if any(
            variant != base and variant.is_relative_to(base)
            for variant in variants
            for root in write_roots
            for base in _protected_subpath_bases(root)
        ):
            for variant in variants:
                _append_unique_path(protected_subpaths, variant)
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


def _linux_root(
    mount: MountSpec,
    *,
    writable_profile: FileSystemPermissionProfile | None = None,
) -> LinuxRoot:
    mount = canonical_linux_mount(mount)
    host_path = mount.host_path
    frozen_write_authority: Path | None = None
    if writable_profile is not None:
        variants = writable_profile.writable_path_variants(host_path)
        if variants:
            host_path = Path(variants[-1])
            frozen_write_authority = host_path
    return LinuxRoot(
        host_path=host_path,
        sandbox_path=Path(str(mount.sandbox_path)),
        required=mount.required,
        frozen_write_authority=frozen_write_authority,
    )


def _protected_subpaths_for_root(root: Path) -> tuple[Path, ...]:
    return tuple(root / name for name in PROTECTED_SUBPATH_NAMES)


def _protected_subpath_bases(root: LinuxRoot) -> tuple[Path, ...]:
    if root.host_path == root.sandbox_path:
        return (root.host_path,)
    return (root.host_path, root.sandbox_path)


def _append_unique_root(roots: list[LinuxRoot], root: LinuxRoot) -> None:
    if any(
        existing.host_path == root.host_path and existing.sandbox_path == root.sandbox_path
        for existing in roots
    ):
        return
    roots.append(root)


def _append_effective_write_root(
    write_roots: list[LinuxRoot],
    read_roots: list[LinuxRoot],
    root: LinuxRoot,
    *,
    profile: FileSystemPermissionProfile | None,
) -> None:
    authority = root.frozen_write_authority
    if profile is None or authority is None:
        _append_unique_root(write_roots, root)
        return
    access = profile.resolve(authority)
    if access is FileSystemAccess.WRITE:
        _append_unique_root(write_roots, root)
    elif access is FileSystemAccess.READ:
        _append_unique_root(
            read_roots,
            LinuxRoot(
                host_path=root.host_path,
                sandbox_path=root.sandbox_path,
                required=root.required,
            ),
        )


def _append_unique_path(paths: list[Path], path: Path) -> None:
    if path not in paths:
        paths.append(path)
