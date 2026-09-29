"""Bound synchronous URL validation and extraction outside the event loop."""

from __future__ import annotations

import asyncio
import contextvars
import queue
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

_BLOCKING_WORKERS = 4


class _FetchWorkers:
    """Daemon workers for abandonable DNS, parsing and unused TLS preparation.

    Never use this pool for process creation, database writes or file mutation:
    interpreter exit may abandon these workers without completing their jobs.
    """

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[
            tuple[Future[Any], Callable[..., Any], tuple[Any, ...]]
        ] = queue.SimpleQueue()
        self._start_lock = threading.Lock()
        self._threads_started = 0

    def submit[T](self, function: Callable[..., T], *args: Any) -> Future[T]:
        future: Future[T] = Future()
        with self._start_lock:
            if self._threads_started < _BLOCKING_WORKERS:
                threading.Thread(
                    target=self._worker,
                    name=f"fetch-work-{self._threads_started}",
                    daemon=True,
                ).start()
                self._threads_started += 1
        self._queue.put((future, function, args))
        return future

    def _worker(self) -> None:
        while True:
            work = self._queue.get()
            try:
                self._run(work)
            finally:
                # Do not retain URL bodies or a tool's copied Context while an
                # idle daemon waits for its next job.
                del work

    @staticmethod
    def _run(work: tuple[Future[Any], Callable[..., Any], tuple[Any, ...]]) -> None:
        future, function, args = work
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = function(*args)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)


_blocking_executor = _FetchWorkers()
_blocking_slots: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[asyncio.BoundedSemaphore]
] = weakref.WeakKeyDictionary()


async def run_blocking_fetch_work[T](function: Callable[..., T], *args: Any) -> T:
    """Bound synchronous fetch work, including work abandoned by cancellation."""
    loop = asyncio.get_running_loop()
    slots_ref = _blocking_slots.get(loop)
    slots = slots_ref() if slots_ref is not None else None
    if slots is None:
        slots = asyncio.BoundedSemaphore(_BLOCKING_WORKERS)
        _blocking_slots[loop] = weakref.ref(slots)
    await slots.acquire()
    try:
        work = _blocking_executor.submit(contextvars.copy_context().run, function, *args)
    except BaseException:
        slots.release()
        raise

    def finished(_work: Any) -> None:
        # A cancelled await cannot stop DNS or parsing already running in a
        # thread. Hold its slot until that actual work ends, even after timeout.
        try:
            loop.call_soon_threadsafe(slots.release)
        except RuntimeError:
            pass  # This loop has closed; it cannot admit any further work.

    work.add_done_callback(finished)
    future = asyncio.wrap_future(work, loop=loop)
    # Retrieve late failures after the caller has cancelled its shielded wait.
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        work.cancel()  # Cancel a queued job; running jobs keep their slot.
        raise


