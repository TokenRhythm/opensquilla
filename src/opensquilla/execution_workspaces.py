"""Durable task execution roots, separate from Agent identity and memory.

These bindings authorize a path, not a permanent filesystem object. Each new
execution validates the current root; preview capabilities still need their own
request-time containment and lifetime checks.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import structlog

from opensquilla.project_workspaces import ProjectWorkspaceStateError

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PreparedExecutionWorkspace:
    """A binding plus deletion authority for this allocation only; never persisted."""

    binding: dict[str, Any]
    directory_identity: tuple[int, int]
    parent_identity: tuple[int, int]

    def rollback(self) -> None:
        """Best-effort empty-root cleanup within the trusted profile namespace.

        Identity checks and rmdir are not atomic against another local process
        replacing the path. rmdir still refuses to remove any file contents.
        """
        root = Path(self.binding["root"])
        try:
            parent_stat = root.parent.lstat()
            root_stat = root.lstat()
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or not stat.S_ISDIR(parent_stat.st_mode)
                or (root_stat.st_dev, root_stat.st_ino) != self.directory_identity
                or (parent_stat.st_dev, parent_stat.st_ino) != self.parent_identity
                or root.resolve(strict=True) != root
                or getattr(root, "is_junction", lambda: False)()
            ):
                _log.warning("execution_workspace.rollback_identity_changed", root=str(root))
                return
            # rmdir is deliberately non-recursive: source files, replacements,
            # configured roots, and the shared tasks/profile parents are not ours.
            root.rmdir()
        except FileNotFoundError:
            return
        except (OSError, RuntimeError, ValueError) as exc:
            _log.warning(
                "execution_workspace.rollback_preserved", root=str(root),
                error_type=type(exc).__name__,
            )


def normalize_execution_workspace(value: Any) -> dict[str, Any]:
    """Validate durable metadata without performing filesystem operations."""

    if not isinstance(value, dict) or set(value) != {"version", "id", "kind", "root"}:
        raise ProjectWorkspaceStateError("binding_changed")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ProjectWorkspaceStateError("binding_changed")
    if value["kind"] not in {"managed", "configured"}:
        raise ProjectWorkspaceStateError("binding_changed")
    try:
        if not isinstance(value["id"], str):
            raise ValueError("invalid binding id")
        UUID(value["id"])
        if not isinstance(value["root"], str) or not value["root"].strip():
            raise ValueError("invalid root")
        root = Path(value["root"])
        if not root.is_absolute() or root.parent == root or ".." in root.parts:
            raise ValueError("invalid root")
    except (TypeError, ValueError):
        raise ProjectWorkspaceStateError("binding_changed") from None
    return dict(value)


def validate_execution_workspace(value: Any) -> dict[str, Any]:
    binding = normalize_execution_workspace(value)
    path = Path(binding["root"])
    try:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ProjectWorkspaceStateError("canonical_changed")
        canonical = path.resolve(strict=True)
        if canonical != path:
            raise ProjectWorkspaceStateError("canonical_changed")
        if not canonical.is_dir():
            raise ProjectWorkspaceStateError("unavailable")
        # Match the existing project availability check, without creating a
        # missing root or silently changing the authorization to another path.
        with os.scandir(canonical):
            pass
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectWorkspaceStateError("unavailable") from exc
    return binding


def configured_execution_workspace(path: str | Path) -> dict[str, Any]:
    """Bind an existing directory selected by a trusted creation/CLI boundary."""

    try:
        canonical = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectWorkspaceStateError("unavailable") from exc
    return validate_execution_workspace({
        "version": 1, "id": uuid4().hex, "kind": "configured", "root": str(canonical),
    })


def create_managed_workspace(profile_home: Path) -> dict[str, Any]:
    """Create an immediately owned root (legacy allocation API)."""

    return prepare_managed_workspace(profile_home).binding


def prepare_managed_workspace(profile_home: Path) -> PreparedExecutionWorkspace:
    prepared = None
    try:
        profile = profile_home.expanduser().resolve(strict=False)
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = profile / "tasks"
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ProjectWorkspaceStateError("canonical_changed")
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.resolve(strict=True) != parent:
            raise ProjectWorkspaceStateError("canonical_changed")
        identity = uuid4().hex
        root = parent / identity
        root.mkdir(mode=0o700)
        root_stat = root.lstat()
        parent_stat = parent.lstat()
        prepared = PreparedExecutionWorkspace(
            binding={"version": 1, "id": identity, "kind": "managed", "root": str(root)},
            directory_identity=(root_stat.st_dev, root_stat.st_ino),
            parent_identity=(parent_stat.st_dev, parent_stat.st_ino),
        )
        validate_execution_workspace(prepared.binding)
        return prepared
    except (OSError, RuntimeError, ValueError) as exc:
        if prepared is not None:
            prepared.rollback()
        raise ProjectWorkspaceStateError("unavailable") from exc
