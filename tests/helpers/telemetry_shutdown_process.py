"""Telemetry entry point for the SQLite-only process watchdog."""

from pathlib import Path

from tests.helpers.sqlite_process_probe import ProbeResult, run_sqlite_probe

PROBE = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry_shutdown_probe.py"
READY = "TELEMETRY_PROBE_READY"


def run_shutdown_probe(
    directory: Path,
    *,
    mode: str = "close",
    fault: str = "none",
    startup_seconds: float = 90,
    execution_seconds: float = 30,
) -> ProbeResult:
    return run_sqlite_probe(
        directory, PROBE, (mode, fault), ready_signal=READY,
        startup_seconds=startup_seconds, execution_seconds=execution_seconds,
    )
