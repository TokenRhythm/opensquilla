from __future__ import annotations

import errno
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Literal, Protocol

from tui_real_terminal.framebuffer import (
    StyledFramebuffer,
    parse_tmux_styled_framebuffer,
)

try:
    import pty
except ImportError:  # pragma: no cover - exercised only on platforms without pty
    pty = None  # type: ignore[assignment]

DriverKind = Literal["tmux", "pty"]
DriverSelection = Literal["auto", "tmux", "pty"]
_RUN_ID_COUNTER = count()


@dataclass(frozen=True)
class TerminalSize:
    cols: int = 100
    rows: int = 30

    def __post_init__(self) -> None:
        if self.cols <= 0 or self.rows <= 0:
            raise ValueError("terminal size must be positive")


@dataclass(frozen=True)
class TerminalFrame:
    checkpoint: str
    text: str
    captured_at_ms: int
    size: TerminalSize


@dataclass(frozen=True)
class TerminalCapabilities:
    tmux_available: bool
    pty_available: bool
    screenshot_available: bool
    resize_available: bool
    preferred_driver: Literal["tmux", "pty", "none"]
    skip_reason: str | None = None


class RealTerminalSession(Protocol):
    run_id: str
    kind: DriverKind
    size: TerminalSize

    def start(self) -> None: ...

    def send_text(self, text: str) -> None: ...

    def send_key(self, key: str) -> None: ...

    def paste(self, text: str) -> None: ...

    def mouse_scroll(
        self,
        direction: Literal["up", "down"],
        *,
        ticks: int = 1,
        x: int = 4,
        y: int = 4,
    ) -> None: ...

    def resize(self, size: TerminalSize) -> None: ...

    def capture_text(self, checkpoint: str) -> TerminalFrame: ...

    def capture_framebuffer(self, checkpoint: str) -> StyledFramebuffer | None: ...

    def capture_scrollback_text(self, checkpoint: str) -> TerminalFrame: ...

    def cursor_position(self) -> tuple[int, int] | None: ...

    def alternate_screen_active(self) -> bool: ...

    def wait_for_text(
        self,
        needle: str,
        *,
        timeout_s: float,
        checkpoint: str,
    ) -> TerminalFrame: ...

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|P[^\x1b]*\x1b\\"
    r"|[@-Z\\-_]"
    r")"
)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _run_id_suffix() -> str:
    return f"{time.time_ns()}-{os.getpid()}-{next(_RUN_ID_COUNTER)}"


def build_run_id(scenario_id: str) -> str:
    safe = _SAFE_ID_RE.sub("-", scenario_id.strip().lower()).strip("-") or "scenario"
    return f"opensquilla-tui-{safe}-{_run_id_suffix()}"


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def probe_terminal_capabilities() -> TerminalCapabilities:
    tmux_available = shutil.which("tmux") is not None
    pty_available = sys.platform != "win32" and pty is not None and hasattr(pty, "openpty")
    if tmux_available:
        preferred_driver: Literal["tmux", "pty", "none"] = "tmux"
    elif pty_available:
        preferred_driver = "pty"
    else:
        preferred_driver = "none"

    if preferred_driver != "none":
        skip_reason: str | None = None
    elif sys.platform == "win32":
        skip_reason = (
            "real-terminal harness needs tmux or a Unix PTY, which native Windows "
            "lacks; run under WSL2 (see docs/tui-real-terminal-harness.md)"
        )
    else:
        skip_reason = "tmux and PTY are unavailable"

    return TerminalCapabilities(
        tmux_available=tmux_available,
        pty_available=pty_available,
        screenshot_available=False,
        resize_available=tmux_available or pty_available,
        preferred_driver=preferred_driver,
        skip_reason=skip_reason,
    )


@dataclass
class _BaseTerminalSession:
    command: list[str]
    cwd: Path
    env: dict[str, str]
    run_id: str
    size: TerminalSize
    terminal_log: Path
    kind: DriverKind = field(init=False)

    def _append_log(self, text: str) -> None:
        self.terminal_log.parent.mkdir(parents=True, exist_ok=True)
        with self.terminal_log.open("a", encoding="utf-8") as fh:
            fh.write(text)
            if text and not text.endswith("\n"):
                fh.write("\n")


@dataclass
class TmuxTerminalSession(_BaseTerminalSession):
    kind: DriverKind = field(init=False, default="tmux")

    def start(self) -> None:
        env_prefix = ["env", *[f"{key}={value}" for key, value in sorted(self.env.items())]]
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.run_id,
                "-x",
                str(self.size.cols),
                "-y",
                str(self.size.rows),
                "-c",
                str(self.cwd),
                *env_prefix,
                *self.command,
            ],
            check=True,
        )

    def send_text(self, text: str) -> None:
        subprocess.run(["tmux", "send-keys", "-t", self.run_id, "-l", text], check=True)
        subprocess.run(["tmux", "send-keys", "-t", self.run_id, "Enter"], check=True)

    def send_key(self, key: str) -> None:
        subprocess.run(["tmux", "send-keys", "-t", self.run_id, key], check=True)

    def paste(self, text: str) -> None:
        subprocess.run(
            ["tmux", "load-buffer", "-b", self.run_id, "-"],
            check=True,
            input=text,
            text=True,
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-p", "-t", self.run_id, "-b", self.run_id],
            check=True,
        )

    def mouse_scroll(
        self,
        direction: Literal["up", "down"],
        *,
        ticks: int = 1,
        x: int = 4,
        y: int = 4,
    ) -> None:
        """Send real SGR mouse-wheel reports through the child PTY."""

        if direction not in {"up", "down"}:
            raise ValueError("mouse scroll direction must be up or down")
        button = 64 if direction == "up" else 65
        report = f"\x1b[<{button};{max(1, x)};{max(1, y)}M".encode()
        hex_bytes = [f"{byte:02x}" for byte in report]
        for _ in range(max(1, ticks)):
            subprocess.run(
                ["tmux", "send-keys", "-H", "-t", self.run_id, *hex_bytes],
                check=True,
            )

    def resize(self, size: TerminalSize) -> None:
        self.size = size
        subprocess.run(
            [
                "tmux",
                "resize-window",
                "-t",
                self.run_id,
                "-x",
                str(size.cols),
                "-y",
                str(size.rows),
            ],
            check=True,
        )

    def capture_text(self, checkpoint: str) -> TerminalFrame:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", self.run_id, "-p", "-J"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        frame = TerminalFrame(checkpoint, result.stdout, _now_ms(), self.size)
        self._append_log(f"\n--- {checkpoint} ---\n{frame.text}")
        return frame

    def capture_framebuffer(self, checkpoint: str) -> StyledFramebuffer:
        # Default capture reads the currently visible screen. `-a` would read
        # tmux's saved normal screen while the TUI is in alternate-screen mode,
        # so it is deliberately absent. `-J` is likewise absent because it
        # joins wrapped rows and destroys exact cell geometry.
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", self.run_id, "-e", "-N", "-p"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        captured_at_ms = _now_ms()
        return parse_tmux_styled_framebuffer(
            result.stdout,
            checkpoint=checkpoint,
            cols=self.size.cols,
            rows=self.size.rows,
            captured_at_ms=captured_at_ms,
        )

    def capture_scrollback_text(self, checkpoint: str) -> TerminalFrame:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", self.run_id, "-S", "-", "-p", "-J"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        frame = TerminalFrame(checkpoint, result.stdout, _now_ms(), self.size)
        self._append_log(f"\n--- {checkpoint} scrollback ---\n{frame.text}")
        return frame

    def cursor_position(self) -> tuple[int, int]:
        result = subprocess.run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                self.run_id,
                "#{cursor_x},#{cursor_y}",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        x, y = result.stdout.strip().split(",", 1)
        return int(x), int(y)

    def alternate_screen_active(self) -> bool:
        # tmux tracks the pane's alternate-screen mode itself, so this probes
        # real terminal state rather than inferring it from captured text: an
        # app that exited without leaving the alternate screen still shows
        # later shell output, just drawn over the stale alternate screen.
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", self.run_id, "#{alternate_on}"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        return result.stdout.strip() == "1"

    def wait_for_text(
        self,
        needle: str,
        *,
        timeout_s: float,
        checkpoint: str,
    ) -> TerminalFrame:
        deadline = time.monotonic() + timeout_s
        last = self.capture_text(checkpoint)
        if needle in last.text:
            return last
        while time.monotonic() < deadline:
            time.sleep(0.05)
            last = self.capture_text(checkpoint)
            if needle in last.text:
                return last
        raise TimeoutError(f"timed out waiting for {needle!r}; last screen: {last.text}")

    def is_alive(self) -> bool:
        return (
            subprocess.run(
                ["tmux", "has-session", "-t", self.run_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            == 0
        )

    def terminate(self) -> None:
        subprocess.run(
            ["tmux", "kill-session", "-t", self.run_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


@dataclass
class PtyTerminalSession(_BaseTerminalSession):
    kind: DriverKind = field(init=False, default="pty")
    _master_fd: int | None = field(init=False, default=None)
    _process: subprocess.Popen[bytes] | None = field(init=False, default=None)
    _buffer: bytearray = field(init=False, default_factory=bytearray)

    def start(self) -> None:
        if pty is None or not hasattr(pty, "openpty"):
            raise RuntimeError("PTY terminal driver is unavailable")
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._configure_master_fd()
        self._set_pty_size()
        env = self._process_env()
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        finally:
            os.close(slave_fd)

    def send_text(self, text: str) -> None:
        self._write(text.encode("utf-8"))
        self.send_key("Enter")

    def send_key(self, key: str) -> None:
        sequences = {
            "Enter": b"\r",
            "C-c": b"\x03",
            "Ctrl-C": b"\x03",
            "C-d": b"\x04",
            "EOF": b"\x04",
            "Escape": b"\x1b",
            "Tab": b"\t",
            "Backspace": b"\x7f",
        }
        self._write(sequences.get(key, key.encode("utf-8")))

    def paste(self, text: str) -> None:
        payload = f"\x1b[200~{text}\x1b[201~"
        self._write(payload.encode("utf-8"))

    def mouse_scroll(
        self,
        direction: Literal["up", "down"],
        *,
        ticks: int = 1,
        x: int = 4,
        y: int = 4,
    ) -> None:
        if direction not in {"up", "down"}:
            raise ValueError("mouse scroll direction must be up or down")
        button = 64 if direction == "up" else 65
        report = f"\x1b[<{button};{max(1, x)};{max(1, y)}M".encode()
        self._write(report * max(1, ticks))

    def resize(self, size: TerminalSize) -> None:
        self.size = size
        if self._master_fd is not None:
            self._set_pty_size()

    def capture_text(self, checkpoint: str) -> TerminalFrame:
        self._drain_output()
        text = _strip_ansi(self._buffer.decode("utf-8", errors="replace"))
        frame = TerminalFrame(checkpoint, text, _now_ms(), self.size)
        self._append_log(f"\n--- {checkpoint} ---\n{frame.text}")
        return frame

    def capture_framebuffer(self, checkpoint: str) -> None:
        # A byte-accumulating PTY is not a terminal emulator and cannot report
        # the current cell grid after cursor movement or alternate-screen use.
        return None

    def alternate_screen_active(self) -> bool:
        return False

    def capture_scrollback_text(self, checkpoint: str) -> TerminalFrame:
        return self.capture_text(checkpoint)

    def cursor_position(self) -> None:
        # The PTY driver records an append-only byte stream; without a terminal
        # emulator it cannot observe the physical hardware cursor.
        return None

    def wait_for_text(
        self,
        needle: str,
        *,
        timeout_s: float,
        checkpoint: str,
    ) -> TerminalFrame:
        deadline = time.monotonic() + timeout_s
        last = self.capture_text(checkpoint)
        if needle in last.text:
            return last
        while time.monotonic() < deadline:
            time.sleep(0.05)
            last = self.capture_text(checkpoint)
            if needle in last.text:
                return last
        raise TimeoutError(f"timed out waiting for {needle!r}; last screen: {last.text}")

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def _configure_master_fd(self) -> None:
        if self._master_fd is None:
            return
        os.set_blocking(self._master_fd, False)

    def _process_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.env)
        env.setdefault("TERM", "xterm-256color")
        env["COLUMNS"] = str(self.size.cols)
        env["LINES"] = str(self.size.rows)
        return env

    def _set_pty_size(self) -> None:
        if self._master_fd is None:
            return
        import fcntl
        import termios

        packed_size = struct.pack("HHHH", self.size.rows, self.size.cols, 0, 0)
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, packed_size)

    def _write(self, data: bytes) -> None:
        if self._master_fd is None:
            raise RuntimeError("PTY session has not started")
        os.write(self._master_fd, data)

    def _drain_output(self) -> None:
        if self._master_fd is None:
            return
        while True:
            try:
                chunk = os.read(self._master_fd, 4096)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    return
                raise
            if not chunk:
                return
            self._buffer.extend(chunk)


def open_real_terminal_session(
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    run_id: str,
    size: TerminalSize,
    artifact_dir: Path,
    driver: DriverSelection = "auto",
) -> RealTerminalSession:
    capabilities = probe_terminal_capabilities()
    selected = capabilities.preferred_driver if driver == "auto" else driver
    terminal_log = artifact_dir / "terminal.log"
    if selected == "tmux" and capabilities.tmux_available:
        return TmuxTerminalSession(command, cwd, env, run_id, size, terminal_log)
    if selected == "pty" and capabilities.pty_available:
        return PtyTerminalSession(command, cwd, env, run_id, size, terminal_log)
    if driver != "auto":
        raise RuntimeError(f"requested terminal driver {selected!r} is unavailable")
    reason = capabilities.skip_reason or f"requested terminal driver {selected!r} is unavailable"
    raise RuntimeError(reason)
