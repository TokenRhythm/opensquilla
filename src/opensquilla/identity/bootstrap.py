"""Workspace bootstrap template seeding and one-way setup state."""

from __future__ import annotations

import json
import ntpath
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

WORKSPACE_STATE_DIRNAME = ".opensquilla"
WORKSPACE_STATE_FILENAME = "workspace-state.json"
WORKSPACE_STATE_SCHEMA_VERSION = 1

CORE_BOOTSTRAP_TEMPLATE_FILENAMES = (
    "AGENTS.md",
    "SOUL.md",
    "TOOLS.md",
    "IDENTITY.md",
    "USER.md",
    "MEMORY.md",
    "HEARTBEAT.md",
)
ONE_SHOT_BOOTSTRAP_FILENAME = "BOOTSTRAP.md"


@dataclass(frozen=True)
class AgentWorkspaceBootstrapResult:
    """Result from ensuring an agent workspace exists and is initialized."""

    workspace_dir: Path
    state_path: Path
    bootstrap_path: Path
    created_files: tuple[str, ...]
    bootstrap_seeded: bool
    bootstrap_completed: bool


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def _state_path(workspace_dir: Path) -> Path:
    return workspace_dir / WORKSPACE_STATE_DIRNAME / WORKSPACE_STATE_FILENAME


def _read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": WORKSPACE_STATE_SCHEMA_VERSION}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": WORKSPACE_STATE_SCHEMA_VERSION}
    if not isinstance(raw, dict):
        return {"schema_version": WORKSPACE_STATE_SCHEMA_VERSION}
    state = dict(raw)
    state["schema_version"] = WORKSPACE_STATE_SCHEMA_VERSION
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    payload = dict(state)
    payload["schema_version"] = WORKSPACE_STATE_SCHEMA_VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{datetime.now(UTC).timestamp()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


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


def _has_workspace_user_indicators(workspace_dir: Path) -> bool:
    indicators = [
        *(workspace_dir / name for name in CORE_BOOTSTRAP_TEMPLATE_FILENAMES),
        workspace_dir / ONE_SHOT_BOOTSTRAP_FILENAME,
        workspace_dir / "MEMORY.md",
        workspace_dir / "memory",
        workspace_dir / ".git",
    ]
    return any(path.exists() for path in indicators)


def ensure_agent_workspace(
    workspace_dir: str | Path,
    *,
    seed_templates: bool = True,
    skip_bootstrap: bool = False,
) -> AgentWorkspaceBootstrapResult:
    """Create an agent workspace and seed bootstrap templates conservatively.

    Core templates are created only when missing. ``BOOTSTRAP.md`` is one-shot:
    it is created for fresh empty workspaces, and once removed after seeding the
    workspace is marked complete and the file is not recreated.
    """

    workspace = Path(workspace_dir).expanduser()
    native_workspace = _native_io_path(workspace)
    native_workspace.mkdir(parents=True, exist_ok=True)
    state_path = _state_path(workspace)
    bootstrap_path = workspace / ONE_SHOT_BOOTSTRAP_FILENAME

    if not seed_templates:
        return AgentWorkspaceBootstrapResult(
            workspace_dir=workspace,
            state_path=state_path,
            bootstrap_path=bootstrap_path,
            created_files=(),
            bootstrap_seeded=False,
            bootstrap_completed=False,
        )

    native_state_path = _state_path(native_workspace)
    native_bootstrap_path = native_workspace / ONE_SHOT_BOOTSTRAP_FILENAME
    had_user_indicators = _has_workspace_user_indicators(native_workspace)
    created: list[str] = []
    for filename in CORE_BOOTSTRAP_TEMPLATE_FILENAMES:
        if _write_template_if_missing(native_workspace, filename):
            created.append(filename)

    memory_dir = native_workspace / "memory"
    if not memory_dir.exists():
        memory_dir.mkdir(parents=True)
        created.append("memory/")

    state = _read_state(native_state_path)
    state["workspace_dir"] = str(workspace)
    dirty = not native_state_path.is_file()

    seeded_at = state.get("bootstrap_seeded_at")
    completed_at = state.get("bootstrap_completed_at")
    bootstrap_exists = native_bootstrap_path.exists()

    if isinstance(seeded_at, str) and not bootstrap_exists and not isinstance(completed_at, str):
        state["bootstrap_completed_at"] = _now_iso()
        dirty = True
    elif bootstrap_exists and not isinstance(seeded_at, str):
        state["bootstrap_seeded_at"] = _now_iso()
        dirty = True
    elif (
        not skip_bootstrap
        and not isinstance(seeded_at, str)
        and not isinstance(completed_at, str)
        and not bootstrap_exists
    ):
        if had_user_indicators:
            state["bootstrap_completed_at"] = _now_iso()
            dirty = True
        elif _write_template_if_missing(native_workspace, ONE_SHOT_BOOTSTRAP_FILENAME):
            created.append(ONE_SHOT_BOOTSTRAP_FILENAME)
            state["bootstrap_seeded_at"] = _now_iso()
            dirty = True
    elif skip_bootstrap and not isinstance(completed_at, str):
        state["bootstrap_completed_at"] = _now_iso()
        dirty = True

    if dirty:
        _write_state(native_state_path, state)

    return AgentWorkspaceBootstrapResult(
        workspace_dir=workspace,
        state_path=state_path,
        bootstrap_path=bootstrap_path,
        created_files=tuple(created),
        bootstrap_seeded=isinstance(state.get("bootstrap_seeded_at"), str),
        bootstrap_completed=isinstance(state.get("bootstrap_completed_at"), str),
    )
