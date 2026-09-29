from __future__ import annotations

import json
import time

from opensquilla.gateway.stall_watchdog import GatewayStallWatchdog


def test_stall_watchdog_is_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_STALL_DIAGNOSTICS", raising=False)
    assert GatewayStallWatchdog.from_environment() is None


def test_stall_watchdog_samples_main_thread_without_locals(tmp_path) -> None:
    output = tmp_path / "stalls.jsonl"
    watchdog = GatewayStallWatchdog(
        output_path=output,
        threshold_s=0.25,
        sample_interval_s=0.05,
        stack_interval_s=0.1,
    )
    watchdog.start()
    try:
        # The event-loop heartbeat is intentionally not called.  A separate
        # watchdog thread must still capture the blocked main Python stack.
        time.sleep(0.55)
    finally:
        watchdog.stop()

    events = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    samples = [event for event in events if event["type"] == "stack_sample"]
    assert samples
    assert any(item["function"] == "test_stall_watchdog_samples_main_thread_without_locals"
               for item in samples[0]["stack"])
    assert all("locals" not in item for item in samples[0]["stack"])
    assert any(event["type"] == "stall_started" for event in events)

