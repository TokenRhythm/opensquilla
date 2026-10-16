"""Real restart recovery with observed state barriers and a process watchdog.

Only the parent bounds execution and cleanup. The 2.1s commit delay is a test
design control, not evidence about the cause of any historical CI timeout.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from opensquilla.gateway.task_runtime import TaskDependencyError, TaskRuntime
from opensquilla.session.models import AgentTaskStatus
from tests.helpers.sqlite_process_probe import READY
from tests.test_gateway.test_steer_restart_deferred import KEY, MESSAGE_ID, _open_restarted


async def observed_recovery_wait(
    worker: asyncio.Task[Any], *events: asyncio.Event,
) -> None:
    """Wait for a semantic observation, surfacing the original worker failure."""
    observations = [asyncio.create_task(event.wait()) for event in events]
    try:
        done, _ = await asyncio.wait([worker, *observations], return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker
            raise AssertionError("recovery worker exited before its notification wait")
    finally:
        for observation in observations:
            observation.cancel()
        await asyncio.gather(*observations, return_exceptions=True)


async def terminal_record(
    runtime: TaskRuntime, task_id: str, drivers: dict[str, asyncio.Task[Any]],
) -> Any:
    """Observe the execution driver as well as done; a recovery worker is neither."""
    task = runtime._tasks.get(task_id)
    # activate() assigns the driver before returning, even when _execute has
    # not had its first step. Retain the observer reference after task removal.
    driver = drivers.get(task_id) or (task.asyncio_task if task else None)
    assert driver is not None, "activated restart task had no observable execution driver"
    completion = asyncio.create_task(runtime.wait(task_id))
    try:
        done, _ = await asyncio.wait([completion, driver], return_when=asyncio.FIRST_COMPLETED)
        if driver in done:
            await driver  # Propagate its original exception, not a later timeout.
        record = await completion
        await driver
        return record
    finally:
        if not completion.done():
            completion.cancel()
        await asyncio.gather(completion, return_exceptions=True)


async def scenario(directory: Path, fault: str) -> None:
    print("phase=restart-storage-setup", flush=True)
    storage = await _open_restarted(directory / "sessions.db", resumed=True)
    services = {"desktop_browser": {"status": "starting"}}
    runs: list[Any] = []
    attempts = 0
    reservations: list[Any] = []
    drivers: dict[str, asyncio.Task[Any]] = {}
    initial_wait = asyncio.Event()
    abort_returned = asyncio.Event()
    idle_after_abort = asyncio.Event()
    unexpected_retry = asyncio.Event()
    worker: asyncio.Task[Any] | None = None
    resume_notified = False

    async def handler(run: Any) -> None:
        runs.append(run)

    runtime = TaskRuntime(storage=storage, turn_handler=handler, service_snapshot=lambda: services)
    notification = runtime._steer_recovery_changed
    original_wait = notification.wait
    original_abort = runtime.abort_reservation
    original_restore = runtime._restore_durable_accepted_model_routing
    original_signal = runtime._signal_driver_state_changed
    original_terminal = runtime._persist_terminal_update
    original_execute = runtime._execute
    original_dispatch = runtime._run_turn_handler_with_write_lock_bypass

    async def observe_execute(task: Any) -> None:
        driver = asyncio.current_task()
        assert driver is not None
        drivers[task.task_id] = driver
        await original_execute(task)

    async def observe_dispatch(*args: Any, **kwargs: Any) -> None:
        await original_dispatch(*args, **kwargs)
        if fault == "duplicate-dispatch":
            print("fault=duplicate-dispatch", flush=True)
            # Actually dispatch the original handler twice.
            await original_dispatch(*args, **kwargs)

    async def observe_notification_wait() -> bool:
        if asyncio.current_task() is worker and not notification.is_set():
            if attempts == 0:
                initial_wait.set()
            elif abort_returned.is_set() and not resume_notified:
                # No await between observation and entering the ORIGINAL Event
                # wait. The observer wakes only after that coroutine suspends.
                idle_after_abort.set()
        return await original_wait()

    async def observe_abort(reservation: Any) -> None:
        reservations.append(reservation)
        if fault != "leak-reservation":
            await original_abort(reservation)
        else:
            print("fault=leak-reservation", flush=True)
        abort_returned.set()

    def signal_driver_changed() -> None:
        original_signal()
        if fault == "self-retry" and asyncio.current_task() is worker:
            # Recreate the broken owner-filter boundary: own reserve/abort is
            # incorrectly treated as new capacity, so the real worker retries.
            notification.set()
            print("fault=self-retry", flush=True)

    async def fail_after_reservation(*_args: Any) -> None:
        nonlocal attempts
        attempts += 1
        assert runtime._resident_count == 1
        if attempts > 1:
            unexpected_retry.set()
        raise TaskDependencyError(
            session_key=KEY, kind="DEPENDENCY_STARTING", services=("desktop_browser",),
        )

    async def observe_terminal(*args: Any, **kwargs: Any) -> bool:
        print("phase=terminal-persistence", flush=True)
        if fault == "hang-terminal":
            print("phase=terminal-persistence-blocked", flush=True)
            # The short watchdog starts ONLY at the faulty boundary, not while
            # a slow runner is still setting up/recovering its real database.
            print("STEER_TERMINAL_HANG_ENTERED", flush=True)
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    continue
        if fault == "slow-terminal":
            original_commit = storage.conn.commit
            commits = 0

            async def delayed_commit() -> None:
                nonlocal commits
                commits += 1
                print("terminal_commit_delay=2.1", flush=True)
                await asyncio.sleep(2.1)  # Controlled latency, never a readiness signal.
                await original_commit()

            started = time.monotonic()
            with patch.object(storage.conn, "commit", delayed_commit):
                result = await original_terminal(*args, **kwargs)
            assert commits == 1, "slow control did not preserve one real terminal commit"
            assert time.monotonic() - started >= 2.1
            return result
        return await original_terminal(*args, **kwargs)

    try:
        with (
            patch.object(notification, "wait", observe_notification_wait),
            patch.object(runtime, "abort_reservation", observe_abort),
            patch.object(runtime, "_signal_driver_state_changed", signal_driver_changed),
            patch.object(runtime, "_persist_terminal_update", observe_terminal),
            patch.object(runtime, "_execute", observe_execute),
            patch.object(runtime, "_run_turn_handler_with_write_lock_bypass", observe_dispatch),
        ):
            if fault != "hang-terminal":
                print(READY, flush=True)
            print("phase=dependency-deferred", flush=True)
            assert (await runtime.recover_stranded_steers())["deferred"] == 1
            worker = runtime._steer_recovery_task
            assert worker is not None
            await observed_recovery_wait(worker, initial_wait)
            with patch.object(runtime, "_restore_durable_accepted_model_routing",
                              fail_after_reservation):
                services["desktop_browser"]["status"] = "ready"
                runtime.notify_recovery_dependencies_changed()
                print("phase=wait-abort-and-idle", flush=True)
                await observed_recovery_wait(worker, idle_after_abort, unexpected_retry)
                assert attempts == 1, "recovery retried without an external notification"
                assert abort_returned.is_set() and idle_after_abort.is_set()
                assert runtime._resident_count == 0, (
                    "aborted reservation retained its resident lease"
                )
                assert runtime._reservations_by_session == {} and runs == []
                assert not notification.is_set() and not worker.done()
            assert runtime._restore_durable_accepted_model_routing == original_restore
            resume_notified = True
            runtime.notify_recovery_dependencies_changed()
            print("phase=wait-recovery-worker", flush=True)
            await worker  # Activation/recovery only; not proof of terminal completion.
            receipt = await storage.get_turn_ingress_receipt(
                source_scope="rpc:web:steer.v2", request_session_key=KEY,
                client_request_id=f"request-{MESSAGE_ID}",
            )
            assert receipt is not None
            print("phase=wait-task-terminal", flush=True)
            record = await terminal_record(runtime, receipt.receipt.task_id, drivers)
            assert record is not None and record.status == AgentTaskStatus.SUCCEEDED
            again = await runtime.recover_stranded_steers()
            assert again["promoted"] == again["resumed"] == again["deferred"] == 0
            assert len(runs) == 1, "accepted restart input executed more than once"
            assert runs[0].message == "continue the accepted browser task"
            tasks = await storage.list_agent_tasks(session_key=KEY)
            assert len(tasks) == 2
            persisted = next(task for task in tasks if task.task_id == record.task_id)
            assert persisted.status == AgentTaskStatus.SUCCEEDED
            assert runtime._resident_count == 0 and runtime._reservations_by_session == {}
    finally:
        original_error = sys.exc_info()[1]
        cleanup_errors: list[BaseException] = []
        print("phase=cleanup", flush=True)
        # Undo fault boundaries before actual cleanup. This does not turn a
        # failed assertion into success; the original error still propagates.
        for reservation in reservations:
            try:
                await original_abort(reservation)
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            result = await runtime.shutdown(cancel=True)
            assert result.clean, f"runtime shutdown did not finish cleanly: {result!r}"
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            await storage.close()
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            primary = original_error if original_error is not None else cleanup_errors[0]
            for error in cleanup_errors:
                if error is not primary:
                    primary.add_note(f"cleanup also failed: {error!r}")
            if original_error is None:
                raise primary
        else:
            print("phase=cleanup-complete", flush=True)
    print("steer_restart_deferred_contract=passed", flush=True)


if __name__ == "__main__":
    asyncio.run(scenario(Path(sys.argv[1]), sys.argv[2]))
