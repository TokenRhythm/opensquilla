"""Durable task execution roots, separate from Agent identity and memory.

These bindings authorize a path, not a permanent filesystem object. Each new
execution validates the current root; preview capabilities still need their own
request-time containment and lifetime checks.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from opensquilla.project_workspaces import ProjectWorkspaceStateError


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
        return validate_execution_workspace({
            "version": 1, "id": identity, "kind": "managed", "root": str(root),
        })
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectWorkspaceStateError("unavailable") from exc
