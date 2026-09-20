"""Optional, platform-neutral PTY adapter for managed shell sessions.

The module intentionally contains no eager imports of native PTY packages.  A
normal ``exec_command`` therefore keeps the same startup path when the optional
``pty`` extra is not installed.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any


class PtyBackendError(RuntimeError):
    """A PTY could not be created or operated safely."""

    def __init__(
        self,
        message: str,
        *,
        started: bool = False,
        handle: PtyHandle | None = None,
    ) -> None:
        super().__init__(message)
        self.started = started
        self.handle = handle


@dataclass
class PtyHandle:
    """Small async-friendly wrapper around ptyprocess or pywinpty."""

    raw: Any
    platform: str
    _returncode: int | None = None
    _wait_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def pid(self) -> int | None:
        value = getattr(self.raw, "pid", None)
        return int(value) if isinstance(value, int) else None

    @property
    def returncode(self) -> int | None:
        value = getattr(self.raw, "returncode", None)
        if isinstance(value, int):
            return value
        return self._returncode

    def read(self, size: int) -> bytes:
        try:
            value = self.raw.read(size)
        except (EOFError, OSError, ValueError) as exc:
            raise EOFError from exc
        if isinstance(value, str):
            return value.encode("utf-8", errors="replace")
        return bytes(value or b"")

    def write(self, data: bytes) -> None:
        value: str | bytes = data
        if self.platform == "windows":
            # ConPTY consumes terminal keystrokes: Enter is CR, while a bare
            # LF merely echoes and leaves console readline waiting forever.
            value = data.decode("utf-8", errors="replace")
            value = value.replace("\r\n", "\n").replace("\n", "\r")
        try:
            self.raw.write(value)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError, EOFError) as exc:
            raise PtyBackendError("PTY input is closed", started=True) from exc

    def resize(self, cols: int, rows: int) -> None:
        try:
            if hasattr(self.raw, "setwinsize"):
                self.raw.setwinsize(rows, cols)
            elif hasattr(self.raw, "resize"):
                self.raw.resize(cols, rows)
            else:
                raise PtyBackendError("PTY backend does not support resize", started=True)
        except PtyBackendError:
            raise
        except (OSError, ValueError) as exc:
            raise PtyBackendError("PTY resize failed", started=True) from exc

    def eof(self) -> None:
        try:
            if self.platform == "windows":
                # ConPTY uses Windows console input; Ctrl-D is ordinary input.
                self.raw.write("\x1a\r\n")
            elif hasattr(self.raw, "sendeof"):
                self.raw.sendeof()
            elif hasattr(self.raw, "sendcontrol"):
                self.raw.sendcontrol("d")
            elif hasattr(self.raw, "close"):
                self.raw.close(force=False)
        except (OSError, ValueError, EOFError) as exc:
            raise PtyBackendError("PTY EOF failed", started=True) from exc

    def terminate(self) -> None:
        try:
            if hasattr(self.raw, "terminate"):
                try:
                    self.raw.terminate(force=True)
                except TypeError:
                    self.raw.terminate()
            elif hasattr(self.raw, "close"):
                self.raw.close(force=True)
        except (OSError, ValueError, EOFError) as exc:
            raise PtyBackendError("PTY termination failed", started=True) from exc

    def close_reader(self) -> None:
        if self.platform == "windows":
            # pywinpty's isalive() sets closed before close() can release its
            # sockets. Wake blocked recv threads even after the Job has exited.
            stream = getattr(self.raw, "fileobj", None)
            if stream is not None:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()
            server = getattr(self.raw, "_server", None)
            if server is not None:
                server.close()
        elif hasattr(self.raw, "close"):
            self.raw.close(force=False)

    def wait(self) -> int | None:
        with self._wait_lock:
            try:
                value = self.raw.wait()
            except (EOFError, OSError, ValueError):
                value = getattr(self.raw, "exitstatus", None)
            if isinstance(value, int):
                self._returncode = value
        return self.returncode


class _OwnedPtyProcess:
    """Provide the existing tree launcher with a Popen-shaped PTY handle."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self._poll_lock = threading.RLock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)

    @property
    def returncode(self) -> int | None:
        with self._poll_lock:
            if self.raw.isalive():
                return None
            status = self.raw.exitstatus
            signalstatus = getattr(self.raw, "signalstatus", None)
            return status if status is not None else (-signalstatus if signalstatus else None)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while (status := self.returncode) is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("PTY helper", float(timeout or 0))
            time.sleep(0.01)
        return status

    def terminate(self, *, force: bool = True) -> None:
        with self._poll_lock:
            self.raw.terminate(force=force)

    def kill(self) -> None:
        self.terminate()


def _shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        return ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command]
    return ["/bin/sh", "-lc", command]


def spawn_pty(
    command: str,
    *,
    cwd: str | None,
    env: dict[str, str] | None,
    cols: int = 120,
    rows: int = 30,
) -> PtyHandle:
    """Spawn one PTY process using a lazy platform-specific import."""

    if cols <= 0 or rows <= 0:
        raise PtyBackendError("PTY dimensions must be positive")
    if os.name == "nt":
        try:
            from winpty import PtyProcess  # type: ignore[import-not-found,import-untyped]
        except ImportError as exc:
            raise PtyBackendError(
                "Windows PTY support is unavailable in this runtime"
            ) from exc
        try:
            from opensquilla.process_tree import create_owned_popen

            def spawn(argv: list[str], **kwargs: Any) -> _OwnedPtyProcess:
                kwargs.pop("creationflags", None)
                job = kwargs.pop("owner_job")

                class _JobPtyProcess(PtyProcess):  # type: ignore[misc]
                    def __init__(self, native: Any) -> None:
                        job.assign_pid(int(native.pid))
                        super().__init__(native)

                return _OwnedPtyProcess(_JobPtyProcess.spawn(
                    list(argv), dimensions=(rows, cols), backend="0", **kwargs,
                ))

            raw = create_owned_popen(
                _shell_argv(command), process_factory=spawn, cwd=cwd, env=env,
            )
            handle = PtyHandle(raw, "windows")
            try:
                if hasattr(raw, "setwinsize"):
                    raw.setwinsize(rows, cols)
            except Exception as exc:
                raise PtyBackendError(
                    f"Windows PTY initialization failed: {exc}",
                    started=True,
                    handle=handle,
                ) from exc
            return handle
        except PtyBackendError:
            raise
        except Exception as exc:
            # Backend constructors can fail after CreateProcess. Never replay
            # an uncertain launch through pipes, even without a returned handle.
            raise PtyBackendError(f"Windows PTY spawn failed: {exc}", started=True) from exc

    try:
        from ptyprocess import PtyProcess  # type: ignore[import-not-found,import-untyped]
    except ImportError as exc:
        raise PtyBackendError("POSIX PTY support is unavailable in this runtime") from exc
    try:
        from opensquilla.process_tree import create_owned_posix_pty

        def spawn(argv: list[str], **kwargs: Any) -> _OwnedPtyProcess:
            return _OwnedPtyProcess(PtyProcess.spawn(argv, **kwargs))

        raw = create_owned_posix_pty(
            _shell_argv(command), spawn, cwd=cwd, env=env, dimensions=(rows, cols),
        )
        return PtyHandle(raw, "posix")
    except Exception as exc:
        raise PtyBackendError(f"POSIX PTY spawn failed: {exc}", started=True) from exc


async def wait_pty(handle: PtyHandle) -> int | None:
    return await asyncio.to_thread(handle.wait)


async def read_pty(handle: PtyHandle, size: int = 8192) -> bytes:
    stream = getattr(handle.raw, "fileobj", None)
    if handle.platform == "windows" and isinstance(stream, socket.socket):
        # pywinpty already copies native output into this socket. Reading it
        # through the shared executor can miss the post-exit drain deadline
        # while unrelated workers are busy, even when the bytes are ready.
        stream.setblocking(False)
        while True:
            chunk = await asyncio.get_running_loop().sock_recv(stream, size)
            if chunk != b"0011Ignore":
                return chunk
    return await asyncio.to_thread(handle.read, size)


async def write_pty(handle: PtyHandle, data: bytes) -> None:
    await asyncio.to_thread(handle.write, data)


async def resize_pty(handle: PtyHandle, cols: int, rows: int) -> None:
    await asyncio.to_thread(handle.resize, cols, rows)


async def eof_pty(handle: PtyHandle) -> None:
    await asyncio.to_thread(handle.eof)


async def terminate_pty(handle: PtyHandle, *, close_reader: bool = True) -> None:
    owner = getattr(handle.raw, "_opensquilla_process_tree_owner", None)
    if owner is not None:
        if not await owner.terminate(graceful_timeout=0.2, kill_timeout=2.0):
            raise PtyBackendError("PTY process tree did not stop", started=True, handle=handle)
    else:
        await asyncio.to_thread(handle.terminate)
    if close_reader:
        await asyncio.to_thread(handle.close_reader)
