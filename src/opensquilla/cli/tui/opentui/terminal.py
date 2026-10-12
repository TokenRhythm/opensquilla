"""Terminal state guardians for the OpenTUI host lifecycle."""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from typing import Any

TERMINAL_RESET_SEQUENCE = (
    b"\x1b[?1049l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1004l\x1b[?1006l\x1b[?2004l\x1b[?25h\x1b[0m"
)


class TerminalGuardian:
    """No-op terminal lifecycle used on platforms without POSIX termios."""

    restored: bool

    def __init__(self) -> None:
        self.restored = False

    def capture(self) -> None:
        self.restored = False

    def restore(self) -> None:
        self.restored = True


class PosixTerminalGuardian(TerminalGuardian):
    """Capture and restore the controlling tty around an external host."""

    def __init__(self) -> None:
        super().__init__()
        self.tty_fd: int | None = None
        self.saved_termios: list[Any] | None = None

    def capture(self) -> None:
        super().capture()
        self.tty_fd = _controlling_tty_fd()
        self.saved_termios = None
        if self.tty_fd is None:
            return
        termios = _termios()
        if termios is not None:
            with suppress(Exception):
                self.saved_termios = termios.tcgetattr(self.tty_fd)

    def restore(self) -> None:
        if self.restored:
            return
        super().restore()
        if self.tty_fd is None:
            return
        termios = _termios()
        if termios is not None and self.saved_termios is not None:
            with suppress(Exception):
                termios.tcsetattr(self.tty_fd, termios.TCSADRAIN, self.saved_termios)
        with suppress(Exception):
            os.write(self.tty_fd, TERMINAL_RESET_SEQUENCE)


def create_terminal_guardian() -> TerminalGuardian:
    if os.name == "posix":
        return PosixTerminalGuardian()
    return TerminalGuardian()


def _termios() -> Any | None:
    try:
        import termios
    except ImportError:  # pragma: no cover - native Windows
        return None
    return termios


def _controlling_tty_fd() -> int | None:
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            continue
        if os.isatty(fd):
            return fd
    return None
