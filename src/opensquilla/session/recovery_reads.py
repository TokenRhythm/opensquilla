"""Transport-neutral budgets and exclusive leases for recovery reads.

Cancellation ends the caller's wait, but never releases an executing SQLite
worker's permit. The storage owner and budget retain the cleanup task until
the native call, progress handler, and transaction have all settled.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from opensquilla.session.keys import canonicalize_session_key


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


@dataclass(eq=False, slots=True)
class _Lease:
    budget: ReadBudget
    stopped: threading.Event = field(default_factory=threading.Event)
    connection: Any = None
    ready: bool = False
    finishing: bool = False
    interrupt_task: asyncio.Task[Any] | None = None

    def progress(self) -> int:
        return int(self.stopped.is_set() or self.budget.cancel_token.cancelled
                   or time.monotonic() >= self.budget.deadline)

    def retire(self) -> None:
        self.stopped.set()
        if self.connection is not None and not self.finishing and self.interrupt_task is None:
            # interrupt() must bypass the connection's query queue/lock. The
            # lease remains exclusive until this exact interrupt has completed.
            self.interrupt_task = asyncio.create_task(self.connection.interrupt())


class RecoveryReadPool:
    """Lazy, bounded recovery readers owned by one SessionStorage runtime.

    WAL readers are additional to the legacy shared reader. The serial fallback
    borrows the writer only while holding its existing operation lock.
    """

    def __init__(
        self,
        factory: Callable[[], Awaitable[Any]],
        *,
        initialize: Callable[[Any], Awaitable[None]] | None = None,
        shared_lock: asyncio.Lock | None = None,
        on_shared_failure: Callable[[], None] | None = None,
        limit: int = 4,
        bulk_limit: int = 3,
    ) -> None:
        self._factory = factory
        self._initialize = initialize
        self._shared_lock = shared_lock
        self._on_shared_failure = on_shared_failure
        self._limit = 1 if shared_lock is not None else limit
        self._bulk_limit = 1 if shared_lock is not None else bulk_limit
        self._idle: list[Any] = []
        self._physical_count = 0
        self._active: dict[str, _Lease] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._quarantined: dict[str, Any] = {}
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def physical_count(self) -> int:
        return self._physical_count

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def retiring_count(self) -> int:
        return sum(lease.stopped.is_set() for lease in self._active.values())

    async def run[Result](
        self, budget: ReadBudget, read: Callable[[Any], Awaitable[Result]],
    ) -> Result:
        budget.check()
        if (
            self._closed or budget.key in self._active or len(self._active) >= self._limit
            or (not self._idle and self._physical_count >= self._limit)
        ):
            raise ReadCapacityError("Recovery reader unavailable")
        if budget.workload == "bulk" and sum(
            lease.budget.workload == "bulk" for lease in self._active.values()
        ) >= self._bulk_limit:
            raise ReadCapacityError("Recovery bulk reader limit reached")
        lease = _Lease(budget)
        self._active[budget.key] = lease
        task = asyncio.create_task(self._execute(lease, read), name="session-recovery-read")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            return await budget.wait_for(task)
        except BaseException:
            lease.retire()
            raise

    async def _execute[Result](
        self, lease: _Lease, read: Callable[[Any], Awaitable[Result]],
    ) -> Result:
        reader: Any = None
        acquired = False
        counted = False
        try:
            if self._shared_lock is not None:
                # This waiting operation owns no native work yet. Check the
                # cancellation flag between bounded gate waits, without ever
                # interrupting the writer which currently owns the lock.
                while not acquired:
                    lease.budget.check()
                    if lease.stopped.is_set():
                        raise asyncio.CancelledError
                    try:
                        async with asyncio.timeout(min(0.05, lease.budget.remaining)):
                            await self._shared_lock.acquire()
                        acquired = True
                    except TimeoutError:
                        lease.budget.check()
            if self._idle:
                reader = self._idle.pop()
            else:
                self._physical_count += 1
                counted = True
                reader = await self._factory()
            lease.connection = reader
            lease.budget.check()
            if lease.stopped.is_set():
                raise asyncio.CancelledError
            # Factory returns the raw connection before any fallible setup.
            # From here every failure retains this reader through cleanup and
            # quarantine, including configuration followed by failed close.
            if counted and self._initialize is not None:
                await self._initialize(reader)
                lease.budget.check()
                if lease.stopped.is_set():
                    raise asyncio.CancelledError
            await reader.set_progress_handler(lease.progress, 1000)
            lease.ready = True
            return await read(reader)
        finally:
            lease.finishing = True
            cleanup = asyncio.create_task(self._release(lease, reader, acquired, counted))
            await _drain(cleanup)
            cleanup.result()

    async def _release(
        self, lease: _Lease, reader: Any, acquired: bool, counted: bool,
    ) -> None:
        reusable = False
        try:
            if lease.interrupt_task is not None:
                await _drain(lease.interrupt_task)
            if reader is not None:
                # Queued after all native work. Cancellation cannot make a
                # connection reusable before this drain and transaction check.
                await reader.set_progress_handler(None, 0)
                if reader.in_transaction:
                    await reader.rollback()
                async with reader.execute("SELECT 1") as cursor:
                    await cursor.fetchone()
                if reader.in_transaction:
                    raise RuntimeError("Recovery reader transaction did not roll back")
                reusable = lease.ready
        except BaseException:
            if self._shared_lock is not None and self._on_shared_failure is not None:
                self._on_shared_failure()
            raise
        finally:
            try:
                if reader is not None and self._shared_lock is None:
                    if reusable and not self._closed:
                        self._idle.append(reader)
                    else:
                        try:
                            await reader.close()
                        except BaseException:
                            # Do not manufacture replacement capacity when
                            # physical close cannot be proven to have finished.
                            lease.stopped.set()
                            self._quarantined[lease.budget.key] = reader
                            raise
                        self._physical_count -= 1
                elif reader is not None or counted:
                    self._physical_count -= 1
            finally:
                if acquired:
                    assert self._shared_lock is not None
                    self._shared_lock.release()
                if lease.budget.key not in self._quarantined:
                    self._active.pop(lease.budget.key, None)

    async def close(self) -> None:
        self._closed = True
        # A concurrent shutdown/reconnect must never queue a second native
        # close for the same reader. Keep the lock through physical completion
        # even when the shutdown caller is cancelled repeatedly.
        async with self._close_lock:
            closing = asyncio.create_task(self._close_once())
            await _drain(closing)
            closing.result()

    async def _close_once(self) -> None:
        for lease in tuple(self._active.values()):
            lease.retire()
        while self._tasks:
            pending = tuple(self._tasks)
            await _drain(asyncio.gather(*pending, return_exceptions=True))
            self._tasks.difference_update(pending)
        for key, reader in tuple(self._quarantined.items()):
            await reader.close()
            del self._quarantined[key]
            self._active.pop(key, None)
            self._physical_count -= 1
        while self._idle:
            reader = self._idle[-1]
            await reader.close()
            self._idle.pop()
            self._physical_count -= 1
