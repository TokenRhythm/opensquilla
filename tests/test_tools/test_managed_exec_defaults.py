from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine.types import ToolCall
from opensquilla.tools.builtin import shell
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import CallerKind, RetryableToolInputError, ToolContext


@pytest.mark.parametrize("io_mode", ["closed", "pipe", "pty"])
@pytest.mark.parametrize("timeout", [None, 12.5])
async def test_exec_mode_selects_lifetime_without_explicit_yield(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, io_mode: str, timeout: float | None,
) -> None:
    synchronous = AsyncMock(return_value="exit_code=0\nfinished")
    managed = AsyncMock(return_value='{"execution_id":"managed-job"}')
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: True)
    monkeypatch.setattr(shell, "_run_full_host_shell_command", synchronous)
    monkeypatch.setattr(shell, "_start_exec_command_session", managed)
    arguments: dict[str, object] = {"workdir": str(tmp_path)}
    if io_mode != "closed":
        arguments["io_mode"] = io_mode
    if timeout is not None:
        arguments["timeout"] = timeout

    result = await shell.exec_command("echo finished", **arguments)

    if io_mode == "closed":
        assert result == "exit_code=0\nfinished"
        synchronous.assert_awaited_once()
        managed.assert_not_awaited()
        assert synchronous.await_args.kwargs["timeout"] == (60.0 if timeout is None else timeout)
    else:
        assert json.loads(result)["execution_id"] == "managed-job"
        managed.assert_awaited_once()
        synchronous.assert_not_awaited()
        assert managed.await_args.kwargs["timeout"] == (1800.0 if timeout is None else timeout)
        assert managed.await_args.kwargs["io_mode"] == io_mode
        assert managed.await_args.kwargs["yield_time_ms"] == 0


@pytest.mark.parametrize("yield_time_ms", [None, 0])
async def test_invalid_io_mode_is_correctable_before_any_execution(
    monkeypatch: pytest.MonkeyPatch, yield_time_ms: int | None,
) -> None:
    synchronous = AsyncMock()
    managed = AsyncMock()
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "_run_full_host_shell_command", synchronous)
    monkeypatch.setattr(shell, "_start_exec_command_session", managed)

    with pytest.raises(RetryableToolInputError, match="io_mode"):
        await shell.exec_command("echo unused", io_mode="terminal", yield_time_ms=yield_time_ms)

    synchronous.assert_not_awaited()
    managed.assert_not_awaited()


@pytest.mark.parametrize("execution_id", [None, "missing-execution"])
async def test_dispatch_reports_missing_process_handle_as_correctable(
    monkeypatch: pytest.MonkeyPatch, execution_id: str | None,
) -> None:
    monkeypatch.setattr(shell, "_bg_sessions", {})
    context = ToolContext(
        is_owner=True, caller_kind=CallerKind.AGENT, session_key="agent:main:own",
    )
    handler = build_tool_handler(get_default_registry(), context)
    arguments = {"action": "poll"}
    if execution_id is not None:
        arguments["execution_id"] = execution_id

    result = await handler(ToolCall("missing-process", "process", arguments))

    payload = json.loads(result.content)
    assert result.is_error is True
    assert result.terminates_turn is False
    assert payload["retry_allowed"] is True
    assert payload["error_class"] == "RetryableToolInputError"
    assert "execution_id" in payload["user_message"]
    assert "process(action='list')" in payload["user_message"]
    assert "internal error" not in payload["user_message"]


async def test_dispatch_preserves_process_owner_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    private_command = "echo private-other-session-command"
    session = shell._BgSession(
        session_id="owned-execution",
        command=private_command,
        process=SimpleNamespace(returncode=0),
        session_key="agent:main:other",
        done=True,
        returncode=0,
    )
    monkeypatch.setattr(shell, "_bg_sessions", {session.session_id: session})
    context = ToolContext(
        is_owner=True, caller_kind=CallerKind.AGENT, session_key="agent:main:own",
    )
    handler = build_tool_handler(get_default_registry(), context)

    result = await handler(ToolCall(
        "foreign-process", "process", {"action": "poll", "execution_id": session.session_id},
    ))
    listed = await handler(ToolCall("visible-processes", "process", {"action": "list"}))

    assert result.is_error is True
    assert json.loads(result.content)["retry_allowed"] is False
    assert private_command not in result.content
    assert json.loads(listed.content)["sessions"] == []
    assert shell._bg_sessions[session.session_id] is session
