"""Outer watchdog for the telemetry pilot, not a general subprocess framework.

The probe owns one Python process and SQLite threads, never descendant processes.
Kill the exact Popen handle on timeout; do not kill by process name or scan PIDs.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry_shutdown_probe.py"
ROOT = Path(__file__).resolve().parents[2]
READY = "TELEMETRY_PROBE_READY"


@dataclass(frozen=True)
class ProbeResult:
    returncode: int
    timed_out: str | None
    output: str


def run_shutdown_probe(
    directory: Path,
    *,
    mode: str = "close",
    fault: str = "none",
    startup_seconds: float = 90,
    execution_seconds: float = 30,
) -> ProbeResult:
    """Allow cold setup once, then bound the entire case INCLUDING teardown."""
    if not all(
        math.isfinite(value) and value > 0 for value in (startup_seconds, execution_seconds)
    ):
        raise ValueError("probe watchdog budgets must be finite and positive")
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "probe.log"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "src"), environment.get("PYTHONPATH", "")]
    )
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with log_path.open("wb") as log:
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, str(PROBE), str(directory), mode, fault],
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        timed_out = None
        phase = "startup"
        deadline = started + startup_seconds
        # A single READY transition may allocate the execution budget. No other
        # log line/heartbeat renews it. The absolute cap also bounds teardown.
        absolute_deadline = deadline + execution_seconds
        try:
            while process.poll() is None:
                output = log_path.read_text(encoding="utf-8", errors="replace")
                now = time.monotonic()
                if phase == "startup" and READY in output.splitlines():
                    phase = "execution/cleanup"
                    deadline = min(now + execution_seconds, absolute_deadline)
                if now >= deadline:
                    timed_out = phase
                    break
                time.sleep(0.05)  # Poll the child state, never infer readiness from sleep.
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
    return ProbeResult(
        returncode=process.returncode,
        timed_out=timed_out,
        output=log_path.read_text(encoding="utf-8", errors="replace"),
    )
