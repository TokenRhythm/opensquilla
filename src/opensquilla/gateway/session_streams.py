"""In-memory session stream replay buffers for gateway events."""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

_JS_MAX_SAFE_INTEGER = (1 << 53) - 1


def _epoch_time_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class BufferedSessionEvent:
    event_name: str
    payload: dict[str, Any]
    stream_seq: int


@dataclass(frozen=True)
class ReplayResult:
    stream_generation: str
    current_stream_seq: int
    replay_complete: bool
    events: list[BufferedSessionEvent]
    gap_reason: str | None = None


@dataclass(frozen=True)
class LiveTurnSnapshot:
    stream_generation: str
    current_stream_seq: int
    task_id: str | None
    events: list[BufferedSessionEvent]


class SessionStreamRegistry:
    """Small in-memory replay buffer keyed by session.

    The WebSocket frame ``seq`` is per connection. ``stream_seq`` is per
    session and survives reconnects long enough to replay recent run events.
    """

    def __init__(
        self,
        *,
        max_events_per_session: int = 500,
        stream_generation: str | None = None,
    ) -> None:
        self._max_events_per_session = max_events_per_session
        self._stream_generation = stream_generation or uuid.uuid4().hex
        self._seq_by_session: dict[str, int] = {}
        self._events_by_session: dict[str, deque[BufferedSessionEvent]] = {}
        self._live_events_by_session: dict[str, list[BufferedSessionEvent]] = {}
        self._live_task_by_session: dict[str, str | None] = {}

    @property
    def stream_generation(self) -> str:
        """Return the process-local generation for every session stream."""

        return self._stream_generation

    @staticmethod
    def _is_replay_lossy(
        event_name: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if event_name in {
            "session.event.text_delta",
            "session.event.run_heartbeat",
            "session.event.tool_use_delta",
        }:
            return True
        return bool(
            isinstance(payload, dict)
            and payload.get("heartbeat") is True
            and event_name
            in {
                "session.event.compaction",
                "session.event.provider_activity",
            }
        )

    def _trim_session_events(self, events: deque[BufferedSessionEvent]) -> None:
        while len(events) > self._max_events_per_session:
            for index, event in enumerate(events):
                if self._is_replay_lossy(event.event_name, event.payload):
                    del events[index]
                    break
            else:
                events.popleft()

    def current_seq(self, session_key: str) -> int:
        return self._seq_by_session.get(session_key, 0)

    def promote_legacy_cursor(self, session_key: str, since_stream_seq: int | None) -> bool:
        """Keep pre-generation clients receiving events after a Gateway restart.

        Older clients compare only ``stream_seq`` and therefore discard a new
        Gateway's low sequence numbers after reconnecting.  A subscribe request
        without ``since_stream_generation`` is the compatibility signal: raise
        the process-local counter to the client's safe-integer cursor before the
        next event is recorded.  Generation-aware clients never use this path.
        """

        if since_stream_seq is None:
            return False
        if not 0 <= since_stream_seq < _JS_MAX_SAFE_INTEGER:
            return False
        current = self.current_seq(session_key)
        if since_stream_seq <= current:
            return False
        self._seq_by_session[session_key] = since_stream_seq
        return True

    def record(
        self,
        session_key: str,
        event_name: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        stream_seq = self.current_seq(session_key) + 1
        self._seq_by_session[session_key] = stream_seq

        enriched = dict(payload or {})
        enriched["session_key"] = session_key
        enriched["stream_generation"] = self.stream_generation
        enriched["stream_seq"] = stream_seq
        enriched["emitted_at"] = _epoch_time_ms()

        event = BufferedSessionEvent(event_name=event_name, payload=enriched, stream_seq=stream_seq)
        events = self._events_by_session.setdefault(session_key, deque())
        events.append(event)
        self._trim_session_events(events)
        self._record_live_event(session_key, event)
        return enriched

    @staticmethod
    def _task_id(payload: dict[str, Any]) -> str | None:
        raw = payload.get("task_id", payload.get("taskId"))
        return str(raw) if isinstance(raw, str) and raw else None

    @staticmethod
    def _tool_id(payload: dict[str, Any]) -> str:
        raw = payload.get(
            "tool_use_id",
            payload.get("toolUseId", payload.get("id", "")),
        )
        return str(raw) if raw is not None else ""

    @staticmethod
    def _delta_field(payload: dict[str, Any]) -> str:
        for field in ("json_fragment", "jsonFragment", "fragment"):
            if field in payload:
                return field
        return "json_fragment"

    def _replace_compacted_event(
        self,
        events: list[BufferedSessionEvent],
        index: int,
        event: BufferedSessionEvent,
        *,
        field: str,
    ) -> None:
        existing = events[index]
        payload = dict(existing.payload)
        payload[field] = f"{payload.get(field, '')}{event.payload.get(field, '')}"
        events[index] = BufferedSessionEvent(
            event_name=existing.event_name,
            payload=payload,
            stream_seq=existing.stream_seq,
        )

    def _record_live_event(
        self,
        session_key: str,
        event: BufferedSessionEvent,
    ) -> None:
        task_id = self._task_id(event.payload)
        current_task_id = self._live_task_by_session.get(session_key)
        if task_id and current_task_id and task_id != current_task_id:
            self._live_events_by_session.pop(session_key, None)
        if task_id:
            self._live_task_by_session[session_key] = task_id
        elif session_key not in self._live_task_by_session:
            self._live_task_by_session[session_key] = None

        if event.event_name in {"session.event.done", "session.event.error"}:
            self._live_events_by_session.pop(session_key, None)
            self._live_task_by_session.pop(session_key, None)
            return

        events = self._live_events_by_session.setdefault(session_key, [])
        if event.event_name == "session.event.thinking":
            for index, existing in enumerate(events):
                if existing.event_name == event.event_name:
                    self._replace_compacted_event(events, index, event, field="text")
                    return
        elif event.event_name == "session.event.tool_use_delta":
            tool_id = self._tool_id(event.payload)
            for index in range(len(events) - 1, -1, -1):
                existing = events[index]
                if (
                    existing.event_name == event.event_name
                    and self._tool_id(existing.payload) == tool_id
                ):
                    field = self._delta_field(existing.payload)
                    incoming_field = self._delta_field(event.payload)
                    normalized = event
                    if incoming_field != field:
                        normalized_payload = dict(event.payload)
                        normalized_payload[field] = normalized_payload.pop(incoming_field, "")
                        normalized = BufferedSessionEvent(
                            event_name=event.event_name,
                            payload=normalized_payload,
                            stream_seq=event.stream_seq,
                        )
                    self._replace_compacted_event(
                        events,
                        index,
                        normalized,
                        field=field,
                    )
                    return
        elif event.event_name == "session.event.text_delta" and events:
            if events[-1].event_name == event.event_name:
                self._replace_compacted_event(
                    events,
                    len(events) - 1,
                    event,
                    field="text",
                )
                return
        elif event.event_name == "session.event.run_heartbeat":
            for index in range(len(events) - 1, -1, -1):
                if events[index].event_name == event.event_name:
                    events[index] = event
                    return
        elif (
            event.event_name == "session.event.provider_activity"
            and event.payload.get("heartbeat") is True
        ):
            activity_id = str(event.payload.get("activity_id") or "")
            phase = str(event.payload.get("phase") or "")
            for index in range(len(events) - 1, -1, -1):
                existing = events[index]
                if existing.event_name != event.event_name:
                    continue
                if str(existing.payload.get("activity_id") or "") != activity_id:
                    continue
                if str(existing.payload.get("phase") or "") != phase:
                    continue
                events[index] = event
                return
        events.append(event)

    def live_snapshot(self, session_key: str) -> LiveTurnSnapshot:
        """Return the compact materialized view of the active turn."""

        events = [
            BufferedSessionEvent(
                event_name=event.event_name,
                payload=dict(event.payload),
                stream_seq=event.stream_seq,
            )
            for event in self._live_events_by_session.get(session_key, ())
        ]
        return LiveTurnSnapshot(
            stream_generation=self.stream_generation,
            current_stream_seq=self.current_seq(session_key),
            task_id=self._live_task_by_session.get(session_key),
            events=events,
        )

    def replay(
        self,
        session_key: str,
        since_stream_seq: int | None,
        since_stream_generation: str | None = None,
    ) -> ReplayResult:
        current = self.current_seq(session_key)
        if (
            since_stream_generation is not None
            and since_stream_generation != self.stream_generation
        ):
            return ReplayResult(
                stream_generation=self.stream_generation,
                current_stream_seq=current,
                replay_complete=False,
                events=[],
                gap_reason="stream_generation_changed",
            )
        if since_stream_seq is None:
            return ReplayResult(
                stream_generation=self.stream_generation,
                current_stream_seq=current,
                replay_complete=True,
                events=[],
            )

        events = list(self._events_by_session.get(session_key, ()))
        if current == 0:
            return ReplayResult(
                stream_generation=self.stream_generation,
                current_stream_seq=0,
                replay_complete=since_stream_seq == 0,
                events=[],
                gap_reason=None if since_stream_seq == 0 else "stream_buffer_reset",
            )

        if since_stream_seq > current:
            return ReplayResult(
                stream_generation=self.stream_generation,
                current_stream_seq=current,
                replay_complete=False,
                events=[],
                gap_reason="cursor_ahead_of_stream",
            )

        if since_stream_seq == current:
            return ReplayResult(
                stream_generation=self.stream_generation,
                current_stream_seq=current,
                replay_complete=True,
                events=[],
            )

        if not events:
            return ReplayResult(
                stream_generation=self.stream_generation,
                current_stream_seq=current,
                replay_complete=False,
                events=[],
                gap_reason="stream_buffer_empty",
            )

        first_seq = events[0].stream_seq
        replay_complete = since_stream_seq >= first_seq - 1
        replay_events = [event for event in events if event.stream_seq > since_stream_seq]
        return ReplayResult(
            stream_generation=self.stream_generation,
            current_stream_seq=current,
            replay_complete=replay_complete,
            events=replay_events,
            gap_reason=None if replay_complete else "buffer_window_missed",
        )


_session_streams = SessionStreamRegistry()


def get_session_streams() -> SessionStreamRegistry:
    return _session_streams


def reset_session_streams(
    *,
    max_events_per_session: int = 500,
    stream_generation: str | None = None,
) -> SessionStreamRegistry:
    """Begin a fresh stream generation for one Gateway lifecycle.

    The Gateway can be stopped and started again inside one Python process
    (Desktop and embedded deployments do this). Module import lifetime is
    therefore not a valid stream-generation lifetime. Replacing the registry
    atomically also clears replay/live snapshots before the new listener can
    accept subscriptions.
    """

    global _session_streams
    _session_streams = SessionStreamRegistry(
        max_events_per_session=max_events_per_session,
        stream_generation=stream_generation,
    )
    return _session_streams
