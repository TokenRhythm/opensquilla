from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from opensquilla.engine.types import ToolCall
from opensquilla.gateway.task_runtime import TaskRuntimeShuttingDownError
from opensquilla.tools.builtin import sessions as sessions_tools
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, ToolError, ToolSpec, current_tool_context


class _TerminalSessionManager:
    async def get_session(self, session_key: str) -> object:
        return SimpleNamespace(session_key=session_key, status="done")


class _ActiveSessionManager:
    async def get_session(self, session_key: str) -> object:
        return SimpleNamespace(session_key=session_key, status="active")


def _sessions_send_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="sessions_send",
            description="send",
            parameters={
                "session_key": {"type": "string"},
                "message": {"type": "string"},
            },
            required=["session_key", "message"],
        ),
        sessions_tools.sessions_send,
    )
    return registry


@pytest.mark.asyncio
async def test_sessions_send_terminal_session_error_is_user_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions_tools, "_session_manager", _TerminalSessionManager())

    handler = build_tool_handler(_sessions_send_registry())
    result = await handler(
        ToolCall(
            tool_use_id="tc-sessions-send-terminal",
            tool_name="sessions_send",
            arguments={
                "session_key": "agent:main:subagent:done",
                "message": "hello",
            },
        )
    )

    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["error_class"] == "SafeToolError"
    assert "terminated" in payload["user_message"]
    assert "internal error" not in payload["user_message"]


@pytest.mark.asyncio
async def test_sessions_send_shutdown_error_is_user_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosingRuntime:
        async def send(self, session_key: str, *_args, **_kwargs) -> None:
            raise TaskRuntimeShuttingDownError(session_key=session_key)

    monkeypatch.setattr(sessions_tools, "_session_manager", _ActiveSessionManager())
    monkeypatch.setattr(sessions_tools, "_task_runtime", ClosingRuntime())

    with pytest.raises(ToolError, match="Gateway is shutting down"):
        await sessions_tools.sessions_send("agent:main:target", "hello")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "kwargs"),
    [
        (sessions_tools.sessions_send, {"session_key": "owner", "message": "hello"}),
        (sessions_tools.sessions_spawn, {"task": "hello"}),
        (sessions_tools.sessions_list, {}),
        (sessions_tools.sessions_history, {"session_key": "owner"}),
        (sessions_tools.sessions_yield, {}),
        (sessions_tools.session_status, {}),
    ],
    ids=("send", "spawn", "list", "history", "yield", "status"),
)
async def test_guest_direct_session_handler_calls_fail_before_manager_access(
    monkeypatch: pytest.MonkeyPatch,
    handler,
    kwargs: dict[str, object],
) -> None:
    def manager_must_not_be_read():
        raise AssertionError("guest session handler reached the session manager")

    monkeypatch.setattr(sessions_tools, "_get_session_manager", manager_must_not_be_read)
    token = current_tool_context.set(ToolContext(guest_safe=True))
    try:
        with pytest.raises(ToolError, match="GUEST_TOOL_UNAVAILABLE"):
            await handler(**kwargs)
    finally:
        current_tool_context.reset(token)
