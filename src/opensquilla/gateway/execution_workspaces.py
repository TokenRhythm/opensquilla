"""Allocate task roots at the Gateway's trusted session-creation boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from opensquilla.agents.scope import _configured_agent_workspace, resolve_agent_workspace_dir
from opensquilla.execution_workspaces import (
    PreparedExecutionWorkspace,
    configured_execution_workspace,
    prepare_managed_workspace,
)
from opensquilla.paths import default_opensquilla_home
from opensquilla.session.keys import is_guest_webchat_key, is_subagent_key
from opensquilla.session.models import SessionNode


def build_execution_workspace_factory(
    config: Any, *, profile_home: str | Path | None = None,
) -> Callable[[SessionNode], Awaitable[dict[str, Any] | PreparedExecutionWorkspace | None]]:
    """Allocate only new ordinary task roots; existing sessions are never migrated."""

    async def create(session: SessionNode) -> dict[str, Any] | PreparedExecutionWorkspace | None:
        key = session.session_key
        if (
            session.workspace_id or session.parent_session_key or session.spawned_by
            or is_subagent_key(key) or is_guest_webchat_key(key)
            or key.startswith(("cron:", "heartbeat:", "system:"))
        ):
            return None
        source = getattr(config, "workspace_dir_source", None)
        configured = source == "configured" or (
            source is None and bool(getattr(config, "workspace_dir", None))
        )
        if _configured_agent_workspace(config, session.agent_id) is not None or configured:
            return await asyncio.to_thread(
                configured_execution_workspace,
                resolve_agent_workspace_dir(session.agent_id, config),
            )
        home = Path(profile_home) if profile_home is not None else default_opensquilla_home()
        return await asyncio.to_thread(prepare_managed_workspace, home)

    return create
