"""Failure-only observation for one existing Windows gateway timeout test.

Opt in with ``-p opensquilla_ci_gateway_timeout`` and
``OPENSQUILLA_TIMEOUT_DIAG_DIR``. This plugin does not change the test's deadline,
SQLite settings, SQL, task scheduling policy, or assertions. Observation adds
bounded in-memory bookkeeping; JSON is written only after the test call ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import json
import os
import sys
import threading
import time
import traceback
from collections import Counter, deque
from pathlib import Path
from typing import Any

import pytest

TARGET = (
    "tests/test_gateway/test_steer_restart_deferred.py::"
    "test_resumed_post_reservation_error_releases_lease_without_busy_retry"
)
EVENT_LIMIT = 4096
_CAPTURE = pytest.StashKey[Any]()


def _await_chain(coroutine: Any) -> list[dict[str, Any]]:
    frames = []
    for _ in range(24):
        frame = getattr(coroutine, "cr_frame", None) or getattr(coroutine, "gi_frame", None)
        if frame is not None:
            frames.append({"file": frame.f_code.co_filename, "line": frame.f_lineno,
                           "function": frame.f_code.co_name})
        coroutine = getattr(coroutine, "cr_await", None) or getattr(coroutine, "gi_yieldfrom", None)
        if coroutine is None:
            break
    return frames


class _Capture:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.started = time.perf_counter()
        self.wall_started = time.time()
        self.active = True
        self.installed = False
        self.sql_enabled = False
        self.events: deque[dict[str, Any]] = deque(maxlen=EVENT_LIMIT)
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()
        self.patches: list[tuple[Any, str, Any]] = []
        self.runtimes: list[Any] = []
        self.tasks: dict[int, asyncio.Task[Any]] = {}
        self.connections: dict[int, dict[str, Any]] = {}
        self.transactions: dict[tuple[int, str], Any] = {}
        self.errors: deque[str] = deque(maxlen=8)
        self.calls = itertools.count(1)
        self.timer: asyncio.TimerHandle | None = None
        self.max_loop_lag_ms = 0.0
        self.snapshot_count = 0
        self.wait_started: float | None = None
        self.wait_elapsed_ms: float | None = None

    def record(self, kind: str, **fields: Any) -> None:
        if not self.active:
            return
        try:
            event = {"ms": (time.perf_counter() - self.started) * 1000,
                     "thread_id": threading.get_ident(),
                     "thread_cpu_ms": time.thread_time_ns() / 1_000_000,
                     "kind": kind, **fields}
            with self.lock:
                self.events.append(event)
                self.counts[kind] += 1
        except Exception as error:
            self.errors.append(f"record: {type(error).__name__}")

    def remember_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is not None and (id(task) in self.tasks or len(self.tasks) < 128):
            self.tasks[id(task)] = task

    def state(self, runtime: Any) -> dict[str, Any]:
        try:
            storage = runtime._storage
            tasks = []
            for task in list(runtime._tasks.values())[:64]:
                self.remember_task(task.asyncio_task)
                tasks.append({"task_id": task.task_id, "status": str(task.status),
                              "done": task.done.is_set(),
                              "terminal_settling": task.terminal_settling,
                              "terminal_settled": task.terminal_settled,
                              "terminal_emitted": task.terminal_emitted,
                              "cancel_requested": task.cancel_requested,
                              "execution_started": task.execution_started,
                              "driver_id": id(task.asyncio_task) if task.asyncio_task else None})
            self.remember_task(runtime._steer_recovery_task)
            return {"runtime": id(runtime), "resident": runtime._resident_count,
                    "closing": runtime._closing,
                    "reservations": len(runtime._reservations_by_session),
                    "operation_holder": storage._operation_holder,
                    "writer_locked": storage._operation_lock.locked(),
                    "writer": id(storage._conn) if storage._conn else None,
                    "reader": (id(storage._transcript_reader)
                               if storage._transcript_reader else None),
                    "tasks": tasks}
        except Exception as error:
            self.errors.append(f"state: {type(error).__name__}")
            return {"runtime": id(runtime), "diagnostic_error": type(error).__name__}

    def snapshot(self, reason: str) -> None:
        # Keep one pre-propagation failure snapshot and one final failure snapshot.
        if self.snapshot_count >= 2:
            return
        self.snapshot_count += 1
        try:
            states = [self.state(runtime) for runtime in self.runtimes]
            try:
                for task in asyncio.all_tasks():
                    self.remember_task(task)
            except RuntimeError:
                pass  # pytest's call report can be created after the loop stopped.
            tasks = []
            for task in list(self.tasks.values()):
                entry = {"id": id(task), "name": task.get_name(), "done": task.done(),
                         "cancelled": task.cancelled(), "cancelling": task.cancelling(),
                         "await_chain": _await_chain(task.get_coro()),
                         "stack": [{"file": frame.f_code.co_filename, "line": frame.f_lineno,
                                    "function": frame.f_code.co_name}
                                   for frame in task.get_stack(limit=16)]}
                if task.done() and not task.cancelled():
                    # Do not mark an otherwise unobserved exception retrieved.
                    error = getattr(task, "_exception", None)
                    entry["unretrieved_exception"] = getattr(task, "_log_traceback", None)
                    entry["exception"] = (None if error is None else {
                        "type": type(error).__name__, "message": str(error)[:512],
                    })
                tasks.append(entry)
            names = {thread.ident: thread.name for thread in threading.enumerate()}
            frames = sys._current_frames()
            threads = [{"id": ident, "name": names.get(ident),
                        "stack": traceback.format_list(traceback.extract_stack(frame, limit=16))}
                       for ident, frame in list(frames.items())[:64]]
            transactions = [{"storage": key[0], "operation": key[1],
                             "phase": timing.phase, "timings": timing.timings.copy(),
                             "current_phase_elapsed_ms": max(
                                 0.0, (time.perf_counter() - timing.phase_started) * 1000)}
                            for key, timing in self.transactions.items()]
            self.record("snapshot", reason=reason, runtimes=states, asyncio_tasks=tasks,
                        threads=threads, thread_count=len(frames), transactions=transactions)
        except Exception as error:
            self.errors.append(f"snapshot: {type(error).__name__}")

    def start_heartbeat(self) -> None:
        if self.timer is not None:
            return
        loop = asyncio.get_running_loop()
        expected = time.perf_counter() + 0.02

        def tick() -> None:
            nonlocal expected
            if not self.active:
                return
            now = time.perf_counter()
            lag_ms = max(0.0, (now - expected) * 1000)
            self.max_loop_lag_ms = max(self.max_loop_lag_ms, lag_ms)
            if lag_ms >= 5:
                self.record("loop_lag", lag_ms=lag_ms)
            expected = now + 0.02
            self.timer = loop.call_later(0.02, tick)

        self.timer = loop.call_later(0.02, tick)

    def patch(self, owner: Any, name: str, replacement: Any) -> None:
        self.patches.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    def wrap_runtime(self, owner: Any, name: str) -> None:
        original = getattr(owner, name)
        capture = self

        @functools.wraps(original)
        async def wrapped(runtime: Any, *args: Any, **kwargs: Any) -> Any:
            if name == "recover_stranded_steers":
                capture.sql_enabled = True
                capture.start_heartbeat()
            # In particular, retain the eager terminal-settlement child Task:
            # all_tasks() alone excludes it once it has failed or completed.
            capture.remember_task(asyncio.current_task())
            fields = {"method": name, "runtime": id(runtime)}
            if name == "wait":
                capture.wait_started = time.perf_counter()
                fields["timeout"] = kwargs.get("timeout", args[1] if len(args) > 1 else None)
                for task in asyncio.all_tasks():
                    capture.remember_task(task)
            capture.record("method_enter", **fields, state=capture.state(runtime))
            try:
                result = await original(runtime, *args, **kwargs)
            except BaseException as error:
                capture.record("method_error", **fields, error=type(error).__name__,
                               state=capture.state(runtime))
                if name == "wait":
                    capture.snapshot(f"runtime.wait raised {type(error).__name__}")
                raise
            else:
                capture.record("method_exit", **fields, state=capture.state(runtime))
                return result
            finally:
                if name == "wait" and capture.wait_started is not None:
                    capture.wait_elapsed_ms = (time.perf_counter() - capture.wait_started) * 1000

        self.patch(owner, name, wrapped)

    def install(self) -> None:
        # These modules are already imported by the collected target test. Do
        # not import ApprovalQueue here and prewarm its actual terminal path.
        from opensquilla.compat import aiosqlite as compat
        from opensquilla.gateway.task_runtime import TaskRuntime
        from opensquilla.session.storage import SessionStorage, _TransactionTiming

        capture = self
        original_init = TaskRuntime.__init__

        @functools.wraps(original_init)
        def initialize(runtime: Any, *args: Any, **kwargs: Any) -> None:
            original_init(runtime, *args, **kwargs)
            if len(capture.runtimes) < 8:
                capture.runtimes.append(runtime)
            storage = runtime._storage
            roles = (("writer", storage._conn), ("reader", storage._transcript_reader))
            for role, connection in roles:
                if connection is not None and len(capture.connections) < 32:
                    capture.connections[id(connection)] = {
                        "role": role, "storage": id(storage),
                        "backend": f"{type(connection).__module__}.{type(connection).__name__}",
                    }
            capture.record("runtime_created", state=capture.state(runtime))

        self.patch(TaskRuntime, "__init__", initialize)
        for name in ("recover_stranded_steers", "_retry_stranded_steers", "activate",
                     "abort_reservation", "_execute", "_mark_running", "_persist_running_state",
                     "wait", "_mark_terminal", "_mark_terminal_claimed", "_terminal_details_update",
                     "_persist_terminal_update", "_notify_task_lifecycle",
                     "_release_activation_permit", "shutdown"):
            self.wrap_runtime(TaskRuntime, name)

        original_diagnostics = SessionStorage._transaction_diagnostics

        @contextlib.contextmanager
        def diagnostics(storage: Any, operation: str, role: str):
            key = (id(storage), operation)
            observed = None
            phase = None
            failure = None
            capture.record("transaction_enter", storage=id(storage), operation=operation, role=role)
            try:
                with original_diagnostics(storage, operation, role) as existing:
                    # Reuse the production phase markers without enabling any
                    # additional logging or disk writes on the timed path.
                    observed = existing or _TransactionTiming(
                        time.perf_counter, time.perf_counter(), "queue_ms", {})
                    if len(capture.transactions) < 32:
                        capture.transactions[key] = observed
                    try:
                        yield observed
                    finally:
                        phase = observed.phase
                        if existing is None:
                            observed.advance("finished")
            except BaseException as error:
                failure = type(error).__name__
                raise
            finally:
                capture.transactions.pop(key, None)
                capture.record("transaction_exit", storage=id(storage),
                               operation=operation, role=role,
                               phase=phase, error=failure,
                               timings=observed.timings.copy() if observed else None)

        self.patch(SessionStorage, "_transaction_diagnostics", diagnostics)
        original_put = compat._NativeQueue.put_nowait

        def native_put(queue: Any, item: Any) -> None:
            connection = queue._owner()
            if (not capture.sql_enabled or id(connection) not in capture.connections
                    or not isinstance(item, tuple) or len(item) != 2
                    or not isinstance(item[0], asyncio.Future)
                    or not isinstance(item[1], functools.partial)):
                original_put(queue, item)
                return
            future, function = item
            sql_verb = (function.args[0].split(None, 1)[0][:32]
                        if function.args and isinstance(function.args[0], str)
                        and function.args[0].strip() else None)
            fields = {"call_id": next(capture.calls), "connection": id(connection),
                      "operation": getattr(function.func, "__name__", type(function.func).__name__),
                      "sql_verb": sql_verb}
            capture.record("sqlite_queued", **fields)

            def execute_original() -> Any:
                capture.record("sqlite_worker_start", **fields)
                try:
                    return function()
                except BaseException as error:
                    capture.record("sqlite_worker_error", **fields,
                                   error=type(error).__name__,
                                   sqlite_errorcode=getattr(error, "sqlite_errorcode", None),
                                   sqlite_errorname=getattr(error, "sqlite_errorname", None))
                    raise
                finally:
                    capture.record("sqlite_worker_end", **fields)

            # Preserve ordinary partial vs native close closure/sentinel. The
            # production cancellation guard still owns the same Future.
            original_put(queue, (future, functools.partial(execute_original)))
            future.add_done_callback(lambda _: capture.record("sqlite_loop_ready", **fields))

        self.patch(compat._NativeQueue, "put_nowait", native_put)
        original_fallback = compat._run_sqlite_call

        @functools.wraps(original_fallback)
        async def fallback(function: Any, *args: Any, **kwargs: Any) -> Any:
            if not capture.sql_enabled:
                return await original_fallback(function, *args, **kwargs)
            fields = {"call_id": next(capture.calls),
                      "operation": getattr(function, "__name__", type(function).__name__)}
            capture.record("fallback_queued", **fields)

            def execute_original(*worker_args: Any, **worker_kwargs: Any) -> Any:
                capture.record("fallback_worker_start", **fields)
                try:
                    return function(*worker_args, **worker_kwargs)
                except BaseException as error:
                    capture.record("fallback_worker_error", **fields,
                                   error=type(error).__name__,
                                   sqlite_errorcode=getattr(error, "sqlite_errorcode", None),
                                   sqlite_errorname=getattr(error, "sqlite_errorname", None))
                    raise
                finally:
                    capture.record("fallback_worker_end", **fields)

            try:
                return await original_fallback(execute_original, *args, **kwargs)
            finally:
                capture.record("fallback_loop_ready", **fields)

        self.patch(compat, "_run_sqlite_call", fallback)
        self.installed = True
        self.record("probe_installed", nodeid=self.nodeid, source=TaskRuntime.__module__)

    def stop(self) -> None:
        self.active = False
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        for owner, name, original in reversed(self.patches):
            setattr(owner, name, original)
        self.patches.clear()

    def write(self, failed: bool) -> None:
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        output = Path(os.environ["OPENSQUILLA_TIMEOUT_DIAG_DIR"])
        output.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "probe": "gateway-sqlite-3", "nodeid": self.nodeid,
                   "enabled": self.installed,
                   "failed": failed, "pid": os.getpid(), "worker": worker, "python": sys.version,
                   "wall_started": self.wall_started,
                   "perf_counter_started": self.started,
                   "event_limit": EVENT_LIMIT, "event_count": sum(self.counts.values()),
                   "dropped_events": max(0, sum(self.counts.values()) - len(self.events)),
                   "counts": dict(self.counts), "connections": self.connections,
                   "wait_elapsed_ms": self.wait_elapsed_ms, "max_loop_lag_ms": self.max_loop_lag_ms,
                   "diagnostic_errors": list(self.errors), "sampling_stopped": not self.active,
                   "patches_restored": not self.patches}
        if failed:
            payload["events"] = list(self.events)
        (output / f"gateway-sqlite-3-{worker}-{os.getpid()}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self.tasks.clear()
        self.runtimes.clear()
        self.transactions.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: Any):
    if item.nodeid != TARGET or not os.environ.get("OPENSQUILLA_TIMEOUT_DIAG_DIR"):
        yield
        return
    capture = _Capture(item.nodeid)
    item.stash[_CAPTURE] = capture
    try:
        capture.install()
    except Exception as error:
        capture.errors.append(f"install: {type(error).__name__}: {str(error)[:256]}")
        capture.stop()
    try:
        outcome = yield
        if outcome.excinfo is not None:
            capture.snapshot("test call failed after original cleanup")
        capture.record("test_call_end", failed=outcome.excinfo is not None)
    finally:
        capture.stop()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any):
    outcome = yield
    if call.when == "call" and _CAPTURE in item.stash:
        capture = item.stash[_CAPTURE]
        try:
            capture.write(outcome.get_result().failed)
        except OSError as error:
            # A diagnostic artifact failure must not replace the test result.
            print(f"gateway timeout diagnostic write failed: {type(error).__name__}",
                  file=sys.stderr)
