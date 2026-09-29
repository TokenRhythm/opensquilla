"""Opt-in, local-only early startup timing; never part of telemetry.

Set OPENSQUILLA_STARTUP_TIMING=1 in the launching process environment. The
decision is captured before CLI dotenv loading. Output goes only to stderr,
using fixed stages and numbers so no profile, arguments or exception can leak.
This module deliberately imports no application services or logging framework.
Durations use one process-local monotonic clock; at_unix_ms is the emission
time for correlation with the Desktop log, not a shared monotonic epoch. An
unfinished start record means the operation did not reach its success boundary.
"""

from __future__ import annotations

import os
import sys
from time import monotonic_ns as _monotonic_ns
from time import time_ns as _wall_ns

_ENABLED = os.environ.get("OPENSQUILLA_STARTUP_TIMING") == "1"
_STAGES = frozenset({
    "diagnostic_setup",
    "frozen_hook_imports",
    "frozen_ca_trust",
    "cli_import",
    "cli_command_imports",
    "gateway_command_import",
    "gateway_boot_import",
    "profile_lock",
    "profile_inspect",
    "legacy_lock",
    "gateway_run_enter",
})


def _emit(stage: str, status: str, at_ns: int, started_ns: int) -> None:
    # Only fixed strings and integral measurements enter the wire representation.
    # An unavailable/closed stderr or a failed clock must not change startup.
    try:
        if stage not in _STAGES or status not in {"start", "complete", "failed"}:
            return
        elapsed_us = max(0, (at_ns - started_ns) // 1000)
        wall_ms = _wall_ns() // 1_000_000
        sys.stderr.write(
            '{"event":"gateway.startup_early","stage":"' + stage
            + '","status":"' + status
            + f'","pid":{os.getpid()},"at_unix_ms":{wall_ms},'
            + f'"monotonic_ns":{at_ns},"duration_us":{elapsed_us}}}\n'
        )
        sys.stderr.flush()
    except Exception:
        pass


def startup_phase_start(stage: str) -> int | None:
    """Emit a fixed start boundary and return its process-local clock value."""
    if not _ENABLED or stage not in _STAGES:
        return None
    try:
        started_ns = _monotonic_ns()
        _emit(stage, "start", started_ns, started_ns)
        return started_ns
    except Exception:
        return None


def startup_phase_end(
    stage: str, started_ns: int | None, *, failed: bool = False,
) -> None:
    """Record an end boundary without inspecting the result or exception."""
    if not _ENABLED or started_ns is None or stage not in _STAGES:
        return
    try:
        _emit(stage, "failed" if failed else "complete", _monotonic_ns(), started_ns)
    except Exception:
        pass
