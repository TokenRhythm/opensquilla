"""Bounded observation for scheduler tasks whose cancellation may be drained."""

from __future__ import annotations

import asyncio


async def wait_task[R](task: asyncio.Task[R], timeout: float = 2) -> R:
    done, _ = await asyncio.wait({task}, timeout=timeout)
    assert task in done, f"scheduler operation did not settle: {task.get_name()}"
    return task.result()


async def drain_tasks(*tasks: asyncio.Task) -> None:
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=5)
    for task in pending:
        task.cancel()
    assert not pending, f"scheduler cleanup did not settle: {[task.get_name() for task in pending]}"
    await asyncio.gather(*done, return_exceptions=True)
