"""Bounded process-output capture, with retained fragments in ToolResultStore.

Readers await each write before reading again. There is no producer queue, and
neither reaching the output budget nor a storage failure stops draining pipes.
"""

from __future__ import annotations

import asyncio
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opensquilla.engine.cancellation import park_background_task
from opensquilla.engine.tool_result_store import (
    DEFAULT_TOOL_RESULT_MAX_BYTES,
    ToolOutputSpool,
    ToolResultStore,
)
from opensquilla.subprocess_encoding import decode_subprocess_output
from opensquilla.tools.types import current_tool_context

OUTPUT_PREVIEW_BYTES = 1024 * 1024
OUTPUT_READ_BYTES = 64 * 1024
OUTPUT_SETUP_WAIT_SECONDS = 5.0
OUTPUT_FINALIZE_WAIT_SECONDS = 5.0
OUTPUT_CANCEL_FINALIZE_WAIT_SECONDS = 0.25
OUTPUT_CAPTURE_TIMEOUT_PADDING = OUTPUT_SETUP_WAIT_SECONDS + OUTPUT_FINALIZE_WAIT_SECONDS
_OUTPUT_FRAME_HEADER = struct.Struct(">BI")


def _close_late_spool(opening: asyncio.Task[ToolOutputSpool]) -> None:
    async def close_when_ready() -> None:
        try:
            spool = await opening
        except Exception:
            return
        await asyncio.to_thread(spool.close)

    park_background_task(
        asyncio.create_task(close_when_ready()), operation="tool_output_setup_cleanup",
    )


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
        # Until head fills, the newest bytes still live there rather than in
        # tail. A small disk budget can be exhausted well before that happens.
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
        self._finish_task: asyncio.Task[None] | None = None
        self._finish_parked = False
        self._finalization_pending = False
        self._release_after_finish = False
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
            # Storage setup is optional: bound observation without cancelling
            # its worker. Late leases stay owned until cleanup can close them.
            opening = asyncio.create_task(asyncio.to_thread(
                ToolResultStore(root).open_output_spool,
                tool_name=tool_name, session_id=session_id,
                session_key=ctx.session_key or session_id, agent_id=ctx.agent_id or "main",
                max_bytes=min(
                    DEFAULT_TOOL_RESULT_MAX_BYTES,
                    ctx.tool_result_store_max_bytes
                    if ctx.tool_result_store_max_bytes is not None
                    else DEFAULT_TOOL_RESULT_MAX_BYTES,
                ),
                disk_budget_bytes=ctx.tool_result_store_disk_budget_bytes,
                retention_seconds=ctx.tool_result_store_retention_seconds,
            ))
            try:
                done, _pending = await asyncio.wait({opening}, timeout=OUTPUT_SETUP_WAIT_SECONDS)
            except asyncio.CancelledError:
                _close_late_spool(opening)
                raise
            if done:
                capture.spool = opening.result()
            else:
                capture.storage_error = "OutputSetupTimeout"
                _close_late_spool(opening)
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
                        # Framing shares the existing spool cap, including a
                        # final partial frame when that cap is reached.
                        for offset in range(0, len(chunk), OUTPUT_READ_BYTES):
                            fragment = chunk[offset:offset + OUTPUT_READ_BYTES]
                            self.spool.append(_OUTPUT_FRAME_HEADER.pack(
                                self._stream_indexes[stream], len(fragment),
                            ) + fragment)
                    else:
                        self.spool.append(chunk)
                except OSError as exc:
                    self.storage_error = type(exc).__name__

    async def drain(self, reader: asyncio.StreamReader | None, stream: str = "stdout") -> None:
        if reader is None:
            return
        try:
            while chunk := await reader.read(OUTPUT_READ_BYTES):
                await asyncio.to_thread(self.feed, chunk, stream)
        except (asyncio.CancelledError, OSError):
            self.incomplete_reason = "output drain interrupted"
            raise

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
            self._release_after_finish = True
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
        return self.preview() + "\n[retained output unavailable; latest diagnostic fallback only]"

    def describe(self) -> dict[str, Any]:
        with self._lock:
            observed = sum(item.observed for item in self.previews.values())
            omitted = (
                self._released_omitted if self._preview_released
                else sum(item.omitted for item in self.previews.values())
            )
            result: dict[str, Any] = {
                "observed_bytes": observed,
                "preview_omitted_bytes": omitted,
                "retained_bytes": self.retained_bytes,
                "retained_output_complete": bool(
                    self.finished and self.handle
                    and not self.storage_error and not self.incomplete_reason
                    and self.spool is not None and self.spool.size < self.spool.max_bytes
                ),
            }
            if self._finalization_pending and not self.handle:
                result["finalization_pending"] = True
            if self.handle:
                result["tool_result_handle"] = self.handle
                if self.retrieval_available:
                    result["retrieval"] = "retrieve_tool_result(handle=tool_result_handle)"
            if self.incomplete_reason:
                result["incomplete_reason"] = self.incomplete_reason
            if self.storage_error:
                result["storage_error"] = self.storage_error
            return result

    def notice(self) -> str:
        info = self.describe()
        if not (
            info.get("tool_result_handle") or info["preview_omitted_bytes"]
            or self.storage_error or self.incomplete_reason or info.get("finalization_pending")
        ):
            return ""
        text = "\n[output capture: "
        if info.get("tool_result_handle"):
            text += f"retained output tool_result_handle={self.handle}; "
            if self.retrieval_available:
                text += "use retrieve_tool_result to inspect retained fragments; "
        text += f"preview omitted {info['preview_omitted_bytes']} bytes"
        if not info["retained_output_complete"]:
            text += "; retained output may omit bytes; not a full log"
        if info.get("finalization_pending"):
            text += "; retained output finalization pending, not yet persisted"
        if self.incomplete_reason:
            text += f"; {self.incomplete_reason}"
        if self.storage_error:
            text += f"; storage unavailable ({self.storage_error}), pipe still drained"
        return text + "]"

    def finish(self) -> None:
        with self._write_lock:
            with self._lock:
                if self.finished:
                    return
                self.finished = True
            if self.spool is None:
                return
            try:
                raw = self.spool.prefix()
                text = self._decode_retained_prefix(raw)
                if len(text.encode("utf-8")) > self.spool.max_bytes:
                    self.incomplete_reason = (
                        self.incomplete_reason or "retained output exceeded its text budget"
                    )
                if (
                    self.spool.size >= self.spool.max_bytes
                    or self.storage_error or self.incomplete_reason
                ):
                    tail = "\n".join(
                        f"[{name} latest diagnostics]\n"
                        + decode_subprocess_output(view.latest(OUTPUT_PREVIEW_BYTES))
                        for name, view in self.previews.items()
                    )
                    marker = "\n[output omitted between retained prefix and latest diagnostics]\n"
                    # UTF-8 transcoding may expand legacy bytes. Reserve space
                    # for the latest diagnostic tail and the omission marker.
                    if len(marker.encode()) > self.spool.max_bytes // 4:
                        marker = "\n[omitted]\n" if self.spool.max_bytes >= 48 else ""
                    tail_limit = min(OUTPUT_PREVIEW_BYTES, self.spool.max_bytes // 2)
                    tail_bytes = tail.encode("utf-8")[-tail_limit:] if tail_limit else b""
                    room = max(0, self.spool.max_bytes - len(tail_bytes) - len(marker.encode()))
                    text = (
                        text.encode("utf-8")[:room].decode("utf-8", errors="ignore")
                        + marker + tail_bytes.decode("utf-8", errors="ignore")
                    )
                encoded = text.encode("utf-8")
                if len(encoded) > self.spool.max_bytes:
                    marker = "\n[retained output truncated after decoding]\n"
                    text = encoded[:self.spool.max_bytes - len(marker.encode())].decode(
                        "utf-8", errors="ignore"
                    ) + marker
                    self.storage_error = self.storage_error or "DecodedOutputLimit"
                self.retained_bytes = len(text.encode("utf-8"))
                self.handle = self.spool.finish(text)
            except Exception as exc:
                # Storage/lock failures must never turn a completed command
                # into a tool failure or prevent pipe drainage.
                self.storage_error = type(exc).__name__
            finally:
                self.spool.close()

    def _decode_retained_prefix(self, raw: bytes) -> str:
        if not self._framed_output:
            return decode_subprocess_output(raw)
        # These buffers together cannot exceed the one bounded spool prefix.
        # Grouping by stream preserves multibyte characters split across reads
        # and avoids decoding already-transcoded UTF-8 as a legacy code page.
        streams = [bytearray() for _ in self._stream_names]
        offset = 0
        while offset + _OUTPUT_FRAME_HEADER.size <= len(raw):
            stream_index, size = _OUTPUT_FRAME_HEADER.unpack_from(raw, offset)
            offset += _OUTPUT_FRAME_HEADER.size
            if stream_index >= len(streams) or size > OUTPUT_READ_BYTES:
                raise ValueError("invalid retained output frame")
            end = min(len(raw), offset + size)
            streams[stream_index].extend(raw[offset:end])
            offset = end
        return "".join(
            f"\n[{name}]\n{decode_subprocess_output(bytes(content))}"
            for name, content in zip(self._stream_names, streams, strict=True)
            if content
        )

    def _finish_settled(self, task: asyncio.Task[None]) -> None:
        self._finalization_pending = False
        if not task.cancelled():
            exception = task.exception()
            if exception is not None:
                self.storage_error = type(exception).__name__
        if self._release_after_finish:
            self.release_preview()

    def _park_finalizer(self) -> None:
        if self._finish_task is not None and not self._finish_parked:
            self._finish_parked = True
            park_background_task(self._finish_task, operation="tool_output_finalize")

    async def finish_async(self) -> None:
        """Bound observation of storage cleanup; never cancel its worker.

        A parked writer still owns its spool lease. Pending output returns an
        honest in-memory diagnostic, without publishing an unready handle.
        Repeated finally blocks observe the same task instead of queuing more
        workers behind a stalled disk write.
        """
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(asyncio.to_thread(self.finish))
            self._finish_task.add_done_callback(self._finish_settled)
        task = self._finish_task
        if self._finalization_pending and not task.done():
            return
        caller = asyncio.current_task()
        timeout = (
            OUTPUT_CANCEL_FINALIZE_WAIT_SECONDS
            if caller is not None and caller.cancelling()
            else OUTPUT_FINALIZE_WAIT_SECONDS
        )
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            if not task.done():
                self._finalization_pending = True
                self._park_finalizer()
            raise
        if not done:
            self._finalization_pending = True
            self._park_finalizer()
        else:
            self._finish_settled(task)
