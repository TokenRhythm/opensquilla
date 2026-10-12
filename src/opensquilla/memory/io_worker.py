"""File I/O that retains ownership until its worker has settled."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from functools import partial
from typing import Any


async def await_settled[T](operation: asyncio.Future[T]) -> T:
    """Cancellation must not release a file operation's lock before completion."""
    cancellation: asyncio.CancelledError | None = None
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            break
    try:
        result = operation.result()
    except BaseException as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
    if cancellation is not None:
        raise cancellation
    return result


async def run_memory_io[T](function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a complete filesystem operation off-loop, including its cleanup."""
    operation = asyncio.get_running_loop().run_in_executor(
        None, contextvars.copy_context().run, partial(function, *args, **kwargs),
    )
    return await await_settled(operation)
