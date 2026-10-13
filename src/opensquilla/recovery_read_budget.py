"""Transport-neutral recovery read budgets shared by application and storage.

These values own deadlines and cancellation, independent of SQLite readers.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from opensquilla.session_key import canonicalize_session_key


class ReadCancelToken:
    """Event-loop cancellation with a flag readable by a SQLite worker."""

    def __init__(self) -> None:
        self._flag = threading.Event()
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._flag.is_set()

    def cancel(self) -> None:
        self._flag.set()
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


async def _drain(task: asyncio.Future[Any]) -> None:
    """Keep ownership through repeated shutdown cancellation."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except (asyncio.CancelledError, Exception):
            pass
    with contextlib.suppress(BaseException):
        task.result()


@dataclass(eq=False, slots=True)
class ReadBudget:
    key: str
    deadline: float
    cancel_token: ReadCancelToken
    workload: Literal["identity", "bulk"] = "bulk"
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        if self.cancel_token.cancelled:
            raise asyncio.CancelledError
        if self.remaining <= 0:
            raise TimeoutError("Recovery read deadline exceeded")

    def track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        with contextlib.suppress(BaseException):
            task.result()

    async def drain(self) -> None:
        """Wait for physical work; scope exit itself is deliberately immediate."""

        while self._tasks:
            pending = tuple(self._tasks)
            await _drain(asyncio.gather(*pending, return_exceptions=True))
            # gather(completed tasks) can finish synchronously before their
            # call_soon callbacks run. Remove them here as well to avoid a
            # busy loop starving those callbacks.
            for task in pending:
                self._finished(task)

    async def wait_for[Result](self, task: asyncio.Task[Result]) -> Result:
        """Bound the caller's wait without cancelling physical child work."""

        self.track(task)
        cancellation = asyncio.create_task(self.cancel_token.wait())
        try:
            self.check()
            await asyncio.wait({task, cancellation}, timeout=self.remaining,
                               return_when=asyncio.FIRST_COMPLETED)
            self.check()
            if task.done():
                return task.result()
            raise TimeoutError("Recovery read deadline exceeded")
        except asyncio.CancelledError:
            self.cancel_token.cancel()
            raise
        finally:
            cancellation.cancel()
            await _drain(cancellation)


_READ_BUDGET: ContextVar[ReadBudget | None] = ContextVar(
    "opensquilla_recovery_read_budget", default=None,
)


def current_read_budget() -> ReadBudget | None:
    return _READ_BUDGET.get()


@contextlib.contextmanager
def recovery_read_scope(
    key: str,
    *,
    deadline: float,
    cancel_token: ReadCancelToken | None = None,
    workload: Literal["identity", "bulk"] = "bulk",
) -> Iterator[ReadBudget]:
    """Bind one admission-time monotonic deadline across application reads."""

    budget = ReadBudget(canonicalize_session_key(key), deadline, cancel_token or ReadCancelToken(),
                        workload)
    token = _READ_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _READ_BUDGET.reset(token)


class ReadCapacityError(RuntimeError):
    """No physical lease is available without waiting while holding resources."""
