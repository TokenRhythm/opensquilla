from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.tools import pty_backend
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


class _RawPty:
    pid = 42

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self.closed = False

    def read(self, _size: int) -> str:
        if self.closed:
            raise EOFError
        self.closed = True
        return "héllo"

    def write(self, value: str) -> None:
        self.writes.append(value)

    def setwinsize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))

    def sendeof(self) -> None:
        self.closed = True

    def terminate(self, *, force: bool = False) -> None:
        self.closed = True

    def wait(self) -> int:
        return 0


@pytest.mark.pty
@pytest.mark.asyncio
async def test_pty_handle_lifecycle_preserves_utf8_and_dimensions() -> None:
    raw = _RawPty()
    handle = pty_backend.PtyHandle(raw, "windows")

    assert await pty_backend.read_pty(handle) == "héllo".encode()
    await pty_backend.write_pty(handle, "输入".encode())
    await pty_backend.resize_pty(handle, 77, 19)
    await pty_backend.eof_pty(handle)
    await pty_backend.terminate_pty(handle)
    assert raw.writes == ["输入"]
    assert raw.sizes == [(19, 77)]
    assert await pty_backend.wait_pty(handle) == 0


@pytest.mark.pty
@pytest.mark.asyncio
async def test_pipe_resize_reports_capability_error() -> None:
    class _Process:
        returncode = None
        stdin = None

    previous = dict(shell._bg_sessions)
    shell._bg_sessions.clear()
    session = shell._BgSession(
        session_id="pipe-test",
        command="cat",
        process=_Process(),
        session_key="test:session",
    )
    shell._bg_sessions[session.session_id] = session
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            session_key="test:session",
        )
    )
    try:
        payload = json.loads(
            await shell.process("resize", execution_id=session.session_id, cols=80, rows=24)
        )
    finally:
        current_tool_context.reset(token)
        shell._bg_sessions.clear()
        shell._bg_sessions.update(previous)
    assert payload["status"] == "capability_error"
    assert payload["reason"] == "resize_requires_pty"


@pytest.mark.asyncio
async def test_pty_failure_before_spawn_falls_back_once(monkeypatch, tmp_path) -> None:
    spawn = Mock(side_effect=pty_backend.PtyBackendError("backend unavailable"))
    monkeypatch.setattr(shell, "spawn_pty", spawn)
    pipe_spawn = AsyncMock(wraps=shell._create_host_shell_subprocess)
    monkeypatch.setattr(shell, "_create_host_shell_subprocess", pipe_spawn)
    token = current_tool_context.set(ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, session_key="test:pty-fallback",
    ))
    execution_id = None
    try:
        # Leave the real child observable while process ownership is captured.
        argv = [sys.executable, "-c", "import time; time.sleep(0.2); print('single-spawn')"]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        result = await shell._start_host_background_process(
            command, cwd=str(tmp_path), effective_timeout=10, runtime=None,
            io_mode="pty",
        )
        execution_id = shell._session_id_from_start_result(result)
        assert execution_id
        waited = json.loads(await shell.process("wait", execution_id=execution_id, timeout=10))
        session = waited["session"]
        assert session["io_mode_requested"] == "pty"
        assert session["io_mode_used"] == "pipe"
        assert session["fallback_reason"] == "backend unavailable"
        assert "not a TTY" in session["warning"]
        assert "install" not in session["warning"]
        assert waited["output"].count("single-spawn") == 1
        spawn.assert_called_once()
        pipe_spawn.assert_awaited_once()
    finally:
        if execution_id:
            # The durable tree anchor reports completion after the command's
            # own exit. Observe that boundary before asking to remove its log.
            owner = shell._bg_sessions[execution_id].process_tree
            for _attempt in range(300):
                if owner is None or not owner.is_active():
                    break
                await asyncio.sleep(0.01)
            assert owner is None or not owner.is_active()
            await shell.process("remove", execution_id=execution_id)
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_started_pty_initialization_failure_is_cleaned_without_pipe_retry(
    monkeypatch, tmp_path,
) -> None:
    handle = pty_backend.PtyHandle(_RawPty(), "windows")
    monkeypatch.setattr(shell, "spawn_pty", Mock(side_effect=pty_backend.PtyBackendError(
        "resize failed", started=True, handle=handle,
    )))
    terminate = AsyncMock()
    wait = AsyncMock(return_value=0)
    pipe_spawn = AsyncMock()
    monkeypatch.setattr(shell, "terminate_pty", terminate)
    monkeypatch.setattr(shell, "wait_pty", wait)
    monkeypatch.setattr(shell, "_create_host_shell_subprocess", pipe_spawn)
    result = json.loads(await shell._start_host_background_process(
        "echo never-repeat", cwd=str(tmp_path), effective_timeout=10, runtime=None,
        io_mode="pty",
    ))
    assert result["status"] == "capability_error"
    assert result["reason"] == "pty_started_but_handle_initialization_failed"
    terminate.assert_awaited_once_with(handle)
    wait.assert_awaited_once_with(handle)
    pipe_spawn.assert_not_awaited()
