"""Real-clock application-fixture measurements of production Gateway writer paths.

This uses the real WsConnection and FlowWindow with an Event-blocked socket
fixture. It does NOT use TCP, a browser, Electron, an ASGI server, kernel
backpressure, a remote network, physical sleep, or production user profiles.
Timeout constants are never patched. All payloads are synthetic.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import platform
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import structlog  # noqa: E402
from starlette.websockets import WebSocketState  # noqa: E402

from opensquilla.gateway import websocket as ws_module  # noqa: E402
from opensquilla.gateway.protocol import make_ok_res  # noqa: E402
from opensquilla.gateway.transport_flow import get_transport_budget  # noqa: E402

SCENARIOS = (
    "writer_timeout60", "recovery_credit_timeout30", "flow_pause30",
    "flow_pause20", "flow_pause13", "flow_pause5", "direct_timeout2",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_json(path: Path, value: Any) -> str:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(encoded)
    return digest(encoded)


class ControlledSocket:
    """Cooperatively cancellable send pause; no network or hidden timer scale."""

    client_state = WebSocketState.CONNECTED
    application_state = WebSocketState.CONNECTED

    def __init__(self, *, blocked: bool) -> None:
        self.started_at = time.monotonic()
        self.release = asyncio.Event()
        self.send_entered = asyncio.Event()
        self.closed = asyncio.Event()
        self.frames_sent = 0
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.timeline: list[dict[str, Any]] = []
        if not blocked:
            self.release.set()

    def note(self, phase: str, **fields: Any) -> None:
        self.timeline.append({
            "phase": phase, "elapsed_s": time.monotonic() - self.started_at,
            "at_utc": datetime.now(UTC).isoformat(), **fields,
        })

    async def send_text(self, text: str) -> None:
        self.note("send_enter", encoded_bytes=len(text.encode()))
        self.send_entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.note("send_cancelled")
            raise
        self.frames_sent += 1
        self.note("frame_sent", frame_number=self.frames_sent)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code, self.close_reason = code, reason
        self.client_state = self.application_state = WebSocketState.DISCONNECTED
        self.note("close", code=code, reason=reason)
        self.closed.set()


async def wait_frames(socket: ControlledSocket, count: int) -> None:
    async with asyncio.timeout(2):
        while socket.frames_sent < count:
            await asyncio.sleep(0.001)


async def trial(
    scenario: str, index: int, semaphore: asyncio.Semaphore,
    active: dict[str, Any],
) -> dict[str, Any]:
    async with semaphore:
        socket = ControlledSocket(blocked=scenario != "recovery_credit_timeout30")
        conn = ws_module.WsConnection(f"fixture-{scenario}-{index}", socket)  # type: ignore[arg-type]
        active[conn.conn_id] = conn
        result: dict[str, Any] = {
            "scenario": scenario, "trial": index,
            "fixture": "production-WsConnection-controlled-async-Event-socket",
            "topology": "application-fixture-no-network",
            "clock": "real-monotonic-unscaled", "started_at_utc": datetime.now(UTC).isoformat(),
        }
        writer: asyncio.Task[None] | None = None
        snapshots: list[dict[str, Any]] = []

        def sample(phase: str) -> None:
            snapshots.append({"phase": phase, **conn.transport_diagnostics()})

        try:
            if scenario == "direct_timeout2":
                try:
                    await conn.send_raw_text('{"type":"pong"}')
                    raise AssertionError("blocked direct send returned without TimeoutError")
                except TimeoutError:
                    socket.note("direct_timeout_returned")
                assert socket.close_code == 1011
                assert socket.close_reason == "direct_send_timeout"
                result["expected_boundary_s"] = 2
            else:
                conn._recovery_enabled = True
                conn._enable_flow()
                conn._start_writer(maxsize=512, enabled=True)
                writer = conn._writer_task
                assert writer is not None
                if scenario == "writer_timeout60":
                    await conn.send_raw_text('{"type":"pong"}')
                    await socket.send_entered.wait()
                    sample("blocked")
                    await asyncio.wait_for(socket.closed.wait(), timeout=70)
                    await asyncio.wait_for(asyncio.shield(writer), timeout=3)
                    assert socket.close_code == 1011
                    assert socket.close_reason == "writer_send_failed"
                    result["expected_boundary_s"] = 60
                elif scenario == "recovery_credit_timeout30":
                    transfer = conn.snapshot_registry().admit("synthetic", "r1", object())
                    payload = await transfer.create("synthetic", "r1", lambda: {
                        "stream_generation": "g1", "current_stream_seq": 0,
                        "task_id": None, "messages": [],
                    })
                    socket.note("credit_reserve")
                    receipt = conn.reserve_snapshot_delivery(
                        1024, "synthetic", payload["snapshot_id"], "r1",
                    )
                    await conn.send_res(make_ok_res("synthetic-snapshot", {
                        **payload, "delivery": receipt,
                    }))
                    await wait_frames(socket, 1)
                    sample("sent_unacknowledged")
                    await asyncio.wait_for(socket.closed.wait(), timeout=40)
                    assert socket.close_code == 1013
                    assert socket.close_reason == "recovery_credit_timeout"
                    result["expected_boundary_s"] = 30
                else:
                    pause = int(scenario.removeprefix("flow_pause"))
                    await conn.send_event("session.event.text_delta", {
                        "session_key": "synthetic", "stream_generation": "g1",
                        "stream_seq": 1, "chunk": "synthetic",
                    })
                    await conn.send_raw_text('{"type":"pong"}')
                    await socket.send_entered.wait()
                    sample("blocked")
                    assert conn._flow is not None and len(conn._flow.deliveries) == 1
                    await asyncio.sleep(pause)
                    socket.note("fixture_release", requested_pause_s=pause)
                    sample("before_release")
                    socket.release.set()
                    await wait_frames(socket, 2)
                    sample("sent_unacknowledged")
                    conn._flow.acknowledge(conn._flow.epoch, conn._flow.next_id - 1)
                    socket.note("acknowledged")
                    sample("acknowledged")
                    assert conn._transport_bytes == 0
                    assert socket.close_code is None and not conn._closing
                    result["requested_pause_s"] = pause
            result["passed"] = True
        except Exception as error:
            result["passed"] = False
            result["failure"] = f"{type(error).__name__}: {error}"
        finally:
            socket.release.set()
            await conn._stop_writer()
            if writer is not None:
                await asyncio.gather(writer, return_exceptions=True)
            conn._cleanup_transport()
            sample("cleanup")
            result["cleanup_connection_bytes"] = conn._transport_bytes
            result["cleanup_global_budget_bytes"] = get_transport_budget().used
            result["cleanup_other_connections_bytes"] = sum(
                other._transport_bytes for other in active.values() if other is not conn
            )
            result["cleanup_global_budget_balanced"] = (
                result["cleanup_global_budget_bytes"] == result["cleanup_other_connections_bytes"]
            )
            if conn._transport_bytes:
                result["passed"] = False
                result["failure"] = "connection transport bytes remain after cleanup"
            if not result["cleanup_global_budget_balanced"]:
                result["passed"] = False
                result["failure"] = "global budget differs from remaining active connections"
            result.update({
                "elapsed_s": time.monotonic() - socket.started_at,
                "close_code": socket.close_code, "close_reason": socket.close_reason,
                "frames_sent": socket.frames_sent, "timeline": socket.timeline,
                "diagnostics": snapshots,
            })
            close = next((event for event in socket.timeline if event["phase"] == "close"), None)
            sent = next(
                (event for event in socket.timeline if event["phase"] == "frame_sent"), None,
            )
            cancelled = next(
                (event for event in socket.timeline if event["phase"] == "send_cancelled"), None,
            )
            result["close_elapsed_s"] = close["elapsed_s"] if close else None
            result["first_frame_elapsed_s"] = sent["elapsed_s"] if sent else None
            result["send_cancelled_elapsed_s"] = cancelled["elapsed_s"] if cancelled else None
            del active[conn.conn_id]
        return result


def statistics(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_s": (ordered[(len(ordered) - 1) // 2] + ordered[len(ordered) // 2]) / 2,
        "p95_s": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "minimum_s": ordered[0], "maximum_s": ordered[-1],
        "range_s": ordered[-1] - ordered[0],
    }


async def run(output: Path, repeats: int, concurrency: int) -> int:
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Evidence output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    snapshots = output / "source-snapshot"
    snapshots.mkdir()
    sources: dict[str, str] = {}
    for relative in (
        "scripts/gateway_writer_real_clock.py", "src/opensquilla/gateway/websocket.py",
        "src/opensquilla/gateway/transport_flow.py", "src/opensquilla/gateway/snapshot_transfer.py",
        "src/opensquilla/gateway/boot.py", "src/opensquilla/observability/log_privacy.py",
        "pyproject.toml", "uv.lock",
    ):
        raw = (ROOT / relative).read_bytes()
        sources[relative] = digest(raw)
        (snapshots / relative.replace("/", "__")).write_bytes(raw)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    manifest = {
        "schema": 1, "base_sha": head, "working_tree_candidate": True,
        "python": sys.version, "platform": platform.platform(), "source_sha256": sources,
        "clock": "real-monotonic-unscaled", "repeats_per_scenario": repeats,
        "clock_implementation": time.get_clock_info("monotonic").implementation,
        "clock_resolution_s": time.get_clock_info("monotonic").resolution,
        "max_concurrent_trials": concurrency,
        "fixture": "production-WsConnection-controlled-async-Event-socket",
        "scope": "application fixture; not TCP/kernel/browser/Electron/physical-sleep evidence",
        "logging": "Gateway stdlib privacy bridge; NullHandler sink, no log file I/O",
        "backend_timeout_constants_seconds": {
            "direct_send": ws_module._DIRECT_SEND_TIMEOUT_SECONDS,
            "writer_send": ws_module._WRITER_SEND_TIMEOUT_SECONDS,
            "recovery_credit": ws_module.RECOVERY_CREDIT_SECONDS,
        },
        "started_at_utc": datetime.now(UTC).isoformat(),
    }
    assert manifest["backend_timeout_constants_seconds"] == {
        "direct_send": 2.0, "writer_send": 60.0, "recovery_credit": 30.0,
    }
    assert get_transport_budget().used == 0
    assert not ws_module._WRITER_TASKS and not ws_module._SOCKET_CLOSE_TASKS
    save_json(output / "manifest.json", manifest)
    semaphore = asyncio.Semaphore(concurrency)
    active: dict[str, Any] = {}
    peaks = {"writer_tasks": 0, "close_tasks": 0, "global_budget_bytes": 0}

    async def observe() -> None:
        while True:
            peaks["writer_tasks"] = max(peaks["writer_tasks"], len(ws_module._WRITER_TASKS))
            peaks["close_tasks"] = max(peaks["close_tasks"], len(ws_module._SOCKET_CLOSE_TASKS))
            peaks["global_budget_bytes"] = max(
                peaks["global_budget_bytes"], get_transport_budget().used,
            )
            await asyncio.sleep(0.01)

    observer = asyncio.create_task(observe())
    started = time.monotonic()
    try:
        results = await asyncio.gather(*[
            trial(scenario, index + 1, semaphore, active)
            for scenario in SCENARIOS for index in range(repeats)
        ])
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
    await asyncio.sleep(0)
    files: dict[str, str] = {}
    for result in results:
        filename = f"{result['scenario']}-{result['trial']:02d}.json"
        files[filename] = save_json(output / filename, result)
    cells: dict[str, Any] = {}
    for scenario in SCENARIOS:
        entries = [result for result in results if result["scenario"] == scenario]
        measurement = (
            "first_frame_elapsed_s" if scenario.startswith("flow_pause") else "close_elapsed_s"
        )
        timings = [entry[measurement] for entry in entries if entry[measurement] is not None]
        cells[scenario] = {
            "trials": len(entries), "passed": sum(entry["passed"] for entry in entries),
            "failures": [entry.get("failure") for entry in entries if not entry["passed"]],
            "measurement": measurement, **(statistics(timings) if timings else {}),
        }
        cancellations = [entry["send_cancelled_elapsed_s"] for entry in entries
                         if entry["send_cancelled_elapsed_s"] is not None]
        if cancellations:
            cells[scenario]["send_cancelled"] = statistics(cancellations)
    unchanged = all(
        digest((ROOT / relative).read_bytes()) == sha for relative, sha in sources.items()
    )
    result_summary = {
        "cells": cells, "elapsed_s": time.monotonic() - started,
        "global_budget_after": get_transport_budget().used,
        "writer_tasks_after": len(ws_module._WRITER_TASKS),
        "close_tasks_after": len(ws_module._SOCKET_CLOSE_TASKS),
        "sampled_peaks": peaks, "peak_sampling_interval_s": 0.01,
        "source_unchanged_during_run": unchanged, "trial_sha256": files,
    }
    passed = (
        all(result["passed"] for result in results) and unchanged
        and result_summary["global_budget_after"] == 0
        and result_summary["writer_tasks_after"] == 0
        and result_summary["close_tasks_after"] == 0
    )
    result_summary["passed"] = passed
    summary_sha = save_json(output / "results.json", result_summary)
    print(json.dumps({"output": str(output), "summary_sha256": summary_sha,
                      "passed": passed, "cells": cells}, indent=2))
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Empty evidence directory; default: a new system temporary directory"
        ),
    )
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=96)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 1000 or not 1 <= args.concurrency <= 96:
        parser.error("repeats must be 1..1000; concurrency must be 1..96")
    # Use Gateway's privacy processor, not structlog's default rich traceback
    # renderer, which can add synchronous work unrelated to deployed Gateway.
    from opensquilla.gateway.boot import _bridge_structlog_to_stdlib

    structlog.reset_defaults()
    _bridge_structlog_to_stdlib()
    gateway_logger = logging.getLogger("opensquilla")
    gateway_logger.handlers = [logging.NullHandler()]
    gateway_logger.setLevel(logging.DEBUG)
    gateway_logger.propagate = False
    output = args.output or Path(
        tempfile.mkdtemp(prefix="opensquilla-gateway-writer-")
    )
    return asyncio.run(run(output.resolve(), args.repeats, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
