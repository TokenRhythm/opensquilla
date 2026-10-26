"""Keep synchronous telemetry state transactions off the application loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def run_state_io[T](operation: Callable[[], T]) -> T:
    """Finish an admitted transaction before releasing its caller's policy gate.

    Cancelling an executor future does not stop its thread. Waiting for that
    transaction here prevents a later privacy transition from overtaking a
    still-running write. Telemetry file locks use immediate acquisition so
    contention is retried by the producer rather than sleeping in a worker.
    """

    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        await asyncio.gather(task, return_exceptions=True)
        raise
