"""Real SQLite + real asyncio timer probe, supervised by an external watchdog.

Only HTTP is simulated. The commit barrier delays a REAL transaction; it does
not replace the database, consent lock, deadline, cancellation or close path.
Fault modes are negative controls: a green harness must still reject them.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from opensquilla.telemetry import runtime as runtime_module
from opensquilla.telemetry.consent import TelemetryScope
from opensquilla.telemetry.outbox import TelemetryOutbox
from opensquilla.telemetry.recorder import RecordStatus
from opensquilla.telemetry.runtime import ScopedTelemetryRuntime
from opensquilla.telemetry.uploader import TelemetryUploader
from tests.helpers.telemetry_runtime import runtime_config, turn_event
from tests.helpers.telemetry_shutdown_process import READY


async def wait_event(event: asyncio.Event, phase: str, seconds: float = 10) -> None:
    print(f"phase={phase}", flush=True)
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except TimeoutError as exc:
        raise AssertionError(f"{phase} did not become ready") from exc


async def drain(tasks: list[asyncio.Task], *, cancel: bool) -> None:
    if cancel:
        for task in tasks:
            if not task.done():
                task.cancel()
    if not tasks:
        return
    # These are test completion/cleanup guards, not product latency promises.
    done, pending = await asyncio.wait(tasks, timeout=5 if cancel else 10)
    for task in done:
        if not task.cancelled():
            task.result()
    assert not pending, "cleanup did not finish; external watchdog owns final termination"


async def shutdown_case(directory: Path, mode: str, fault: str) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    commit_entered = asyncio.Event()
    release_commit = asyncio.Event()
    requests = []
    tasks: list[asyncio.Task] = []

    async def stalled(request):
        requests.append(request)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(stalled)) as client:
        with ExitStack() as patches:
            patches.enter_context(
                patch.object(
                    runtime_module,
                    "TelemetryUploader",
                    lambda *args, **kwargs: TelemetryUploader(*args, **kwargs, http_client=client),
                )
            )
            runtime = ScopedTelemetryRuntime(
                config=runtime_config(directory),
                upload_interval_seconds=60,
                env={},
            )
            try:
                assert (await runtime.record(turn_event(1))).status is RecordStatus.RECORDED
                await runtime.start()
                await wait_event(entered, "upload-entered")
                scoped = runtime._scopes[TelemetryScope.RELIABILITY]
                commit = scoped.outbox._connection.commit

                async def delayed_commit():
                    # Reaching here proves that cancellation released the
                    # consent lock and the accepted producer reached SQLite.
                    commit_entered.set()
                    await release_commit.wait()
                    await commit()

                patches.enter_context(
                    patch.object(scoped.outbox._connection, "commit", delayed_commit)
                )
                patches.enter_context(
                    patch.object(runtime_module, "SHUTDOWN_UPLOAD_TIMEOUT_SECONDS", 0.05)
                )

                if fault == "no-cancel":

                    async def missing_guard(task, _deadline):
                        await task

                    patches.enter_context(
                        patch.object(runtime, "_finish_upload_loop", missing_guard)
                    )
                elif fault == "drop-record":

                    async def lost_record(_event, **_kwargs):
                        commit_entered.set()
                        await release_commit.wait()
                        return SimpleNamespace(status=RecordStatus.RECORDED)

                    patches.enter_context(patch.object(runtime, "record", lost_record))

                print(READY, flush=True)
                if mode == "prepare":
                    runtime.prepare_shutdown()
                    deadline = runtime._shutdown_deadline
                    runtime.prepare_shutdown()
                    assert runtime._shutdown_deadline == deadline
                    recording = asyncio.create_task(runtime.record(turn_event(2)))
                    tasks.append(recording)
                else:
                    runtime.record_background(turn_event(2))
                    tasks.extend(runtime._record_tasks)
                    tasks.append(asyncio.create_task(runtime.close()))

                # Preserve the existing one-second real cancellation check.
                # No cold database setup or commit is charged to this bound.
                await wait_event(cancelled, "upload-cancelled", seconds=1)
                await wait_event(commit_entered, "commit-entered")
                assert not any(task.done() for task in tasks), "accepted work was not drained"
                assert len(requests) == 1
                release_commit.set()
                print("phase=durable-drain", flush=True)
                await drain(tasks, cancel=False)
                if mode == "prepare":
                    assert recording.result().status is RecordStatus.RECORDED
                    runtime.record_background(turn_event(3))
                    await drain([asyncio.create_task(runtime.close())], cancel=False)
                assert runtime.opened_scopes == frozenset()
            finally:
                print("phase=cleanup", flush=True)
                release_commit.set()
                await drain(tasks, cancel=True)
                await drain([asyncio.create_task(runtime.close(flush=False))], cancel=False)

    print("phase=reopen", flush=True)
    outbox = await TelemetryOutbox.open(directory, TelemetryScope.RELIABILITY)
    try:
        stats = await outbox.stats()
        expected = 3 if mode == "prepare" else 2
        assert stats.pending_events == expected, "accepted records were lost"
        assert stats.leased_events == 1, "unacknowledged lease was not preserved"
    finally:
        await outbox.close()
    print("TELEMETRY_PROBE_PASSED", flush=True)


async def cleanup_hang() -> None:
    async def stubborn():
        try:
            await asyncio.Event().wait()
        finally:
            print("phase=cleanup-blocked", flush=True)
            print(READY, flush=True)
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    continue

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)  # Let the owned task enter its try/finally.
    task.cancel()
    await task


if __name__ == "__main__":
    directory, mode, fault = sys.argv[1:]
    assert mode in {"close", "prepare"}
    assert fault in {"none", "no-cancel", "drop-record", "cleanup-hang"}
    asyncio.run(
        cleanup_hang() if fault == "cleanup-hang" else shutdown_case(Path(directory), mode, fault)
    )
