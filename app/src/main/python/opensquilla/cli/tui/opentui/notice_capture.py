"""Capture stray console output as OpenTUI host notices.

In the OpenTUI backend the host owns the terminal (alternate screen) and is
driven entirely over the fd IPC bridge. Slash-command handlers, however, still
emit their notices with Rich ``console.print``, which writes to ``sys.stdout``.
With nothing intercepting it that output bleeds raw onto the host's screen —
overlapping the composer and clipping (``compact:`` rendered as ``act:``).

This module installs a ``sys.stdout``/``sys.stderr`` replacement (OpenTUI mode
only) that forwards each complete console line to a sink, which the runtime ships
to the host as a ``notice.write`` message. ``isatty()`` returns ``True`` so Rich
keeps emitting its ANSI styling, which the host parses back into theme colors.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable, Iterator
from typing import TextIO

# A spinner/progress writer that repaints with \r and never emits \n would
# otherwise accumulate frames forever; anything past this size is forwarded
# as a notice instead of growing without bound.
_MAX_BUFFER_CHARS = 8192
# An unterminated trailing escape longer than this is garbage rather than a
# sequence split across writes, so it is forwarded instead of held back.
_MAX_ESCAPE_HOLDBACK = 32

_real_stderr: TextIO | None = None


def real_stderr() -> TextIO:
    """Return the terminal's stderr even while a notice capture is installed.

    While a capture is active ``sys.stderr`` is the forwarding stream whose sink
    dies with the host bridge; crash and teardown diagnostics must instead reach
    the stream the capture replaced, which is kept accessible here.
    """
    if _real_stderr is not None:
        return _real_stderr
    return sys.stderr


class NoticeForwardingStream:
    """A text stream that forwards complete lines to ``forward`` (not a terminal).

    Partial writes are buffered until a newline (or an explicit ``flush``). The
    sink is wrapped so a failing forward can never break the program that is just
    trying to print.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, forward: Callable[[str], None]) -> None:
        self._forward = forward
        self._buffer = ""

    def write(self, data: str) -> int:
        if not data:
            return 0
        text = data if isinstance(data, str) else str(data)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(_latest_cr_frame(line))
        self._collapse_cr_frames()
        self._spill_oversized_buffer()
        return len(text)

    def _collapse_cr_frames(self) -> None:
        # \r-repaint output (spinners, progress bars) may never see a newline;
        # keep only the latest frame so superseded repaints neither grow the
        # buffer nor concatenate into one garbled notice later. A trailing \r
        # stays buffered — it may be the first half of a \r\n split across
        # writes — and the complete frame before it is kept either way.
        trailing_cr = self._buffer.endswith("\r")
        body = self._buffer[:-1] if trailing_cr else self._buffer
        cr = body.rfind("\r")
        if cr >= 0:
            body = body[cr + 1 :]
        self._buffer = body + ("\r" if trailing_cr else "")

    def _spill_oversized_buffer(self) -> None:
        if len(self._buffer) <= _MAX_BUFFER_CHARS:
            return
        emit, keep = _split_trailing_escape(self._buffer)
        if not emit:
            emit, keep = self._buffer, ""
        self._buffer = keep
        self._emit(emit)

    def _emit(self, line: str) -> None:
        with contextlib.suppress(Exception):
            self._forward(line)

    def flush(self) -> None:
        if not self._buffer:
            return
        emit, keep = _split_trailing_escape(_latest_cr_frame(self._buffer))
        self._buffer = keep
        if emit:
            self._emit(emit)

    def isatty(self) -> bool:
        # Make Rich emit ANSI styling so the host can recolor by severity.
        return True

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def fileno(self) -> int:
        # Not backed by a real descriptor; callers (e.g. Rich size probing) must
        # fall back to a default width rather than touching a terminal.
        raise OSError("NoticeForwardingStream has no fileno")


def _latest_cr_frame(line: str) -> str:
    # "frame1\rframe2" repaints one row, so only the last frame survives. A \r
    # directly before the line end is Windows-style \r\n, not a repaint.
    if line.endswith("\r"):
        line = line[:-1]
    cr = line.rfind("\r")
    if cr >= 0:
        return line[cr + 1 :]
    return line


def _split_trailing_escape(text: str) -> tuple[str, str]:
    """Split off an unterminated trailing ANSI escape sequence.

    The host strips only complete escape sequences; a fragment cut mid-sequence
    would render as raw control bytes, so it is held back until the rest of the
    sequence arrives (or silently dropped on the final flush).
    """
    esc = text.rfind("\x1b")
    if esc < 0:
        return text, ""
    tail = text[esc:]
    if len(tail) > _MAX_ESCAPE_HOLDBACK or _is_complete_escape(tail):
        return text, ""
    return text[:esc], tail


def _is_complete_escape(seq: str) -> bool:
    if len(seq) < 2:
        return False
    kind = seq[1]
    if kind == "[":
        # CSI: terminated by a final byte in the @-~ range.
        return any("\x40" <= char <= "\x7e" for char in seq[2:])
    if kind == "]":
        # OSC: terminated by BEL. The ESC-\ (ST) form would itself contain the
        # later ESC, so it can never be the tail located by rfind above.
        return "\x07" in seq
    return True


@contextlib.contextmanager
def capture_stdout_as_notices(forward: Callable[[str], None]) -> Iterator[None]:
    """Redirect ``sys.stdout``/``sys.stderr`` to a notice-forwarding stream.

    Restores the originals on exit (always), flushing any buffered partial line
    first so a trailing prompt fragment is not silently dropped. The replaced
    stderr stays reachable through :func:`real_stderr` so teardown/crash
    diagnostics can bypass the capture once the host bridge is gone.
    """
    global _real_stderr
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    saved_real_stderr = _real_stderr
    stream = NoticeForwardingStream(forward)
    sys.stdout = stream
    sys.stderr = stream
    _real_stderr = original_stderr
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            stream.flush()
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        _real_stderr = saved_real_stderr
