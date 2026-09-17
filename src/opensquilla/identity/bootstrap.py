"""Conservative, missing-only workspace template seeding."""

from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

CORE_BOOTSTRAP_TEMPLATE_FILENAMES = (
    "AGENTS.md",
    "SOUL.md",
    "IDENTITY.md",
    "USER.md",
    "MEMORY.md",
)
RETIRED_WORKSPACE_FILENAMES = frozenset({"BOOTSTRAP.md", "HEARTBEAT.md", "TOOLS.md"})


@dataclass(frozen=True)
class AgentWorkspaceBootstrapResult:
    """Result from ensuring an agent workspace exists and is initialized."""

    workspace_dir: Path
    created_files: tuple[str, ...]


def _native_io_path(path: Path) -> Path:
    """Return an internal OS spelling without changing the logical workspace path."""

    if os.name != "nt":
        return path
    value = ntpath.abspath(str(path))
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{value[2:]}")
    return Path(f"\\\\?\\{value}")


def _template_text(filename: str) -> str:
    template = files("opensquilla.identity").joinpath("templates", "bootstrap", filename)
    return template.read_text(encoding="utf-8")


def _write_template_if_missing(workspace_dir: Path, filename: str) -> bool:
    target = workspace_dir / filename
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_template_text(filename), encoding="utf-8")
    return True


def ensure_agent_workspace(
    workspace_dir: str | Path,
    *,
    seed_templates: bool = True,
) -> AgentWorkspaceBootstrapResult:
    """Create directories and missing core files without updating existing text.

    Retired files and historical onboarding state are left untouched. Updating
    known old defaults is a separate, profile-lease-protected startup operation,
    never a side effect of this function (also used by file-list/read RPCs).
    """

    workspace = Path(workspace_dir).expanduser()
    native_workspace = _native_io_path(workspace)
    native_workspace.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    if seed_templates:
        for filename in CORE_BOOTSTRAP_TEMPLATE_FILENAMES:
            if _write_template_if_missing(native_workspace, filename):
                created.append(filename)
        memory_dir = native_workspace / "memory"
        if not memory_dir.exists():
            memory_dir.mkdir(parents=True)
            created.append("memory/")

    return AgentWorkspaceBootstrapResult(workspace_dir=workspace, created_files=tuple(created))
