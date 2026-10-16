"""Real committed acceptance followed by caller cancellation, under an outer watchdog.

The two-second readiness poll used to couple this ordering contract to disk speed.
Event barriers now establish the ordering; the parent bounds cold startup and the
entire execution/cleanup separately. Delays and faults only validate this fixture.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from opensquilla.channels.delivery_store import ChannelDeliveryStore
from opensquilla.channels.storage_worker import AsyncChannelDeliveryStore
from opensquilla.channels.types import IncomingMessage
from tests.helpers.sqlite_process_probe import READY


async def event_or_failure(event: asyncio.Event, task: asyncio.Task, phase: str) -> None:
    """Surface an operation that fails before its barrier, without a second timeout."""
    waiter = asyncio.create_task(event.wait())
    try:
        await asyncio.wait((waiter, task), return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            await task  # Preserve the actual enqueue/consumer exception and traceback.
            raise AssertionError(f"operation completed before {phase}")
        await waiter
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def scenario(directory: Path, fault: str) -> None:
    started = time.monotonic()

    def phase(name: str) -> None:
        print(f"phase={name} elapsed_seconds={time.monotonic() - started:.6f}", flush=True)

    loop = asyncio.get_running_loop()
    committed = asyncio.Event()
    handed_off = asyncio.Event()
    release_accept = threading.Event()
    never_release = threading.Event()
    accepting = False
    accept_committed = False
    delayed = False

    class ObservedConnection(sqlite3.Connection):
        def commit(self):
            nonlocal accept_committed, delayed
            if accepting:
                phase("accept-commit-start")
                if fault == "slow-commit" and not delayed:
                    delayed = True
                    print("synthetic_commit_delay_seconds=2.1", flush=True)
                    # One deliberate delay inside the actual commit boundary,
                    # never used to decide whether the operation is ready.
                    delay_started = time.monotonic()
                    time.sleep(2.1)
                    delay_elapsed = time.monotonic() - delay_started
                    print(f"observed_commit_delay_seconds={delay_elapsed:.6f}", flush=True)
                    assert delay_elapsed > 2, (
                        "synthetic delay did not exceed the former poll budget"
                    )
            result = super().commit()
            if accepting:
                accept_committed = True
                phase("accept-commit-complete")
            return result

    class AcceptStore(ChannelDeliveryStore):
        def accept_inbound(self, name, message):
            nonlocal accept_committed, accepting
            phase("accept-worker-start")
            if fault == "early-error":
                print("fault=early-error", flush=True)
                raise RuntimeError("injected accept submission failure")
            accepting = True
            accept_committed = False
            try:
                accepted = super().accept_inbound(name, message)
            finally:
                accepting = False
            if accepted:
                assert accept_committed, "acceptance returned without a durable commit"
                # This notification follows the real FULL/WAL commit, while
                # the worker is still held before returning to its handoff.
                phase("accept-committed-gate")
                if fault == "hang-handoff":
                    phase("noncooperative-accept-thread")
                    # Only this fault boundary starts the short negative-control
                    # watchdog; slow normal setup cannot satisfy that control.
                    print("CHANNEL_ACCEPT_HANG_READY", flush=True)
                    never_release.wait()
                loop.call_soon_threadsafe(committed.set)
                release_accept.wait()  # Bounded by the exact child's watchdog.
            return accepted

    class ObservedQueue(asyncio.Queue):
        def put_nowait(self, item):
            super().put_nowait(item)
            phase("handoff")
            handed_off.set()

    worker = AsyncChannelDeliveryStore(directory / "accept.sqlite", store_factory=AcceptStore)
    queue = ObservedQueue()
    message = IncomingMessage(
        sender_id="u", channel_id="c", content="original",
        metadata={"event_id": "one", "nested": {"value": 1}},
    )
    task = None
    real_connect = sqlite3.connect

    def observed_connect(*args, **kwargs):
        return real_connect(*args, factory=ObservedConnection, **kwargs)

    if fault == "duplicate-handoff":
        real_call = worker._call

        async def duplicate_handoff(method, *args, **kwargs):
            callback = kwargs.get("on_result")
            if method == "accept_inbound" and callback is not None:
                def twice(result):
                    callback(result)
                    if result:
                        print("fault=duplicate-handoff", flush=True)
                        callback(result)

                kwargs["on_result"] = twice
            return await real_call(method, *args, **kwargs)

        # Invoke the actual enqueue handoff closure twice at the worker's
        # on_result boundary, instead of merely faking a queue size or failure.
        worker._call = duplicate_handoff

    print(f"probe_pid={os.getpid()}", flush=True)
    try:
        phase("open")
        # Only connection construction is patched; every SQL operation still
        # uses a real sqlite3.Connection on the normal owned worker thread.
        with patch("sqlite3.connect", observed_connect):
            await worker.open()
        print(READY, flush=True)
        phase("enqueue")
        task = asyncio.create_task(worker.enqueue("channel", message, queue))
        await event_or_failure(committed, task, "committed acceptance")
        assert not release_accept.is_set()
        assert queue.qsize() == 0, "handoff ran before the committed worker was released"
        assert task.cancel(), "enqueue had already completed before cancellation"
        try:
            await task
        except asyncio.CancelledError:
            phase("caller-cancelled")
        else:
            raise AssertionError("submitted enqueue suppressed caller cancellation")
        release_accept.set()
        assert worker._consumer is not None
        await event_or_failure(handed_off, worker._consumer, "cancelled caller handoff")
        assert queue.qsize() == 1, "accepted event was handed off more than once"
        assert await worker.enqueue("channel", message, queue) is False
        assert queue.qsize() == 1, "duplicate admission produced another handoff"
        phase("duplicate-rejected")
    finally:
        original_error = sys.exc_info()[1]
        try:
            phase("cleanup")
            release_accept.set()
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await worker.close()
            phase("worker-closed")
            # close shuts down the executor without waiting; join these exact
            # owned threads so the outer watchdog also covers resource release.
            for thread in worker._executor._threads:
                thread.join()
                assert not thread.is_alive(), "channel worker thread survived close"
            phase("worker-threads-stopped")
        except BaseException as cleanup_error:
            if original_error is None:
                raise
            original_error.add_note(f"cleanup also failed: {cleanup_error!r}")
    phase("reopen")
    with sqlite3.connect(worker.path) as connection:
        assert connection.execute("SELECT state FROM channel_ingress").fetchall() == [("accepted",)]
    if fault == "slow-commit":
        assert delayed, "the slow-commit control did not reach the commit boundary"
    print("accept_cancel_contract=passed", flush=True)


if __name__ == "__main__":
    # The parent reads UTF-8; redirected Windows streams otherwise use the locale.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    print("probe_stdout_utf8=\u2713", flush=True)
    print("probe_stderr_utf8=\u2713", file=sys.stderr, flush=True)
    asyncio.run(scenario(Path(sys.argv[1]), sys.argv[2]))
