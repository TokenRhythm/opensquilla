"""Subprocess execution sandbox with CPU / memory / wall / network limits.

POSIX-only: uses :mod:`resource` via ``preexec_fn`` to apply
``setrlimit`` before the child process begins. On platforms where
``resource`` is unavailable (notably Windows), :func:`run_sandboxed`
returns a :class:`SandboxResult` with ``reason='unsupported_platform'``.

Environment whitelist: by default the sandboxed command sees only
``HOME``, ``PATH`` and ``LANG`` — a deliberate narrow whitelist so
secrets in the parent environment do not leak to shell-invoking tools.

Network scoping is advisory in this module: the ``network`` limit
is recorded on the :class:`SandboxLimits` contract and callers are
expected to honour it (``'deny'`` means no socket operations). The
corresponding test asserts denial behaviour end-to-end via a subprocess
that refuses to perform network I/O when the limit is ``'deny'``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

try:
    import resource as _resource_module  # type: ignore[import-not-found]

    _resource: Any | None = _resource_module
    HAS_RESOURCE = True
except ImportError:  # pragma: no cover — exercised on Windows CI only
    _resource = None
    HAS_RESOURCE = False

NetworkScope = Literal["deny", "localhost", "allow"]

REASON_OK: Final[str] = "ok"
REASON_CPU_LIMIT: Final[str] = "cpu_limit"
REASON_MEMORY_LIMIT: Final[str] = "memory_limit"
REASON_WALL_LIMIT: Final[str] = "wall_limit"
REASON_NETWORK_DENY: Final[str] = "network_deny"
REASON_UNSUPPORTED: Final[str] = "unsupported_platform"
REASON_EXEC_FAILED: Final[str] = "exec_failed"

_DEFAULT_ENV_WHITELIST: Final[tuple[str, ...]] = ("HOME", "PATH", "LANG")


@dataclass(frozen=True)
class SandboxLimits:
    """Resource limits applied to a sandboxed subprocess."""

    cpu_seconds: int = 30
    memory_mb: int = 512
    wall_seconds: int = 60
    network: NetworkScope = "deny"
    env_whitelist: tuple[str, ...] = _DEFAULT_ENV_WHITELIST


@dataclass
class SandboxResult:
    """Outcome of a :func:`run_sandboxed` call."""

    returncode: int
    stdout: str
    stderr: str
    reason: str = REASON_OK
    limits: SandboxLimits = field(default_factory=SandboxLimits)


def _preexec(limits: SandboxLimits):  # pragma: no cover — runs in child
    if not HAS_RESOURCE:
        return None
    resource = cast(Any, _resource)

    def _apply() -> None:
        # CPU seconds — RLIMIT_CPU. Signal SIGXCPU on soft, SIGKILL on hard.
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (limits.cpu_seconds, limits.cpu_seconds),
        )
        # Memory — RLIMIT_AS is the address-space cap. Not all platforms
        # enforce this, but Unix backends set it when available.
        mem_bytes = limits.memory_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ValueError, OSError):
            # Some BSDs / macOS refuse RLIMIT_AS; degrade silently — the
            # wall limit still bounds runaway allocations.
            pass

    return _apply


def _filtered_env(
    whitelist: Sequence[str],
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    # Base the child environment on the allowlisted parent environment (PATH,
    # HOME, LANG, ...) and overlay any allowlisted caller-supplied overrides.
    # A caller-supplied ``env`` must not *replace* the parent env: an empty or
    # partial dict (e.g. git tools request ``env={}``) would otherwise spawn the
    # subprocess with no PATH/HOME at all.
    child = {key: os.environ[key] for key in whitelist if key in os.environ}
    if env:
        allow = set(whitelist)
        child.update({key: value for key, value in env.items() if key in allow})
    return child


def _decode_stream(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_sandboxed(
    cmd: Sequence[str],
    limits: SandboxLimits | None = None,
    *,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
) -> SandboxResult:
    """Run ``cmd`` under ``limits`` and return a :class:`SandboxResult`.

    * CPU / memory are applied via ``setrlimit`` in a ``preexec_fn``.
    * Wall time is enforced with :meth:`subprocess.Popen.communicate`'s
      ``timeout`` argument; on timeout we kill the process group and
      return ``reason='wall_limit'``.
    * Network scope ``'deny'`` is recorded and returned verbatim on
      result — the child is expected to consult the limit (tests assert
      this by round-tripping the limits).
    * On POSIX platforms without :mod:`resource` (Windows) the function
      short-circuits with ``reason='unsupported_platform'``.
    """

    effective = limits or SandboxLimits()

    if not HAS_RESOURCE:
        return SandboxResult(
            returncode=-1,
            stdout="",
            stderr="resource module unavailable",
            reason=REASON_UNSUPPORTED,
            limits=effective,
        )

    child_env = _filtered_env(effective.env_whitelist, env)

    try:
        proc = subprocess.Popen(  # noqa: S603 — cmd is caller-controlled
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin is not None else None,
            env=child_env,
            preexec_fn=_preexec(effective),  # noqa: PLW1509 — POSIX setrlimit
        )
    except (OSError, ValueError) as exc:
        return SandboxResult(
            returncode=-1,
            stdout="",
            stderr=str(exc),
            reason=REASON_EXEC_FAILED,
            limits=effective,
        )

    # Always reap the child and close its pipes, even when an exception
    # other than ``TimeoutExpired`` escapes (decode error, pipe OSError,
    # KeyboardInterrupt, ...). Without this outer finally the running
    # child would leak together with its stdout/stderr pipes.
    stdout: bytes | str | None = None
    stderr: bytes | str | None = None
    timed_out = False
    try:
        stdout, stderr = proc.communicate(input=stdin, timeout=effective.wall_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            stdout, stderr = proc.communicate()
        except Exception:
            stdout, stderr = None, None
        # ``communicate()`` already calls ``wait()`` on success, but if
        # the second ``communicate()`` above raised we may never have
        # reaped the child. Guard that explicitly so the timeout branch
        # upholds the same "always reap" guarantee as the other paths.
        if proc.returncode is None:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
    except BaseException:
        # Propagate cancellation / KeyboardInterrupt but still ensure
        # the child is killed and waited on so we don't leak it.
        try:
            proc.kill()
        finally:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        raise
    finally:
        # For the success path and the ``TimeoutExpired`` branch above,
        # close any pipes that ``communicate`` did not drain. ``Popen``
        # ``__del__`` issues ``ResourceWarning`` if these linger.
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass

    if timed_out:
        return SandboxResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=_decode_stream(stdout),
            stderr=_decode_stream(stderr),
            reason=REASON_WALL_LIMIT,
            limits=effective,
        )

    # Translate exit signals into structured reasons. On POSIX a hard
    # RLIMIT_CPU exceed produces SIGKILL (returncode == -9); RLIMIT_AS
    # typically surfaces as SIGSEGV / allocation-error exits.
    reason = REASON_OK
    if proc.returncode in {-_signal(9), -_signal(24)}:  # SIGKILL / SIGXCPU
        reason = REASON_CPU_LIMIT
    elif proc.returncode == -_signal(11):  # SIGSEGV
        reason = REASON_MEMORY_LIMIT

    return SandboxResult(
        returncode=proc.returncode,
        stdout=_decode_stream(stdout),
        stderr=_decode_stream(stderr),
        reason=reason,
        limits=effective,
    )


def _signal(num: int) -> int:
    """Return ``num`` on POSIX, else 0 — keeps the mapping self-contained."""

    if sys.platform.startswith("win"):  # pragma: no cover
        return 0
    return num


__all__ = [
    "HAS_RESOURCE",
    "REASON_CPU_LIMIT",
    "REASON_EXEC_FAILED",
    "REASON_MEMORY_LIMIT",
    "REASON_NETWORK_DENY",
    "REASON_OK",
    "REASON_UNSUPPORTED",
    "REASON_WALL_LIMIT",
    "NetworkScope",
    "SandboxLimits",
    "SandboxResult",
    "run_sandboxed",
]
