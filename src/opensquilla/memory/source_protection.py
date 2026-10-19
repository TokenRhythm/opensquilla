"""Keep implicit source maintenance out of user-selected execution trees.

This is a maintenance boundary, not a new filesystem permission system. Explicit
user writes still use their existing tool/RPC authorization and conflict checks.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from opensquilla.agents.scope import default_workspace_dir, resolve_agent_workspace_dir


def _canonical(path: str | Path) -> str:
    value = os.path.normcase(str(Path(path).expanduser().resolve()))
    # Native long-path and public Windows spellings must compare identically.
    if value.lower().startswith("\\\\?\\unc\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return value.replace("\\", "/").rstrip("/") + "/"


def paths_overlap(first: str | Path, second: str | Path) -> bool:
    """Whether either tree includes the other (including aliases on this host)."""
    try:
        left, right = _canonical(first), _canonical(second)
        return left.startswith(right) or right.startswith(left)
    except (OSError, RuntimeError, ValueError):
        # An unresolvable source cannot safely authorize implicit deletion.
        return True


class SourceMaintenancePolicy:
    def __init__(
        self,
        config: Any,
        project_roots: Callable[[], Iterable[str | Path]] | None = None,
    ) -> None:
        self.config = config
        self.project_roots = project_roots or (lambda: ())

    def _protected_roots(self) -> Iterable[str | Path]:
        yield from self.project_roots()
        configured = getattr(self.config, "workspace_dir", None)
        if configured and (
            getattr(self.config, "workspace_dir_source", None) == "configured"
            or _canonical(configured) != _canonical(default_workspace_dir())
        ):
            yield configured
        agents = getattr(self.config, "agents", ())
        entries = agents.values() if isinstance(agents, Mapping) else agents
        for agent in entries or ():
            workspace = (
                agent.get("workspace") if isinstance(agent, Mapping)
                else getattr(agent, "workspace", None)
            )
            if workspace:
                yield workspace

    def allows(
        self,
        agent_id: str,
        workspace_dir: Path,
        notes_dir: Path | None = None,
    ) -> bool:
        del agent_id  # Source paths, rather than the caller's identity, determine overlap.
        try:
            sources = (workspace_dir,) if notes_dir is None else (workspace_dir, notes_dir)
            return not any(
                paths_overlap(source, root)
                for root in self._protected_roots()
                for source in sources
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def bootstrap_allowed(self, agent_id: str) -> bool:
        return self.allows(agent_id, resolve_agent_workspace_dir(agent_id, self.config))

    def legacy_data_migration_allowed(self, directory: str | Path) -> bool:
        # A relative CWD/data directory is not proof that its DB belongs to us.
        # Only an explicitly configured state root authorizes the legacy move.
        state_dir = getattr(self.config, "state_dir", None)
        return bool(
            state_dir
            and _canonical(state_dir) == _canonical(directory)
            and self.allows("main", Path(directory))
        )


def policy_for_storage(config: Any, storage: Any | None) -> SourceMaintenancePolicy:
    return SourceMaintenancePolicy(
        config,
        project_roots=lambda: getattr(storage, "protected_project_paths", ()),
    )
