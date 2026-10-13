"""Bound blocking command preparation without releasing running work on cancellation."""

from __future__ import annotations

import asyncio
import contextvars
import weakref
from collections.abc import Callable

_PREPARATION_SLOTS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


async def prepare_runtime[Result](worker: Callable[[], Result]) -> Result:
    loop = asyncio.get_running_loop()
    reference = _PREPARATION_SLOTS.get(loop)
    slots = reference() if reference is not None else None
    if slots is None:
        # Bound integrity-check followers, not user tasks: they otherwise occupy
        # the default executor while waiting for another worker's validation.
        slots = asyncio.Semaphore(2)
        _PREPARATION_SLOTS[loop] = weakref.ref(slots)
    await slots.acquire()
    try:
        future = loop.run_in_executor(None, contextvars.copy_context().run, worker)
    except BaseException:
        slots.release()
        raise

    def finished(done: asyncio.Future[Result]) -> None:
        # Cancellation of the caller cannot stop a running validation thread.
        slots.release()
        if not done.cancelled():
            done.exception()

    future.add_done_callback(finished)
    return await asyncio.shield(future)
