"""Bounded recovery admission and physical work ownership.

Queued requests do not create tasks. A cancelled running request retains its
slot until its coroutine (including native cleanup) actually finishes. All
state belongs to one event loop; no worker or connection is created at boot.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from typing import Any

RECOVERY_CAPABILITY = "transport.recovery.v1"
READ_BUDGET_SECONDS = 7.0
MAX_RUNNING = 16
MAX_CONNECTION_RUNNING = 2
MAX_WAITING = 64
MAX_CONNECTION_WAITING = 8


@dataclass(eq=False, slots=True)
class RecoveryOperation:
    request_id: str
    connection_id: str
    method: str
    key: str
    runtime: object
    deadline: float
    is_current: Callable[[], bool] = lambda: True
    predecessors: tuple[asyncio.Future[Any], ...] = ()
    subscription_token: object | None = None
    transfer: Any = None
    subscription_created: bool = False
    closed: bool = False
    started: bool = False
    task: asyncio.Task[None] | None = None
    cancel_callbacks: list[Callable[[], None]] = field(default_factory=list)

    @property
    def scope_key(self) -> tuple[int, str]:
        return id(self.runtime), self.key

    def current(self) -> bool:
        return not self.closed and time.monotonic() < self.deadline and self.is_current()

    def retire(self) -> None:
        if self.closed:
            return
        self.closed = True
        for callback in self.cancel_callbacks:
            callback()
        if self.task is not None and not self.task.done():
            self.task.cancel()


CURRENT_RECOVERY_OPERATION: ContextVar[RecoveryOperation | None] = ContextVar(
    "gateway_recovery_operation", default=None
)


@dataclass(slots=True)
class _Job:
    operation: RecoveryOperation
    run: Callable[[], Awaitable[None]]
    finish: Callable[[], None]
    expire: Callable[[], None]
    timer: asyncio.TimerHandle | None = None


class RecoveryScheduler:
    """Fair connection turns with one physical job per runtime/session key."""

    def __init__(self) -> None:
        self._waiting: OrderedDict[str, list[_Job]] = OrderedDict()
        self._active: dict[RecoveryOperation, _Job] = {}
        self._jobs: dict[RecoveryOperation, _Job] = {}
        self._active_keys: set[tuple[int, str]] = set()
        self._active_connections: dict[str, int] = {}
        self._mutation_tails: dict[tuple[int, str], asyncio.Future[None]] = {}

    def mutation_tail(self, runtime: object, key: str) -> asyncio.Future[None] | None:
        return self._mutation_tails.get((id(runtime), key))

    def admit_mutation(
        self, runtime: object, key: str,
    ) -> tuple[asyncio.Future[None] | None, asyncio.Future[None]]:
        scope_key = id(runtime), key
        predecessor = self._mutation_tails.get(scope_key)
        tail: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._mutation_tails[scope_key] = tail

        def clear(_: asyncio.Future[None]) -> None:
            if self._mutation_tails.get(scope_key) is tail:
                del self._mutation_tails[scope_key]

        tail.add_done_callback(clear)
        return predecessor, tail

    @property
    def running(self) -> int:
        return len(self._active)

    @property
    def waiting(self) -> int:
        return sum(map(len, self._waiting.values()))

    def submit(
        self,
        operation: RecoveryOperation,
        run: Callable[[], Awaitable[None]],
        *,
        finish: Callable[[], None] = lambda: None,
        expire: Callable[[], None] = lambda: None,
    ) -> bool:
        if (
            operation in self._jobs
            or self.waiting >= MAX_WAITING
            or len(self._waiting.get(operation.connection_id, ())) >= MAX_CONNECTION_WAITING
            or not operation.current()
        ):
            return False
        job = _Job(operation, run, finish, expire)
        self._jobs[operation] = job
        self._waiting.setdefault(operation.connection_id, []).append(job)
        job.timer = asyncio.get_running_loop().call_later(
            max(0.0, operation.deadline - time.monotonic()), self._expire, operation
        )
        for predecessor in operation.predecessors:
            if not predecessor.done():
                predecessor.add_done_callback(self._predecessor_finished)
        self._pump()
        return True

    def cancel(self, operation: RecoveryOperation) -> None:
        operation.retire()
        if operation in self._active:
            return
        job = self._jobs.get(operation)
        if job is not None:
            self._remove_waiting(job)
            self._finish(job)
        self._pump()

    def cancel_connection(self, connection_id: str) -> None:
        for operation in tuple(self._jobs):
            if operation.connection_id == connection_id:
                self.cancel(operation)

    def _predecessor_finished(self, _: asyncio.Future[Any]) -> None:
        self._pump()

    def _remove_waiting(self, job: _Job) -> None:
        connection_id = job.operation.connection_id
        queue = self._waiting.get(connection_id)
        if queue is not None and job in queue:
            queue.remove(job)
            if not queue:
                del self._waiting[connection_id]

    def _expire(self, operation: RecoveryOperation) -> None:
        job = self._jobs.get(operation)
        if job is None:
            return
        if not operation.closed:
            job.expire()
        self.cancel(operation)

    def _pump(self) -> None:
        while len(self._active) < MAX_RUNNING:
            selected: _Job | None = None
            for connection_id, queue in tuple(self._waiting.items()):
                if self._active_connections.get(connection_id, 0) >= MAX_CONNECTION_RUNNING:
                    continue
                for job in tuple(queue):
                    operation = job.operation
                    if not operation.current():
                        self._remove_waiting(job)
                        operation.retire()
                        self._finish(job)
                        continue
                    if operation.scope_key in self._active_keys or any(
                        not predecessor.done() for predecessor in operation.predecessors
                    ):
                        continue
                    selected = job
                    break
                if selected is not None:
                    break
            if selected is None:
                return
            operation = selected.operation
            self._remove_waiting(selected)
            if operation.connection_id in self._waiting:
                self._waiting.move_to_end(operation.connection_id)
            self._active[operation] = selected
            self._active_keys.add(operation.scope_key)
            self._active_connections[operation.connection_id] = (
                self._active_connections.get(operation.connection_id, 0) + 1
            )
            operation.started = True
            operation.task = asyncio.create_task(
                self._run(selected), name="gateway-recovery"
            )
            operation.task.add_done_callback(partial(self._done, selected))

    async def _run(self, job: _Job) -> None:
        token = CURRENT_RECOVERY_OPERATION.set(job.operation)
        try:
            if job.operation.current():
                await job.run()
        finally:
            CURRENT_RECOVERY_OPERATION.reset(token)

    def _done(self, job: _Job, task: asyncio.Task[None]) -> None:
        operation = job.operation
        self._active.pop(operation, None)
        self._active_keys.discard(operation.scope_key)
        count = self._active_connections.get(operation.connection_id, 1) - 1
        if count:
            self._active_connections[operation.connection_id] = count
        else:
            self._active_connections.pop(operation.connection_id, None)
        if not task.cancelled():
            # Dispatch owns error reporting. Retrieving the exception also
            # supervises unexpected failures without orphan-task warnings.
            task.exception()
        self._finish(job)
        self._pump()

    def _finish(self, job: _Job) -> None:
        if self._jobs.pop(job.operation, None) is None:
            return
        if job.timer is not None:
            job.timer.cancel()
        job.finish()


_SCHEDULERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, RecoveryScheduler] = (
    weakref.WeakKeyDictionary()
)


def get_recovery_scheduler() -> RecoveryScheduler:
    loop = asyncio.get_running_loop()
    scheduler = _SCHEDULERS.get(loop)
    if scheduler is None:
        scheduler = RecoveryScheduler()
        _SCHEDULERS[loop] = scheduler
    return scheduler
