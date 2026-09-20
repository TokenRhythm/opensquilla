"""PTY output shares the bounded pipe reader's cancellation and write ownership."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.tools import pty_backend
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.fixture
def terminal():
    reader, writer = socket.socketpair()
    handle = pty_backend.PtyHandle(SimpleNamespace(fileobj=reader), "windows")
    session = shell._BgSession(
        session_id="socket-drain", command="synthetic",
        process=SimpleNamespace(returncode=None), pty_handle=handle,
    )
    try:
        yield session, writer
    finally:
        reader.close()
        writer.close()


async def test_cancelled_read_preserves_later_input(terminal):
    session, writer = terminal
    task = asyncio.create_task(pty_backend.read_pty(session.pty_handle))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    marker = "terminal: 终端".encode()
    writer.sendall(marker)
    assert await asyncio.wait_for(pty_backend.read_pty(session.pty_handle), 1) == marker


async def test_peer_shutdown_wakes_pending_read_as_eof(terminal):
    session, writer = terminal
    task = asyncio.create_task(pty_backend.read_pty(session.pty_handle))
    await asyncio.sleep(0)
    writer.shutdown(socket.SHUT_WR)
    assert await asyncio.wait_for(task, 1) == b""


async def test_partial_utf8_reads_preserve_complete_output(terminal, monkeypatch):
    session, writer = terminal
    original = pty_backend.read_pty

    async def read_one_byte(handle, size):
        return await original(handle, 1)

    monkeypatch.setattr(shell, "read_pty", read_one_byte)
    writer.sendall("hello 终端".encode())
    writer.shutdown(socket.SHUT_WR)
    await asyncio.wait_for(shell._read_bg_output(session, asyncio.Event()), 1)
    assert session.output_capture.preview() == "hello 终端"
    assert session.output_capture.incomplete_reason is None


async def test_backend_eof_is_complete_output(terminal, monkeypatch):
    session, _ = terminal

    async def closed_reader(handle, size):
        raise EOFError

    monkeypatch.setattr(shell, "read_pty", closed_reader)
    await shell._read_bg_output(session, asyncio.Event())
    assert session.output_capture.incomplete_reason is None


async def test_post_exit_timeout_marks_incomplete_and_settles_reader(terminal, monkeypatch):
    session, _ = terminal
    monkeypatch.setattr(shell, "_BACKGROUND_KILL_TIMEOUT", 0.01)
    before = asyncio.all_tasks()
    exited = asyncio.Event()
    exited.set()
    await shell._read_bg_output(session, exited)
    assert session.output_capture.incomplete_reason == (
        "output pipe remained open after process exit"
    )
    assert not (asyncio.all_tasks() - before)


async def test_cancelled_capture_settles_pending_socket_reader(terminal, monkeypatch):
    session, _ = terminal
    reading = asyncio.Event()
    original = pty_backend.read_pty

    async def observed_read(handle, size):
        reading.set()
        return await original(handle, size)

    monkeypatch.setattr(shell, "read_pty", observed_read)
    before = asyncio.all_tasks()
    task = asyncio.create_task(shell._read_bg_output(session, asyncio.Event()))
    await reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.output_capture.incomplete_reason == "output drain interrupted"
    assert not (asyncio.all_tasks() - before)


async def test_cancelled_capture_finishes_write_of_received_bytes(terminal, monkeypatch):
    session, writer = terminal
    writing = asyncio.Event()
    release = threading.Event()
    original = session.output_capture.feed
    loop = asyncio.get_running_loop()

    def slow_write(chunk, stream):
        loop.call_soon_threadsafe(writing.set)
        assert release.wait(2), "test did not release output write"
        original(chunk, stream)

    monkeypatch.setattr(session.output_capture, "feed", slow_write)
    writer.sendall(b"received before cancellation")
    task = asyncio.create_task(shell._read_bg_output(session, asyncio.Event()))
    try:
        await asyncio.wait_for(writing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert session.output_capture.preview() == "received before cancellation"
    assert session.output_capture.incomplete_reason == "output drain interrupted"


@pytest.fixture
async def draining_execution(monkeypatch):
    release = asyncio.Event()
    session = shell._BgSession(
        session_id="draining", command="synthetic", process=SimpleNamespace(returncode=0),
        session_key="agent:main:draining", task_id="owner", notify_on_exit=True,
        process_event_emitter=AsyncMock(),
    )

    async def collect():
        await release.wait()
        session.output_capture.feed(b"FINAL-CAPTURED-TAIL")
        await shell._finalize_bg_session_async(session)

    session.collector_task = asyncio.create_task(collect())
    monkeypatch.setattr(shell, "_bg_sessions", {session.session_id: session})
    monkeypatch.setattr(shell, "_BACKGROUND_KILL_TIMEOUT", 0.01)
    token = current_tool_context.set(ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, session_key=session.session_key,
    ))
    try:
        yield session, release
    finally:
        release.set()
        await session.collector_task
        current_tool_context.reset(token)


async def test_exec_yield_keeps_collector_ownership_of_pending_tail(
    draining_execution, monkeypatch,
):
    session, release = draining_execution
    monkeypatch.setattr(shell, "background_process", AsyncMock(
        return_value=shell._background_process_result(session),
    ))
    result = json.loads(await shell._start_exec_command_session(
        "synthetic", workdir=None, timeout=1, env=None, stdin=None, io_mode="pty",
        yield_time_ms=0, sandbox_permissions="use_default", justification="",
        prefix_rule=None, approval_id=None,
    ))
    assert not result.get("exited", False)
    assert result["session"]["status"] == "running"
    assert not session.output_capture.finished
    release.set()
    await session.collector_task
    assert session.output_capture.preview() == "FINAL-CAPTURED-TAIL"


@pytest.mark.parametrize("action", ["poll", "log", "wait"])
async def test_manual_read_does_not_consume_a_still_draining_result(draining_execution, action):
    session, release = draining_execution
    result = json.loads(await shell.process(action, execution_id=session.session_id, timeout=0.01))
    assert not result.get("exited", False)
    assert result["session"]["status"] == "running"
    assert not session.completion_consumed
    assert not session.output_capture.finished
    release.set()
    await session.collector_task
    session.process_event_emitter.assert_awaited_once()


@pytest.mark.parametrize("mode", ["any", "all"])
async def test_multi_wait_reports_only_results_with_completed_capture(draining_execution, mode):
    session, release = draining_execution
    ready = shell._BgSession(
        session_id="ready", command="synthetic", process=SimpleNamespace(returncode=0),
        done=True, session_key=session.session_key,
    )
    shell._bg_sessions[ready.session_id] = ready
    result = json.loads(await shell.process(
        "wait", execution_ids=[session.session_id, ready.session_id], wait_mode=mode, timeout=0.01,
    ))
    assert result["exited"] is (mode == "any")
    assert result["completed_execution_ids"] == ["ready"]
    assert not session.completion_consumed
    assert not session.output_capture.finished
    release.set()
    await session.collector_task
    session.process_event_emitter.assert_awaited_once()
