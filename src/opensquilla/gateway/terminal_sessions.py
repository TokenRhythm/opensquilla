"""Connection-owned interactive PTY sessions for the Web Workbench.

The Gateway deliberately keeps the native PTY behind the existing
``opensquilla.tools.pty_backend`` adapter.  This module owns only the public
session lifecycle: a terminal is bound to one WebSocket connection, one
explicit directory target and the workspace binding captured when it was created. Output
is sent as bounded, sequenced events; there is no unbounded replay buffer.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from opensquilla.process_tree import capture_process_tree_owner
from opensquilla.tools.pty_backend import (
    PtyBackendError,
    PtyHandle,
    read_pty,
    resize_pty,
    spawn_pty,
    terminate_pty,
    wait_pty,
    write_pty,
)

MAX_TERMINALS_PER_CONNECTION = 4
MAX_COMMAND_CHARS = 4096
MAX_WRITE_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
OUTPUT_HIGH_WATER_BYTES = 128 * 1024
OUTPUT_LOW_WATER_BYTES = 32 * 1024
MAX_OUTPUT_IN_FLIGHT_FRAMES = 256
MAX_COLS = 400
MAX_ROWS = 200
MAX_CLOSED_TERMINALS = 256
WORKSPACE_RECHECK_SECONDS = 1.0
DEFAULT_COLS = 120
DEFAULT_ROWS = 30


class TerminalSessionError(RuntimeError):
    """A terminal operation could not be completed."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


SendEvent = Callable[[str, dict[str, Any]], Awaitable[bool]]


@dataclass
class TerminalSession:
    terminal_id: str
    conn_id: str
    session_key: str | None
    workspace_binding: str
    workspace_service: Any
    handle: PtyHandle
    cwd: str
    cols: int
    rows: int
    target: dict[str, str] | None = None
    seq: int = 0
    closing: bool = False
    close_reason: str | None = None
    workspace_checked_at: float = 0.0
    reader_task: asyncio.Task[None] | None = field(default=None, repr=False)
    output_flow_control: bool = False
    consumed_seq: int = 0
    output_in_flight_bytes: int = 0
    output_in_flight: deque[tuple[int, int]] = field(default_factory=deque, repr=False)
    output_consumed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


def _dimension(value: Any, default: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise TerminalSessionError(
            "INVALID_REQUEST", f"{name} must be an integer between 1 and {maximum}"
        )
    return value


def _command(value: Any) -> str:
    if value is None or value == "":
        if os.name == "nt":
            return "powershell.exe -NoLogo -NoProfile"
        return f"{os.environ.get('SHELL') or '/bin/sh'} -i"
    if not isinstance(value, str) or not value.strip():
        raise TerminalSessionError("INVALID_REQUEST", "command must be a non-empty string")
    if len(value) > MAX_COMMAND_CHARS or "\x00" in value:
        raise TerminalSessionError("INVALID_REQUEST", "command is too long or contains NUL")
    return value


class TerminalManager:
    """Own process-local interactive terminal sessions."""

    def __init__(self, *, send_event: SendEvent) -> None:
        self._send_event = send_event
        self._sessions: dict[str, TerminalSession] = {}
        self._closed: OrderedDict[str, str] = OrderedDict()
        self._lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._cleanup_connections: set[str] = set()
        self._closing_connections: OrderedDict[str, None] = OrderedDict()
        self._creating: dict[str, int] = {}
        self._closing = False

    def _remember_closing_connection(self, conn_id: str) -> None:
        self._closing_connections[conn_id] = None
        self._closing_connections.move_to_end(conn_id)
        while len(self._closing_connections) > MAX_CLOSED_TERMINALS:
            self._closing_connections.popitem(last=False)

    def _owned(self, terminal_id: str, conn_id: str) -> TerminalSession:
        session = self._sessions.get(terminal_id)
        if session is None:
            raise TerminalSessionError("TERMINAL_NOT_FOUND", "Terminal session was not found")
        if session.conn_id != conn_id:
            raise TerminalSessionError(
                "UNAUTHORIZED", "Terminal session belongs to another connection"
            )
        return session

    def _schedule_handle_cleanup(
        self,
        handle: PtyHandle | None = None,
        *,
        spawn_task: asyncio.Task[PtyHandle] | None = None,
    ) -> None:
        """Dispose a native PTY even when its owner coroutine was cancelled.

        ``asyncio.to_thread`` cannot stop a native spawn that is already in
        progress. Keep the worker task shielded from request cancellation and
        dispose its eventual handle in a tracked background task.
        """

        async def dispose() -> None:
            native = handle
            if native is None and spawn_task is not None:
                try:
                    native = await spawn_task
                except PtyBackendError as exc:
                    native = exc.handle if exc.started else None
                except BaseException:
                    return
            if native is None:
                return
            try:
                capture_process_tree_owner(native.raw, isolated=True).start_completion_monitor()
                await terminate_pty(native)
            except BaseException:
                # Native cleanup is best effort; the process-tree owner is the
                # final Gateway-crash fence if the backend itself is broken.
                pass

        task = asyncio.create_task(dispose(), name="gateway-terminal-native-cleanup")
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def claim_connection_cleanup(self, conn_id: str) -> bool:
        """Return true once per connection lifetime for transport cleanup."""

        if conn_id in self._cleanup_connections:
            return False
        self._cleanup_connections.add(conn_id)
        return True

    def on_connection_cleanup(self, conn_id: str) -> None:
        """Schedule one bounded close for every terminal owned by a socket."""

        self._cleanup_connections.discard(conn_id)
        self._remember_closing_connection(conn_id)
        self.schedule_close_connection(conn_id)

    def _release_create_slot(self, conn_id: str) -> None:
        pending = self._creating.get(conn_id, 0)
        if pending <= 1:
            self._creating.pop(conn_id, None)
        else:
            self._creating[conn_id] = pending - 1

    async def create(
        self,
        *,
        conn_id: str,
        session_key: str | None,
        workspace_service: Any,
        cwd: str,
        command: str | None,
        cols: int,
        rows: int,
        target: dict[str, str] | None = None,
        output_flow_control: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._closing or conn_id in self._closing_connections:
                raise TerminalSessionError(
                    "TERMINAL_CLOSED", "Gateway terminal sessions are shutting down"
                )
            owned_count = sum(1 for item in self._sessions.values() if item.conn_id == conn_id)
            creating_count = self._creating.get(conn_id, 0)
            if owned_count + creating_count >= MAX_TERMINALS_PER_CONNECTION:
                raise TerminalSessionError(
                    "TERMINAL_LIMIT", "Too many terminal sessions on this connection"
                )
            self._creating[conn_id] = creating_count + 1

        try:
            workspace = await workspace_service.workspace(session_key)
        except BaseException:
            self._release_create_slot(conn_id)
            raise
        # ``_directory`` is the same checked, non-reparse-point path resolver
        # used by Workspace Files. Keep cwd relative to the authoritative root.
        from opensquilla.gateway.workspace_files import _directory

        try:
            directory = _directory(workspace, cwd or ".")
        except BaseException:
            self._release_create_slot(conn_id)
            raise
        resolved_cwd = directory.relative_to(workspace.root).as_posix()
        if resolved_cwd == ".":
            resolved_cwd = ""
        env = dict(os.environ)
        env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)
        terminal_id = f"term-{uuid.uuid4().hex}"
        spawn_task: asyncio.Task[PtyHandle] | None = None
        try:
            spawn_task = asyncio.create_task(
                asyncio.to_thread(
                    spawn_pty,
                    _command(command),
                    cwd=str(directory),
                    env=env,
                    cols=cols,
                    rows=rows,
                ),
                name=f"gateway-terminal-spawn-{terminal_id}",
            )
            # A disconnected WebSocket may cancel its request worker while a
            # native ConPTY/ptyprocess spawn is still in flight. Shielding
            # keeps the thread future alive so its eventual handle can be
            # terminated by the cancellation cleanup below.
            handle = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            if spawn_task is not None:
                self._schedule_handle_cleanup(spawn_task=spawn_task)
            self._release_create_slot(conn_id)
            raise
        except TerminalSessionError:
            self._release_create_slot(conn_id)
            raise
        except PtyBackendError as exc:
            # Some native backends can fail after creating the child and return
            # the live handle with ``started=True``.  Reap that process tree
            # before projecting the failure to RPC; otherwise a rejected create
            # can strand a shell that has no terminal identity to close later.
            try:
                if exc.started and exc.handle is not None:
                    try:
                        capture_process_tree_owner(
                            exc.handle.raw, isolated=True,
                        ).start_completion_monitor()
                        await terminate_pty(exc.handle)
                    except asyncio.CancelledError:
                        self._schedule_handle_cleanup(exc.handle)
                        raise
                    except Exception:
                        pass
            finally:
                # Cancellation can arrive during failed-spawn cleanup too.
                # Native ownership transfers to the cleanup task, but the RPC
                # must always release its connection's pending-create slot.
                self._release_create_slot(conn_id)
            raise TerminalSessionError("TERMINAL_UNAVAILABLE", str(exc)) from exc
        except Exception as exc:
            self._release_create_slot(conn_id)
            raise TerminalSessionError("TERMINAL_UNAVAILABLE", "PTY could not be started") from exc

        session = TerminalSession(
            terminal_id=terminal_id,
            conn_id=conn_id,
            session_key=session_key,
            workspace_binding=workspace.binding,
            workspace_service=workspace_service,
            handle=handle,
            cwd=resolved_cwd,
            cols=cols,
            rows=rows,
            output_flow_control=output_flow_control,
            target=dict(target) if target is not None else (
                {"kind": "session", "sessionKey": session_key} if session_key else None
            ),
        )
        registered = False
        try:
            # Native spawn runs in a worker without a running event loop.
            # Start the owner monitor here before validating or publishing the
            # handle so natural exit and failed publication both release it.
            capture_process_tree_owner(handle.raw, isolated=True).start_completion_monitor()
            # Native spawning can outlive a project removal or configuration
            # change. Do not publish a PTY after its target has changed.
            current = await workspace_service.workspace(session_key)
            if current.binding != workspace.binding:
                raise TerminalSessionError(
                    "TERMINAL_STALE", "The terminal working directory changed"
                )
            async with self._lock:
                # Recheck after the potentially slow native spawn. A
                # concurrent create cannot use the race to exceed the
                # per-connection bound.
                if self._closing or conn_id in self._closing_connections:
                    raise TerminalSessionError(
                        "TERMINAL_CLOSED", "Gateway terminal sessions are shutting down"
                    )
                owned_count = sum(1 for item in self._sessions.values() if item.conn_id == conn_id)
                if owned_count >= MAX_TERMINALS_PER_CONNECTION:
                    raise TerminalSessionError(
                        "TERMINAL_LIMIT", "Too many terminal sessions on this connection"
                    )
                self._sessions[terminal_id] = session
                registered = True
            session.reader_task = asyncio.create_task(
                self._pump(session), name=f"gateway-terminal-{terminal_id}"
            )
        except BaseException:
            if registered:
                self._sessions.pop(terminal_id, None)
            self._release_create_slot(conn_id)
            self._schedule_handle_cleanup(handle)
            raise
        self._release_create_slot(conn_id)
        return {
            "terminalId": terminal_id,
            **self._target_metadata(session),
            "cwd": resolved_cwd,
            "cwdPath": str(directory),
            "cols": cols,
            "rows": rows,
            "shell": "powershell" if os.name == "nt" else "posix",
            **({"outputFlowControl": "consumed-v1"} if output_flow_control else {}),
        }

    @staticmethod
    def _target_metadata(session: TerminalSession) -> dict[str, Any]:
        return {
            **({"sessionKey": session.session_key} if session.session_key is not None else {}),
            **({"target": dict(session.target)} if session.target is not None else {}),
        }

    async def _ensure_current(self, session: TerminalSession) -> None:
        now = time.monotonic()
        if now - session.workspace_checked_at < WORKSPACE_RECHECK_SECONDS:
            return
        try:
            current = await session.workspace_service.workspace(session.session_key)
        except Exception as exc:
            await self._close_session(session, reason="workspace_unavailable")
            raise TerminalSessionError(
                "TERMINAL_STALE", "The session workspace is unavailable"
            ) from exc
        if current.binding != session.workspace_binding:
            await self._close_session(session, reason="workspace_changed")
            raise TerminalSessionError("TERMINAL_STALE", "The session workspace changed")
        session.workspace_checked_at = now

    async def write(self, *, terminal_id: str, conn_id: str, data: str) -> dict[str, Any]:
        session = self._owned(terminal_id, conn_id)
        if not isinstance(data, str) or not data:
            raise TerminalSessionError("INVALID_REQUEST", "data must be a non-empty string")
        try:
            encoded = data.encode("utf-8")
        except UnicodeError as exc:
            raise TerminalSessionError(
                "INVALID_REQUEST", "terminal input is not valid UTF-8"
            ) from exc
        if len(encoded) > MAX_WRITE_BYTES:
            raise TerminalSessionError("INVALID_REQUEST", "terminal input is too large")
        if session.closing:
            raise TerminalSessionError("TERMINAL_CLOSED", "Terminal session is closed")
        await self._ensure_current(session)
        try:
            await write_pty(session.handle, encoded)
        except PtyBackendError as exc:
            raise TerminalSessionError("TERMINAL_CLOSED", str(exc)) from exc
        return {"terminalId": terminal_id, "accepted": True, "bytes": len(encoded)}

    async def resize(
        self, *, terminal_id: str, conn_id: str, cols: int, rows: int
    ) -> dict[str, Any]:
        session = self._owned(terminal_id, conn_id)
        if session.closing:
            raise TerminalSessionError("TERMINAL_CLOSED", "Terminal session is closed")
        await self._ensure_current(session)
        try:
            await resize_pty(session.handle, cols, rows)
        except PtyBackendError as exc:
            raise TerminalSessionError("TERMINAL_CLOSED", str(exc)) from exc
        session.cols, session.rows = cols, rows
        return {"terminalId": terminal_id, "cols": cols, "rows": rows}

    async def consume(self, *, terminal_id: str, conn_id: str, seq: object) -> dict[str, Any]:
        """Release output credits only after the owning emulator consumed them."""
        if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= 2**53 - 1:
            raise TerminalSessionError("INVALID_REQUEST", "seq must be a positive safe integer")
        try:
            session = self._owned(terminal_id, conn_id)
        except TerminalSessionError as exc:
            # Parser callbacks can arrive after a natural process exit. Nothing
            # remains buffered, but retain the same connection ownership fence.
            if exc.code == "TERMINAL_NOT_FOUND" and self._closed.get(terminal_id) == conn_id:
                return {"terminalId": terminal_id, "consumedSeq": seq}
            raise
        if not session.output_flow_control or seq > session.seq:
            raise TerminalSessionError("INVALID_REQUEST", "seq is not emitted terminal output")
        if seq > session.consumed_seq:
            session.consumed_seq = seq
            while session.output_in_flight and session.output_in_flight[0][0] <= seq:
                _, size = session.output_in_flight.popleft()
                session.output_in_flight_bytes -= size
            session.output_consumed.set()
        return {"terminalId": terminal_id, "consumedSeq": session.consumed_seq}

    async def _wait_output_capacity(self, session: TerminalSession) -> bool:
        if not session.output_flow_control:
            return True
        if (session.output_in_flight_bytes < OUTPUT_HIGH_WATER_BYTES
                and len(session.output_in_flight) < MAX_OUTPUT_IN_FLIGHT_FRAMES):
            return not session.closing
        # Do not read another PTY block while the emulator is behind. Native
        # pipes/socketpair provide backpressure; there is no Python output tail.
        # Hysteresis avoids a pause/resume cycle for every tiny consumed frame.
        while (not session.closing and (
            session.output_in_flight_bytes > OUTPUT_LOW_WATER_BYTES
            or len(session.output_in_flight) > MAX_OUTPUT_IN_FLIGHT_FRAMES // 4
        )):
            session.output_consumed.clear()
            await session.output_consumed.wait()
        return not session.closing

    async def close(
        self, *, terminal_id: str, conn_id: str, reason: str = "client_closed"
    ) -> dict[str, Any]:
        try:
            session = self._owned(terminal_id, conn_id)
        except TerminalSessionError as exc:
            if exc.code == "TERMINAL_NOT_FOUND" and self._closed.get(terminal_id) == conn_id:
                return {"terminalId": terminal_id, "closed": True}
            raise
        await self._close_session(session, reason=reason)
        return {"terminalId": terminal_id, "closed": True}

    async def _close_session(self, session: TerminalSession, *, reason: str) -> None:
        if session.closing:
            task = session.reader_task
            if task is not None and task is not asyncio.current_task():
                try:
                    async with asyncio.timeout(2.0):
                        await task
                except BaseException:
                    pass
            return
        session.closing = True
        session.close_reason = reason
        session.output_consumed.set()
        try:
            await terminate_pty(session.handle)
        except Exception:
            # The reader still emits a terminal.exit event; native cleanup is
            # best-effort after the owner has already requested closure.
            pass
        task = session.reader_task
        if task is not None and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()

    async def _pump(self, session: TerminalSession) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                if not await self._wait_output_capacity(session):
                    break
                try:
                    data = await read_pty(session.handle, MAX_OUTPUT_BYTES)
                except EOFError:
                    break
                if not data:
                    break
                for start in range(0, len(data), MAX_OUTPUT_BYTES):
                    if not await self._wait_output_capacity(session):
                        break
                    chunk = data[start:start + MAX_OUTPUT_BYTES]
                    text = decoder.decode(chunk, final=False)
                    if not text:
                        continue
                    session.seq += 1
                    if session.output_flow_control:
                        # Count UTF-8 bytes actually sent, including replacement
                        # characters. Overshoot is bounded by one decoded chunk
                        # (at most 3 * MAX_OUTPUT_BYTES for invalid UTF-8).
                        size = len(text.encode("utf-8"))
                        session.output_in_flight.append((session.seq, size))
                        session.output_in_flight_bytes += size
                    sent = await self._send_event(
                        "terminal.output",
                        {
                            "terminalId": session.terminal_id,
                            **self._target_metadata(session),
                            "seq": session.seq,
                            "data": text,
                            "encoding": "utf8",
                        },
                    )
                    if not sent:
                        session.closing = True
                        session.close_reason = "connection_closed"
                        try:
                            await terminate_pty(session.handle)
                        except Exception:
                            pass
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            session.closing = True
            session.close_reason = session.close_reason or "reader_error"
            # Losing the reader does not mean its shell has exited. Reap the
            # process before wait_pty below; later close requests see closing
            # and only await this task, so they cannot rescue a live shell.
            try:
                await terminate_pty(session.handle)
            except asyncio.CancelledError:
                self._schedule_handle_cleanup(session.handle)
                raise
            except Exception:
                self._schedule_handle_cleanup(session.handle)
        finally:
            # Flush a partial UTF-8 code point once the PTY reaches EOF. This
            # avoids replacing a split multibyte character twice across reads.
            tail = decoder.decode(b"", final=True)
            if tail:
                session.seq += 1
                try:
                    await self._send_event(
                        "terminal.output",
                        {
                            "terminalId": session.terminal_id,
                            **self._target_metadata(session),
                            "seq": session.seq,
                            "data": tail,
                            "encoding": "utf8",
                        },
                    )
                except BaseException:
                    pass
            try:
                returncode = await wait_pty(session.handle)
            except BaseException:
                returncode = None
            session.seq += 1
            try:
                await self._send_event(
                    "terminal.exit",
                    {
                        "terminalId": session.terminal_id,
                        **self._target_metadata(session),
                        "seq": session.seq,
                        "exitCode": returncode,
                        "signal": None,
                        "reason": session.close_reason or "process_exit",
                    },
                )
            except BaseException:
                pass
            try:
                async with self._lock:
                    self._sessions.pop(session.terminal_id, None)
                    self._closed[session.terminal_id] = session.conn_id
                    self._closed.move_to_end(session.terminal_id)
                    while len(self._closed) > MAX_CLOSED_TERMINALS:
                        self._closed.popitem(last=False)
            except asyncio.CancelledError:
                # Cancellation after native process exit must not strand the
                # terminal identity in the live-session map.
                self._sessions.pop(session.terminal_id, None)
                self._closed[session.terminal_id] = session.conn_id
                self._closed.move_to_end(session.terminal_id)
                while len(self._closed) > MAX_CLOSED_TERMINALS:
                    self._closed.popitem(last=False)
                raise

    def schedule_close_connection(self, conn_id: str) -> None:
        task = asyncio.create_task(
            self.close_connection(conn_id), name=f"gateway-terminal-close-{conn_id}"
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def close_connection(self, conn_id: str) -> None:
        self._remember_closing_connection(conn_id)
        sessions = [item for item in tuple(self._sessions.values()) if item.conn_id == conn_id]
        for session in sessions:
            await self._close_session(session, reason="connection_closed")

    async def close_all(self) -> None:
        # Fence creates before taking the snapshot. A native PTY spawn can run
        # in a worker after this method starts; its publication check below
        # must reject and dispose that handle instead of registering it after
        # Gateway shutdown has already passed the terminal phase.
        async with self._lock:
            self._closing = True
            sessions = tuple(self._sessions.values())
        for session in sessions:
            await self._close_session(session, reason="gateway_shutdown")


_DEFAULT_MANAGER: TerminalManager | None = None


async def _send_default_event(event: str, payload: dict[str, Any]) -> bool:
    """Route singleton terminal events through the owning WebSocket."""

    terminal_id = payload.get("terminalId")
    manager = _DEFAULT_MANAGER
    session = (
        manager._sessions.get(terminal_id) if manager and isinstance(terminal_id, str) else None
    )
    if session is None:
        return False
    from opensquilla.gateway.websocket import get_registry

    connection = get_registry().get(session.conn_id)
    if connection is None or connection._closing:
        return False
    try:
        await connection.send_event(event, payload)
    except Exception:
        return False
    return True


def get_terminal_manager() -> TerminalManager:
    """Return the process-local manager shared by RPC and Gateway shutdown."""

    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = TerminalManager(send_event=_send_default_event)
    return _DEFAULT_MANAGER


__all__ = [
    "DEFAULT_COLS",
    "DEFAULT_ROWS",
    "MAX_COLS",
    "MAX_CLOSED_TERMINALS",
    "MAX_COMMAND_CHARS",
    "MAX_OUTPUT_BYTES",
    "MAX_ROWS",
    "MAX_TERMINALS_PER_CONNECTION",
    "MAX_WRITE_BYTES",
    "WORKSPACE_RECHECK_SECONDS",
    "TerminalManager",
    "TerminalSessionError",
    "get_terminal_manager",
    "_command",
    "_dimension",
]
