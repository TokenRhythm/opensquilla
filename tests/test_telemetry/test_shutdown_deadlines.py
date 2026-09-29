"""Deadline arithmetic is independent of disk speed and timer delivery.

The real timer/SQLite coupling remains in test_client_runtime's isolated probes.
Only this module's view of the loop is replaced, never the running event loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.telemetry import runtime as runtime_module
from opensquilla.telemetry.runtime import ScopedTelemetryRuntime
from tests.helpers.telemetry_runtime import runtime_config
from tests.helpers.telemetry_shutdown_process import run_shutdown_probe


@pytest.mark.parametrize("budget", [0, -1, float("inf"), float("nan")])
@pytest.mark.parametrize("phase", ["startup_seconds", "execution_seconds"])
def test_shutdown_watchdog_rejects_unbounded_budgets(
    tmp_path: Path, budget: float, phase: str
) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        run_shutdown_probe(tmp_path, **{phase: budget})
    assert not (tmp_path / "probe.log").exists()


@pytest.mark.parametrize("start", [1000.0, 1_000_000.125])
def test_shutdown_deadline_uses_exact_start_and_never_renews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: float,
) -> None:
    now = start
    loop = SimpleNamespace(time=lambda: now)
    guards = []
    upload = object()
    guard = object()
    runtime = ScopedTelemetryRuntime(config=runtime_config(tmp_path), env={})
    runtime._upload_task = upload

    def finish_upload(task, deadline):
        guards.append((task, deadline))
        return guard

    monkeypatch.setattr(runtime, "_finish_upload_loop", finish_upload)
    monkeypatch.setattr(
        runtime_module,
        "asyncio",
        SimpleNamespace(
            **(
                vars(asyncio)
                | {
                    "get_running_loop": lambda: loop,
                    "create_task": lambda coroutine: coroutine,
                }
            )
        ),
    )
    expected = start + runtime_module.SHUTDOWN_UPLOAD_TIMEOUT_SECONDS
    runtime.prepare_shutdown()
    assert runtime._shutdown_deadline == expected
    assert guards == [(upload, expected)]
    assert runtime._shutdown_upload_guard is guard
    assert runtime._upload_stop.is_set()

    # Both before and AFTER expiration, repeat calls must keep the original
    # deadline/guard. A now-relative upper bound would miss an early deadline.
    for now in (start + 0.01, expected + 100):
        runtime.prepare_shutdown()
        assert runtime._shutdown_deadline == expected
        assert guards == [(upload, expected)]
        assert runtime._shutdown_upload_guard is guard


@pytest.mark.parametrize("offset", [-1.0, 0.0])
async def test_real_shutdown_guard_honors_already_expired_deadline(
    tmp_path: Path,
    offset: float,
) -> None:
    runtime = ScopedTelemetryRuntime(config=runtime_config(tmp_path), env={})
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def stalled():
        entered.set()
        try:
            await release.wait()
        finally:
            cancelled.set()

    upload = asyncio.create_task(stalled())
    guard = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        guard = asyncio.create_task(
            runtime._finish_upload_loop(
                upload,
                asyncio.get_running_loop().time() + offset,
            )
        )
        done, pending = await asyncio.wait({guard}, timeout=10)
        assert not pending, "expired shutdown guard did not finish"
        for task in done:
            task.result()
        assert cancelled.is_set()
        assert upload.cancelled()
    finally:
        release.set()
        tasks = {upload} | ({guard} if guard is not None else set())
        for task in tasks:
            if not task.done():
                task.cancel()
        done, pending = await asyncio.wait(tasks, timeout=5)
        for task in done:
            if not task.cancelled():
                task.result()
        assert not pending, "deadline test cleanup did not finish"
