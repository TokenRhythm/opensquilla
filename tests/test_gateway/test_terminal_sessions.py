from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway import terminal_sessions
from opensquilla.process_tree import (
    _POSIX_ANCHOR_EMPTY,
    _POSIX_ANCHOR_RELEASE,
    ProcessTreeOwner,
    _PosixGroupAnchor,
)
from opensquilla.sandbox.permissions import FileSystemPermissionProfile


class _FakeHandle:
    def __init__(self) -> None:
        self.raw = SimpleNamespace(pid=0)
        self.terminated = False
        self.writes: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []


def _completion_handle():
    """Use the real owner protocol while replacing only the native process."""
    handle = _FakeHandle()
    released = asyncio.Event()
    status = asyncio.StreamReader()
    status.feed_data(_POSIX_ANCHOR_EMPTY)

    def write(command):
        if command == _POSIX_ANCHOR_RELEASE:
            released.set()

    control = SimpleNamespace(
        stdin=SimpleNamespace(write=write, close=lambda: None, is_closing=lambda: False),
        stdout=status,
        wait=_done_wait,
    )
    anchor = _PosixGroupAnchor(process=control, pgid=0)
    owner = ProcessTreeOwner(process=handle.raw, pid=0, pgid=0, posix_anchor=anchor)
    anchor.bind(owner)
    handle.raw._opensquilla_process_tree_owner = owner
    return handle, owner, anchor, released


class _WorkspaceService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.binding = "binding-1"
        self.workspace_value = SimpleNamespace(
            root=root,
            binding=self.binding,
            profile=FileSystemPermissionProfile.workspace(
                workspace=root,
                denied_read_roots=(),
                denied_read_globs=(),
                host_root_readonly=False,
                tmp_writable=False,
                tmpdir_env_writable=False,
            ),
        )

    async def workspace(self, _session_key: str):
        self.workspace_value.binding = self.binding
        return self.workspace_value


@pytest.mark.asyncio
async def test_terminal_manager_emits_bounded_ordered_output_and_exit(tmp_path, monkeypatch):
    handle = _FakeHandle()
    events: list[tuple[str, dict]] = []
    chunks = iter([b"hello\n", b"world\n"])

    async def read(_handle, _size):
        try:
            return next(chunks)
        except StopIteration as exc:
            raise EOFError from exc

    async def wait(_handle):
        return 7

    async def send(event: str, payload: dict):
        events.append((event, payload))
        return True

    monkeypatch.setattr(terminal_sessions, "spawn_pty", lambda *args, **kwargs: handle)
    monkeypatch.setattr(terminal_sessions, "read_pty", read)
    monkeypatch.setattr(terminal_sessions, "wait_pty", wait)
    service = _WorkspaceService(tmp_path)
    manager = terminal_sessions.TerminalManager(send_event=send)
    created = await manager.create(
        conn_id="conn-1",
        session_key="agent:main:webchat:terminal",
        workspace_service=service,
        cwd="",
        command="echo ready",
        cols=100,
        rows=24,
    )
    terminal_id = created["terminalId"]
    await asyncio.wait_for(manager._sessions[terminal_id].reader_task, timeout=1)

    assert [event for event, _ in events] == [
        "terminal.output",
        "terminal.output",
        "terminal.exit",
    ]
    assert [payload["seq"] for _, payload in events] == [1, 2, 3]
    assert events[-1][1]["exitCode"] == 7
    assert terminal_id not in manager._sessions
    assert await manager.close(terminal_id=terminal_id, conn_id="conn-1") == {
        "terminalId": terminal_id,
        "closed": True,
    }
    assert await manager.close(terminal_id=terminal_id, conn_id="conn-1") == {
        "terminalId": terminal_id,
        "closed": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, terminal_sessions.MAX_OUTPUT_BYTES])
async def test_terminal_output_waits_for_consumption_with_bounded_window_and_close(
    tmp_path, monkeypatch, chunk_size,
):
    handle = _FakeHandle()
    events = []
    reads = 0
    reached_window = asyncio.Event()
    expected_frames = min(
        terminal_sessions.MAX_OUTPUT_IN_FLIGHT_FRAMES,
        terminal_sessions.OUTPUT_HIGH_WATER_BYTES // chunk_size,
    )

    async def read(_handle, size):
        nonlocal reads
        reads += 1
        return b"x" * min(size, chunk_size)

    async def send(event, payload):
        events.append((event, payload))
        if event == "terminal.output" and payload["seq"] >= expected_frames:
            reached_window.set()
        return True

    async def terminate(_handle):
        handle.terminated = True

    monkeypatch.setattr(terminal_sessions, "spawn_pty", lambda *args, **kwargs: handle)
    monkeypatch.setattr(terminal_sessions, "read_pty", read)
    monkeypatch.setattr(terminal_sessions, "wait_pty", lambda *_args: _done_wait())
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)
    manager = terminal_sessions.TerminalManager(send_event=send)
    created = await manager.create(
        conn_id="conn-1", session_key="session-a", workspace_service=_WorkspaceService(tmp_path),
        cwd="", command=None, cols=80, rows=24, output_flow_control=True,
    )
    assert created["outputFlowControl"] == "consumed-v1"
    terminal_id = created["terminalId"]
    session = manager._sessions[terminal_id]
    await asyncio.wait_for(reached_window.wait(), timeout=1)
    await asyncio.sleep(0)
    assert reads == expected_frames
    assert len(session.output_in_flight) == expected_frames
    assert session.output_in_flight_bytes == expected_frames * chunk_size
    assert not handle.terminated

    with pytest.raises(terminal_sessions.TerminalSessionError, match="another connection"):
        await manager.consume(terminal_id=terminal_id, conn_id="conn-2", seq=1)
    with pytest.raises(terminal_sessions.TerminalSessionError, match="not emitted"):
        await manager.consume(terminal_id=terminal_id, conn_id="conn-1", seq=expected_frames + 1)
    # Partial credits do not resume until the low watermark is reached.
    await manager.consume(terminal_id=terminal_id, conn_id="conn-1", seq=1)
    await asyncio.sleep(0)
    assert reads == expected_frames
    received = await manager.consume(terminal_id=terminal_id, conn_id="conn-1", seq=expected_frames)
    assert received["consumedSeq"] == expected_frames
    await asyncio.sleep(0)
    assert reads == 2 * expected_frames
    assert len(session.output_in_flight) == expected_frames
    assert session.output_in_flight_bytes <= terminal_sessions.OUTPUT_HIGH_WATER_BYTES
    # A stale retry cannot release new credits.
    await manager.consume(terminal_id=terminal_id, conn_id="conn-1", seq=expected_frames)
    await asyncio.sleep(0)
    assert reads == 2 * expected_frames
    # Closing a socket wakes the paused pump and keeps the existing teardown.
    await asyncio.wait_for(manager.close_connection("conn-1"), timeout=1)
    assert handle.terminated
    assert terminal_id not in manager._sessions
    assert events[-1][0] == "terminal.exit"
    assert events[-1][1]["reason"] == "connection_closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("seq", [True, 0, -1, 1.5, None, 2**53])
async def test_terminal_consumption_rejects_invalid_cursors(seq):
    async def send(*_args):
        return True

    manager = terminal_sessions.TerminalManager(send_event=send)
    with pytest.raises(terminal_sessions.TerminalSessionError, match="positive safe integer"):
        await manager.consume(terminal_id="missing", conn_id="conn-1", seq=seq)


@pytest.mark.asyncio
async def test_terminal_manager_enforces_owner_and_workspace_binding(tmp_path, monkeypatch):
    handle = _FakeHandle()
    service = _WorkspaceService(tmp_path)
    stop = asyncio.Event()

    async def read_until_stop(*_args):
        await stop.wait()
        raise EOFError

    async def terminate(_handle):
        stop.set()

    monkeypatch.setattr(terminal_sessions, "spawn_pty", lambda *args, **kwargs: handle)
    monkeypatch.setattr(terminal_sessions, "read_pty", read_until_stop)
    monkeypatch.setattr(terminal_sessions, "wait_pty", lambda *_args: _done_wait())
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)

    async def send(_event: str, _payload: dict):
        return True

    manager = terminal_sessions.TerminalManager(send_event=send)
    created = await manager.create(
        conn_id="conn-1",
        session_key="agent:main:webchat:terminal",
        workspace_service=service,
        cwd="",
        command=None,
        cols=80,
        rows=20,
    )
    terminal_id = created["terminalId"]
    with pytest.raises(terminal_sessions.TerminalSessionError, match="another connection"):
        await manager.write(terminal_id=terminal_id, conn_id="conn-2", data="pwd\n")

    service.binding = "binding-2"
    with pytest.raises(terminal_sessions.TerminalSessionError, match="workspace changed"):
        await manager.write(terminal_id=terminal_id, conn_id="conn-1", data="pwd\n")
    await manager.close_connection("conn-1")


@pytest.mark.asyncio
async def test_terminal_manager_preserves_split_utf8_and_sequences_output(tmp_path, monkeypatch):
    handle = _FakeHandle()
    service = _WorkspaceService(tmp_path)
    chunks = iter([b"\xe4", b"\xbd\xa0\r\n"])
    events: list[tuple[str, dict]] = []

    async def read(_handle, _size):
        try:
            return next(chunks)
        except StopIteration as exc:
            raise EOFError from exc

    async def send(event: str, payload: dict):
        events.append((event, payload))
        return True

    async def wait(_handle):
        return 0

    monkeypatch.setattr(terminal_sessions, "spawn_pty", lambda *args, **kwargs: handle)
    monkeypatch.setattr(terminal_sessions, "read_pty", read)
    monkeypatch.setattr(terminal_sessions, "wait_pty", wait)
    manager = terminal_sessions.TerminalManager(send_event=send)
    created = await manager.create(
        conn_id="conn-1", session_key="agent:main:webchat:utf8",
        workspace_service=service, cwd="", command=None, cols=80, rows=24,
    )
    task = manager._sessions[created["terminalId"]].reader_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1)
    output = [payload["data"] for event, payload in events if event == "terminal.output"]
    assert output == ["你\r\n"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_after_start", [False, True])
async def test_terminal_create_cancellation_reaps_eventual_native_handle(
    tmp_path, monkeypatch, fail_after_start,
):
    handle, owner, anchor, released = _completion_handle()
    started = threading.Event()
    terminated = asyncio.Event()

    def spawn(*_args, **_kwargs):
        owner.start_completion_monitor()
        assert anchor._monitor_task is None  # No running loop in the spawn worker.
        started.set()
        # This function runs in the executor thread; the async event cannot
        # be awaited there, so use a thread event to model a slow native spawn.
        release_thread.wait()
        if fail_after_start:
            raise terminal_sessions.PtyBackendError(
                "reader setup failed", started=True, handle=handle,
            )
        return handle

    release_thread = threading.Event()

    async def terminate(_handle):
        await released.wait()
        terminated.set()

    async def send(_event: str, _payload: dict):
        return True

    monkeypatch.setattr(terminal_sessions, "spawn_pty", spawn)
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)
    manager = terminal_sessions.TerminalManager(send_event=send)
    service = _WorkspaceService(tmp_path)
    create_task = asyncio.create_task(
        manager.create(
            conn_id="conn-cancel",
            session_key="agent:main:webchat:cancel",
            workspace_service=service,
            cwd="",
            command="shell",
            cols=80,
            rows=20,
        )
    )
    await asyncio.sleep(0)
    # The executor call may need one loop turn before its thread reaches the
    # synchronization point; wait without touching the native handle.
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.01)
        create_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await create_task
    finally:
        release_thread.set()
    await asyncio.wait_for(terminated.wait(), timeout=2)
    assert anchor._monitor_task is not None
    await anchor._monitor_task
    assert manager._sessions == {}
    assert manager._creating == {}


@pytest.mark.asyncio
async def test_terminal_manager_terminates_when_output_backpressure_rejects(tmp_path, monkeypatch):
    handle = _FakeHandle()
    service = _WorkspaceService(tmp_path)
    events: list[str] = []

    async def read(_handle, _size):
        if events:
            raise EOFError
        return b"prompt\r\n"

    async def send(event: str, _payload: dict):
        events.append(event)
        return event != "terminal.output"

    async def terminate(value):
        value.terminated = True

    monkeypatch.setattr(terminal_sessions, "spawn_pty", lambda *args, **kwargs: handle)
    monkeypatch.setattr(terminal_sessions, "read_pty", read)
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)
    monkeypatch.setattr(terminal_sessions, "wait_pty", lambda *_args: 143)
    manager = terminal_sessions.TerminalManager(send_event=send)
    created = await manager.create(
        conn_id="conn-1", session_key="agent:main:webchat:backpressure",
        workspace_service=service, cwd="", command=None, cols=80, rows=24,
    )
    task = manager._sessions[created["terminalId"]].reader_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1)
    assert handle.terminated is True
    assert events == ["terminal.output", "terminal.exit"]


async def _done_wait():
    return 0


def test_terminal_dimensions_and_input_limits():
    assert terminal_sessions._dimension(None, 120, 400, "cols") == 120
    with pytest.raises(terminal_sessions.TerminalSessionError):
        terminal_sessions._dimension(401, 120, 400, "cols")
    with pytest.raises(terminal_sessions.TerminalSessionError):
        terminal_sessions._command("x" * (terminal_sessions.MAX_COMMAND_CHARS + 1))


def test_terminal_connection_cleanup_is_registered_once(monkeypatch):
    manager = terminal_sessions.TerminalManager(send_event=lambda *_args: None)
    scheduled: list[str] = []
    monkeypatch.setattr(
        manager,
        "schedule_close_connection",
        lambda conn_id: scheduled.append(conn_id),
    )
    assert manager.claim_connection_cleanup("conn-1") is True
    assert manager.claim_connection_cleanup("conn-1") is False
    manager.on_connection_cleanup("conn-1")
    assert scheduled == ["conn-1"]
    assert manager.claim_connection_cleanup("conn-1") is True


@pytest.mark.asyncio
async def test_terminal_create_counts_inflight_connection_slots(tmp_path):
    manager = terminal_sessions.TerminalManager(send_event=lambda *_args: None)
    manager._creating["conn-1"] = terminal_sessions.MAX_TERMINALS_PER_CONNECTION
    service = _WorkspaceService(tmp_path)
    with pytest.raises(terminal_sessions.TerminalSessionError, match="Too many"):
        await manager.create(
            conn_id="conn-1",
            session_key="agent:main:webchat:limit",
            workspace_service=service,
            cwd="",
            command=None,
            cols=80,
            rows=24,
        )


@pytest.mark.asyncio
async def test_terminal_manager_reaps_backend_handle_when_spawn_fails_after_start(
    tmp_path, monkeypatch
):
    handle, owner, anchor, released = _completion_handle()
    service = _WorkspaceService(tmp_path)
    terminated: list[object] = []

    def spawn(*_args, **_kwargs):
        owner.start_completion_monitor()
        assert anchor._monitor_task is None
        raise terminal_sessions.PtyBackendError(
            "reader setup failed", started=True, handle=handle,
        )

    async def terminate(value):
        await asyncio.wait_for(released.wait(), timeout=1)
        terminated.append(value)

    monkeypatch.setattr(terminal_sessions, "spawn_pty", spawn)
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)
    manager = terminal_sessions.TerminalManager(send_event=lambda *_args: None)
    with pytest.raises(terminal_sessions.TerminalSessionError, match="reader setup failed"):
        await manager.create(
            conn_id="conn-1", session_key="agent:main:webchat:failed",
            workspace_service=service, cwd="", command=None, cols=80, rows=24,
        )
    assert terminated == [handle]
    assert anchor._monitor_task is not None
    await anchor._monitor_task


@pytest.mark.asyncio
async def test_terminal_natural_exit_releases_anchor_and_connection_slot(tmp_path, monkeypatch):
    handles = [_completion_handle(), _completion_handle()]
    pending = iter(handles)
    events: list[tuple[str, dict]] = []

    def spawn(*_args, **_kwargs):
        handle, owner, anchor, _released = next(pending)
        owner.start_completion_monitor()
        assert anchor._monitor_task is None
        return handle

    async def read(handle, _size):
        released = next(item[3] for item in handles if item[0] is handle)
        await released.wait()
        raise EOFError

    async def send(event, payload):
        events.append((event, payload))
        return True

    monkeypatch.setattr(terminal_sessions, "MAX_TERMINALS_PER_CONNECTION", 1)
    monkeypatch.setattr(terminal_sessions, "spawn_pty", spawn)
    monkeypatch.setattr(terminal_sessions, "read_pty", read)
    monkeypatch.setattr(terminal_sessions, "wait_pty", lambda *_args: _done_wait())
    manager = terminal_sessions.TerminalManager(send_event=send)
    for _handle, owner, anchor, released in handles:
        created = await manager.create(
            conn_id="conn-1", session_key="agent:main:webchat:exit",
            workspace_service=_WorkspaceService(tmp_path), cwd="", command="exit 0",
            cols=80, rows=24,
        )
        task = manager._sessions[created["terminalId"]].reader_task
        await asyncio.wait_for(task, timeout=1)
        assert released.is_set()
        assert not owner.is_active()
        assert anchor._monitor_task is not None
        assert anchor._monitor_task.get_loop() is asyncio.get_running_loop()
        await anchor._monitor_task
        assert manager._sessions == {}
        assert manager._creating == {}
    assert [event for event, _payload in events] == ["terminal.exit", "terminal.exit"]
    assert all(payload["reason"] == "process_exit" for _event, payload in events)


@pytest.mark.asyncio
async def test_terminal_failed_spawn_cleanup_cancellation_releases_create_slot(
    tmp_path, monkeypatch,
):
    handle = _FakeHandle()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    terminated = asyncio.Event()

    def spawn(*_args, **_kwargs):
        raise terminal_sessions.PtyBackendError(
            "reader setup failed", started=True, handle=handle,
        )

    async def terminate(_handle):
        cleanup_started.set()
        await release_cleanup.wait()
        terminated.set()

    monkeypatch.setattr(terminal_sessions, "spawn_pty", spawn)
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)
    manager = terminal_sessions.TerminalManager(send_event=lambda *_args: None)
    task = asyncio.create_task(manager.create(
        conn_id="conn-failed", session_key="agent:main:webchat:failed-cancel",
        workspace_service=_WorkspaceService(tmp_path), cwd="", command=None, cols=80, rows=24,
    ))
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager._creating == {}
        assert manager._sessions == {}
    finally:
        release_cleanup.set()
        await asyncio.wait_for(terminated.wait(), timeout=1)
        await asyncio.gather(*manager._cleanup_tasks)


# Native console exit must not compete with unrelated parallel worker I/O.
@pytest.mark.ci_serial
@pytest.mark.platform_pty
@pytest.mark.asyncio
async def test_real_terminal_natural_exit_releases_connection_slot(tmp_path):
    pytest.importorskip("winpty" if os.name == "nt" else "ptyprocess")
    events: list[tuple[str, dict]] = []

    async def send(event, payload):
        events.append((event, payload))
        return True

    manager = terminal_sessions.TerminalManager(send_event=send)
    try:
        created = await manager.create(
            conn_id="conn-native", session_key="agent:main:webchat:native-exit",
            workspace_service=_WorkspaceService(tmp_path), cwd="",
            command=(
                "Write-Output 'terminal-completed'; exit 7" if os.name == "nt"
                else "printf 'terminal-completed\\n'; exit 7"
            ),
            cols=80, rows=24,
        )
        session = manager._sessions[created["terminalId"]]
        reader_task = session.reader_task
        assert reader_task is not None
        try:
            await asyncio.wait_for(asyncio.shield(reader_task), timeout=8)
        except TimeoutError as exc:
            event_names = [event for event, _payload in events[-8:]]
            completed_output = "terminal-completed" in "".join(
                payload["data"] for event, payload in events if event == "terminal.output"
            )
            reader_stack = [
                f"{frame.f_code.co_name}:{frame.f_lineno}"
                for frame in reader_task.get_stack(limit=3)
            ]
            exc.add_note(
                f"events={event_names!r}; completed_output={completed_output}; "
                f"reader_done={reader_task.done()}; reader_stack={reader_stack!r}"
            )
            raise
        output = "".join(payload["data"] for event, payload in events if event == "terminal.output")
        assert "terminal-completed" in output
        assert events[-1][0] == "terminal.exit"
        assert events[-1][1]["exitCode"] == 7
        assert events[-1][1]["reason"] == "process_exit"
        assert manager._sessions == {}
        assert manager._creating == {}
    finally:
        # Release a real anchor even when this regression fails before the
        # manager starts its monitor, so the native reader cannot leak a worker.
        for session in tuple(manager._sessions.values()):
            session.handle.raw._opensquilla_process_tree_owner.start_completion_monitor()
        await manager.close_all()


@pytest.mark.asyncio
async def test_terminal_cancel_during_partial_spawn_cleanup_releases_create_slot(
    tmp_path, monkeypatch
):
    handle = _FakeHandle()
    cleanup_started = asyncio.Event()
    cleanup_completed = asyncio.Event()
    cleanup_calls = 0

    def spawn(*_args, **_kwargs):
        raise terminal_sessions.PtyBackendError(
            "reader setup failed", started=True, handle=handle,
        )

    async def terminate(value):
        nonlocal cleanup_calls
        assert value is handle
        cleanup_calls += 1
        if cleanup_calls == 1:
            cleanup_started.set()
            await asyncio.Event().wait()
        handle.terminated = True
        cleanup_completed.set()

    monkeypatch.setattr(terminal_sessions, "spawn_pty", spawn)
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)
    manager = terminal_sessions.TerminalManager(send_event=lambda *_args: None)
    task = asyncio.create_task(manager.create(
        conn_id="conn-cancel-cleanup", session_key="agent:main:webchat:failed",
        workspace_service=_WorkspaceService(tmp_path), cwd="", command=None,
        cols=80, rows=24,
    ))
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cleanup_completed.wait(), timeout=1)
    assert handle.terminated is True
    assert cleanup_calls == 2
    assert manager._sessions == {}
    assert manager._creating == {}


@pytest.mark.asyncio
async def test_terminal_create_finishing_after_gateway_shutdown_is_rejected(
    tmp_path, monkeypatch
):
    handle = _FakeHandle()
    spawn_started = asyncio.Event()
    release_spawn = threading.Event()
    loop = asyncio.get_running_loop()

    def delayed_spawn(*args, **kwargs):
        loop.call_soon_threadsafe(spawn_started.set)
        assert release_spawn.wait(10)
        return handle

    async def terminate(value):
        assert value is handle
        handle.terminated = True

    monkeypatch.setattr(terminal_sessions, "spawn_pty", delayed_spawn)
    monkeypatch.setattr(
        terminal_sessions,
        "capture_process_tree_owner",
        lambda *_args, **_kwargs: SimpleNamespace(start_completion_monitor=lambda: None),
    )
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)
    manager = terminal_sessions.TerminalManager(send_event=lambda *_args: None)
    creating = asyncio.create_task(manager.create(
        conn_id="conn-shutdown-race", session_key="agent:main:webchat:race",
        workspace_service=_WorkspaceService(tmp_path), cwd="", command="sleep 30",
        cols=80, rows=24,
    ))
    try:
        await asyncio.wait_for(spawn_started.wait(), timeout=2)
        await manager.close_all()
        release_spawn.set()
        with pytest.raises(terminal_sessions.TerminalSessionError, match="shutting down"):
            await creating
    finally:
        release_spawn.set()
        await asyncio.gather(creating, return_exceptions=True)
        await asyncio.gather(*tuple(manager._cleanup_tasks))
    assert handle.terminated is True
    assert manager._sessions == {}
    assert manager._creating == {}


@pytest.mark.asyncio
async def test_terminal_create_finishing_after_connection_close_is_rejected(
    tmp_path, monkeypatch
):
    handle = _FakeHandle()
    spawn_started = asyncio.Event()
    release_spawn = threading.Event()
    loop = asyncio.get_running_loop()

    def delayed_spawn(*args, **kwargs):
        loop.call_soon_threadsafe(spawn_started.set)
        assert release_spawn.wait(10)
        return handle

    async def terminate(value):
        assert value is handle
        handle.terminated = True

    monkeypatch.setattr(terminal_sessions, "spawn_pty", delayed_spawn)
    monkeypatch.setattr(
        terminal_sessions,
        "capture_process_tree_owner",
        lambda *_args, **_kwargs: SimpleNamespace(start_completion_monitor=lambda: None),
    )
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)
    manager = terminal_sessions.TerminalManager(send_event=lambda *_args: None)
    creating = asyncio.create_task(manager.create(
        conn_id="conn-close-race", session_key="agent:main:webchat:race",
        workspace_service=_WorkspaceService(tmp_path), cwd="", command="sleep 30",
        cols=80, rows=24,
    ))
    try:
        await asyncio.wait_for(spawn_started.wait(), timeout=2)
        manager.on_connection_cleanup("conn-close-race")
        release_spawn.set()
        with pytest.raises(terminal_sessions.TerminalSessionError, match="shutting down"):
            await creating
    finally:
        release_spawn.set()
        await asyncio.gather(creating, return_exceptions=True)
        await asyncio.gather(*tuple(manager._cleanup_tasks))
    assert handle.terminated is True
    assert manager._sessions == {}
    assert manager._creating == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_creates", [1, 2])
async def test_terminal_closed_connection_fence_survives_history_eviction(
    tmp_path, monkeypatch, pending_creates
):
    handles = [_FakeHandle() for _ in range(pending_creates)]
    spawn_started = [asyncio.Event() for _ in handles]
    release_spawn = [threading.Event() for _ in handles]
    reader_stopped = asyncio.Event()
    loop = asyncio.get_running_loop()

    def delayed_spawn(command, **kwargs):
        assert command != "must-not-spawn"
        index = int(command)
        loop.call_soon_threadsafe(spawn_started[index].set)
        assert release_spawn[index].wait(10)
        return handles[index]

    async def terminate(handle):
        handle.terminated = True
        reader_stopped.set()

    async def read(*_args):
        await reader_stopped.wait()
        raise EOFError

    async def wait(*_args):
        return 0

    async def send(*_args):
        return True

    monkeypatch.setattr(terminal_sessions, "spawn_pty", delayed_spawn)
    monkeypatch.setattr(
        terminal_sessions,
        "capture_process_tree_owner",
        lambda *_args, **_kwargs: SimpleNamespace(start_completion_monitor=lambda: None),
    )
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)
    monkeypatch.setattr(terminal_sessions, "read_pty", read)
    monkeypatch.setattr(terminal_sessions, "wait_pty", wait)
    manager = terminal_sessions.TerminalManager(send_event=send)
    conn_id = "conn-pending-close"
    creating = [
        asyncio.create_task(manager.create(
            conn_id=conn_id, session_key="agent:main:webchat:race",
            workspace_service=_WorkspaceService(tmp_path), cwd="", command=str(index),
            cols=80, rows=24,
        ))
        for index in range(pending_creates)
    ]
    try:
        await asyncio.wait_for(
            asyncio.gather(*(started.wait() for started in spawn_started)), timeout=2,
        )
        await manager.close_connection(conn_id)
        for index in range(terminal_sessions.MAX_CLOSED_TERMINALS):
            await manager.close_connection(f"conn-history-{index}")
        assert conn_id not in manager._closing_connections
        with pytest.raises(terminal_sessions.TerminalSessionError, match="shutting down"):
            await asyncio.wait_for(manager.create(
                conn_id=conn_id, session_key="agent:main:webchat:race",
                workspace_service=_WorkspaceService(tmp_path), cwd="", command="must-not-spawn",
                cols=80, rows=24,
            ), timeout=2)
        for index, task in enumerate(creating):
            release_spawn[index].set()
            with pytest.raises(terminal_sessions.TerminalSessionError, match="shutting down"):
                await task
            remaining = pending_creates - index - 1
            assert manager._creating.get(conn_id, 0) == remaining
            assert manager._closed_creating == ({conn_id} if remaining else set())
        await asyncio.gather(*tuple(manager._cleanup_tasks))
        assert all(handle.terminated for handle in handles)
        assert manager._sessions == {}
    finally:
        for release in release_spawn:
            release.set()
        await asyncio.gather(*creating, return_exceptions=True)
        await asyncio.gather(*tuple(manager._cleanup_tasks))
        await manager.close_all()



@pytest.mark.asyncio
async def test_terminal_reader_failure_terminates_live_shell_before_wait(
    tmp_path, monkeypatch
):
    handle = _FakeHandle()
    wait_started = asyncio.Event()
    release_wait = asyncio.Event()
    events = []

    async def read(*_args):
        raise terminal_sessions.PtyBackendError("PTY reader disconnected", started=True)

    async def terminate(value):
        assert value is handle
        handle.terminated = True

    async def wait(*_args):
        wait_started.set()
        await release_wait.wait()
        return 143

    async def send(event, payload):
        events.append((event, payload))
        return True

    monkeypatch.setattr(terminal_sessions, "spawn_pty", lambda *args, **kwargs: handle)
    monkeypatch.setattr(terminal_sessions, "read_pty", read)
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)
    monkeypatch.setattr(terminal_sessions, "wait_pty", wait)
    manager = terminal_sessions.TerminalManager(send_event=send)
    created = await manager.create(
        conn_id="conn-reader-error", session_key="agent:main:webchat:reader",
        workspace_service=_WorkspaceService(tmp_path), cwd="", command=None,
        cols=80, rows=24,
    )
    task = manager._sessions[created["terminalId"]].reader_task
    try:
        await asyncio.wait_for(wait_started.wait(), timeout=1)
        # A broken output stream does not imply the shell has exited. Waiting
        # on that live process before terminating it strands the PTY forever.
        assert handle.terminated is True
    finally:
        release_wait.set()
        await asyncio.wait_for(task, timeout=1)
    assert manager._sessions == {}
    assert [kind for kind, _ in events] == ["terminal.exit"]
    assert events[0][1]["reason"] == "reader_error"
