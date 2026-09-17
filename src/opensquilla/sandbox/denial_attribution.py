"""Narrow Codex-compatible attribution of command failure to the sandbox."""

from __future__ import annotations

import re
import signal

from opensquilla.sandbox.types import SandboxResult

_DENIED_KEYWORDS = (
    "operation not permitted",
    "permission denied",
    "read-only file system",
    "seccomp",
    "sandbox",
    "landlock",
    "failed to write file",
)
_QUICK_REJECT_EXIT_CODES = frozenset({2, 126, 127})
_UNSANDBOXED_BACKENDS = frozenset({"", "noop", "none", "host"})
_PYTHON_TRACEBACK_LOCATION = re.compile(
    r'  File "[^"\r\n]+", line [1-9][0-9]*(?:, in (?:\w+|<\w+>))?'
)


def is_likely_sandbox_denied(result: SandboxResult) -> bool:
    """Match Codex's conservative output/signal heuristic plus backend notes."""

    backend_used = str(getattr(result, "backend_used", "sandbox"))
    if backend_used.strip().lower() in _UNSANDBOXED_BACKENDS:
        return False
    if result.backend_notes:
        return True
    if result.returncode == 0:
        return False
    # Frozen Python tracebacks include internal paths such as sandbox/*.py.
    # A frame location is not denial evidence; keep source and error text, and
    # leave the public stderr and all stdout unchanged.
    stderr = "\n".join(
        line
        for line in result.stderr.splitlines()
        if not _PYTHON_TRACEBACK_LOCATION.fullmatch(line)
    )
    combined = "\n".join((stderr, result.stdout)).lower()
    if any(keyword in combined for keyword in _DENIED_KEYWORDS):
        return True
    if result.returncode in _QUICK_REJECT_EXIT_CODES:
        return False
    sigsys = getattr(signal, "SIGSYS", None)
    return sigsys is not None and result.returncode == 128 + int(sigsys)


__all__ = ["is_likely_sandbox_denied"]
