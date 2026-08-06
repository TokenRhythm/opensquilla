"""Ephemeral workspace and environment boundary for unauthenticated LAN tasks."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from opensquilla.sandbox.run_context import MountGrant, RunContext
from opensquilla.sandbox.run_mode import RunMode


class GuestProfileBoundaryError(RuntimeError):
    code = "GUEST_DEFAULT_WORKSPACE_UNSAFE"


_FACTORY_MARKER_NAME = ".opensquilla-guest-root"


@dataclass(frozen=True)
class GuestMount:
    path: Path
    kind: Literal["workspace", "home", "temp", "bundled-runtime"]
    access: Literal["rw", "ro"]


@dataclass
class GuestProfile:
    managed_root: Path
    root: Path
    workspace: Path
    home: Path
    temp: Path
    mounts: tuple[GuestMount, ...]
    environment: dict[str, str]
    cleaned: bool = False

    @property
    def host_home_mounted(self) -> bool:
        return False

    def run_context(self) -> RunContext:
        return RunContext(
            run_mode=RunMode.SAFE,
            workspace=str(self.workspace),
            mounts=tuple(
                MountGrant(path=str(mount.path), access=mount.access, scope="once")
                for mount in self.mounts
                if mount.kind != "workspace"
            ),
            source="guest_safe",
        )

    def cleanup(self) -> None:
        if self.cleaned:
            return
        self.cleaned = cleanup_guest_profile_root(
            self.root,
            managed_root=self.managed_root,
        )


def _safe_task_component(task_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(task_id)).strip(".-")
    return cleaned[:48] or "task"


def managed_guest_workspace_root(state_dir: str | Path) -> Path:
    """Derive a non-authority guest root beside the configured state directory."""

    state = Path(state_dir).expanduser().resolve(strict=False)
    if state.parent == state or not state.name:
        raise GuestProfileBoundaryError(
            f"{GuestProfileBoundaryError.code}: invalid OpenSquilla state directory"
        )
    return state.with_name(f"{state.name}-guest-workspaces")


def _guest_environment(
    *,
    home: Path,
    temp: Path,
    runtime_path: tuple[Path, ...],
) -> dict[str, str]:
    path_entries = [str(root) for root in runtime_path]
    environment = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMP": str(temp),
        "TEMP": str(temp),
        "PATH": os.pathsep.join(path_entries),
        "OPENSQUILLA_GUEST_SAFE": "1",
    }
    if os.name == "nt":
        for key in ("SystemRoot", "ComSpec", "PATHEXT", "WINDIR"):
            value = os.environ.get(key)
            if value:
                environment[key] = value
    return environment


class GuestProfileFactory:
    @staticmethod
    def create(
        task_id: str,
        *,
        state_dir: str | Path,
        runtime_roots: tuple[str | Path, ...] = (),
        runtime_path: tuple[str | Path, ...] = (),
    ) -> GuestProfile:
        managed_root = managed_guest_workspace_root(state_dir)
        managed_root.mkdir(parents=True, exist_ok=True)
        if os.path.normcase(str(managed_root.resolve(strict=False))) != os.path.normcase(
            str(managed_root.absolute())
        ):
            raise GuestProfileBoundaryError(
                f"{GuestProfileBoundaryError.code}: guest workspace root is retargeted"
            )
        root = Path(
            tempfile.mkdtemp(
                prefix=f"opensquilla-guest-{_safe_task_component(task_id)}-",
                dir=str(managed_root),
            )
        ).resolve(strict=False)
        if root.parent != managed_root.resolve(strict=False):
            raise GuestProfileBoundaryError(
                f"{GuestProfileBoundaryError.code}: guest turn root escaped its managed root"
            )
        workspace = root / "workspace"
        home = root / "home"
        temp = root / "tmp"
        for directory in (workspace, home, temp):
            directory.mkdir()
        (root / _FACTORY_MARKER_NAME).write_text(root.name, encoding="utf-8")
        resolved_runtimes = tuple(
            Path(runtime_root).expanduser().resolve(strict=False)
            for runtime_root in runtime_roots
            if Path(runtime_root).expanduser().exists()
        )
        resolved_runtime_path = tuple(
            Path(runtime_root).expanduser().resolve(strict=False)
            for runtime_root in runtime_path
            if Path(runtime_root).expanduser().exists()
        )
        mounts = (
            GuestMount(workspace, "workspace", "rw"),
            GuestMount(home, "home", "rw"),
            GuestMount(temp, "temp", "rw"),
            *(
                GuestMount(runtime_root, "bundled-runtime", "ro")
                for runtime_root in resolved_runtimes
            ),
        )
        return GuestProfile(
            managed_root=managed_root,
            root=root,
            workspace=workspace,
            home=home,
            temp=temp,
            mounts=mounts,
            environment=_guest_environment(
                home=home,
                temp=temp,
                runtime_path=resolved_runtime_path,
            ),
        )


def cleanup_guest_profile_root(
    value: str | Path,
    *,
    managed_root: str | Path,
) -> bool:
    """Remove only a factory-shaped turn root below one exact managed root."""

    root = Path(value).expanduser().absolute()
    canonical_root = root.resolve(strict=False)
    expected_parent = Path(managed_root).expanduser().absolute()
    canonical_parent = expected_parent.resolve(strict=False)
    marker = root / _FACTORY_MARKER_NAME
    try:
        factory_marked = (
            marker.is_file()
            and not marker.is_symlink()
            and marker.read_text(encoding="utf-8") == root.name
        )
    except OSError:
        factory_marked = False
    if (
        not root.name.startswith("opensquilla-guest-")
        or len(root.name) <= len("opensquilla-guest-")
        or canonical_root != root
        or canonical_root.name != root.name
        or canonical_root.parent != canonical_parent
        or expected_parent != canonical_parent
        or canonical_root.parent == canonical_root
        or not factory_marked
    ):
        return False
    shutil.rmtree(root, ignore_errors=True)
    return not root.exists()


__all__ = [
    "cleanup_guest_profile_root",
    "managed_guest_workspace_root",
    "GuestMount",
    "GuestProfile",
    "GuestProfileBoundaryError",
    "GuestProfileFactory",
]
