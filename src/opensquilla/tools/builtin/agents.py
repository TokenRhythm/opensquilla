"""Agent listing and subagent management tools."""

from __future__ import annotations

import json
from typing import Any

import structlog

from opensquilla.tools.builtin.sessions import _get_session_manager, _get_task_runtime
from opensquilla.tools.registry import tool
from opensquilla.tools.types import PlanAccess, ToolError, current_tool_context

_VALID_SUBAGENT_ACTIONS = ("list", "kill", "steer")
_TERMINAL_STATUSES = ("done", "failed", "killed", "timeout")

log = structlog.get_logger(__name__)

_agent_registry: object | None = None


def set_agent_registry(registry: object | None) -> None:
    """Inject the AgentRegistry instance (called from gateway boot)."""
    global _agent_registry
    _agent_registry = registry


def _get_agent_registry() -> object:
    if _agent_registry is None:
        raise ToolError("Agent registry not available")
    return _agent_registry


def _manager_unavailable() -> ToolError:
    return ToolError("Session manager not available")


async def _current_session_key(mgr: Any) -> str | None:
    ctx = current_tool_context.get()
    current_key = ctx.session_key if ctx is not None else None
    try:
        current = await mgr.get_current_session()
        if current_key is None and current is not None:
            current_key = getattr(current, "session_key", None)
    except (AttributeError, NotImplementedError):
        pass
    return current_key


def _spawned_by(session_or_dict: object) -> object:
    if isinstance(session_or_dict, dict):
        return session_or_dict.get("spawned_by")
    return getattr(session_or_dict, "spawned_by", None)


def _check_spawned_by_or_raise(session_or_dict: object, current_key: str | None) -> None:
    if current_key is None:
        raise ToolError("session context required")
    if _spawned_by(session_or_dict) != current_key:
        raise ToolError("Session was not spawned by this session")


# ---------------------------------------------------------------------------
# agents_list
# ---------------------------------------------------------------------------


@tool(
    name="agents_list",
    description="List available agent configurations from the agent registry.",
    params={},
    required=[],
    plan_access=PlanAccess.READ_ONLY,
)
async def agents_list() -> str:
    registry = _get_agent_registry()
    list_agents = getattr(registry, "list_agents", None)
    if not callable(list_agents):
        raise ToolError("Agent registry does not expose list_agents")
    entries = await list_agents(include_builtin=True)
    return json.dumps(entries)


# ---------------------------------------------------------------------------
# subagents
# ---------------------------------------------------------------------------


@tool(
    name="subagents",
    description="List, kill, or steer spawned subagent sessions.",
    params={
        "action": {
            "type": "string",
            "description": "Action: list, kill, steer",
        },
        "session_key": {
            "type": "string",
            "description": "Target subagent session key (required for kill and steer)",
        },
        "message": {
            "type": "string",
            "description": "Steering message (required for steer)",
        },
    },
    required=["action"],
    exposed_by_default=False,
)
async def subagents(
    action: str,
    session_key: str | None = None,
    message: str | None = None,
) -> str:
    if action not in _VALID_SUBAGENT_ACTIONS:
        raise ToolError(f"Invalid action: {action}. Must be list|kill|steer")

    if action in ("kill", "steer") and not session_key:
        raise ToolError(f"'session_key' required for {action}")
    if action == "steer" and not message:
        raise ToolError("'message' required for steer")

    # --- list ---
    if action == "list":
        try:
            mgr = _get_session_manager()
            current_key = await _current_session_key(mgr)
            if current_key is None:
                log.warning("subagents.list_no_session_context")
                return json.dumps({"action": "list", "subagents": []})
            all_sessions = await mgr.list_sessions()
            subs = [
                s
                for s in all_sessions
                if isinstance(s, dict) and s.get("spawned_by") == current_key
            ]
            return json.dumps({"action": "list", "subagents": subs})
        except (ImportError, AttributeError, NotImplementedError) as exc:
            raise _manager_unavailable() from exc

    # --- kill / steer need session lookup ---
    try:
        mgr = _get_session_manager()
        current_key = await _current_session_key(mgr)
        if current_key is None:
            raise ToolError("session context required")
        session = await mgr.get_session(session_key)
        if session is None:
            raise ToolError(f"Subagent not found: {session_key}")

        _check_spawned_by_or_raise(session, current_key)

        # Terminal check
        status = getattr(session, "status", None)
        if isinstance(session, dict):
            status = session.get("status")
        if status in _TERMINAL_STATUSES:
            raise ToolError(f"Session '{session_key}' is already terminated")

        if action == "kill":
            try:
                runtime = _get_task_runtime()
            except ToolError:
                runtime = None
            if runtime is not None:
                await runtime.cancel(
                    session_key=session_key,
                    source="agent_tool_kill",
                    reason="agent_tool_kill",
                )
            await mgr.kill_session(session_key)
            return json.dumps({"action": "kill", "session_key": session_key, "status": "killed"})
        else:  # steer
            try:
                runtime = _get_task_runtime()
            except ToolError:
                runtime = None
            if runtime is not None:
                handle = await runtime.send(
                    session_key,
                    message,
                    provenance={"kind": "internal_system", "source_tool": "subagents.steer"},
                )
                return json.dumps(
                    {
                        "action": "steer",
                        "session_key": session_key,
                        "status": "queued",
                        "task_id": handle.task_id,
                    }
                )
            await mgr.inject_message(session_key, message, provenance="internal_system")
            return json.dumps(
                {"action": "steer", "session_key": session_key, "status": "delivered"}
            )
    except ToolError:
        raise
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable() from exc
