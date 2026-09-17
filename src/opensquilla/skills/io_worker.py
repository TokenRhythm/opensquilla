"""Cooperative, settled worker I/O for uncommitted Skill staging trees."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Callable
from typing import Any

_STOP: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "skill_staging_io_stop", default=None,
)


def check_staging_cancelled() -> None:
    stop = _STOP.get()
    if stop is not None and stop.is_set():
        raise InterruptedError("Skill staging I/O cancelled")


async def run_staging_worker[T](function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Keep the event loop responsive and join a cancelled worker before cleanup."""
    stop = threading.Event()
    token = _STOP.set(stop)
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation: asyncio.CancelledError | None = None
    try:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                stop.set()
            except BaseException:
                if cancellation is None:
                    raise
                break
        if cancellation is not None:
            try:
                operation.result()
            except BaseException:
                pass
            raise cancellation
        return operation.result()
    finally:
        _STOP.reset(token)
