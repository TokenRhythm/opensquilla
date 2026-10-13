"""Outer watchdog for probes owning one Python process and SQLite threads.

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

ROOT = Path(__file__).resolve().parents[2]
READY = "SQLITE_PROBE_READY"


@dataclass(frozen=True)
class ProbeResult:
    returncode: int
    timed_out: str | None
    output: str
    pid: int


def run_sqlite_probe(
    directory: Path,
    probe: Path,
    arguments: tuple[str, ...] = (),
    *,
    ready_signal: str = READY,
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
    interpreter = sys.executable
    if os.name == "nt":
        # A Windows venv python.exe is a redirector with a child process. Start
        # the actual interpreter so this Popen handle owns the SQLite threads.
        # CPython's launcher variable preserves venv prefix/site-packages.
        interpreter = sys._base_executable
        environment["__PYVENV_LAUNCHER__"] = sys.executable
    with log_path.open("wb") as log:
        started = time.monotonic()
        process = subprocess.Popen(
            [interpreter, str(probe), str(directory), *arguments],
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
                if phase == "startup" and ready_signal in output.splitlines():
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
        pid=process.pid,
    )
