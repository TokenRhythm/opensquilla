"""Opt-in, bounded observations for one original channel cancellation test.

Load with ``-p opensquilla_ci_channel_timeout`` and set
OPENSQUILLA_TIMEOUT_DIAG_DIR. Deadlines, SQLite durability and assertions are
owned by the unchanged test. Timed paths only append metadata in memory. A
failed test gets the bounded timeline; a successful test gets a small summary.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import sqlite3
import sys
import threading
import time
from collections import deque
from pathlib import Path

import pytest

TARGET = (
    "tests/test_channels/test_channel_storage_worker.py::"
    "test_submitted_accept_cancellation_still_hands_off_exactly_once"
)
_PROBE = pytest.StashKey()


def _error(error):
    try:
        message = str(error)[:512]
    except Exception:
        message = "<exception text unavailable>"
    return {"type": type(error).__name__, "message": message}


def _frame(frame):
    return {"file": frame.f_code.co_filename, "line": frame.f_lineno,
            "function": frame.f_code.co_name}


def _task_state(task):
    # task.exception()/result() would mark an unobserved error as retrieved.
    # Read metadata only, retaining the original task's exception semantics.
    error = getattr(task, "_exception", None)
    return {
        "name": task.get_name(), "done": task.done(), "cancelled": task.cancelled(),
        "exception": _error(error) if error is not None else None,
        "unretrieved_exception": getattr(task, "_log_traceback", None),
        "stack": [_frame(frame) for frame in task.get_stack(limit=8)],
    }


class _Probe:
    def __init__(self, output):
        self.output = output
        self.rows = deque(maxlen=4096)
        self.total = 0
        self.failed = False
        self.started = time.perf_counter()
        self.wall_started = time.time()
        self.workers = deque(maxlen=4)
        self.enqueue_tasks = deque(maxlen=8)
        self.thread_ids = {threading.get_ident()}
        self.loop = None
        self.last_tick = None
        self.max_loop_gap = 0.0
        self.tick_count = 0
        self.timer = None
        self.stop = threading.Event()
        self.watcher = None
        self.next_token = 0
        self.diagnostic_errors = deque(maxlen=8)
        self.patches_restored = False

    def record(self, event, **values):
        self.rows.append({"t": time.perf_counter() - self.started,
                          "thread_cpu": time.thread_time(),
                          "thread": threading.get_ident(), "event": event, **values})
        self.total += 1

    def thread_stacks(self):
        frames = sys._current_frames()
        result = {}
        for ident in tuple(self.thread_ids.copy())[:5]:
            frame = frames.get(ident)
            stack = []
            while frame is not None and len(stack) < 12:
                stack.append(_frame(frame))
                frame = frame.f_back
            result[str(ident)] = stack
        return result

    def snapshot(self):
        tasks = list(self.enqueue_tasks)
        for task in asyncio.all_tasks():
            if task not in tasks and len(tasks) < 16:
                tasks.append(task)
        return {
            "threads": self.thread_stacks(),
            "tasks": [_task_state(task) for task in tasks],
            "workers": [{
                "active": worker._active,
                "waiting": worker._waiting,
                "queued": {lane: len(queue) for lane, queue in worker._queues.items()},
                "active_method": getattr(worker._active_job, "method", None),
                "draining": worker._draining, "closed": worker._closed,
                "consumer": _task_state(worker._consumer) if worker._consumer else None,
            } for worker in self.workers],
            "loop": {
                "ready_callbacks": len(getattr(self.loop, "_ready", ())),
                "scheduled_callbacks": len(getattr(self.loop, "_scheduled", ())),
                "since_tick": time.perf_counter() - self.last_tick if self.last_tick else None,
                "max_gap": self.max_loop_gap,
            },
        }

    def arm(self):
        if self.loop is not None:
            return
        self.loop = asyncio.get_running_loop()
        self.last_tick = time.perf_counter()

        def heartbeat():
            if self.stop.is_set():
                return
            now = time.perf_counter()
            gap = now - self.last_tick
            self.max_loop_gap = max(self.max_loop_gap, gap)
            self.last_tick = now
            self.tick_count += 1
            if gap > 0.1:
                self.record("loop.gap", gap=gap)
            self.timer = self.loop.call_later(0.05, heartbeat)

        def watch():
            snapshots = 0
            while not self.stop.wait(0.25):
                gap = time.perf_counter() - self.last_tick
                if gap > 0.5 and snapshots < 4:
                    self.record("watch.loop_gap", gap=gap, threads=self.thread_stacks())
                    snapshots += 1

        self.timer = self.loop.call_later(0.05, heartbeat)
        self.watcher = threading.Thread(target=watch, name="channel-timeout-observer", daemon=True)
        self.watcher.start()

    def finish(self):
        self.stop.set()
        if self.timer is not None:
            self.timer.cancel()
        if self.watcher is not None:
            self.watcher.join(timeout=1)
        self.record("observer.stop", watchdog_alive=bool(self.watcher and self.watcher.is_alive()))

    def write_result(self):
        rows = list(self.rows)
        spans = []
        pending = {}
        for row in rows:
            event = row["event"]
            key = (event.rsplit(".", 1)[0], row.get("token"), row.get("sql"))
            if event.endswith(".start"):
                pending[key] = row
            elif event.endswith(".end") and key in pending:
                start = pending.pop(key)
                if len(spans) < 48:
                    spans.append({"operation": key[0], "sql": row.get("sql"),
                                  "wall_ms": 1000 * (row["t"] - start["t"]),
                                  "thread_cpu_ms": 1000 * (row["thread_cpu"] - start["thread_cpu"])
                                  if row["thread"] == start["thread"] else None})
        payload = {
            "schema_version": 1, "probe": "channel_timeout", "enabled": True,
            "target": TARGET, "failed": self.failed, "pid": os.getpid(),
            "xdist_worker": os.environ.get("PYTEST_XDIST_WORKER", "none"),
            "python": sys.version, "wall_started": self.wall_started,
            "rows_recorded": self.total, "rows_dropped": max(0, self.total - len(rows)),
            "max_loop_gap_ms": self.max_loop_gap * 1000, "heartbeat_ticks": self.tick_count,
            "watchdog_stopped": not (self.watcher and self.watcher.is_alive()),
            "patches_restored": self.patches_restored,
            "diagnostic_errors": list(self.diagnostic_errors),
            "executor_dispatch_ms": [row["dispatch_ms"] for row in rows
                                     if row["event"] == "executor.start"][:16],
            "spans": spans,
        }
        if self.failed:
            payload["timeline"] = rows
        self.output.mkdir(parents=True, exist_ok=True)
        suffix = "failure" if self.failed else "summary"
        path = self.output / (
            f"channel-{os.environ.get('PYTEST_XDIST_WORKER', 'none')}-{os.getpid()}-{suffix}.json"
        )
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    probe = item.stash.get(_PROBE, None)
    if probe is not None and report.failed:
        probe.failed = True
        probe.record("pytest.failure", phase=report.when)
    if probe is not None and report.when == "teardown":
        try:
            probe.write_result()
        except Exception as error:
            # Diagnostic I/O must not replace the original test result.
            sys.stderr.write(f"channel timeout diagnostic could not be saved: {_error(error)}\n")


@pytest.fixture(autouse=True)
def channel_timeout_observer(request):
    directory = os.environ.get("OPENSQUILLA_TIMEOUT_DIAG_DIR")
    if not directory or request.node.nodeid.replace("\\", "/") != TARGET:
        yield
        return

    from opensquilla.channels import delivery_store, storage_worker

    probe = _Probe(Path(directory))
    request.node.stash[_PROBE] = probe
    module = request.module
    original_sqlite = delivery_store.sqlite3
    monkeypatch = pytest.MonkeyPatch()
    originals = []

    def patch(obj, name, value):
        originals.append((obj, name, getattr(obj, name)))
        monkeypatch.setattr(obj, name, value)

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            label = " ".join(sql.split())[:80]
            probe.record("sqlite.execute.start", sql=label)
            try:
                return super().execute(sql, parameters)
            finally:
                probe.record("sqlite.execute.end", sql=label)

        def executescript(self, script, /):
            probe.record("sqlite.schema.start")
            try:
                return super().executescript(script)
            finally:
                probe.record("sqlite.schema.end")

        def commit(self):
            probe.record("sqlite.commit.start")
            try:
                return super().commit()
            finally:
                probe.record("sqlite.commit.end")

        def close(self):
            probe.record("sqlite.close.start")
            try:
                return super().close()
            finally:
                probe.record("sqlite.close.end")

    class SqliteView:
        def __getattr__(self, name):
            return getattr(original_sqlite, name)

        def connect(self, *args, **kwargs):
            kwargs.setdefault("factory", Connection)
            probe.record("sqlite.connect.start")
            try:
                return original_sqlite.connect(*args, **kwargs)
            finally:
                probe.record("sqlite.connect.end")

    patch(delivery_store, "sqlite3", SqliteView())
    original_open = storage_worker.AsyncChannelDeliveryStore.open

    @functools.wraps(original_open)
    async def open_worker(worker):
        probe.arm()
        if worker not in probe.workers:
            probe.workers.append(worker)
            original_submit = worker._executor.submit

            def submit(fn, /, *args, **kwargs):
                probe.next_token += 1
                token = probe.next_token
                submitted = time.perf_counter()
                probe.record("executor.submit", token=token,
                             function=getattr(fn, "__qualname__", type(fn).__name__))

                def execute():
                    probe.thread_ids.add(threading.get_ident())
                    probe.record("executor.start", token=token,
                                 dispatch_ms=1000 * (time.perf_counter() - submitted))
                    try:
                        return fn(*args, **kwargs)
                    except BaseException as error:
                        probe.record("executor.error", token=token, error=_error(error))
                        raise
                    finally:
                        probe.record("executor.end", token=token)

                return original_submit(execute)

            patch(worker._executor, "submit", submit)
            original_next = worker._next

            def next_job():
                job = original_next()
                probe.record("worker.selected", method=job.method,
                             submitted_at=job.submitted_at)
                return job

            patch(worker, "_next", next_job)
            original_enqueue = worker.enqueue

            async def enqueue(*args, **kwargs):
                task = asyncio.current_task()
                if task not in probe.enqueue_tasks:
                    probe.enqueue_tasks.append(task)
                probe.record("enqueue.start")
                try:
                    return await original_enqueue(*args, **kwargs)
                except BaseException as error:
                    probe.record("enqueue.error", error=_error(error))
                    raise
                finally:
                    probe.record("enqueue.end")

            patch(worker, "enqueue", enqueue)
        probe.record("open.start")
        try:
            return await original_open(worker)
        finally:
            probe.record("open.end")

    patch(storage_worker.AsyncChannelDeliveryStore, "open", open_worker)
    original_gate_init = module.GateStore.__init__

    @functools.wraps(original_gate_init)
    def gate_init(store, *args, **kwargs):
        entered = kwargs["entered"] if "entered" in kwargs else args[3]
        original_set = entered.set

        def entered_set():
            original_set()
            probe.record("gate.entered", pause=store.pause)

        patch(entered, "set", entered_set)
        return original_gate_init(store, *args, **kwargs)

    patch(module.GateStore, "__init__", gate_init)
    original_pause = module.GateStore._pause

    def pause(store, name):
        probe.record("gate.pause.start", name=name)
        try:
            return original_pause(store, name)
        finally:
            probe.record("gate.pause.end", name=name)

    patch(module.GateStore, "_pause", pause)
    original_until = module.until

    async def until(predicate):
        probe.record("until.start")
        try:
            await original_until(predicate)
        except BaseException as error:
            probe.failed = True
            # Preserve the test's original exception even if observation fails.
            try:
                probe.record("until.error", error=_error(error),
                             predicate_ready=predicate(), snapshot=probe.snapshot())
            except Exception as observation_error:
                probe.diagnostic_errors.append(_error(observation_error))
                probe.record("observer.snapshot_error", error=_error(observation_error))
            raise
        finally:
            probe.record("until.end")

    patch(module, "until", until)
    probe.record("observer.enabled")
    try:
        yield
    finally:
        # Runs after the original gated fixture drains/closes its owned worker.
        try:
            probe.finish()
        finally:
            monkeypatch.undo()
            probe.patches_restored = all(
                getattr(obj, name) == value for obj, name, value in originals
            )
