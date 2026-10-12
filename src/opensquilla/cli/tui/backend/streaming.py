"""Coalescing policy for renderer-neutral token streaming."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from opensquilla.cli.tui.backend.domain_events import (
    KIND_TEXT_FLUSH,
    TuiDomainEvent,
    TuiDomainEventSource,
    now_ms,
)

StreamingFlushReason = Literal["delay", "size", "newline", "finish"]


@dataclass(frozen=True)
class StreamingFlushPolicy:
    max_delay_ms: int = 33
    max_chars: int = 2_048
    newline_min_chars: int = 256


@dataclass(frozen=True)
class StreamingFlush:
    text: str
    reason: StreamingFlushReason
    # Number of deltas coalesced into THIS flush; the lifetime total lives on
    # the plane as ``delta_count``.
    delta_count: int
    chars: int


class StreamingPlane:
    def __init__(
        self,
        policy: StreamingFlushPolicy | None = None,
        *,
        clock_ms: Callable[[], int] = now_ms,
        event_sink: Callable[[TuiDomainEvent], None] | None = None,
        source: TuiDomainEventSource = "renderer",
        turn_id: str | None = None,
        flush_kind: str = KIND_TEXT_FLUSH,
    ) -> None:
        self.policy = StreamingFlushPolicy() if policy is None else policy
        self._clock_ms = clock_ms
        self._event_sink = event_sink
        self._source = source
        self._turn_id = turn_id
        self._flush_kind = flush_kind
        self._buffer: list[str] = []
        self._buffer_chars = 0
        self._pending_deltas = 0
        self._last_flush_ms = clock_ms()
        self.delta_count = 0
        self.flush_count = 0
        self.text_chars = 0
        self.max_buffer_chars = 0

    def append(self, delta: str) -> StreamingFlush | None:
        if not delta:
            return None
        self.delta_count += 1
        delta_chars = len(delta)
        self.text_chars += delta_chars
        if (
            self._buffer_chars > 0
            and self._buffer_chars + delta_chars > self.policy.max_chars
            and not self._delta_needs_flush(delta)
        ):
            # Split flush: emit the bounded buffer and retain the delta. Only
            # valid when the retained delta does not itself satisfy a flush
            # condition — otherwise it would sit deferred until the next
            # append, however long the stream stalls.
            flush = self._flush("size")
            self._buffer.append(delta)
            self._buffer_chars += delta_chars
            self._pending_deltas = 1
            self.max_buffer_chars = max(self.max_buffer_chars, self._buffer_chars)
            return flush
        self._buffer.append(delta)
        self._buffer_chars += delta_chars
        self._pending_deltas += 1
        self.max_buffer_chars = max(self.max_buffer_chars, self._buffer_chars)
        reason = self._flush_reason(delta)
        if reason is not None:
            return self._flush(reason)
        return None

    def flush(self, *, force: bool = False) -> StreamingFlush | None:
        """Drain the buffer when a flush condition holds (always, with force).

        ``append`` only evaluates flush conditions when the next delta
        arrives, so heartbeat/pulse call sites should invoke this to drain a
        buffered tail while the stream is stalled mid-turn.
        """
        if self._buffer_chars == 0:
            return None
        reason = self._flush_reason("")
        if not force and reason is None:
            return None
        return self._flush("finish" if force else reason or "delay")

    def finish(self) -> StreamingFlush | None:
        return self._flush("finish")

    def _flush(self, reason: StreamingFlushReason) -> StreamingFlush | None:
        if self._buffer_chars == 0:
            return None
        text = "".join(self._buffer)
        flush = StreamingFlush(
            text=text,
            reason=reason,
            delta_count=self._pending_deltas,
            chars=len(text),
        )
        self._buffer.clear()
        self._buffer_chars = 0
        self._pending_deltas = 0
        self.flush_count += 1
        self._last_flush_ms = self._clock_ms()
        self._emit_flush_event(flush)
        return flush

    def _emit_flush_event(self, flush: StreamingFlush) -> None:
        if self._event_sink is None:
            return
        self._event_sink(
            TuiDomainEvent(
                kind=self._flush_kind,
                source=self._source,
                payload={
                    "text": flush.text,
                    "chars": flush.chars,
                    "reason": flush.reason,
                    "delta_count": flush.delta_count,
                    "flush_count": self.flush_count,
                },
                turn_id=self._turn_id,
                timestamp_ms=now_ms(),
            )
        )

    def _delta_needs_flush(self, delta: str) -> bool:
        delta_chars = len(delta)
        if delta_chars >= self.policy.max_chars:
            return True
        return "\n" in delta and delta_chars >= self.policy.newline_min_chars

    def _flush_reason(self, delta: str) -> StreamingFlushReason | None:
        if self._buffer_chars >= self.policy.max_chars:
            return "size"
        if "\n" in delta and self._buffer_chars >= self.policy.newline_min_chars:
            return "newline"
        if self._clock_ms() - self._last_flush_ms >= self.policy.max_delay_ms:
            return "delay"
        return None
