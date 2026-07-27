"""Bubblewrap argv planning for the Linux sandbox helper."""

from __future__ import annotations

import glob
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from opensquilla.sandbox.backend.linux_permissions import LinuxPermissions, LinuxRoot
from opensquilla.sandbox.backend.linux_protected_create import SyntheticMountCleanupTarget
from opensquilla.sandbox.types import NetworkMode, SandboxBackendError

HOST_RUNTIME_READONLY_PATHS = (
    Path("/bin"),
    Path("/sbin"),
    Path("/usr"),
    Path("/etc"),
    Path("/lib"),
    Path("/lib64"),
    Path("/nix/store"),
    Path("/run/current-system/sw"),
)


@dataclass(frozen=True)
class BwrapOptions:
    bwrap_path: str
    mount_proc: bool = True
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class BwrapPlan:
    argv: list[str]
    preserved_files: tuple[BinaryIO, ...] = ()
    synthetic_mount_targets: tuple[SyntheticMountCleanupTarget, ...] = ()
    protected_create_targets: tuple[Path, ...] = ()


def build_bwrap_argv(
    *,
    command: list[str],
    command_cwd: Path,
    permissions: LinuxPermissions,
    options: BwrapOptions,
) -> list[str]:
    return build_bwrap_plan(
        command=command,
        command_cwd=command_cwd,
        permissions=permissions,
        options=options,
    ).argv


def build_bwrap_plan(
    *,
    command: list[str],
    command_cwd: Path,
    permissions: LinuxPermissions,
    options: BwrapOptions,
) -> BwrapPlan:
    _validate_path(command_cwd, kind="command cwd")
    command_cwd = _normalize_command_cwd(command_cwd)
    read_all = permissions.read_all
    argv = [
        options.bwrap_path,
        "--new-session",
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-cgroup-try",
        "--cap-drop",
        "ALL",
        "--clearenv",
    ]
    if read_all:
        argv.extend(["--ro-bind", "/", "/"])
    else:
        argv.extend(["--tmpfs", "/"])
    if permissions.network != NetworkMode.HOST:
        argv.append("--unshare-net")
    if options.mount_proc:
        argv.extend(["--proc", "/proc"])
    argv.extend(["--dev", "/dev"])
    host_tmp_writable = any(
        root.host_path == Path("/tmp") and root.sandbox_path == Path("/tmp")
        for root in permissions.write_roots
    )
    if permissions.tmp_writable and not host_tmp_writable:
        argv.extend(["--tmpfs", "/tmp"])
    for key, value in (options.env or {}).items():
        argv.extend(["--setenv", key, value])

    for host_path in HOST_RUNTIME_READONLY_PATHS:
        if not read_all and host_path.exists():
            argv.extend(_dir_chain_args(host_path))
            argv.extend(["--ro-bind", str(host_path), str(host_path)])
    write_mounts = tuple(_write_mount(root) for root in permissions.write_roots)
    allowed_write_paths = tuple(
        dict.fromkeys(
            path
            for mount in write_mounts
            for path in (mount.root.host_path, mount.source)
        )
    )
    special_overlay_roots = [Path("/dev")]
    if permissions.tmp_writable and not host_tmp_writable:
        special_overlay_roots.append(Path("/tmp"))
    if options.mount_proc:
        special_overlay_roots.append(Path("/proc"))

    preserved_files: list[BinaryIO] = []
    synthetic_mount_targets: list[SyntheticMountCleanupTarget] = []
    protected_create_targets: list[Path] = []
    denied_paths = tuple(
        dict.fromkeys(
            _remap_path_for_write_mounts(
                _canonical_target_if_symlinked_path(path) or path,
                write_mounts,
            )
            for path in [
                *permissions.denied_roots,
                *_expand_denied_globs(permissions.denied_globs, cwd=command_cwd),
            ]
        )
    )
    protected_paths_under_writes = tuple(
        dict.fromkeys(
            _remap_path_for_write_mounts(protected, (mount,))
            for mount in write_mounts
            for protected in permissions.protected_subpaths
            if _is_relative_to(protected, mount.root.host_path)
        )
    )
    read_mounts: list[LinuxRoot] = []
    for root in permissions.read_roots:
        _validate_root(root)
        if not read_all and root.host_path == Path("/"):
            continue
        if read_all and root.host_path == Path("/") and root.sandbox_path == Path("/"):
            continue
        # A read-only host root already exposes every identity-mapped path.
        # Rebinding one of those paths is redundant and can fail for absolute
        # symlink aliases (for example uv's versioned Python directory),
        # because bwrap resolves the source through /oldroot while the target
        # resolves through the new root. Keep non-identity mappings, which may
        # intentionally add an alias at a different sandbox path.
        hidden_by_special_mount = any(
            _is_relative_to(root.sandbox_path, special_root)
            for special_root in special_overlay_roots
        )
        if not root.host_path.exists() and not root.required:
            continue
        read_root = _read_mount_root(root, write_mounts)
        # READ entries below a WRITE root are compiled as protected subpaths.
        # Applying them here would be undone by the later writable bind, so
        # defer them to that bind's ordered carveout events.
        if read_root.sandbox_path in protected_paths_under_writes:
            continue
        denied_ancestor = _deepest_path_containing(read_root.sandbox_path, denied_paths)
        if (
            read_all
            and root.host_path == root.sandbox_path
            and root.host_path.exists()
            and not hidden_by_special_mount
            and denied_ancestor is None
        ):
            continue
        read_mounts.append(read_root)

    reopened_read_mounts = tuple(
        sorted(
            (
                root
                for root in read_mounts
                if _deepest_path_containing(root.sandbox_path, denied_paths) is not None
            ),
            key=lambda item: _path_depth(item.sandbox_path),
        )
    )
    for read_root in read_mounts:
        if read_root in reopened_read_mounts:
            continue
        argv.extend(_mount_target_parent_args(read_root))
        argv.extend(_mount_args(read_root, writable=False))

    reopened_read_paths = tuple(
        dict.fromkeys(root.sandbox_path for root in reopened_read_mounts)
    )
    denied_ancestors_of_writes = tuple(
        sorted(
            (
                denied
                for denied in denied_paths
                if not any(_is_relative_to(denied, root) for root in allowed_write_paths)
                and any(_is_relative_to(root, denied) for root in allowed_write_paths)
            ),
            key=_path_depth,
        )
    )
    denied_ancestors_of_reads = tuple(
        sorted(
            (
                denied
                for denied in denied_paths
                if any(
                    denied != read_path and _is_relative_to(read_path, denied)
                    for read_path in reopened_read_paths
                )
            ),
            key=_path_depth,
        )
    )
    overlay_descendant_paths = tuple(
        dict.fromkeys(
            (
                *allowed_write_paths,
                *reopened_read_paths,
                *protected_paths_under_writes,
            )
        )
    )
    early_denied_paths = tuple(
        sorted(
            dict.fromkeys(
                (*denied_ancestors_of_writes, *denied_ancestors_of_reads)
            ),
            key=_path_depth,
        )
    )
    for denied in early_denied_paths:
        _validate_path(denied, kind="denied path")
        _append_denied_path_args(
            argv,
            denied,
            preserved_files,
            allowed_write_paths,
            synthetic_mount_targets,
            overlay_descendant_paths=overlay_descendant_paths,
        )

    for read_root in reopened_read_mounts:
        if masking_root := _deepest_path_containing(
            read_root.sandbox_path,
            denied_paths,
        ):
            argv.extend(
                _mount_target_parent_args_for_path(
                    read_root.sandbox_path,
                    masking_root,
                )
            )
        argv.extend(_mount_target_parent_args(read_root))
        argv.extend(_mount_args(read_root, writable=False))

        nested_denied = tuple(
            sorted(
                (
                    denied
                    for denied in denied_paths
                    if _is_relative_to(denied, read_root.sandbox_path)
                ),
                key=_path_depth,
            )
        )
        for denied in nested_denied:
            _validate_path(denied, kind="denied path")
            _append_denied_path_args(
                argv,
                denied,
                preserved_files,
                allowed_write_paths,
                synthetic_mount_targets,
                overlay_descendant_paths=overlay_descendant_paths,
            )

    for mount in sorted(write_mounts, key=lambda item: _path_depth(item.source)):
        root = mount.root
        _validate_root(root)
        if not root.host_path.exists() and not root.required:
            continue
        if masking_root := _deepest_path_containing(mount.source, denied_paths):
            argv.extend(_mount_target_parent_args_for_path(mount.source, masking_root))
        argv.extend(_mount_target_parent_args(mount.as_root()))
        argv.extend(_mount_args(mount.as_root(), writable=True))

        nested_denied = tuple(
            sorted(
                (
                    denied
                    for denied in denied_paths
                    if denied not in denied_ancestors_of_writes
                    and _is_relative_to(denied, mount.source)
                ),
                key=_path_depth,
            )
        )
        nested_protected = tuple(
            dict.fromkeys(
                _remap_path_for_write_mounts(protected, (mount,))
                for protected in permissions.protected_subpaths
                if _is_relative_to(protected, root.host_path)
            )
        )
        carveout_events = sorted(
            (
                *((path, "read") for path in nested_protected),
                *((path, "deny") for path in nested_denied),
            ),
            key=lambda event: _path_depth(event[0]),
        )
        for path, access in carveout_events:
            if access == "read":
                _append_protected_path_args(
                    argv,
                    path,
                    preserved_files,
                    allowed_write_paths,
                    protected_create_targets,
                    synthetic_mount_targets,
                )
                continue
            _validate_path(path, kind="denied path")
            _append_denied_path_args(
                argv,
                path,
                preserved_files,
                allowed_write_paths,
                synthetic_mount_targets,
                overlay_descendant_paths=overlay_descendant_paths,
            )

    mounted_protected = {
        _remap_path_for_write_mounts(protected, write_mounts)
        for mount in write_mounts
        for protected in permissions.protected_subpaths
        if _is_relative_to(protected, mount.root.host_path)
    }
    for protected in permissions.protected_subpaths:
        remapped = _remap_path_for_write_mounts(protected, write_mounts)
        if remapped in mounted_protected:
            continue
        _append_protected_path_args(
            argv,
            remapped,
            preserved_files,
            allowed_write_paths,
            protected_create_targets,
            synthetic_mount_targets,
        )
    for denied in sorted(denied_paths, key=_path_depth):
        if denied in denied_ancestors_of_writes:
            continue
        if denied in denied_ancestors_of_reads:
            continue
        if any(
            _is_relative_to(denied, read_root.sandbox_path)
            for read_root in reopened_read_mounts
        ):
            continue
        if any(_is_relative_to(denied, mount.source) for mount in write_mounts):
            continue
        _validate_path(denied, kind="denied path")
        _append_denied_path_args(
            argv,
            denied,
            preserved_files,
            allowed_write_paths,
            synthetic_mount_targets,
        )

    argv.extend(["--chdir", str(command_cwd)])
    argv.append("--")
    argv.extend(command)
    return BwrapPlan(
        argv=argv,
        preserved_files=tuple(preserved_files),
        synthetic_mount_targets=tuple(synthetic_mount_targets),
        protected_create_targets=tuple(protected_create_targets),
    )


@dataclass(frozen=True)
class _WriteMount:
    root: LinuxRoot
    source: Path
    dest: Path

    def as_root(self) -> LinuxRoot:
        return LinuxRoot(self.source, self.dest, self.root.required)


def _write_mount(root: LinuxRoot) -> _WriteMount:
    source = _canonical_target_if_symlinked_path(root.host_path) or root.host_path
    dest = (
        source
        if source != root.host_path and root.sandbox_path == root.host_path
        else root.sandbox_path
    )
    return _WriteMount(root=root, source=source, dest=dest)


def _read_mount_root(root: LinuxRoot, write_mounts: tuple[_WriteMount, ...]) -> LinuxRoot:
    if any(_is_relative_to(root.host_path, mount.root.host_path) for mount in write_mounts):
        source = _canonical_target_if_symlinked_path(root.host_path) or root.host_path
        return LinuxRoot(source, source, root.required)
    return root


def _normalize_command_cwd(command_cwd: Path) -> Path:
    try:
        resolved = command_cwd.resolve(strict=True)
    except OSError:
        return command_cwd
    return resolved if resolved != command_cwd else command_cwd


def _canonical_target_if_symlinked_path(path: Path) -> Path | None:
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        try:
            is_symlink = current.is_symlink()
        except OSError:
            return None
        if is_symlink:
            try:
                target = path.resolve(strict=True)
            except OSError:
                return None
            return None if target == path else target
    return None


def _remap_path_for_write_mounts(path: Path, mounts: tuple[_WriteMount, ...]) -> Path:
    remapped = path
    for mount in mounts:
        if mount.source == mount.root.host_path:
            continue
        try:
            relative = remapped.relative_to(mount.root.host_path)
        except ValueError:
            continue
        remapped = mount.source / relative
    return remapped


def _deepest_path_containing(path: Path, candidates: tuple[Path, ...]) -> Path | None:
    matching = [
        candidate
        for candidate in candidates
        if candidate != path and _is_relative_to(path, candidate)
    ]
    return max(matching, key=_path_depth, default=None)


def _validate_root(root: LinuxRoot) -> None:
    _validate_path(root.host_path, kind="host mount path")
    _validate_path(root.sandbox_path, kind="sandbox mount path")


def _validate_path(path: Path, *, kind: str) -> None:
    if not path.is_absolute():
        raise SandboxBackendError(f"{kind} must be absolute: {path!r}")
    if any(part == ".." for part in path.parts):
        raise SandboxBackendError(f"{kind} contains '..': {path!r}")


def _mount_args(root: LinuxRoot, *, writable: bool) -> list[str]:
    flag = "--bind" if writable else "--ro-bind"
    return [flag, str(root.host_path), str(root.sandbox_path)]


def _mount_target_parent_args(root: LinuxRoot) -> list[str]:
    if root.host_path.is_dir():
        return _dir_chain_args(root.sandbox_path)
    parent = root.sandbox_path.parent
    if parent == root.sandbox_path:
        return []
    return _dir_chain_args(parent)


def _mount_target_parent_args_for_path(path: Path, anchor: Path) -> list[str]:
    mount_target = path if path.is_dir() else path.parent
    dirs: list[Path] = []
    current = mount_target
    while current != anchor and _is_relative_to(current, anchor):
        dirs.append(current)
        current = current.parent
    args: list[str] = []
    for directory in reversed(dirs):
        args.extend(["--dir", str(directory)])
    return args


def _denied_target_parent_args(path: Path) -> list[str]:
    parent = path.parent
    if parent == path:
        return []
    return _dir_chain_args(parent)


def _append_denied_path_args(
    argv: list[str],
    path: Path,
    preserved_files: list[BinaryIO],
    allowed_write_paths: tuple[Path, ...],
    synthetic_mount_targets: list[SyntheticMountCleanupTarget],
    *,
    overlay_descendant_paths: tuple[Path, ...] | None = None,
) -> None:
    if symlink := _first_writable_symlink_component(path, allowed_write_paths):
        raise SandboxBackendError(
            "cannot enforce sandbox deny-read path "
            f"{path} because it crosses writable symlink {symlink}"
        )
    if not path.exists():
        missing_component = _first_missing_component(path)
        if missing_component is not None and any(
            _is_relative_to(missing_component, root) for root in allowed_write_paths
        ):
            _append_missing_empty_file_bind_data_args(
                argv,
                missing_component,
                preserved_files,
                synthetic_mount_targets=synthetic_mount_targets,
            )
        return
    argv.extend(_denied_target_parent_args(path))
    if path.exists() and not path.is_dir():
        empty_file = _empty_bind_file_path(preserved_files)
        argv.extend(
            [
                "--ro-bind",
                empty_file,
                str(path),
            ]
        )
        return
    descendant_paths = (
        allowed_write_paths
        if overlay_descendant_paths is None
        else overlay_descendant_paths
    )
    overlay_descendants = sorted(
        (
            root
            for root in descendant_paths
            if root != path and _is_relative_to(root, path)
        ),
        key=_path_depth,
    )
    perms = "111" if path.is_dir() and overlay_descendants else "000"
    argv.extend(["--perms", perms, "--tmpfs", str(path)])
    for descendant in overlay_descendants:
        argv.extend(_mount_target_parent_args_for_path(descendant, path))
    argv.extend(["--remount-ro", str(path)])


def _append_missing_empty_file_bind_data_args(
    argv: list[str],
    path: Path,
    preserved_files: list[BinaryIO],
    synthetic_mount_targets: list[SyntheticMountCleanupTarget] | None,
) -> None:
    argv.extend(["--ro-bind", _empty_bind_file_path(preserved_files), str(path)])
    if synthetic_mount_targets is not None:
        synthetic_mount_targets.append(
            SyntheticMountCleanupTarget(path=path, kind="empty_file")
        )


def _empty_bind_file_path(preserved_files: list[BinaryIO]) -> str:
    if not preserved_files:
        preserved_files.append(_open_empty_bind_file())
    return str(preserved_files[0].name)


def _open_empty_bind_file() -> BinaryIO:
    file = tempfile.NamedTemporaryFile("w+b")
    file.flush()
    file.seek(0)
    return cast(BinaryIO, file)


def _first_missing_component(path: Path) -> Path | None:
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        if not current.exists() and not current.is_symlink():
            return current
    return None


def _append_protected_path_args(
    argv: list[str],
    protected: Path,
    preserved_files: list[BinaryIO],
    allowed_write_paths: tuple[Path, ...],
    protected_create_targets: list[Path],
    synthetic_mount_targets: list[SyntheticMountCleanupTarget],
) -> None:
    _validate_path(protected, kind="protected path")
    if symlink := _first_writable_symlink_component(protected, allowed_write_paths):
        raise SandboxBackendError(
            "cannot enforce sandbox read-only path "
            f"{protected} because it crosses writable symlink {symlink}"
        )
    if transient := _transient_empty_metadata_path(protected):
        kind, identity = transient
        if kind == "empty_file":
            _append_missing_empty_file_bind_data_args(
                argv,
                protected,
                preserved_files=preserved_files,
                synthetic_mount_targets=None,
            )
        else:
            argv.extend(["--perms", "555", "--tmpfs", str(protected)])
            argv.extend(["--remount-ro", str(protected)])
        synthetic_mount_targets.append(
            SyntheticMountCleanupTarget(
                path=protected,
                kind=kind,
                pre_existing_identity=identity,
            )
        )
        return
    if protected.exists():
        argv.extend(["--ro-bind", str(protected), str(protected)])
    elif _should_leave_missing_git_for_parent_repo_discovery(protected):
        protected_create_targets.append(protected)
    else:
        argv.extend(["--perms", "555", "--tmpfs", str(protected)])
        argv.extend(["--remount-ro", str(protected)])
        synthetic_mount_targets.append(
            SyntheticMountCleanupTarget(path=protected, kind="empty_directory")
        )


def _transient_empty_metadata_path(path: Path) -> tuple[str, tuple[int, int]] | None:
    if path.name not in {".git", ".codex", ".agents"}:
        return None
    try:
        stat_result = path.stat()
    except OSError:
        return None
    identity = (stat_result.st_dev, stat_result.st_ino)
    if path.is_file() and stat_result.st_size == 0:
        return ("empty_file", identity)
    if path.is_dir() and _directory_is_empty(path):
        return ("empty_directory", identity)
    return None


def _directory_is_empty(path: Path) -> bool:
    try:
        next(path.iterdir())
    except StopIteration:
        return True
    except OSError:
        return False
    return False


def _dir_chain_args(path: Path) -> list[str]:
    args: list[str] = []
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        args.extend(["--dir", str(current)])
    return args


def _first_writable_symlink_component(
    path: Path,
    allowed_write_paths: tuple[Path, ...],
) -> Path | None:
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        if not any(_is_relative_to(current, root) for root in allowed_write_paths):
            continue
        if current.is_symlink():
            return current
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_depth(path: Path) -> int:
    return len(path.parts)


def _should_leave_missing_git_for_parent_repo_discovery(path: Path) -> bool:
    if path.name != ".git" or path.exists():
        return False
    mount_root = path.parent
    return any(_ancestor_has_git_metadata(ancestor) for ancestor in mount_root.parents)


def _ancestor_has_git_metadata(ancestor: Path) -> bool:
    git_path = ancestor / ".git"
    if not git_path.exists():
        return False
    if git_path.is_dir():
        return (git_path / "HEAD").exists()
    if git_path.is_file():
        try:
            return git_path.read_text(encoding="utf-8").lstrip().startswith("gitdir:")
        except OSError:
            return False
    return False


def _expand_denied_globs(patterns: tuple[str, ...], *, cwd: Path) -> tuple[Path, ...]:
    expanded: dict[Path, None] = {}
    for pattern in patterns:
        absolute_pattern = Path(pattern)
        if not absolute_pattern.is_absolute():
            absolute_pattern = cwd / absolute_pattern
        if _root_prefix_glob_is_too_broad(absolute_pattern):
            continue
        for candidate_pattern in _glob_pattern_variants(absolute_pattern):
            for raw_match in glob.iglob(
                candidate_pattern,
                recursive=True,
                include_hidden=True,
            ):
                _record_denied_glob_match(raw_match, expanded)
    return tuple(expanded)


def _glob_pattern_variants(pattern: Path) -> tuple[str, ...]:
    text = pattern.as_posix()
    variants = [text]
    if "/**/" in text:
        variants.append(text.replace("/**/", "/"))
    return tuple(dict.fromkeys(variants))


def _record_denied_glob_match(raw_match: str, expanded: dict[Path, None]) -> None:
    match = Path(raw_match)
    if not match.exists() and not match.is_symlink():
        return
    expanded[match] = None
    if target := _canonical_target_if_symlinked_path(match):
        expanded[target] = None


def _root_prefix_glob_is_too_broad(pattern: Path) -> bool:
    text = pattern.as_posix()
    return text.startswith("/**") or text in {"/", "/*"}
