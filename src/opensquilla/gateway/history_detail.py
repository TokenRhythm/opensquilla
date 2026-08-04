"""Bounded-memory snapshots for chunked Gateway history details.

The history RPC may need to expose a single canonical transcript entry whose
encoded representation is much larger than a WebSocket response.  This module
keeps small projections in a bounded in-memory LRU and spills larger projections
to an OS-managed temporary file.  The disk budget is deliberately a soft cache
target: one large existing entry may exceed it and remain readable, while all
other disk entries are evicted.  Real filesystem failures are reported instead
of turning a cache policy into a data-access limit.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import tempfile
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

DEFAULT_HISTORY_DETAIL_MEMORY_THRESHOLD_BYTES = 1024 * 1024
DEFAULT_HISTORY_DETAIL_MEMORY_CACHE_BYTES = 64 * 1024 * 1024
DEFAULT_HISTORY_DETAIL_DISK_BUDGET_BYTES = 512 * 1024 * 1024
DEFAULT_HISTORY_DETAIL_MAX_ENTRY_BYTES = 512 * 1024 * 1024
DEFAULT_HISTORY_DETAIL_CACHE_ENTRIES = 16
DEFAULT_HISTORY_DETAIL_TTL_SECONDS = 5 * 60
DEFAULT_HISTORY_DETAIL_MAX_CHUNK_BYTES = 256 * 1024


class HistoryDetailSpoolError(RuntimeError):
    """Base error for the history-detail spool."""


class HistoryDetailSpoolClosedError(HistoryDetailSpoolError):
    """Raised when a request targets a closed spool."""


class HistoryDetailSpoolClearingError(HistoryDetailSpoolError):
    """Raised when a request races explicit cache cleanup."""


class HistoryDetailCapacityError(HistoryDetailSpoolError):
    """Raised when the bounded entry table cannot admit another build."""


class HistoryDetailEntryTooLargeError(HistoryDetailSpoolError):
    """Raised when one projected detail exceeds the hard spool entry cap."""


class HistoryDetailStorageError(HistoryDetailSpoolError):
    """Raised when the operating system cannot create, write, or read a spool."""


@dataclass(frozen=True, slots=True)
class HistoryDetailChunk:
    """One raw chunk and the stable metadata for its complete projection."""

    data: bytes
    offset: int
    next_offset: int | None
    total: int
    sha256: str
    storage: Literal["memory", "disk"]


@dataclass(frozen=True, slots=True)
class HistoryDetailSpoolStats:
    """Non-sensitive resource counters used for diagnostics and tests."""

    entries: int
    inflight: int
    memory_bytes: int
    disk_bytes: int
    building_memory_bytes: int
    building_disk_bytes: int
    disk_over_budget: bool


@dataclass(slots=True)
class _HistoryDetailEntry:
    memory: bytearray | None
    temporary: BinaryIO | None
    total: int
    sha256: str
    expires_at: float

    @property
    def storage(self) -> Literal["memory", "disk"]:
        return "memory" if self.memory is not None else "disk"


@dataclass(slots=True)
class _BuiltHistoryDetail:
    memory: bytearray | None
    temporary: BinaryIO | None
    total: int
    sha256: str


HistoryDetailBuilder = Callable[["HistoryDetailWriter"], Awaitable[None] | None]


class HistoryDetailWriter:
    """Incremental writer supplied to a :class:`HistoryDetailSpool` builder.

    Builders should emit reasonably sized byte chunks.  Keeping projection and
    UTF-8/JSON encoding incremental is what prevents the caller from allocating
    a second, unbounded encoded copy before this class can spill it to disk.
    """

    def __init__(self, owner: HistoryDetailSpool) -> None:
        self._owner = owner
        self._memory = bytearray()
        self._temporary: BinaryIO | None = None
        self._digest = hashlib.sha256()
        self._total = 0
        self._memory_reservation = 0
        self._disk_reservation = 0
        self._finished = False
        self._adopted = False

    @property
    def total(self) -> int:
        return self._total

    async def write(self, data: bytes | bytearray | memoryview) -> None:
        """Append bytes, spilling to disk before the memory threshold is crossed."""
        if self._finished:
            raise RuntimeError("history detail writer is already finalized")
        if not isinstance(data, bytes | bytearray | memoryview):
            raise TypeError("history detail chunks must be bytes-like")
        view = memoryview(data).cast("B")
        if not view:
            return
        if self._total + len(view) > self._owner.max_entry_bytes:
            raise HistoryDetailEntryTooLargeError(
                "History detail exceeds the bounded spool entry limit."
            )

        if (
            self._temporary is None
            and self._total + len(view) <= self._owner.memory_threshold_bytes
            and await self._owner._try_reserve_building_memory(self, len(view))
        ):
            try:
                self._memory.extend(view)
            except BaseException:
                await self._owner._release_building_memory(self, len(view))
                raise
            self._digest.update(view)
            self._total += len(view)
            return

        await self._write_to_disk(view)

    async def _write_to_disk(self, view: memoryview) -> None:
        if self._temporary is None:
            try:
                temporary = self._owner._new_temporary_file()
            except OSError as exc:
                raise HistoryDetailStorageError(
                    "Unable to create a temporary history-detail spool."
                ) from exc

            disk_reservation = len(self._memory) + len(view)
            try:
                await self._owner._reserve_building_disk(self, disk_reservation)
            except BaseException:
                with suppress(OSError):
                    temporary.close()
                raise
            self._temporary = temporary
            try:
                if self._memory:
                    temporary.write(self._memory)
                temporary.write(view)
            except OSError as exc:
                raise HistoryDetailStorageError(
                    "Unable to write the temporary history-detail spool."
                ) from exc

            if self._memory_reservation:
                await self._owner._release_building_memory(
                    self,
                    self._memory_reservation,
                )
            self._memory.clear()
        else:
            await self._owner._reserve_building_disk(self, len(view))
            try:
                self._temporary.write(view)
            except OSError as exc:
                raise HistoryDetailStorageError(
                    "Unable to write the temporary history-detail spool."
                ) from exc

        self._digest.update(view)
        self._total += len(view)

    def _finish(self) -> _BuiltHistoryDetail:
        if self._finished:
            raise RuntimeError("history detail writer is already finalized")
        self._finished = True
        if self._temporary is not None:
            try:
                self._temporary.flush()
                self._temporary.seek(0)
            except OSError as exc:
                raise HistoryDetailStorageError(
                    "Unable to finalize the temporary history-detail spool."
                ) from exc
        return _BuiltHistoryDetail(
            memory=self._memory if self._temporary is None else None,
            temporary=self._temporary,
            total=self._total,
            sha256=self._digest.hexdigest(),
        )

    async def _abort(self) -> None:
        if self._adopted:
            return
        if self._temporary is not None:
            with suppress(OSError):
                self._temporary.close()
            self._temporary = None
        self._memory.clear()
        await self._owner._release_building_reservations(self)


class HistoryDetailSpool:
    """Single-flight, LRU/TTL history-detail cache with bounded memory use.

    ``disk_budget_bytes`` is an LRU target while ``max_entry_bytes`` is a hard
    per-detail ceiling. A single projection may temporarily exceed the soft
    cache target but can never grow without bound. Filesystem exhaustion or
    other I/O errors surface as :class:`HistoryDetailStorageError`.
    """

    def __init__(
        self,
        *,
        memory_threshold_bytes: int = DEFAULT_HISTORY_DETAIL_MEMORY_THRESHOLD_BYTES,
        max_memory_bytes: int = DEFAULT_HISTORY_DETAIL_MEMORY_CACHE_BYTES,
        disk_budget_bytes: int = DEFAULT_HISTORY_DETAIL_DISK_BUDGET_BYTES,
        max_entry_bytes: int = DEFAULT_HISTORY_DETAIL_MAX_ENTRY_BYTES,
        max_entries: int = DEFAULT_HISTORY_DETAIL_CACHE_ENTRIES,
        ttl_seconds: float = DEFAULT_HISTORY_DETAIL_TTL_SECONDS,
        max_chunk_bytes: int = DEFAULT_HISTORY_DETAIL_MAX_CHUNK_BYTES,
        temporary_parent: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if memory_threshold_bytes < 1:
            raise ValueError("memory_threshold_bytes must be positive")
        if max_memory_bytes < memory_threshold_bytes:
            raise ValueError("max_memory_bytes must cover the memory threshold")
        if disk_budget_bytes < 1:
            raise ValueError("disk_budget_bytes must be positive")
        if max_entry_bytes < 1:
            raise ValueError("max_entry_bytes must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_chunk_bytes < 1:
            raise ValueError("max_chunk_bytes must be positive")

        self.memory_threshold_bytes = memory_threshold_bytes
        self.max_memory_bytes = max_memory_bytes
        self.disk_budget_bytes = disk_budget_bytes
        self.max_entry_bytes = max_entry_bytes
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.max_chunk_bytes = max_chunk_bytes
        self._temporary_parent = Path(temporary_parent) if temporary_parent is not None else None
        self._clock = clock

        self._entries: OrderedDict[Hashable, _HistoryDetailEntry] = OrderedDict()
        self._inflight: dict[Hashable, asyncio.Task[None]] = {}
        self._memory_bytes = 0
        self._disk_bytes = 0
        self._building_memory_bytes = 0
        self._building_disk_bytes = 0
        self._spool_root: Path | None = None
        self._lock = asyncio.Lock()
        self._build_lock = asyncio.Lock()
        self._closed = False
        self._clearing = False
        self._generation = 0
        self._expiry_handle: asyncio.TimerHandle | None = None

    async def read_chunk(
        self,
        key: Hashable,
        *,
        offset: int,
        max_bytes: int,
        builder: HistoryDetailBuilder,
    ) -> HistoryDetailChunk:
        """Build once if needed, then read one bounded raw chunk.

        Cancellation of an individual waiter does not cancel a shared build.
        Explicit :meth:`clear` or :meth:`aclose` does cancel it and wakes every
        waiter with cancellation.
        """
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
            or max_bytes > self.max_chunk_bytes
        ):
            raise ValueError(f"max_bytes must be between 1 and {self.max_chunk_bytes}")

        while True:
            await self._ensure_entry(key, builder)
            async with self._lock:
                self._raise_if_unavailable_locked()
                self._prune_expired_locked()
                entry = self._entries.pop(key, None)
                if entry is None:
                    # Another completed build may have displaced this entry
                    # before this waiter reacquired the lock. Rebuild normally.
                    continue
                if offset > entry.total:
                    self._entries[key] = entry
                    raise ValueError(f"offset must be between 0 and {entry.total}")
                entry.expires_at = self._clock() + self.ttl_seconds
                self._entries[key] = entry
                self._schedule_expiry_locked()
                end = min(entry.total, offset + max_bytes)
                try:
                    data = self._read_entry(entry, offset=offset, end=end)
                except OSError as exc:
                    self._evict_key_locked(key)
                    raise HistoryDetailStorageError(
                        "Unable to read the temporary history-detail spool."
                    ) from exc
                return HistoryDetailChunk(
                    data=data,
                    offset=offset,
                    next_offset=end if end < entry.total else None,
                    total=entry.total,
                    sha256=entry.sha256,
                    storage=entry.storage,
                )

    async def stats(self) -> HistoryDetailSpoolStats:
        async with self._lock:
            self._prune_expired_locked()
            self._schedule_expiry_locked()
            return HistoryDetailSpoolStats(
                entries=len(self._entries),
                inflight=len(self._inflight),
                memory_bytes=self._memory_bytes,
                disk_bytes=self._disk_bytes,
                building_memory_bytes=self._building_memory_bytes,
                building_disk_bytes=self._building_disk_bytes,
                disk_over_budget=self._disk_bytes > self.disk_budget_bytes,
            )

    async def clear(self) -> None:
        """Cancel in-flight builds and release every cached resource."""
        async with self._lock:
            if self._closed:
                return
            self._clearing = True
            self._generation += 1
            if self._expiry_handle is not None:
                self._expiry_handle.cancel()
                self._expiry_handle = None
            tasks = list(self._inflight.values())
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            for key in list(self._entries):
                self._evict_key_locked(key)
            self._inflight.clear()
            self._building_memory_bytes = 0
            self._building_disk_bytes = 0
            self._clearing = False

    async def aclose(self) -> None:
        """Permanently close the spool and remove its private temporary root."""
        async with self._lock:
            if self._closed:
                return
        await self.clear()
        async with self._lock:
            self._closed = True
            root = self._spool_root
            self._spool_root = None
        if root is not None:
            with suppress(FileNotFoundError, OSError):
                root.rmdir()

    async def _ensure_entry(self, key: Hashable, builder: HistoryDetailBuilder) -> None:
        async with self._lock:
            self._raise_if_unavailable_locked()
            self._prune_expired_locked()
            entry = self._entries.pop(key, None)
            if entry is not None:
                entry.expires_at = self._clock() + self.ttl_seconds
                self._entries[key] = entry
                self._schedule_expiry_locked()
                return
            task = self._inflight.get(key)
            if task is None:
                self._make_entry_slot_locked()
                generation = self._generation
                task = asyncio.create_task(
                    self._build_and_store(key, builder, generation),
                    name="gateway-history-detail-build",
                )
                task.add_done_callback(_consume_task_exception)
                self._inflight[key] = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            # A cancelled caller must remain cancellable without tearing down a
            # shared build. Conversely, clear()/aclose() cancel the shared task
            # itself; translate that cache lifecycle race into a regular spool
            # error so an RPC waiter can receive a bounded retryable ResFrame.
            if task.cancelled():
                raise HistoryDetailSpoolClearingError(
                    "History-detail cache was cleared during projection."
                ) from exc
            raise

    async def _build_and_store(
        self,
        key: Hashable,
        builder: HistoryDetailBuilder,
        generation: int,
    ) -> None:
        writer = HistoryDetailWriter(self)
        try:
            async with self._build_lock:
                result = builder(writer)
                if inspect.isawaitable(result):
                    await result
                elif result is not None:
                    raise TypeError("history detail builder must return None")
                built = writer._finish()
                async with self._lock:
                    if self._closed or self._clearing or generation != self._generation:
                        raise HistoryDetailSpoolClearingError(
                            "History-detail cache was cleared during projection."
                        )
                    entry = _HistoryDetailEntry(
                        memory=built.memory,
                        temporary=built.temporary,
                        total=built.total,
                        sha256=built.sha256,
                        expires_at=self._clock() + self.ttl_seconds,
                    )
                    if entry.memory is not None:
                        self._building_memory_bytes -= writer._memory_reservation
                        self._memory_bytes += entry.total
                        writer._memory_reservation = 0
                    else:
                        self._building_disk_bytes -= writer._disk_reservation
                        self._disk_bytes += entry.total
                        writer._disk_reservation = 0
                    writer._adopted = True
                    self._entries[key] = entry
                    self._trim_disk_locked()
                    self._schedule_expiry_locked()
        except BaseException:
            await writer._abort()
            raise
        finally:
            async with self._lock:
                current = self._inflight.get(key)
                if current is asyncio.current_task():
                    self._inflight.pop(key, None)

    async def _try_reserve_building_memory(
        self,
        writer: HistoryDetailWriter,
        amount: int,
    ) -> bool:
        async with self._lock:
            self._raise_if_unavailable_locked()
            self._prune_expired_locked()
            while self._memory_bytes + self._building_memory_bytes + amount > self.max_memory_bytes:
                key = self._oldest_key_for_storage_locked("memory")
                if key is None:
                    return False
                self._evict_key_locked(key)
            self._building_memory_bytes += amount
            writer._memory_reservation += amount
            return True

    async def _release_building_memory(
        self,
        writer: HistoryDetailWriter,
        amount: int,
    ) -> None:
        async with self._lock:
            self._building_memory_bytes = max(0, self._building_memory_bytes - amount)
            writer._memory_reservation = max(0, writer._memory_reservation - amount)

    async def _reserve_building_disk(
        self,
        writer: HistoryDetailWriter,
        amount: int,
    ) -> None:
        async with self._lock:
            self._raise_if_unavailable_locked()
            self._prune_expired_locked()
            self._building_disk_bytes += amount
            writer._disk_reservation += amount
            self._trim_disk_locked()

    async def _release_building_reservations(self, writer: HistoryDetailWriter) -> None:
        async with self._lock:
            self._building_memory_bytes = max(
                0,
                self._building_memory_bytes - writer._memory_reservation,
            )
            self._building_disk_bytes = max(
                0,
                self._building_disk_bytes - writer._disk_reservation,
            )
            writer._memory_reservation = 0
            writer._disk_reservation = 0
            self._trim_disk_locked()

    def _make_entry_slot_locked(self) -> None:
        while len(self._entries) + len(self._inflight) >= self.max_entries:
            if not self._entries:
                raise HistoryDetailCapacityError(
                    "History-detail build concurrency reached the entry limit."
                )
            self._evict_key_locked(next(iter(self._entries)))

    def _trim_disk_locked(self) -> None:
        while self._disk_bytes + self._building_disk_bytes > self.disk_budget_bytes:
            disk_keys = [
                key for key, entry in self._entries.items() if entry.storage == "disk"
            ]
            if not disk_keys:
                return
            # Once a build finishes, one oversized entry is allowed to own the
            # disk cache.  A later disk build evicts that owner before growing.
            if self._building_disk_bytes == 0 and len(disk_keys) == 1:
                return
            self._evict_key_locked(disk_keys[0])

    def _prune_expired_locked(self) -> None:
        now = self._clock()
        for key, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                self._evict_key_locked(key)

    def _schedule_expiry_locked(self) -> None:
        if self._expiry_handle is not None:
            self._expiry_handle.cancel()
            self._expiry_handle = None
        if self._closed or self._clearing or not self._entries:
            return
        next_expiry = min(entry.expires_at for entry in self._entries.values())
        delay = max(0.0, next_expiry - self._clock())
        loop = asyncio.get_running_loop()
        self._expiry_handle = loop.call_later(delay, self._expiry_due)

    def _expiry_due(self) -> None:
        self._expiry_handle = None
        try:
            task = asyncio.create_task(
                self._expire_entries(),
                name="gateway-history-detail-expiry",
            )
        except RuntimeError:
            return
        task.add_done_callback(_consume_task_exception)

    async def _expire_entries(self) -> None:
        async with self._lock:
            if self._closed or self._clearing:
                return
            self._prune_expired_locked()
            self._schedule_expiry_locked()

    def _oldest_key_for_storage_locked(
        self,
        storage: Literal["memory", "disk"],
    ) -> Hashable | None:
        return next(
            (key for key, entry in self._entries.items() if entry.storage == storage),
            None,
        )

    def _evict_key_locked(self, key: Hashable) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        if entry.memory is not None:
            self._memory_bytes = max(0, self._memory_bytes - entry.total)
            entry.memory.clear()
        else:
            self._disk_bytes = max(0, self._disk_bytes - entry.total)
            if entry.temporary is not None:
                with suppress(OSError):
                    entry.temporary.close()

    @staticmethod
    def _read_entry(entry: _HistoryDetailEntry, *, offset: int, end: int) -> bytes:
        if entry.memory is not None:
            return bytes(entry.memory[offset:end])
        if entry.temporary is None:
            raise OSError("history-detail disk entry has no temporary file")
        entry.temporary.seek(offset)
        data = entry.temporary.read(end - offset)
        if len(data) != end - offset:
            raise OSError("history-detail spool ended before its declared size")
        return data

    def _new_temporary_file(self) -> BinaryIO:
        if self._spool_root is None:
            parent = str(self._temporary_parent) if self._temporary_parent is not None else None
            self._spool_root = Path(
                tempfile.mkdtemp(prefix="opensquilla-history-detail-", dir=parent)
            )
        return tempfile.TemporaryFile(  # noqa: SIM115 - retained by the cache entry
            mode="w+b",
            prefix="detail-",
            dir=self._spool_root,
        )

    def _raise_if_unavailable_locked(self) -> None:
        if self._closed:
            raise HistoryDetailSpoolClosedError("History-detail spool is closed.")
        if self._clearing:
            raise HistoryDetailSpoolClearingError("History-detail spool is being cleared.")


def _consume_task_exception(task: asyncio.Task[None]) -> None:
    """Retrieve background failures when the only waiter was cancelled."""
    with suppress(asyncio.CancelledError, Exception):
        task.exception()
