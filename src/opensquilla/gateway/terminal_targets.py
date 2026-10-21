"""Resolve interactive-terminal roots without allocating a conversation or task."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from opensquilla.agent_ids import normalize_agent_id
from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.artifact_session.working_files import checked_path
from opensquilla.artifacts import _is_reparse_point
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.gateway.workspace_files import (
    WorkspaceFiles,
    WorkspaceFileUnavailableError,
    _Workspace,
)
from opensquilla.paths import native_io_path
from opensquilla.project_workspaces import (
    ProjectWorkspaceStateError,
    resolve_validated_project_workspace,
)
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.session.keys import canonicalize_session_key


@dataclass(frozen=True)
class TerminalTarget:
    kind: Literal["default-agent", "project", "session"]
    identifier: str

    @property
    def session_key(self) -> str | None:
        return self.identifier if self.kind == "session" else None

    def payload(self) -> dict[str, str]:
        field = {"default-agent": "agentId", "project": "workspaceId", "session": "sessionKey"}
        return {"kind": self.kind, field[self.kind]: self.identifier}


def parse_terminal_target(values: dict[str, Any]) -> TerminalTarget:
    """Accept an explicit target or the legacy sessionKey, never both."""

    if "target" not in values:
        key = values.get("sessionKey", values.get("key"))
        raw = {"kind": "session", "sessionKey": key}
    else:
        if "sessionKey" in values or "key" in values:
            raise ValueError("Specify target or sessionKey, not both")
        raw = values["target"]
    if not isinstance(raw, dict):
        raise ValueError("target must be an object")
    kind = raw.get("kind")
    fields = {"default-agent": "agentId", "project": "workspaceId", "session": "sessionKey"}
    if not isinstance(kind, str) or kind not in fields:
        raise ValueError("Unknown terminal target kind")
    field = fields[kind]
    identifier = raw.get(field)
    if set(raw) != {"kind", field}:
        raise ValueError(f"{kind} target requires only kind and {field}")
    if (
        not isinstance(identifier, str)
        or not identifier.strip()
        or len(identifier) > 512
        or any(ord(char) < 32 for char in identifier)
    ):
        raise ValueError(f"{field} must be a bounded non-empty identifier")
    identifier = identifier.strip()
    if kind == "default-agent":
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier):
            raise ValueError("agentId must be an agent identifier")
        identifier = normalize_agent_id(identifier)
    elif kind == "session":
        identifier = canonicalize_session_key(identifier)
    return TerminalTarget(cast(Literal["default-agent", "project", "session"], kind), identifier)


def _known_agent(config: Any, agent_id: str) -> bool:
    if agent_id == "main":
        return True
    agents = getattr(config, "agents", ())
    entries = (
        agents.items()
        if isinstance(agents, Mapping)
        else ((getattr(entry, "id", ""), entry) for entry in agents)
    )
    for name, entry in entries:
        enabled = (
            entry.get("enabled", True)
            if isinstance(entry, Mapping)
            else getattr(entry, "enabled", True)
        )
        if normalize_agent_id(name) == agent_id:
            return bool(enabled)
    channels = getattr(getattr(config, "channels", None), "channels", ())
    return any(
        normalize_agent_id(getattr(channel, "agent_id", "main")) == agent_id for channel in channels
    )


class TerminalTargetResolver:
    """Revalidate the captured target on every manager binding check.

    Only the first default-agent resolution may create the configured visible
    directory. Later checks never resurrect a directory removed while a PTY ran.
    Session targets retain the existing authoritative execution-root behavior.
    """

    def __init__(self, config: Any, session_manager: Any, target: TerminalTarget) -> None:
        self.config = config
        self.manager = session_manager
        self.target = target
        self._allow_create_default = True

    async def workspace(self, _session_key: str | None = None) -> _Workspace:
        if self.target.kind == "session":
            return await WorkspaceFiles(self.config, self.manager).workspace(self.target.identifier)
        try:
            if self.target.kind == "project":
                storage = get_session_storage(self.manager)
                if storage is None:
                    raise WorkspaceFileUnavailableError()
                validated = await resolve_validated_project_workspace(
                    storage, self.target.identifier
                )
                root = Path(validated.canonical_path)
            else:
                if not _known_agent(self.config, self.target.identifier):
                    raise WorkspaceFileUnavailableError()
                root = resolve_agent_workspace_dir(self.target.identifier, self.config).expanduser()
            allow_create = self.target.kind == "default-agent" and self._allow_create_default
            self._allow_create_default = False
            return await asyncio.to_thread(self._snapshot, root, allow_create=allow_create)
        except (OSError, ValueError, ProjectWorkspaceStateError) as exc:
            raise WorkspaceFileUnavailableError() from exc

    def _snapshot(self, root: Path, *, allow_create: bool) -> _Workspace:
        if _is_reparse_point(root):
            raise WorkspaceFileUnavailableError()
        if allow_create:
            native_io_path(root).mkdir(parents=True, exist_ok=True)
        root = checked_path(root, ".")
        metadata = native_io_path(root).lstat()
        if not stat.S_ISDIR(metadata.st_mode) or _is_reparse_point(root):
            raise WorkspaceFileUnavailableError()
        identity = [self.target.payload(), str(root), metadata.st_dev, metadata.st_ino]
        binding = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        sandbox = getattr(self.config, "sandbox", None)
        profile = FileSystemPermissionProfile.workspace(
            workspace=root,
            denied_read_roots=(
                Path(path).expanduser() for path in getattr(sandbox, "denied_read_roots", ())
            ),
            denied_read_globs=getattr(sandbox, "denied_read_globs", ()),
            host_root_readonly=False,
            tmp_writable=False,
            tmpdir_env_writable=False,
        )
        return _Workspace(root=root, binding=binding, profile=profile)
