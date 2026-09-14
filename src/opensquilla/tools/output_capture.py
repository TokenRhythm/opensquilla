"""Bounded process-output previews backed by complete execution logs.

Readers await each write before reading again. There is no producer queue.
Physical storage failures remain explicit while readers continue draining pipes.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from opensquilla.engine.tool_result_store import (
    ToolOutputSpool,
    ToolResultStore,
    _iter_output_text,
    iter_output_bytes,
)
from opensquilla.subprocess_encoding import (
    decode_subprocess_output,
    select_subprocess_output_encoding,
)
from opensquilla.tools.types import current_execution_log, current_tool_context

OUTPUT_PREVIEW_BYTES = 1024 * 1024
OUTPUT_READ_BYTES = 64 * 1024
_OUTPUT_FRAME_HEADER = struct.Struct(">BI")


class OutputReader(Protocol):
    async def read(self, n: int) -> bytes: ...


@dataclass
class _Preview:
    limit: int
    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    observed: int = 0

    def append(self, chunk: bytes) -> None:
        self.observed += len(chunk)
        take = min(len(chunk), self.limit // 2 - len(self.head))
        if take > 0:
            self.head.extend(chunk[:take])
        remaining = chunk[take:]
        tail_limit = self.limit - self.limit // 2
        if len(remaining) >= tail_limit:
            self.tail[:] = remaining[-tail_limit:]
        else:
            overflow = len(self.tail) + len(remaining) - tail_limit
            if overflow > 0:
                del self.tail[:overflow]
            self.tail.extend(remaining)

    @property
    def omitted(self) -> int:
        return max(0, self.observed - len(self.head) - len(self.tail))

    def text(self) -> str:
        if not self.omitted:
            return decode_subprocess_output(bytes(self.head + self.tail))
        return (
            decode_subprocess_output(bytes(self.head))
            + f"\n[preview omitted {self.omitted} output bytes]\n"
            + decode_subprocess_output(bytes(self.tail))
        )

    def latest(self, limit: int) -> bytes:
        if limit <= 0:
            return b""
        # Until head fills, the newest bytes still live there rather than in tail.
        return bytes(self.head[-limit:] + self.tail)[-limit:]


class BoundedOutputCapture:
    def __init__(
        self,
        *,
        streams: tuple[str, ...] = ("stdout",),
        preview_bytes: int = OUTPUT_PREVIEW_BYTES,
    ) -> None:
        self.previews = {name: _Preview(max(2, preview_bytes // len(streams))) for name in streams}
        self._stream_names = tuple(self.previews)
        self._stream_indexes = {name: index for index, name in enumerate(self._stream_names)}
        self._framed_output = len(streams) > 1
        self.spool: ToolOutputSpool | None = None
        self.handle: str | None = None
        self.retrieval_available = False
        self.storage_error: str | None = None
        self.incomplete_reason: str | None = None
        self.retained_bytes = 0
        self.finished = False
        self._preview_released = False
        self._released_omitted = 0
        self._fallback_previews: dict[str, str] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    @classmethod
    async def create(
        cls, tool_name: str, *, streams: tuple[str, ...] = ("stdout",)
    ) -> BoundedOutputCapture:
        capture = cls(streams=streams)
        ctx = current_tool_context.get()
        if ctx is None:
            return capture
        capture.retrieval_available = ctx.tool_result_retrieval_available
        root = ctx.tool_result_store_dir
        if not root and ctx.artifact_media_root:
            root = str(Path(ctx.artifact_media_root) / "tool-results")
        session_id = ctx.tool_result_store_session_id or ctx.artifact_session_id or ctx.session_key
        if not root or not session_id:
            return capture
        try:
            opening = asyncio.create_task(asyncio.to_thread(
                ToolResultStore(root).open_output_spool,
                tool_name=tool_name, session_id=session_id,
                session_key=ctx.session_key or session_id, agent_id=ctx.agent_id or "main",
                retention_seconds=ctx.tool_result_store_retention_seconds,
            ))
            try:
                capture.spool = await asyncio.shield(opening)
                if (execution_log := current_execution_log.get()) is not None:
                    execution_log["handle"] = capture.spool.handle
            except asyncio.CancelledError:
                # A thread cannot be cancelled. Settle the open operation and
                # close its lease before relinquishing ownership.
                while not opening.done():
                    try:
                        await asyncio.shield(opening)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not opening.cancelled() and opening.exception() is None:
                    opening.result().close()
                raise
        except Exception as exc:
            capture.storage_error = type(exc).__name__
        return capture

    def feed(self, chunk: bytes, stream: str = "stdout") -> None:
        with self._write_lock:
            with self._lock:
                if self.finished:
                    return
                self.previews[stream].append(chunk)
            # A slow disk must not hold the preview/status lock used by the
            # gateway event loop. Only reader workers wait for this write.
            if self.spool is not None and self.storage_error is None:
                try:
                    if self._framed_output:
                        # Retain original bytes until each stream's encoding
                        # can be selected independently. Python subprocesses
                        # can mix UTF-8 with a native child's Windows code page.
                        # Frames preserve byte boundaries without truncating the log.
                        for offset in range(0, len(chunk), OUTPUT_READ_BYTES):
                            fragment = chunk[offset:offset + OUTPUT_READ_BYTES]
                            self.spool.append(_OUTPUT_FRAME_HEADER.pack(
                                self._stream_indexes[stream], len(fragment),
                            ) + fragment)
                    else:
                        self.spool.append(chunk)
                except OSError as exc:
                    self.storage_error = type(exc).__name__

    async def drain(
        self, reader: OutputReader | None, stream: str = "stdout", *,
        process_exited: asyncio.Event | None = None, idle_timeout: float = 1.0,
    ) -> None:
        if reader is None:
            return
        exited = (
            asyncio.create_task(process_exited.wait()) if process_exited is not None else None
        )
        try:
            while True:
                reading = asyncio.create_task(reader.read(OUTPUT_READ_BYTES))
                try:
                    if exited is not None and not exited.done():
                        await asyncio.wait({reading, exited}, return_when=asyncio.FIRST_COMPLETED)
                    if exited is not None and exited.done() and not reading.done():
                        chunk = await asyncio.wait_for(reading, timeout=idle_timeout)
                    else:
                        chunk = await reading
                finally:
                    if not reading.done():
                        reading.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await reading
                if not chunk:
                    break
                # The idle deadline applies only to waiting for pipe data, never
                # to a healthy write or its wait for an executor worker.
                writing = asyncio.create_task(asyncio.to_thread(self.feed, chunk, stream))
                try:
                    await asyncio.shield(writing)
                except asyncio.CancelledError:
                    # Keep ownership of this already-read chunk until its write
                    # settles. The turn's existing cancellation grace can park us.
                    while not writing.done():
                        try:
                            await asyncio.shield(writing)
                        except asyncio.CancelledError:
                            continue
                    writing.result()
                    raise
        except TimeoutError:
            self.incomplete_reason = "output pipe remained open after process exit"
        except OSError as exc:
            self.incomplete_reason = f"output read failed ({type(exc).__name__})"
        except asyncio.CancelledError:
            self.incomplete_reason = "output drain interrupted"
            raise
        except Exception as exc:
            self.incomplete_reason = f"output capture failed ({type(exc).__name__})"
            raise
        finally:
            if exited is not None and not exited.done():
                exited.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await exited

    def preview(self, stream: str = "stdout") -> str:
        with self._lock:
            if self._preview_released:
                return self._fallback_previews.get(stream, "")
            return self.previews[stream].text()

    def release_preview(self) -> None:
        """Drop completed background buffers after a retained handle exists.

        Keep only a 4 KiB last-diagnostic fallback per stream, so disk eviction
        or later IO failure still produces useful, explicitly partial output.
        """
        with self._lock:
            if self._preview_released or not self.finished or not self.handle:
                return
            self._released_omitted = sum(view.omitted for view in self.previews.values())
            for name, view in self.previews.items():
                recent = view.latest(4096)
                self._fallback_previews[name] = decode_subprocess_output(recent)
                view.head.clear()
                view.tail.clear()
            self._preview_released = True

    async def preview_async(self) -> str:
        if not self._preview_released:
            return self.preview()
        if self.spool is not None and self.handle:
            try:
                return await asyncio.to_thread(
                    self.spool.store.read_output_preview,
                    self.handle, session_id=self.spool.session_id, max_bytes=OUTPUT_PREVIEW_BYTES,
                )
            except (OSError, ValueError):
                pass
        self.incomplete_reason = "retained output unavailable; latest diagnostic fallback only"
        return self.preview() + f"\n[{self.incomplete_reason}]"

    def describe(self, *, only_if_needed: bool = False) -> dict[str, Any]:
        with self._lock:
            observed = sum(item.observed for item in self.previews.values())
            omitted = (
                self._released_omitted if self._preview_released
                else sum(item.omitted for item in self.previews.values())
            )
            if only_if_needed and not (
                omitted or self.storage_error or self.incomplete_reason
            ):
                return {}
            result: dict[str, Any] = {
                "observed_bytes": observed,
                "preview_omitted_bytes": omitted,
                "retained_bytes": self.retained_bytes,
                "retained_output_complete": bool(
                    self.finished and self.handle
                    and not self.storage_error and not self.incomplete_reason
                    and self.spool is not None
                ),
            }
            if self.handle:
                result["tool_result_handle"] = self.handle
                if self.retrieval_available:
                    result["retrieval"] = "retrieve_tool_result(handle=tool_result_handle)"
            if self.incomplete_reason:
                result["incomplete_reason"] = self.incomplete_reason
            if self.storage_error:
                result["storage_error"] = self.storage_error
            return result

    def notice(self, *, retrieval_needed: bool = False) -> str:
        info = self.describe(only_if_needed=not (retrieval_needed and self.handle))
        if not info:
            return ""
        details = []
        if info.get("tool_result_handle"):
            details.append(f"retained output tool_result_handle={self.handle}")
            if self.retrieval_available:
                details.append("use retrieve_tool_result to read the execution log")
        if info["preview_omitted_bytes"]:
            details.append(f"preview omitted {info['preview_omitted_bytes']} bytes")
        if not info["retained_output_complete"]:
            details.append("retained output may omit bytes; not a full log")
        if self.incomplete_reason:
            details.append(self.incomplete_reason)
        if self.storage_error:
            details.append(f"storage unavailable ({self.storage_error}), pipe still drained")
        return "\n[output capture: " + "; ".join(details) + "]"

    def finish(self) -> None:
        with self._write_lock:
            with self._lock:
                if self.finished:
                    return
            if self.spool is None:
                self.finished = True
                return
            try:
                encodings, decoder_final = self._decoders()
                self.handle = self.spool.finish(
                    streams=self._stream_names, encodings=encodings,
                    decoder_final=decoder_final,
                    complete=not bool(self.storage_error or self.incomplete_reason),
                )
                self.retained_bytes = self.spool.size
            except Exception as exc:
                self.storage_error = type(exc).__name__
            finally:
                self.spool.close()
                with self._lock:
                    self.finished = True

    def _decoders(self, *, max_bytes: int | None = None) -> tuple[
        tuple[str, ...], tuple[bool, ...],
    ]:
        assert self.spool is not None
        selected = [
            select_subprocess_output_encoding(iter_output_bytes(
                self.spool.path, stream_index=index, framed=self._framed_output,
                max_bytes=max_bytes, stream_count=len(self._stream_names),
            ))
            for index in range(len(self._stream_names))
        ]
        return tuple(item[0] for item in selected), tuple(item[1] for item in selected)

    def read_slice(self, start: int, end: int | None = None) -> tuple[str, int]:
        """Read a character range of the actual log, rather than its preview.

        The caller bounds the requested range and runs this disk operation off
        the event loop. Open a separate reader so writers keep their position.
        """
        start = max(0, start)
        if end is None:
            end = start + OUTPUT_PREVIEW_BYTES
        end = max(start, end)
        if self.spool is None:
            text = self.preview()
            return text[start:end], len(text)
        total_chars = None
        if self.handle:
            meta = self.spool.store.read_output_metadata(
                self.handle, session_id=self.spool.session_id,
            )
            total_chars = int(meta["chars"]) if meta is not None else None
            chunks = self.spool.store.iter_text_chunks(
                self.handle, session_id=self.spool.session_id,
            )
        else:
            # Snapshot only the byte count under the writer lock. Readers do
            # not hold it while scanning, and ignore subsequent appended frames.
            with self._write_lock:
                size = self.spool.size
            encodings, decoder_final = self._decoders(max_bytes=size)
            if not self.finished:
                # A running process can append the rest of a character on its
                # next write. Do not expose a replacement at the temporary EOF
                # and then shift previously returned character offsets.
                decoder_final = (False,) * len(self._stream_names)
            chunks = _iter_output_text(
                self.spool.path, self._stream_names, encodings, decoder_final,
                max_bytes=size,
            )
        offset = 0
        parts = []
        for text in chunks:
            next_offset = offset + len(text)
            if next_offset > start and offset < end:
                parts.append(text[max(0, start - offset):max(0, end - offset)])
            offset = next_offset
            if next_offset >= end and total_chars is not None:
                break
        return "".join(parts), total_chars if total_chars is not None else offset

    async def finish_async(self) -> None:
        # Keep queued file cleanup runnable even if the caller is cancelled.
        await asyncio.shield(asyncio.to_thread(self.finish))
