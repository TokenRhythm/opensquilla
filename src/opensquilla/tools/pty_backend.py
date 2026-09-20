"""Optional, platform-neutral PTY adapter for managed shell sessions.

The module intentionally contains no eager imports of native PTY packages.  A
normal ``exec_command`` therefore keeps the same startup path when the optional
``pty`` extra is not installed.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
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
            value = data.decode("utf-8", errors="replace")
        try:
            self.raw.write(value)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError) as exc:
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
            if hasattr(self.raw, "sendeof"):
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

    def wait(self) -> int | None:
        try:
            value = self.raw.wait()
        except (EOFError, OSError, ValueError):
            value = getattr(self.raw, "exitstatus", None)
        if isinstance(value, int):
            self._returncode = value
        return self.returncode


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
            from winpty import PtyProcess  # type: ignore[import-not-found]
        except ImportError as exc:
            raise PtyBackendError(
                "Windows PTY support is unavailable in this runtime"
            ) from exc
        try:
            raw = PtyProcess.spawn(_shell_argv(command), cwd=cwd, env=env)
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
            raise PtyBackendError(f"Windows PTY spawn failed: {exc}", started=False) from exc

    try:
        from ptyprocess import PtyProcess  # type: ignore[import-not-found,import-untyped]
    except ImportError as exc:
        raise PtyBackendError("POSIX PTY support is unavailable in this runtime") from exc
    try:
        raw = PtyProcess.spawn(_shell_argv(command), cwd=cwd, env=env, dimensions=(rows, cols))
        return PtyHandle(raw, "posix")
    except Exception as exc:
        # ptyprocess raises before returning a handle when fork/exec did not
        # start.  Callers may safely fall back to pipes in that case.
        raise PtyBackendError(f"POSIX PTY spawn failed: {exc}", started=False) from exc


async def wait_pty(handle: PtyHandle) -> int | None:
    return await asyncio.to_thread(handle.wait)


async def read_pty(handle: PtyHandle, size: int = 8192) -> bytes:
    return await asyncio.to_thread(handle.read, size)


async def write_pty(handle: PtyHandle, data: bytes) -> None:
    await asyncio.to_thread(handle.write, data)


async def resize_pty(handle: PtyHandle, cols: int, rows: int) -> None:
    await asyncio.to_thread(handle.resize, cols, rows)


async def eof_pty(handle: PtyHandle) -> None:
    await asyncio.to_thread(handle.eof)


async def terminate_pty(handle: PtyHandle) -> None:
    await asyncio.to_thread(handle.terminate)
