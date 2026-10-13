"""One owned SQLite thread, with bounded async admission and settlement.

Only database work runs on the thread. Network operations and queue handoff
remain on the event loop. Waiting SDK callbacks are upstream of this budget.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import structlog

from opensquilla.channels.contract import classify_channel_send_error, normalize_channel_send_result
from opensquilla.channels.delivery_store import (
    ChannelDeliveryStore,
    TransportLease,
    _safe_error_text,
)

log = structlog.get_logger(__name__)


@dataclass(eq=False)
class _Job:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    result: asyncio.Future[Any]
    submitted_at: float
    lane: str
    key: str = ""
    started: bool = False
    abandoned: bool = False
    on_result: Callable[[Any], None] | None = None
    on_abandoned: Callable[[Any], None] | None = None


class ChannelStorageClosedError(RuntimeError):
    """The manager is no longer accepting work for this store/channel."""


class AsyncChannelDeliveryStore:
    """Async channel store; all connection access belongs to one worker.

    Ordinary and settlement queues hold 64 and 16 waiting jobs. Leases have
    one reserved job per channel, with earliest expiry first. Eight settlement
    jobs yield to an ordinary job; leases always take precedence. A running
    SQLite call is not interruptible and retains the existing busy timeout.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        ordinary_capacity: int = 64,
        settlement_capacity: int = 16,
        store_factory: Callable[..., ChannelDeliveryStore] = ChannelDeliveryStore,
    ) -> None:
        self.path = Path(db_path)
        self._factory = store_factory
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="channel-sqlite")
        self._store: ChannelDeliveryStore | None = None
        self._open_task: asyncio.Task[Any] | None = None
        self._consumer: asyncio.Task[Any] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._condition = asyncio.Condition()
        self._queues: dict[str, deque[_Job]] = {
            "ordinary": deque(),
            "settlement": deque(),
            "lease": deque(),
        }
        self._capacities = {
            "ordinary": max(1, ordinary_capacity),
            "settlement": max(1, settlement_capacity),
        }
        self._lease_keys: set[str] = set()
        self._closed_channels: set[str] = set()
        self._owned: set[asyncio.Task[Any]] = set()
        self._settlement_error: Exception | None = None
        self._ingress: dict[str, set[asyncio.Future[None]]] = {}
        self._aborting = False
        self._draining = False
        self._closing = False
        self._closed = False
        self._settlement_streak = 0
        self._active = 0
        self._active_job: _Job | None = None
        self._waiting = 0
        self._high_water = 0
        self._max_wait_seconds = 0.0

    async def open(self) -> None:
        if self._draining or self._closing or self._closed:
            raise ChannelStorageClosedError("Channel storage is closing")
        if self._open_task is None:
            self._open_task = asyncio.create_task(self._open(), name="channel-storage-open")
        await asyncio.shield(self._open_task)

    async def _open(self) -> None:
        loop = asyncio.get_running_loop()
        self._store = await loop.run_in_executor(self._executor, self._factory, self.path)
        self._consumer = asyncio.create_task(self._consume(), name="channel-storage-worker")

    def resume_channel(self, channel_name: str) -> None:
        self._closed_channels.discard(channel_name)

    def stop_accepting(self, channel_name: str) -> None:
        self._closed_channels.add(channel_name)

    async def drain_channel(self, channel_name: str) -> None:
        while tasks := self._ingress.get(channel_name):
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(tasks)), return_exceptions=True
            )

        async with self._condition:
            await self._condition.wait_for(
                lambda: (
                    not (
                        self._active_job is not None
                        and self._active_job.method == "accept_inbound"
                        and self._active_job.args[0] == channel_name
                    )
                )
            )

    def metrics(self) -> dict[str, int | float]:
        return {
            "queued": sum(map(len, self._queues.values())),
            "active": self._active,
            "waiting": self._waiting,
            "queue_high_water": self._high_water,
            "max_queue_wait_seconds": self._max_wait_seconds,
        }

    def _retain(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name="channel-storage-settlement")
        self._owned.add(task)

        def settled(done: asyncio.Task[Any]) -> None:
            self._owned.discard(done)
            if not done.cancelled():
                # Retrieve exceptions even when a caller was cancelled; normal
                # callers still observe the same exception through await.
                error = done.exception()
                if isinstance(error, Exception):
                    self._settlement_error = error
                    log.error("channel.storage_settlement_failed", error_type=type(error).__name__)

        task.add_done_callback(settled)
        return task

    async def _settle(self, method: str, *args: Any, **kwargs: Any) -> Any:
        # A terminal result must survive caller cancellation even while waiting
        # for reserved capacity. Never turn a known success into "unknown".
        args, kwargs = copy.deepcopy((args, kwargs))
        task = self._retain(self._call(method, *args, lane="settlement", **kwargs))
        return await asyncio.shield(task)

    async def _call(
        self,
        method: str,
        *args: Any,
        lane: str = "ordinary",
        key: str = "",
        on_result: Callable[[Any], None] | None = None,
        on_abandoned: Callable[[Any], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        # Freeze before the first await: callers may mutate messages while
        # this operation is waiting for a queue slot.
        args, kwargs = copy.deepcopy((args, kwargs))
        if (self._draining and lane != "settlement") or (
            self._aborting and method not in {"release_transport_lease", "fail_inbound_snapshot"}
        ):
            raise ChannelStorageClosedError("Channel storage is draining")
        if self._open_task is None:
            await self.open()
        else:
            await asyncio.shield(self._open_task)
        loop = asyncio.get_running_loop()
        job = _Job(
            method,
            args,
            kwargs,
            loop.create_future(),
            time.monotonic(),
            lane,
            key,
            on_result=on_result,
            on_abandoned=on_abandoned,
        )
        async with self._condition:
            self._waiting += 1
            try:
                await self._condition.wait_for(
                    lambda: (
                        self._closing
                        or (self._draining and lane != "settlement")
                        or (
                            key not in self._lease_keys
                            if lane == "lease"
                            else len(self._queues[lane]) < self._capacities[lane]
                        )
                    )
                )
                if (
                    self._closing
                    or (self._draining and lane != "settlement")
                    or (
                        self._aborting
                        and method not in {"release_transport_lease", "fail_inbound_snapshot"}
                    )
                ):
                    raise ChannelStorageClosedError("Channel storage is closing")
                self._queues[lane].append(job)
                if lane == "lease":
                    self._lease_keys.add(key)
                self._high_water = max(self._high_water, sum(map(len, self._queues.values())))
                self._condition.notify_all()
            finally:
                self._waiting -= 1
        try:
            return await asyncio.shield(job.result)
        except asyncio.CancelledError:
            async with self._condition:
                job.abandoned = True
                if not job.started:
                    if job in self._queues[lane]:
                        self._queues[lane].remove(job)
                    self._lease_keys.discard(key)
                    if job.result.done() and not job.result.cancelled():
                        job.result.exception()
                    else:
                        job.result.cancel()
                    self._condition.notify_all()
                elif job.result.done() and not job.result.cancelled():
                    if on_abandoned is not None and job.result.exception() is None:
                        on_abandoned(job.result.result())
                        job.on_abandoned = None
            raise

    def _next(self) -> _Job:
        leases = self._queues["lease"]
        if leases:
            job = min(leases, key=lambda item: getattr(item.args[0], "expires_at", float("inf")))
            leases.remove(job)
            return job
        ordinary, settlement = self._queues["ordinary"], self._queues["settlement"]
        if settlement and (self._settlement_streak < 8 or not ordinary):
            self._settlement_streak += 1
            return settlement.popleft()
        self._settlement_streak = 0
        return ordinary.popleft()

    def _execute(self, job: _Job) -> Any:
        assert self._store is not None
        result = getattr(self._store, job.method)(*job.args, **job.kwargs)
        # sqlite.Row is a connection-independent value, but do not expose any
        # SQLite object through the async boundary.
        if job.method == "send_record" and result is not None:
            result = dict(result)
        return result

    async def _consume(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            async with self._condition:
                await self._condition.wait_for(lambda: self._closed or any(self._queues.values()))
                if self._closed and not any(self._queues.values()):
                    return
                job = self._next()
                job.started = True
                self._active = 1
                self._active_job = job
                self._max_wait_seconds = max(
                    self._max_wait_seconds, time.monotonic() - job.submitted_at
                )
                self._condition.notify_all()
            try:
                result = await loop.run_in_executor(self._executor, self._execute, job)
                if job.on_result is not None:
                    job.on_result(result)
                if job.abandoned and job.on_abandoned is not None:
                    job.on_abandoned(result)
                    job.on_abandoned = None
                job.result.set_result(result)
            except Exception as exc:
                job.result.set_exception(exc)
                if job.abandoned:
                    job.result.exception()
                    log.error(
                        "channel.storage_abandoned_failed",
                        operation=job.method,
                        error_type=type(exc).__name__,
                    )
            finally:
                async with self._condition:
                    self._active = 0
                    self._active_job = None
                    self._lease_keys.discard(job.key)
                    self._condition.notify_all()

    async def abort_pending(self) -> None:
        """Deadline expired: reject jobs not yet submitted, retain active SQL.

        Only cleanup for an already committed claim/lease may subsequently
        enter the settlement lane. The worker closes its connection after
        that active operation and its cleanup have settled.
        """
        async with self._condition:
            self._aborting = self._draining = True
            for queue in self._queues.values():
                while queue:
                    job = queue.popleft()
                    self._lease_keys.discard(job.key)
                    job.result.set_exception(
                        ChannelStorageClosedError("Channel shutdown deadline expired")
                    )
                    if job.abandoned:
                        job.result.exception()
            self._condition.notify_all()

    async def close(self) -> None:
        if self._close_task is None:
            self._draining = True
            self._close_task = asyncio.create_task(self._close(), name="channel-storage-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        async with self._condition:
            self._condition.notify_all()
        if self._open_task is not None:
            try:
                await asyncio.shield(self._open_task)
            except BaseException:
                self._closing = self._closed = True
                self._executor.shutdown(wait=False)
                raise
        # A just-finished abandoned claim can create a settlement owner.
        # Check idle and ownership together before stopping queue admission.
        while True:
            if self._owned:
                await asyncio.gather(*tuple(self._owned), return_exceptions=True)
            async with self._condition:
                await self._condition.wait_for(
                    lambda: not self._active and not any(self._queues.values())
                )
                if self._owned:
                    continue
                self._closing = True
                self._closed = True
                self._condition.notify_all()
                break
        if self._consumer is not None:
            await self._consumer
        if self._store is not None:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._store.close)
        self._executor.shutdown(wait=False)
        if self._settlement_error is not None:
            raise RuntimeError(
                "Channel storage settlement failed during shutdown"
            ) from self._settlement_error

    async def accept_inbound(self, channel_name: str, message: Any) -> bool:
        return cast(bool, await self._call("accept_inbound", channel_name, message))

    async def enqueue(self, channel_name: str, message: Any, queue: Any) -> bool:
        if channel_name in self._closed_channels:
            raise ChannelStorageClosedError(f"Channel {channel_name} is stopping")
        snapshot = copy.deepcopy(message)
        completed = asyncio.get_running_loop().create_future()
        tasks = self._ingress.setdefault(channel_name, set())
        tasks.add(completed)

        def handoff(accepted: bool) -> None:
            if accepted and not self._aborting:
                queue.put_nowait(snapshot)

        try:
            return cast(
                bool, await self._call("accept_inbound", channel_name, snapshot, on_result=handoff)
            )
        finally:
            completed.set_result(None)
            tasks.discard(completed)

    async def claim_inbound(self, channel_name: str, message: Any) -> Any:
        def abandoned(claim: Any) -> None:
            if claim is not None:
                self._retain(self.fail_inbound(claim, asyncio.CancelledError()))

        return await self._call("claim_inbound", channel_name, message, on_abandoned=abandoned)

    async def complete_inbound(self, *args: Any, **kwargs: Any) -> Any:
        return await self._settle("complete_inbound", *args, **kwargs)

    async def fail_inbound(self, claim: Any, error: BaseException) -> Any:
        return await self._settle(
            "fail_inbound_snapshot",
            claim,
            type(error).__name__,
            _safe_error_text(error),
        )

    async def recover_inbound(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("recover_inbound", *args, **kwargs)

    async def begin_send(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("begin_send", *args, **kwargs)

    async def begin_send_once(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("begin_send_once", *args, **kwargs)

    async def send_record(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("send_record", *args, **kwargs)

    async def claim_failed_artifact_retry(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("claim_failed_artifact_retry", *args, **kwargs)

    async def complete_send(
        self,
        send_id: str,
        result: Any,
        *,
        capability: str = "message",
        target_id: str = "",
        safe_reason: str | None = None,
    ) -> Any:
        # SDK return objects may own locks/sockets. Normalize only the receipt
        # fields before snapshotting; never copy unrelated transport state.
        normalized = normalize_channel_send_result(
            result, capability=capability, target_id=target_id
        )
        state = "sent_unconfirmed" if result is None else normalized.status.value
        return await self._settle(
            "complete_send_receipt",
            send_id,
            normalized,
            state,
            safe_reason=safe_reason,
        )

    async def fail_send(self, send_id: str, error: BaseException, **kwargs: Any) -> Any:
        return await self._settle(
            "fail_send_snapshot",
            send_id,
            type(error).__name__,
            _safe_error_text(error),
            classify_channel_send_error(error),
            **kwargs,
        )

    async def request_pairing(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("request_pairing", *args, **kwargs)

    async def list_pairings(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("list_pairings", *args, **kwargs)

    async def set_pairing_status(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("set_pairing_status", *args, **kwargs)

    async def approve_pairing_once(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("approve_pairing_once", *args, **kwargs)

    async def diagnostics(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("diagnostics", *args, **kwargs)

    async def admission_reason_counts(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("admission_reason_counts", *args, **kwargs)

    def _abandoned_lease(self, lease: TransportLease | None) -> None:
        if lease is not None:
            # A late acquisition/renewal must not keep an adapter that never
            # received ownership alive. The SQL owner/token condition fences
            # this cleanup against a newer owner's lease.
            self._retain(self._settle("release_transport_lease", lease))

    async def acquire_transport_lease(
        self,
        channel_name: str,
        account_id: str,
        owner_id: str,
        *,
        ttl_seconds: float = 120.0,
    ) -> TransportLease | None:
        return cast(
            TransportLease | None,
            await self._call(
                "acquire_transport_lease",
                channel_name,
                account_id,
                owner_id,
                lane="lease",
                key=f"{channel_name}:{account_id}",
                on_abandoned=self._abandoned_lease,
                ttl_seconds=ttl_seconds,
            ),
        )

    async def renew_transport_lease(self, lease: TransportLease, **kwargs: Any) -> Any:
        return await self._call(
            "renew_transport_lease",
            lease,
            lane="lease",
            key=f"{lease.channel_name}:{lease.account_id}",
            on_abandoned=self._abandoned_lease,
            **kwargs,
        )

    async def release_transport_lease(self, lease: TransportLease) -> bool:
        # Release is terminal cleanup and remains admissible after a deadline.
        return cast(bool, await self._settle("release_transport_lease", lease))
