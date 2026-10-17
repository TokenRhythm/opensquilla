"""Config-backed durable agent registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from opensquilla.agents import files as agent_files
from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.gateway.config import AgentEntryConfig, GatewayConfig
from opensquilla.identity.parser import parse_identity
from opensquilla.session.keys import normalize_agent_id

_WORKSPACE_AGENT_FILE_NAMES = agent_files.AGENT_FILE_NAMES


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

    async def get_agent_file(
        self, agent_id: str, name: str, *, missing_ok: bool = False,
    ) -> dict[str, Any]:
        root = self._workspace_root(agent_id)
        return await agent_files.read_file(root, name, missing_ok=missing_ok)

    async def get_identity(self, agent_id: str) -> dict[str, Any]:
        """Return parsed public identity fields without mutating the workspace.

        Like the general agent-file read APIs, identity decoration must not
        initialize missing directories or files. The shared hardened reader
        still rejects escaping paths, symlinks, non-regular files, and hardlinks.
        """

        normalized = normalize_agent_id(agent_id)
        root = self.get_agent_workspace(normalized)
        _safe_name, path = self._resolve_workspace_agent_file(root, "IDENTITY.md")
        try:
            content = self._read_workspace_agent_file(path)
        except FileNotFoundError:
            content = ""
        return {"agent_id": normalized, **asdict(parse_identity(content))}

    async def set_agent_file(
        self, agent_id: str, name: str, content: Any, *,
        expected_revision: str | None = None, on_write: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        root = self._workspace_root(agent_id)
        return await agent_files.replace_file(
            root, name, content, expected_revision=expected_revision, on_write=on_write,
        )

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
        return self.get_agent_workspace(normalized).expanduser()

    _workspace_file_entry = staticmethod(agent_files.file_entry)
    _validate_workspace_file_name = staticmethod(agent_files.validate_name)
    _resolve_workspace_agent_file = staticmethod(agent_files.resolve_file)
    _validate_safe_file_stat = staticmethod(agent_files.validate_file_stat)
    _read_workspace_agent_file = staticmethod(agent_files.read_text)
