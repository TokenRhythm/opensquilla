"""Narrow Codex-compatible attribution of command failure to the sandbox."""

from __future__ import annotations

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


def is_likely_sandbox_denied(result: SandboxResult) -> bool:
    """Match Codex's conservative output/signal heuristic plus backend notes."""

    backend_used = str(getattr(result, "backend_used", "sandbox"))
    if backend_used.strip().lower() in _UNSANDBOXED_BACKENDS:
        return False
    if result.backend_notes:
        return True
    if result.returncode == 0:
        return False
    combined = "\n".join((result.stderr, result.stdout)).lower()
    if any(keyword in combined for keyword in _DENIED_KEYWORDS):
        return True
    if result.returncode in _QUICK_REJECT_EXIT_CODES:
        return False
    sigsys = getattr(signal, "SIGSYS", None)
    return sigsys is not None and result.returncode == 128 + int(sigsys)


__all__ = ["is_likely_sandbox_denied"]
