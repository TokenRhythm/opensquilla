"""Config-backed durable agent registry."""

from __future__ import annotations

import os
import stat
from dataclasses import asdict
from pathlib import Path
from typing import Any

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.gateway.config import AgentEntryConfig, GatewayConfig
from opensquilla.identity.bootstrap import (
    CORE_BOOTSTRAP_TEMPLATE_FILENAMES,
    ONE_SHOT_BOOTSTRAP_FILENAME,
    ensure_agent_workspace,
)
from opensquilla.identity.parser import parse_identity
from opensquilla.session.keys import normalize_agent_id

_WORKSPACE_AGENT_FILE_NAMES = (
    *CORE_BOOTSTRAP_TEMPLATE_FILENAMES,
    ONE_SHOT_BOOTSTRAP_FILENAME,
    "MEMORY.md",
    "memory.md",
)
_WORKSPACE_AGENT_FILE_NAME_SET = frozenset(_WORKSPACE_AGENT_FILE_NAMES)
_ALLOWED_FILE_EXTENSIONS = frozenset({".md", ".txt", ".yaml", ".yml", ".j2"})


class AgentRegistry:
    """Durable agent registry backed by ``GatewayConfig.agents``."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        config_path: str | Path | None = None,
        persist_changes: bool = True,
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.persist_changes = persist_changes

    async def list_agents(self, *, include_builtin: bool = True) -> list[dict[str, Any]]:
        agents: list[dict[str, Any]] = []
        if include_builtin:
            agents.append(self._main_agent_summary())
        agents.extend(self._entry_summary(entry) for entry in self.config.agents)
        return agents

    async def create_agent(
        self,
        *,
        agent_id: str,
        name: str | None = None,
        description: str | None = None,
        model: str | None = None,
        workspace: str | None = None,
        agent_dir: str | None = None,
        tools: dict[str, Any] | list[str] | str | None = None,
        enabled: bool = True,
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_user_agent_id(agent_id)
        if self._find_index(normalized) >= 0:
            raise ValueError(f'Agent "{normalized}" already exists')
        entry = AgentEntryConfig(
            id=normalized,
            name=(name or normalized).strip() or normalized,
            description=description or None,
            model=model or None,
            workspace=workspace or None,
            agent_dir=agent_dir or None,
            tools=tools,
            enabled=enabled,
            system_prompt=system_prompt or None,
        )
        self.config.agents.append(entry)
        await self._persist()
        return self._entry_summary(entry)

    async def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        normalized = self._normalize_user_agent_id(agent_id)
        index = self._require_index(normalized)
        entry = self.config.agents[index]
        updates: dict[str, Any] = {}
        for field in (
            "name",
            "description",
            "model",
            "workspace",
            "agent_dir",
            "tools",
            "enabled",
            "system_prompt",
        ):
            if field in fields:
                updates[field] = fields[field]
        if "systemPrompt" in fields:
            updates["system_prompt"] = fields["systemPrompt"]
        if "agentDir" in fields:
            updates["agent_dir"] = fields["agentDir"]
        if not updates:
            raise ValueError("No fields to update")
        next_entry = entry.model_copy(update=updates)
        self.config.agents[index] = next_entry
        await self._persist()
        return self._entry_summary(next_entry)

    async def delete_agent(self, agent_id: str) -> None:
        normalized = self._normalize_user_agent_id(agent_id)
        index = self._require_index(normalized)
        del self.config.agents[index]
        await self._persist()

    def get_agent_model(self, agent_id: str) -> str | None:
        entry = self._find_entry(normalize_agent_id(agent_id))
        return entry.model if entry is not None and entry.enabled else None

    def get_agent_workspace(self, agent_id: str) -> Path:
        entry = self._find_entry(normalize_agent_id(agent_id))
        if entry is not None and entry.workspace:
            return Path(entry.workspace).expanduser()
        return resolve_agent_workspace_dir(agent_id, self.config)

    async def list_agent_files(self, agent_id: str) -> list[dict[str, Any]]:
        root = self._workspace_root(agent_id)
        return [self._workspace_file_entry(root, name) for name in _WORKSPACE_AGENT_FILE_NAMES]

    async def get_agent_file(self, agent_id: str, name: str) -> dict[str, Any]:
        root = self._workspace_root(agent_id)
        safe_name, path = self._resolve_workspace_agent_file(root, name)
        content = self._read_workspace_agent_file(path)
        return {"name": safe_name, "content": content}

    async def get_identity(self, agent_id: str) -> dict[str, Any]:
        """Return parsed public identity fields without mutating the workspace.

        Unlike the general agent-file APIs, this read path deliberately does
        not call :func:`ensure_agent_workspace`: identity decoration is used by
        read-scoped RPCs and must not seed files as a side effect.  The shared
        hardened reader still rejects escaping paths, symlinks, non-regular
        files, and hardlinks.
        """

        normalized = normalize_agent_id(agent_id)
        root = self.get_agent_workspace(normalized)
        _safe_name, path = self._resolve_workspace_agent_file(root, "IDENTITY.md")
        try:
            content = self._read_workspace_agent_file(path)
        except FileNotFoundError:
            content = ""
        return {"agent_id": normalized, **asdict(parse_identity(content))}

    async def set_agent_file(self, agent_id: str, name: str, content: Any) -> dict[str, Any]:
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        if ext not in _ALLOWED_FILE_EXTENSIONS:
            raise ValueError(
                f"File extension not allowed: {ext}. Allowed: {sorted(_ALLOWED_FILE_EXTENSIONS)}"
            )
        root = self._workspace_root(agent_id)
        safe_name, path = self._resolve_workspace_agent_file(root, name)
        text = content if isinstance(content, str) else str(content)
        data = text.encode("utf-8")
        fd = self._open_workspace_agent_file_for_write(path)
        try:
            self._validate_safe_file_stat(os.fstat(fd))
            os.ftruncate(fd, 0)
            os.write(fd, data)
        finally:
            os.close(fd)
        return {"name": safe_name, "path": safe_name, "size": len(data)}

    def _find_index(self, agent_id: str) -> int:
        for index, entry in enumerate(self.config.agents):
            if normalize_agent_id(entry.id) == agent_id:
                return index
        return -1

    def _require_index(self, agent_id: str) -> int:
        index = self._find_index(agent_id)
        if index < 0:
            raise KeyError(f'Agent "{agent_id}" not found')
        return index

    def _find_entry(self, agent_id: str) -> AgentEntryConfig | None:
        index = self._find_index(agent_id)
        return self.config.agents[index] if index >= 0 else None

    @staticmethod
    def _normalize_user_agent_id(agent_id: str) -> str:
        normalized = normalize_agent_id(agent_id)
        if normalized == "main":
            raise ValueError("Cannot modify builtin agent: main")
        return normalized

    async def _persist(self) -> None:
        if not self.persist_changes:
            return
        from opensquilla.onboarding.config_store import persist_config

        persist_config(
            self.config,
            path=self.config_path or self.config.config_path,
            restart_required=True,
        )

    def _main_agent_summary(self) -> dict[str, Any]:
        return {
            "id": "main",
            "name": "Main Agent",
            "description": "Primary OpenSquilla agent",
            "model": None,
            "workspace": str(resolve_agent_workspace_dir("main", self.config)),
            "enabled": True,
            "isBuiltin": True,
            "type": "builtin",
            "tools": [],
        }

    def _entry_summary(self, entry: AgentEntryConfig) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "id": entry.id,
            "name": entry.name or entry.id,
            "description": entry.description,
            "model": entry.model,
            "workspace": entry.workspace or str(resolve_agent_workspace_dir(entry.id, self.config)),
            "agentDir": entry.agent_dir,
            "enabled": entry.enabled,
            "isBuiltin": False,
            "type": "custom",
            "tools": self._tool_summary(entry.tools),
        }
        if entry.subagents is not None:
            summary["subagents"] = entry.subagents.model_dump(exclude_none=True)
        return summary

    @staticmethod
    def _tool_summary(tools: dict[str, Any] | list[str] | str | None) -> list[str]:
        if tools is None:
            return []
        if isinstance(tools, str):
            return [tools]
        if isinstance(tools, list):
            return [str(item) for item in tools if str(item).strip()]
        allow = tools.get("allow")
        if isinstance(allow, str):
            return [allow]
        if isinstance(allow, list):
            return [str(item) for item in allow if str(item).strip()]
        return []

    def _workspace_root(self, agent_id: str) -> Path:
        normalized = normalize_agent_id(agent_id)
        return ensure_agent_workspace(self.get_agent_workspace(normalized)).workspace_dir

    @staticmethod
    def _workspace_file_entry(root: Path, name: str) -> dict[str, Any]:
        path = root / name
        entry: dict[str, Any] = {
            "name": name,
            "path": name,
            "exists": False,
            "missing": True,
            "status": "missing",
        }
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return entry
        entry.update({"exists": True, "missing": False})
        if stat.S_ISLNK(file_stat.st_mode):
            entry.update({"status": "unsafe", "unsafeReason": "symlink"})
            return entry
        if not stat.S_ISREG(file_stat.st_mode):
            entry.update({"status": "unsafe", "unsafeReason": "not-regular-file"})
            return entry
        if getattr(file_stat, "st_nlink", 1) > 1:
            entry.update({"status": "unsafe", "unsafeReason": "hardlink"})
            return entry
        entry.update({"status": "present", "size": file_stat.st_size})
        return entry

    @staticmethod
    def _validate_workspace_file_name(name: str) -> str:
        if not isinstance(name, str) or not name:
            raise ValueError("params.name is required")
        if name != Path(name).name or "/" in name or "\\" in name:
            raise ValueError("workspace file name must not contain path separators")
        if name not in _WORKSPACE_AGENT_FILE_NAME_SET:
            raise ValueError(f"Unsupported workspace agent file: {name}")
        return name

    def _resolve_workspace_agent_file(self, root: Path, name: str) -> tuple[str, Path]:
        safe_name = self._validate_workspace_file_name(name)
        path = root / safe_name
        try:
            path.resolve(strict=False).relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("workspace file escapes workspace root") from exc
        return safe_name, path

    @staticmethod
    def _validate_safe_file_stat(file_stat: os.stat_result) -> None:
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("workspace agent file must be a regular file")
        if getattr(file_stat, "st_nlink", 1) > 1:
            raise ValueError("workspace agent file must not be hardlinked")

    def _read_workspace_agent_file(self, path: Path) -> str:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, os.O_RDONLY | nofollow)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError("workspace agent file must not be a symlink") from exc

        try:
            self._validate_safe_file_stat(os.fstat(fd))
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                return handle.read()
        finally:
            if fd != -1:
                os.close(fd)

    def _open_workspace_agent_file_for_write(self, path: Path) -> int:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            try:
                return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
            except FileExistsError:
                file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode):
            raise ValueError("workspace agent file must not be a symlink")
        self._validate_safe_file_stat(file_stat)
        try:
            # O_NOFOLLOW on the WRONLY branch mirrors the read-side hardening:
            # an attacker who races the lstat()/open() pair cannot redirect the
            # write to a symlinked target.
            return os.open(path, os.O_WRONLY | nofollow)
        except OSError as exc:
            raise ValueError("workspace agent file must not be a symlink") from exc
