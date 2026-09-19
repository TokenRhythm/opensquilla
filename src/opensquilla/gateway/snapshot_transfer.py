"""Bounded, connection-owned snapshot transfers, independent of RPC dispatch.

Capture is synchronous and copies containers only (strings are immutable).
Encoding is incremental, including inside a large string, and yields between
bounded chunks. Never serialize the complete snapshot before splitting it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

SNAPSHOT_MAX_BYTES = 25 * 1024 * 1024
SNAPSHOT_SEGMENT_BYTES = 192 * 1024
SNAPSHOT_IDLE_SECONDS = 120.0
_CAPTURE_MAX_NODES = 250_000
_STRING_CHARS = 16 * 1024
_MAX_PROCESS_ENCODERS = 4
_active_encoders = 0


class SnapshotTransferError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _Object:
    items: tuple[tuple[str, Any], ...]


def freeze_json(value: Any, *, max_bytes: int = SNAPSHOT_MAX_BYTES) -> Any:
    """Own a stable JSON tree without encoding large strings under the reader."""
    nodes = 0
    minimum_bytes = 0

    def visit(item: Any, depth: int) -> Any:
        nonlocal nodes, minimum_bytes
        nodes += 1
        minimum_bytes += len(item) if isinstance(item, str) else 1
        if nodes > _CAPTURE_MAX_NODES or minimum_bytes > max_bytes or depth > 128:
            raise SnapshotTransferError("SNAPSHOT_TOO_LARGE")
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float) and math.isfinite(item):
            return item
        if isinstance(item, dict):
            pairs = []
            for key, child in item.items():
                if not isinstance(key, str):
                    raise SnapshotTransferError("SNAPSHOT_STALE")
                pairs.append((visit(key, depth + 1), visit(child, depth + 1)))
            return _Object(tuple(pairs))
        if isinstance(item, (list, tuple)):
            return tuple(visit(child, depth + 1) for child in item)
        raise SnapshotTransferError("SNAPSHOT_STALE")

    return visit(value, 0)


def _json_chunks(value: Any) -> Iterator[bytes]:
    if isinstance(value, _Object):
        yield b"{"
        for index, (key, item) in enumerate(value.items):
            if index:
                yield b","
            yield from _json_chunks(key)
            yield b":"
            yield from _json_chunks(item)
        yield b"}"
    elif isinstance(value, tuple):
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from _json_chunks(item)
        yield b"]"
    elif isinstance(value, str):
        yield b'"'
        for offset in range(0, len(value), _STRING_CHARS):
            # Escape each bounded string fragment, without its outer quotes.
            yield json.dumps(value[offset : offset + _STRING_CHARS], ensure_ascii=False)[
                1:-1
            ].encode("utf-8")
        yield b'"'
    else:
        yield json.dumps(value, allow_nan=False, separators=(",", ":")).encode("ascii")


async def encode_segments(
    frozen: Any,
    *,
    max_bytes: int = SNAPSHOT_MAX_BYTES,
    is_current: Callable[[], bool] = lambda: True,
    on_bytes: Callable[[int], None] | None = None,
) -> tuple[bytes, ...]:
    segments: list[bytes] = []
    pending = bytearray()
    size = 0
    last_yield = time.monotonic()
    try:
        for chunk in _json_chunks(frozen):
            if not is_current():
                raise SnapshotTransferError("SNAPSHOT_STALE")
            size += len(chunk)
            if size > max_bytes:
                raise SnapshotTransferError("SNAPSHOT_TOO_LARGE")
            if on_bytes is not None:
                on_bytes(len(chunk))
            pending.extend(chunk)
            while len(pending) >= SNAPSHOT_SEGMENT_BYTES:
                segments.append(bytes(pending[:SNAPSHOT_SEGMENT_BYTES]))
                del pending[:SNAPSHOT_SEGMENT_BYTES]
                await asyncio.sleep(0)
                last_yield = time.monotonic()
            if time.monotonic() - last_yield >= 0.005:
                await asyncio.sleep(0)
                last_yield = time.monotonic()
        if pending:
            segments.append(bytes(pending))
        return tuple(segments)
    finally:
        # Exception tracebacks must not retain a cancelled encoder's buffers.
        segments.clear()
        pending.clear()
        frozen = None



class SnapshotTransfer:
    """Frozen bytes and an installation receipt owned by one recovery attempt.

    Standalone legacy instances remain reusable. Registry-owned instances have
    an immutable admission identity and cannot be revived after retirement.
    """

    def __init__(
        self,
        reserve: Callable[[int], bool],
        release: Callable[[int], None],
        *,
        max_bytes: int = SNAPSHOT_MAX_BYTES,
        idle_seconds: float = SNAPSHOT_IDLE_SECONDS,
        hard_seconds: float = SNAPSHOT_IDLE_SECONDS,
        key: str | None = None,
        sync_revision: str | None = None,
        lease_token: object | None = None,
        is_current: Callable[[], bool] = lambda: True,
        can_build: Callable[[], bool] = lambda: True,
    ) -> None:
        self._reserve = reserve
        self._release = release
        self._max_bytes = max_bytes
        self._idle_seconds = idle_seconds
        self._hard_seconds = hard_seconds
        self._bound = key is not None
        self.key = key
        self.sync_revision = sync_revision
        self.lease_token = lease_token
        self._is_current = is_current
        self._can_build = can_build
        self.deadline = time.monotonic() + hard_seconds
        self._reserved = 0
        self._revision = 0
        self._building = False
        self._closed = False
        self._installing = False
        self._staged: set[int] = set()
        self._timer: asyncio.TimerHandle | None = None
        self._segments: tuple[bytes, ...] = ()
        self._metadata: dict[str, Any] = {}
        self.identity: tuple[str | None, int | None] = (None, None)

    @property
    def building(self) -> bool:
        return self._building

    @property
    def closed(self) -> bool:
        return self._closed or time.monotonic() >= self.deadline or not self._is_current()

    @property
    def snapshot_id(self) -> str | None:
        return self._metadata.get("snapshot_id")

    def close(self) -> None:
        self._revision += 1
        self._closed = True
        self._segments = ()
        self._metadata = {}
        self._staged.clear()
        if self._timer:
            self._timer.cancel()
            self._timer = None
        # A suspended encoder still owns its local buffers until it unwinds.
        if self._reserved and not self._building:
            self._release(self._reserved)
            self._reserved = 0

    def _touch(self) -> None:
        if self._timer:
            self._timer.cancel()
        remaining = max(0.0, self.deadline - time.monotonic())
        self._timer = asyncio.get_running_loop().call_later(
            min(self._idle_seconds, remaining), self.close
        )

    def _check_current(self) -> None:
        if self.closed:
            expired = time.monotonic() >= self.deadline
            self.close()
            raise SnapshotTransferError("SNAPSHOT_EXPIRED" if expired else "SNAPSHOT_STALE")

    def matches_install(self, value: dict[str, Any]) -> bool:
        return not self.closed and bool(self._metadata) and all(
            value.get(field) == self._metadata.get(metadata_field)
            for field, metadata_field in (
                ("key", "key"), ("snapshot_id", "snapshot_id"),
                ("sync_revision", "sync_revision"),
                ("stream_generation", "stream_generation"),
                ("stream_seq", "current_stream_seq"),
            )
        )

    def stage_piece(self, index: int) -> None:
        """Record only validated data deliveries, never cancellation tombstones."""
        if not self.closed and type(index) is int and 0 <= index < self._metadata.get(
            "segment_count", 0
        ):
            self._staged.add(index)

    def begin_install(self, receipt: dict[str, Any]) -> tuple[str | None, int | None]:
        self._check_current()
        if not self.matches_install(receipt):
            raise SnapshotTransferError("SNAPSHOT_STALE")
        if len(self._staged) != self._metadata["segment_count"]:
            raise SnapshotTransferError("SNAPSHOT_BUSY")
        # The client now owns the complete base. Do not hold the body while
        # waiting for replay output capacity. The bounded receipt outlives it.
        self._installing = True
        self._segments = ()
        if self._reserved:
            self._release(self._reserved)
            self._reserved = 0
        return self.identity

    async def create(
        self,
        key: str,
        sync_revision: str,
        capture: Callable[[], dict[str, Any]],
        *,
        identity: tuple[str | None, int | None] = (None, None),
    ) -> dict[str, Any]:
        if self._bound:
            self._check_current()
            if key != self.key or sync_revision != self.sync_revision:
                raise SnapshotTransferError("SNAPSHOT_STALE")
        if self._building:
            raise SnapshotTransferError("SNAPSHOT_BUSY")
        if (
            self._metadata.get("key") == key
            and self._metadata.get("sync_revision") == sync_revision
        ):
            if identity != self.identity:
                self.close()
                raise SnapshotTransferError("SNAPSHOT_STALE")
            return self.read(key, sync_revision, self._metadata["snapshot_id"], 0)
        global _active_encoders
        if not self._can_build() or _active_encoders >= _MAX_PROCESS_ENCODERS:
            raise SnapshotTransferError("SNAPSHOT_BUSY")
        if not self._bound:
            self.close()
            self._closed = False
            self.deadline = time.monotonic() + self._hard_seconds
            self.key, self.sync_revision = key, sync_revision
        self._installing = False
        self._staged.clear()
        # Scratch is bounded; encoded chunks are admitted as they are produced.
        # A failed try_reserve aborts this unpublished build without waiting
        # while holding partial bytes that could deadlock another builder.
        scratch = 2 * SNAPSHOT_SEGMENT_BYTES
        if not self._reserve(scratch):
            raise SnapshotTransferError("SNAPSHOT_BUSY")
        self._reserved = scratch
        revision = self._revision
        self._building = True
        _active_encoders += 1
        frozen: Any = None
        segments: tuple[bytes, ...] = ()
        published = False

        def reserve_chunk(size: int) -> None:
            if not self._reserve(size):
                raise SnapshotTransferError("SNAPSHOT_BUSY")
            self._reserved += size

        try:
            payload = capture()
            frozen = freeze_json(payload, max_bytes=self._max_bytes)
            metadata = {
                "key": key, "sync_revision": sync_revision,
                "snapshot_id": uuid4().hex, "encoding": "base64-json-utf8",
                "stream_generation": payload["stream_generation"],
                "current_stream_seq": payload["current_stream_seq"],
                "task_id": payload["task_id"],
                "session_id": identity[0], "session_epoch": identity[1],
            }
            del payload
            segments = await encode_segments(
                frozen, max_bytes=self._max_bytes,
                is_current=lambda: revision == self._revision and not self.closed,
                on_bytes=reserve_chunk,
            )
            self._check_current()
            if revision != self._revision:
                raise SnapshotTransferError("SNAPSHOT_STALE")
            self._segments = segments
            self.identity = identity
            self._metadata = {
                **metadata, "segment_count": len(segments),
                "byte_length": sum(map(len, segments)),
            }
            published = True
            return self.read(key, sync_revision, metadata["snapshot_id"], 0)
        finally:
            frozen = None
            segments = ()
            self._building = False
            _active_encoders -= 1
            if not published or self.closed:
                self._segments = ()
                self._metadata = {}
                if self._reserved:
                    self._release(self._reserved)
                    self._reserved = 0

    def read(
        self, key: str, sync_revision: str, snapshot_id: str, segment_index: int
    ) -> dict[str, Any]:
        if not self._segments or snapshot_id != self._metadata.get("snapshot_id"):
            raise SnapshotTransferError("SNAPSHOT_EXPIRED")
        self._check_current()
        if key != self._metadata["key"] or sync_revision != self._metadata["sync_revision"]:
            raise SnapshotTransferError("SNAPSHOT_STALE")
        if type(segment_index) is not int or not 0 <= segment_index < len(self._segments):
            raise SnapshotTransferError("INVALID_REQUEST")
        self._touch()
        return {
            **self._metadata, "segment_index": segment_index,
            "data": base64.b64encode(self._segments[segment_index]).decode("ascii"),
        }


class SnapshotRegistry:
    """Bounded exact-attempt ownership; retirement never revives a captured object."""

    def __init__(
        self, reserve: Callable[[int], bool], release: Callable[[int], None], *,
        max_retained: int = 4, max_building: int = 2,
        hard_seconds: float = SNAPSHOT_IDLE_SECONDS,
    ) -> None:
        self._reserve = reserve
        self._release = release
        self._max_retained = max_retained
        self._max_building = max_building
        self._hard_seconds = hard_seconds
        self._entries: dict[tuple[str, str], SnapshotTransfer] = {}
        self._retiring: set[SnapshotTransfer] = set()
        self._closed = False

    def _prune(self) -> None:
        for owner, transfer in tuple(self._entries.items()):
            if transfer.closed:
                transfer.close()
                if transfer.building:
                    self._retiring.add(transfer)
                del self._entries[owner]
        self._retiring = {item for item in self._retiring if item.building}

    def _can_build(self) -> bool:
        self._prune()
        return sum(item.building for item in (*self._entries.values(), *self._retiring)) < (
            self._max_building
        )

    def admit(
        self, key: str, sync_revision: str, lease_token: object | None, *,
        is_current: Callable[[], bool] = lambda: True,
    ) -> SnapshotTransfer:
        if self._closed:
            raise SnapshotTransferError("SNAPSHOT_STALE")
        existing = self._entries.get((key, sync_revision))
        if existing is not None:
            if existing.lease_token is not lease_token or existing.closed:
                raise SnapshotTransferError("SNAPSHOT_STALE")
            return existing
        self._prune()
        for owner, transfer in tuple(self._entries.items()):
            if owner[0] == key:
                transfer.close()
        self._prune()
        if len(self._entries) + len(self._retiring) >= self._max_retained:
            raise SnapshotTransferError("SNAPSHOT_BUSY")
        transfer = SnapshotTransfer(
            self._reserve, self._release, key=key, sync_revision=sync_revision,
            lease_token=lease_token, is_current=is_current,
            can_build=self._can_build, hard_seconds=self._hard_seconds,
        )
        self._entries[key, sync_revision] = transfer
        transfer._touch()
        return transfer

    def get(
        self, key: str, sync_revision: str, snapshot_id: str | None = None
    ) -> SnapshotTransfer | None:
        transfer = self._entries.get((key, sync_revision))
        if transfer is None or transfer.closed:
            return None
        if snapshot_id is not None and transfer.snapshot_id != snapshot_id:
            return None
        return transfer

    def release(self, key: str, sync_revision: str, snapshot_id: str | None = None) -> bool:
        transfer = self.get(key, sync_revision, snapshot_id)
        if transfer is None:
            return False
        transfer.close()
        return True

    def retire_lease(self, key: str, lease_token: object | None) -> None:
        for transfer in self._entries.values():
            if transfer.key == key and transfer.lease_token is lease_token:
                transfer.close()

    def close(self) -> None:
        self._closed = True
        for transfer in (*self._entries.values(), *self._retiring):
            transfer.close()
