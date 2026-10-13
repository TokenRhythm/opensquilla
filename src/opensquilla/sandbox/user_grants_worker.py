"""Serialize user-grant I/O off the event loop without abandoning mutations."""

from __future__ import annotations

import asyncio
import contextvars
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

_GRANTS_SLOTS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


class _UserGrantsOperation:
    def __init__(self) -> None:
        self.cancellation: asyncio.CancelledError | None = None

    async def wait[Result](self, operation: asyncio.Future[Result]) -> Result:
        # Preserve the actual result/error for the caller's rollback handler.
        # Cancellation is propagated only after leaving the operation scope.
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError as exc:
                if self.cancellation is None:
                    self.cancellation = exc
            except BaseException:
                break
        return operation.result()

    async def run[Result](self, operation: Callable[[], Result]) -> Result:
        worker = asyncio.get_running_loop().run_in_executor(
            None, contextvars.copy_context().run, operation,
        )
        return await self.wait(worker)


@asynccontextmanager
async def user_grants_operation() -> AsyncIterator[_UserGrantsOperation]:
    loop = asyncio.get_running_loop()
    reference = _GRANTS_SLOTS.get(loop)
    slots = reference() if reference is not None else None
    if slots is None:
        # Waiters stay on the loop instead of occupying executor threads, and
        # neither threads nor loop-bound locks are created at gateway startup.
        slots = asyncio.Semaphore(1)
        _GRANTS_SLOTS[loop] = weakref.ref(slots)
    async with slots:
        operation = _UserGrantsOperation()
        try:
            yield operation
        except BaseException as exc:
            if operation.cancellation is not None:
                raise operation.cancellation from exc
            raise
        if operation.cancellation is not None:
            raise operation.cancellation


async def run_user_grants_worker[Result](operation: Callable[[], Result]) -> Result:
    async with user_grants_operation() as owner:
        # A "read" may migrate legacy grants. Hold both its slot and request
        # owner through commit/unlink/close, including repeated cancellation.
        return await owner.run(operation)
