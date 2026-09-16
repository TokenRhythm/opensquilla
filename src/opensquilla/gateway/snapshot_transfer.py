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
) -> tuple[bytes, ...]:
    segments: list[bytes] = []
    pending = bytearray()
    size = 0
    last_yield = time.monotonic()
    for chunk in _json_chunks(frozen):
        if not is_current():
            raise SnapshotTransferError("SNAPSHOT_STALE")
        size += len(chunk)
        if size > max_bytes:
            raise SnapshotTransferError("SNAPSHOT_TOO_LARGE")
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


class SnapshotTransfer:
    """One transfer per connection; all encoded bytes share transport budgets."""

    def __init__(
        self,
        reserve: Callable[[int], bool],
        release: Callable[[int], None],
        *,
        max_bytes: int = SNAPSHOT_MAX_BYTES,
        idle_seconds: float = SNAPSHOT_IDLE_SECONDS,
    ) -> None:
        self._reserve = reserve
        self._release = release
        self._max_bytes = max_bytes
        self._idle_seconds = idle_seconds
        self._reserved = 0
        self._revision = 0
        self._building = False
        self._timer: asyncio.TimerHandle | None = None
        self._segments: tuple[bytes, ...] = ()
        self._metadata: dict[str, Any] = {}
        self.identity: tuple[str | None, int | None] = (None, None)

    def close(self) -> None:
        self._revision += 1
        self._segments = ()
        self._metadata = {}
        if self._timer:
            self._timer.cancel()
            self._timer = None
        # An encoder suspended at a yield still owns local buffers. Keep its
        # reservation until its finally block, rather than oversubscribing
        # the shared budget while that old coroutine unwinds.
        if self._reserved and not self._building:
            self._release(self._reserved)
            self._reserved = 0

    def _touch(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().call_later(self._idle_seconds, self.close)

    def matches_install(self, value: dict[str, Any]) -> bool:
        """Validate an installed cursor before a flow owner lifts its barrier."""
        return bool(self._segments) and all(
            value.get(field) == self._metadata.get(metadata_field)
            for field, metadata_field in (
                ("key", "key"),
                ("snapshot_id", "snapshot_id"),
                ("sync_revision", "sync_revision"),
                ("stream_generation", "stream_generation"),
                ("stream_seq", "current_stream_seq"),
            )
        )

    async def create(
        self,
        key: str,
        sync_revision: str,
        capture: Callable[[], dict[str, Any]],
        *,
        identity: tuple[str | None, int | None] = (None, None),
    ) -> dict[str, Any]:
        if self._building:
            raise SnapshotTransferError("SNAPSHOT_BUSY")
        # Same logical first-page retry reuses the same immutable bytes.
        if (
            self._segments
            and self._metadata.get("key") == key
            and self._metadata.get("sync_revision") == sync_revision
        ):
            if identity != self.identity:
                self.close()
                raise SnapshotTransferError("SNAPSHOT_STALE")
            return self.read(key, sync_revision, self._metadata["snapshot_id"], 0)
        self.close()
        # Account for the encoder's pending fragment and base64 response copy.
        reservation = self._max_bytes + 2 * SNAPSHOT_SEGMENT_BYTES
        if not self._reserve(reservation):
            raise SnapshotTransferError("SNAPSHOT_BUSY")
        self._reserved = reservation
        revision = self._revision
        self._building = True
        try:
            payload = capture()
            frozen = freeze_json(payload, max_bytes=self._max_bytes)
            metadata = {
                "key": key,
                "sync_revision": sync_revision,
                "snapshot_id": uuid4().hex,
                "encoding": "base64-json-utf8",
                "stream_generation": payload["stream_generation"],
                "current_stream_seq": payload["current_stream_seq"],
                "task_id": payload["task_id"],
                "session_id": identity[0],
                "session_epoch": identity[1],
            }
            # No live-state reads beyond this capture boundary.
            del payload
            segments = await encode_segments(
                frozen,
                max_bytes=self._max_bytes,
                is_current=lambda: revision == self._revision,
            )
            if revision != self._revision:
                raise SnapshotTransferError("SNAPSHOT_STALE")
            size = sum(map(len, segments))
            self._segments = segments
            self.identity = identity
            self._metadata = {
                **metadata, "segment_count": len(segments), "byte_length": size
            }
            retained = size + 2 * SNAPSHOT_SEGMENT_BYTES
            self._release(self._reserved - retained)
            self._reserved = retained
            return self.read(key, sync_revision, metadata["snapshot_id"], 0)
        except BaseException:
            # A close during encoding already relinquished this reservation.
            if revision == self._revision:
                self.close()
            raise
        finally:
            self._building = False
            if revision != self._revision:
                self.close()

    def read(
        self, key: str, sync_revision: str, snapshot_id: str, segment_index: int
    ) -> dict[str, Any]:
        if not self._segments or snapshot_id != self._metadata.get("snapshot_id"):
            raise SnapshotTransferError("SNAPSHOT_EXPIRED")
        if key != self._metadata["key"] or sync_revision != self._metadata["sync_revision"]:
            raise SnapshotTransferError("SNAPSHOT_STALE")
        if type(segment_index) is not int or not 0 <= segment_index < len(self._segments):
            raise SnapshotTransferError("INVALID_REQUEST")
        self._touch()
        return {
            **self._metadata,
            "segment_index": segment_index,
            "data": base64.b64encode(self._segments[segment_index]).decode("ascii"),
        }
