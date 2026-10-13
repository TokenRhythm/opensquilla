from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import socket
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla import process_tree
from opensquilla.tools import pty_backend
from opensquilla.tools.builtin import shell
from opensquilla.tools.pty_backend import PtyBackendError, PtyHandle
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.fixture(params=["pty", "stdin"])
def native_launch(request, tmp_path, monkeypatch):
    kind = request.param
    if kind == "stdin" and os.name != "nt":
        pytest.skip("explicit blocking stdin is Windows-only")
    if kind == "pty":
        pytest.importorskip("winpty" if os.name == "nt" else "ptyprocess")
    readers = []
    if kind == "pty" and os.name == "nt":
        original_init = pty_backend._OwnedPtyProcess.__init__

        def capture_reader(wrapper, raw):
            original_init(wrapper, raw)
            readers.append(raw)

        monkeypatch.setattr(pty_backend._OwnedPtyProcess, "__init__", capture_reader)
    marker = tmp_path / "executed.txt"
    script = tmp_path / "command.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        + ("print('launch-ok héllo 世界', flush=True)\n" if kind == "pty"
           else "print('launch-ok', flush=True)\n"),
        encoding="utf-8",
    )
    command = (
        "& '" + sys.executable.replace("'", "''") + "' '" + str(script).replace("'", "''") + "'"
        if os.name == "nt" else shlex.join([sys.executable, str(script)])
    )
    state = tmp_path / "state"

    async def launch():
        token = current_tool_context.set(ToolContext(
            is_owner=True, caller_kind=CallerKind.CLI, session_key="slow-launch",
            task_id="slow-launch-task",
        ))
        try:
            with process_tree.task_process_scope(
                state, session_key="slow-launch", task_id="slow-launch-task",
            ):
                if kind == "pty":
                    return await shell._start_host_background_process(
                        command, cwd=str(tmp_path), effective_timeout=5,
                        runtime=None, io_mode="pty",
                    )
                return await shell._run_windows_host_shell_command_with_stdin(
                    command, cwd=str(tmp_path), env=dict(os.environ),
                    stdin_bytes=b"input", effective_timeout=5,
                )
        finally:
            current_tool_context.reset(token)

    async def finish(result):
        if kind == "pty":
            execution = shell._session_id_from_start_result(result)
            assert execution, result
            session = shell._bg_sessions[execution]
            assert session.io_mode_used == "pty"
            try:
                await asyncio.wait_for(session.collector_task, timeout=10)
                assert "launch-ok" in shell._bg_rendered_output(session)
                assert "héllo 世界" in shell._bg_rendered_output(session)
            finally:
                await shell._stop_bg_session(session)
                shell._bg_sessions.pop(execution)
        else:
            assert "exit_code=0" in result, result
            assert "launch-ok" in result

    async def assert_readers_closed():
        if kind == "pty" and os.name == "nt":
            assert len(readers) == 1, "must observe the actual native PTY process"
        for raw in readers:
            if hasattr(raw, "fileobj"):
                assert raw.fileobj.fileno() == -1, "PTY output socket survived launch cleanup"
                assert raw._server.fileno() == -1, "PTY writer survived launch cleanup"
            if hasattr(raw, "_thread"):
                await asyncio.to_thread(raw._thread.join, 1)
                assert not raw._thread.is_alive(), "PTY reader survived launch cleanup"

    return SimpleNamespace(
        kind=kind, launch=launch, finish=finish, state=state, marker=marker,
        script=script, assert_readers_closed=assert_readers_closed,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["finish", "cancel", "fail"])
async def test_slow_native_launch_preserves_loop_and_ownership(
    native_launch, monkeypatch, outcome, request,
):
    case = native_launch
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    original_insert = process_tree._insert_owner_record
    references = []
    launch_started = time.monotonic()
    phases = []
    worker_id = None

    def phase(name):
        phases.append((name, round(time.monotonic() - launch_started, 3)))

    def diagnose_pending_launch():
        # Capture before wait_for's cancellation drains the worker, when its
        # blocked frame still identifies registration, native I/O or cleanup.
        elapsed = time.monotonic() - launch_started
        lines = [f"native launch {case.kind}/{outcome}: elapsed={elapsed:.3f}s phases={phases!r}\n"]
        frames = sys._current_frames()
        for thread in threading.enumerate():
            if (
                thread.ident == worker_id or thread.name.startswith("asyncio_")
                or thread.name == "opensquilla-pty-reader"
            ):
                frame = frames.get(thread.ident)
                if frame is not None:
                    lines.append(f"thread={thread.name}\n")
                    lines.extend(traceback.format_stack(frame))
        request.node.add_report_section("call", "native launch before timeout", "".join(lines))

    def persist(*args, **kwargs):
        nonlocal worker_id
        worker_id = threading.get_ident()
        phase("registration-entered")
        assert process_tree._current_task_process_scope() is not None
        assert current_tool_context.get().task_id == "slow-launch-task"
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "event loop did not release slow registration"
        phase("registration-released")
        if outcome == "fail":
            raise process_tree.ProcessTreeOwnershipError("synthetic registration failure")
        reference = original_insert(*args, **kwargs)
        phase("registration-committed")
        references.append(reference)
        return reference

    monkeypatch.setattr(process_tree, "_insert_owner_record", persist)
    task = asyncio.create_task(case.launch())
    diagnostic = loop.call_later(9, diagnose_pending_launch)
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        # This event-loop round trip happens while registration is blocked in
        # the worker, and the command must still be behind its ownership gate.
        await asyncio.sleep(0.01)
        assert not task.done()
        assert not case.marker.exists()
        if outcome == "cancel":
            task.cancel("cancel slow launch")
            await asyncio.sleep(0)
            task.cancel("repeat cancellation")
            await asyncio.sleep(0)
            assert not task.done()
        phase("release-requested")
        release.set()
        diagnostic.cancel()
        diagnostic = loop.call_later(9, diagnose_pending_launch)
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError, match="cancel slow launch"):
                await asyncio.wait_for(task, timeout=10)
        else:
            result = await asyncio.wait_for(task, timeout=10)
            diagnostic.cancel()
            if outcome == "finish":
                await case.finish(result)
                assert case.marker.exists()
            else:
                assert "capability_error" in result if case.kind == "pty" else "[error]" in result
    finally:
        diagnostic.cancel()
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert process_tree._load_owner_records(case.state) == ()
    if outcome != "finish":
        assert not case.marker.exists()
    assert bool(references) is (outcome != "fail")
    await case.assert_readers_closed()


@pytest.mark.skipif(os.name != "nt", reason="Windows native PTY reader lifecycle")
@pytest.mark.parametrize("native_launch", ["pty"], indirect=True)
@pytest.mark.parametrize("outcome", ["fail", "cancel"])
@pytest.mark.asyncio
async def test_quiet_native_reader_is_reclaimed_after_initialization(
    native_launch, monkeypatch, outcome,
):
    case = native_launch
    case.script.write_text(
        "import time\nfrom pathlib import Path\n"
        f"Path({str(case.marker)!r}).write_text('quiet-ready')\n"
        "time.sleep(60)\n", encoding="utf-8",
    )
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    original_start = pty_backend._start_windows_pty_reader
    readers = []

    def start(raw):
        original_start(raw)
        readers.append(raw._thread)
        # Prove the real child reached its quiet wait, not just that the
        # helper's ownership gate opened. The outer watchdog releases this
        # wait on failure, so the launch still drains its native resources.
        while not case.marker.exists():
            assert not release.wait(0.01), "quiet child did not start"
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "event loop did not release reader initialization"
        if outcome == "fail":
            raise OSError("synthetic failure after native reader initialization")

    monkeypatch.setattr(pty_backend, "_start_windows_pty_reader", start)
    task = asyncio.create_task(case.launch())
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        assert process_tree._load_owner_records(case.state)
        if outcome == "cancel":
            task.cancel("cancel during reader initialization")
            await asyncio.sleep(0)
        release.set()
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError, match="cancel during reader"):
                await asyncio.wait_for(task, timeout=10)
        else:
            result = await asyncio.wait_for(task, timeout=10)
            assert "capability_error" in result
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert readers
    assert process_tree._load_owner_records(case.state) == ()
    await case.assert_readers_closed()


@pytest.mark.skipif(os.name != "nt", reason="Windows native PTY reader lifecycle")
def test_windows_reader_keeps_empty_reads_and_utf8_tail():
    winpty = pytest.importorskip("winpty")
    chunks = iter(["", "héllo 世界\n", winpty.WinptyError("Standard out reached EOF")])

    def read(_size, *, blocking):
        assert not blocking
        value = next(chunks)
        if isinstance(value, Exception):
            raise value
        return value

    raw = SimpleNamespace(pty=SimpleNamespace(read=read))
    pty_backend._start_windows_pty_reader(raw)
    try:
        raw.fileobj.settimeout(2)
        received = bytearray()
        while chunk := raw.fileobj.recv(4096):
            received.extend(chunk)
        assert received.decode("utf-8") == "héllo 世界\n"
    finally:
        PtyHandle(raw, "windows").close_reader()
    assert not raw._thread.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="Windows native PTY reader lifecycle")
def test_windows_reader_closes_when_output_socket_is_full(monkeypatch):
    pytest.importorskip("winpty")
    reader, writer = socket.socketpair()
    reader.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    monkeypatch.setattr(pty_backend.socket, "socketpair", lambda: (reader, writer))
    entered = threading.Event()

    def read(_size, *, blocking):
        assert not blocking
        entered.set()
        return "x" * (2 * 1024 * 1024)

    raw = SimpleNamespace(pty=SimpleNamespace(read=read))
    pty_backend._start_windows_pty_reader(raw)
    try:
        assert entered.wait(2)
        reader.settimeout(2)
        assert reader.recv(1) == b"x"
        # Receiving one byte proves sendall started. Leave the rest undrained:
        # close must wake a writer whose output exceeds the small buffers.
    finally:
        PtyHandle(raw, "windows").close_reader()
    assert not raw._thread.is_alive()
    assert reader.fileno() == writer.fileno() == -1


def test_runner_shutdown_waits_for_native_launch_cleanup(native_launch, monkeypatch):
    case = native_launch
    release = threading.Event()
    original_insert = process_tree._insert_owner_record
    committed = []
    timer = None

    async def main():
        nonlocal timer
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def persist(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            reference = original_insert(*args, **kwargs)
            committed.append(reference)
            return reference

        monkeypatch.setattr(process_tree, "_insert_owner_record", persist)
        asyncio.create_task(case.launch())
        await asyncio.wait_for(started.wait(), timeout=10)
        timer = threading.Timer(0.1, release.set)
        timer.start()
        # Let asyncio.run cancel the actual pending launch, not a stand-in.

    try:
        asyncio.run(main())
    finally:
        release.set()
        if timer is not None:
            timer.join()
    assert len(committed) == 1
    assert process_tree._load_owner_records(case.state) == ()
    assert not case.marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("pty", [False, True])
async def test_cancel_after_worker_release_reclaims_exact_process(monkeypatch, pty):
    loop = asyncio.get_running_loop()
    proc = SimpleNamespace(pid=54321, returncode=None)
    owner = process_tree.ProcessTreeOwner(proc, proc.pid)
    proc._opensquilla_process_tree_owner = owner
    cleaned = []

    async def stop(process, captured_owner=None):
        assert process is proc
        assert captured_owner is owner
        cleaned.append(process)

    async def stop_pty(handle):
        assert handle.raw is proc
        cleaned.append(handle.raw)

    def launch(*, cancel_event):
        assert not cancel_event.is_set()
        loop.call_soon_threadsafe(task.cancel, "cancel at handoff")
        return PtyHandle(proc, "windows") if pty else proc

    monkeypatch.setattr(shell, "_terminate_exec_process_tree", stop)
    monkeypatch.setattr(shell, "terminate_pty", stop_pty)
    task = asyncio.create_task(shell._start_blocking_host_process(launch))
    with pytest.raises(asyncio.CancelledError, match="cancel at handoff"):
        await task
    assert cleaned == [proc]


@pytest.mark.asyncio
async def test_cancelled_partial_pty_initialization_still_closes_handle(monkeypatch):
    loop = asyncio.get_running_loop()
    proc = SimpleNamespace(pid=54322, returncode=None)
    handle = PtyHandle(proc, "windows")
    terminate = AsyncMock()

    def launch(*, cancel_event):
        loop.call_soon_threadsafe(task.cancel)
        raise PtyBackendError("resize failed after spawn", started=True, handle=handle)

    monkeypatch.setattr(shell, "terminate_pty", terminate)
    task = asyncio.create_task(shell._start_blocking_host_process(launch))
    with pytest.raises(asyncio.CancelledError):
        await task
    terminate.assert_awaited_once_with(handle)


@pytest.mark.asyncio
async def test_launch_cancellation_waits_for_owner_row_deletion(monkeypatch):
    loop = asyncio.get_running_loop()
    deleting = asyncio.Event()
    release = threading.Event()
    deleted = []
    reference = object()
    proc = SimpleNamespace(pid=54324, returncode=0)
    owner = process_tree.ProcessTreeOwner(proc, proc.pid, persisted_owner=reference)
    proc._opensquilla_process_tree_owner = owner

    def delete(row):
        assert row is reference
        loop.call_soon_threadsafe(deleting.set)
        assert release.wait(5)
        deleted.append(row)

    def launch(*, cancel_event):
        loop.call_soon_threadsafe(task.cancel, "cancel at handoff")
        return proc

    monkeypatch.setattr(process_tree, "_delete_owner_record", delete)
    task = asyncio.create_task(shell._start_blocking_host_process(launch))
    try:
        await asyncio.wait_for(deleting.wait(), timeout=3)
        task.cancel("cancel during deletion")
        await asyncio.sleep(0)
        task.cancel("repeat cancellation during deletion")
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="cancel at handoff"):
            await task
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert deleted == [reference]


def test_cancelled_windows_sync_launch_closes_job_before_failing_row_delete(monkeypatch):
    events = []
    reference = object()
    cancellation = threading.Event()
    cancellation.set()
    gate = SimpleNamespace(
        gate_name="test-gate", ready_name="test-ready", wait_ready=lambda _timeout: None,
        release=lambda: events.append("released"), close=lambda: events.append("gate-closed"),
    )
    job = SimpleNamespace(
        assign_pid=lambda _pid: None, close=lambda: events.append("job-closed"),
    )
    proc = SimpleNamespace(
        pid=54325, poll=lambda: None, terminate=lambda: events.append("terminated"),
        wait=lambda **_kwargs: 0,
    )

    def delete(row):
        assert row is reference
        events.append("delete-failed")
        raise OSError("synthetic database failure")

    monkeypatch.setattr(
        process_tree, "os", SimpleNamespace(**{**vars(process_tree.os), "name": "nt"}),
    )
    monkeypatch.setattr(process_tree._WindowsLaunchGate, "create", lambda: gate)
    monkeypatch.setattr(process_tree._WindowsJob, "create", lambda *_args: job)
    monkeypatch.setattr(process_tree, "_current_task_process_scope", lambda: object())
    monkeypatch.setattr(process_tree, "_insert_owner_record", lambda *_a, **_kw: reference)
    monkeypatch.setattr(process_tree, "_delete_owner_record", delete)
    monkeypatch.setattr(process_tree.subprocess, "Popen", lambda *_a, **_kw: proc)
    with pytest.raises(OSError, match="synthetic database failure"):
        process_tree.create_owned_popen(["synthetic.exe"], cancel_event=cancellation)
    assert events == ["terminated", "job-closed", "delete-failed", "gate-closed"]


@pytest.mark.asyncio
async def test_posix_pty_monitor_is_installed_once_on_owner_loop():
    read_entered = asyncio.Event()
    hold = asyncio.Event()

    async def read(_size):
        read_entered.set()
        await hold.wait()
        return b""

    proc = SimpleNamespace(pid=54323, stdout=SimpleNamespace(read=read))
    anchor = process_tree._PosixGroupAnchor(process=proc, pgid=proc.pid)
    owner = process_tree.ProcessTreeOwner(proc, proc.pid, posix_anchor=anchor, pgid=proc.pid)
    anchor.bind(owner)
    await asyncio.to_thread(owner.start_completion_monitor)
    assert anchor._monitor_task is None
    owner.start_completion_monitor()
    monitor = anchor._monitor_task
    owner.start_completion_monitor()
    assert anchor._monitor_task is monitor
    assert monitor.get_loop() is asyncio.get_running_loop()
    await asyncio.wait_for(read_entered.wait(), timeout=1)
    monitor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await monitor


@pytest.mark.asyncio
async def test_cancelled_posix_retirement_releases_and_reaps_empty_anchor(monkeypatch):
    loop = asyncio.get_running_loop()
    deleting = asyncio.Event()
    release = threading.Event()
    writes = []
    closed = []
    waited = []
    reference = object()
    control = SimpleNamespace(
        write=writes.append, close=lambda: closed.append(True), is_closing=lambda: bool(closed),
    )

    async def read(_size):
        return process_tree._POSIX_ANCHOR_EMPTY

    async def wait():
        assert closed
        waited.append(True)
        return 0

    def delete(row):
        assert row is reference
        loop.call_soon_threadsafe(deleting.set)
        assert release.wait(5)

    proc = SimpleNamespace(pid=54326, stdin=control, stdout=SimpleNamespace(read=read), wait=wait)
    anchor = process_tree._PosixGroupAnchor(process=proc, pgid=proc.pid)
    owner = process_tree.ProcessTreeOwner(
        proc, proc.pid, pgid=proc.pid, posix_anchor=anchor, persisted_owner=reference,
    )
    anchor.bind(owner)
    monkeypatch.setattr(process_tree, "_delete_owner_record", delete)
    owner.start_completion_monitor()
    monitor = anchor._monitor_task
    try:
        await asyncio.wait_for(deleting.wait(), timeout=3)
        monitor.cancel("cancel retirement")
        await asyncio.sleep(0)
        monitor.cancel("repeat cancellation")
        await asyncio.sleep(0)
        assert not monitor.done()
        assert not writes
        release.set()
        with pytest.raises(asyncio.CancelledError, match="cancel retirement"):
            await monitor
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor
    assert writes == [process_tree._POSIX_ANCHOR_RELEASE]
    assert waited == [True]
    await owner._close_empty_posix_owner()
    assert writes == [process_tree._POSIX_ANCHOR_RELEASE]
