from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import shlex
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.tools import pty_backend
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.mark.asyncio
async def test_backend_spawn_exception_is_not_proof_command_never_started(monkeypatch) -> None:
    class Backend:
        spawn = Mock(side_effect=OSError("post-spawn reader initialization failed"))

    backend = SimpleNamespace(PtyProcess=Backend)
    monkeypatch.setitem(sys.modules, "winpty" if os.name == "nt" else "ptyprocess", backend)
    with pytest.raises(pty_backend.PtyBackendError) as error:
        pty_backend.spawn_pty("echo only-once", cwd=None, env=None)
    assert error.value.started is True
    Backend.spawn.assert_called_once()


@pytest.mark.asyncio
async def test_windows_pty_reads_ready_bytes_without_executor_capacity(monkeypatch) -> None:
    reader, writer = socket.socketpair()
    marker = "final output: 终端".encode()
    handle = pty_backend.PtyHandle(SimpleNamespace(fileobj=reader), "windows")
    loop = asyncio.get_running_loop()

    def unavailable_executor(*args, **kwargs):
        raise AssertionError("ready terminal bytes must not wait for a worker")

    try:
        writer.sendall(marker)
        writer.shutdown(socket.SHUT_WR)
        with monkeypatch.context() as context:
            context.setattr(loop, "run_in_executor", unavailable_executor)
            chunks = []
            while chunk := await asyncio.wait_for(pty_backend.read_pty(handle, 3), timeout=1):
                chunks.append(chunk)
        assert b"".join(chunks) == marker
    finally:
        reader.close()
        writer.close()


@pytest.mark.platform_pty
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["kill", "timeout", "eof"])
@pytest.mark.parametrize("detached", [False, True])
async def test_real_pty_cleans_descendant_tree(action, detached, tmp_path) -> None:
    from opensquilla.process_tree import _strict_process_start_identity

    child_pid = tmp_path / "child.pid"
    child = tmp_path / "child.py"
    child.write_text(
        "import os, pathlib, time\n"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n", encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    child_options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP if detached else 0}
        if os.name == "nt" else {"start_new_session": detached}
    )
    parent.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}], **{child_options!r})\n"
        "print('TTY=' + str(sys.stdin.isatty()), flush=True)\n"
        + ("sys.stdin.readline()\n" if action == "eof" else "time.sleep(60)\n"),
        encoding="utf-8",
    )
    argv = [sys.executable, str(parent)]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    token = current_tool_context.set(ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, session_key="test:pty-tree",
        task_id=f"pty-{action}",
    ))
    session = None
    child_process = None
    child_identity = None
    child_handle = None
    kernel32 = None
    try:
        result = await shell._start_host_background_process(
            command, cwd=str(tmp_path), effective_timeout=3 if action == "timeout" else 30,
            runtime=None, io_mode="pty",
        )
        execution_id = shell._session_id_from_start_result(result)
        assert execution_id, result
        session = shell._bg_sessions[execution_id]
        assert session.io_mode_used == "pty", result
        for _ in range(200):
            # A child can publish its PID before the parent writes or ConPTY
            # delivers the TTY proof. Observe both before terminating either.
            if (
                child_pid.exists() and child_pid.read_text()
                and "TTY=True" in shell._bg_rendered_output(session)
            ):
                break
            await asyncio.sleep(0.02)
        assert "TTY=True" in shell._bg_rendered_output(session)
        child_process = int(child_pid.read_text())
        child_identity = _strict_process_start_identity(child_process)
        assert child_identity
        if os.name == "nt":
            kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            child_handle = kernel32.OpenProcess(0x00100000, False, child_process)
            assert child_handle
        if action != "timeout":
            await shell.process(action, execution_id=execution_id)
        await asyncio.wait_for(asyncio.shield(session.collector_task), timeout=10)
        assert "TTY=True" in shell._bg_rendered_output(session)
        assert session.process_tree is not None and session.process_tree.durable
        assert not session.process_tree.is_active()
        if action == "kill":
            returncode = session.returncode
            repeated = json.loads(await shell.process("kill", execution_id=execution_id))
            assert repeated["status"] == "killed"
            assert repeated["session"]["status"] == "killed"
            assert session.done
            assert repeated["session"]["returncode"] == returncode
            assert not session.process_tree.is_active()
        if kernel32 is not None:
            assert kernel32.WaitForSingleObject(child_handle, 5000) == 0
        else:
            assert _strict_process_start_identity(child_process) != child_identity
    finally:
        if session is not None:
            await shell._terminate_bg_session(session)
            if session.collector_task is not None:
                await asyncio.wait_for(asyncio.shield(session.collector_task), timeout=5)
            shell._bg_sessions.pop(session.session_id, None)
        if (
            child_process is not None
            and _strict_process_start_identity(child_process) == child_identity
        ):
            import signal
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(child_process, signal.SIGTERM)
        if kernel32 is not None and child_handle is not None:
            kernel32.CloseHandle(child_handle)
        current_tool_context.reset(token)


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


@pytest.mark.platform_pty
@pytest.mark.asyncio
async def test_real_pty_drains_tail_before_closing_reader(tmp_path) -> None:
    child = tmp_path / "tail.py"
    child.write_text(
        # Unbuffered Windows console writes may accept only part of a buffer.
        # Account for that in the producer so this checks the PTY reader's tail.
        "import sys\npayload = b'x' * 65536 + b'\\nFINAL-PTY-TAIL-MARKER\\n'\n"
        "offset = 0\n"
        "while offset < len(payload):\n"
        "    written = sys.stdout.buffer.write(payload[offset:])\n"
        "    assert written and written > 0, written\n"
        "    offset += written\n"
        "sys.stdout.flush()\n", encoding="utf-8",
    )
    argv = [sys.executable, str(child)]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    session = None
    try:
        result = await shell._start_host_background_process(
            command, cwd=str(tmp_path), effective_timeout=15, runtime=None, io_mode="pty",
        )
        execution_id = shell._session_id_from_start_result(result)
        assert execution_id, result
        session = shell._bg_sessions[execution_id]
        assert session.io_mode_used == "pty"
        await asyncio.wait_for(asyncio.shield(session.collector_task), timeout=20)
        assert session.returncode == 0, shell._bg_rendered_output(session)[-2000:]
        assert "FINAL-PTY-TAIL-MARKER" in shell._bg_rendered_output(session)
        assert session.output_capture.incomplete_reason is None
    finally:
        if session is not None:
            await shell._terminate_bg_session(session)
            shell._bg_sessions.pop(session.session_id, None)


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
    assert raw.writes == ["输入", "\x1a\r\n"]
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
