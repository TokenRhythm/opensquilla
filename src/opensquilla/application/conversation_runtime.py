"""Transport-neutral conversation snapshot application seam.

The Gateway used to combine stream lookup, event projection and terminal-task
ownership in the RPC handler.  This module owns that policy behind two narrow
Ports.  It deliberately knows nothing about WebSocket connections, v4 field
aliases, ``RpcContext`` or the concrete ``SessionStreamRegistry``.  A later
bootstrap/hydration slice can reuse the same Ports without making the UI or
the engine depend on Gateway details.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ConversationSnapshotEvent:
    """One transport-neutral event retained in a live-turn snapshot."""

    name: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LiveConversationSnapshot:
    """Input returned by the stream Port."""

    task_id: str | None
    stream_generation: str
    current_stream_seq: int
    events: tuple[ConversationSnapshotEvent, ...]


@dataclass(frozen=True, slots=True)
class ProjectedConversationSnapshot:
    """Projected snapshot consumed by a Gateway Adapter."""

    task_id: str | None
    stream_generation: str
    current_stream_seq: int
    events: tuple[ConversationSnapshotEvent, ...]


class ConversationSnapshotReader(Protocol):
    """Port for the in-memory/live stream implementation."""

    def read_live_snapshot(self, session_key: str) -> LiveConversationSnapshot: ...


ConversationEventProjector = Callable[
    [str, Mapping[str, Any], frozenset[str]],
    tuple[str, Mapping[str, Any]] | None,
]


class ConversationSnapshotApplication:
    """Apply event projection and terminal ownership rules once.

    The reader and projector are replaceable.  In production the Gateway
    adapter supplies the current stream registry and capability-aware v4
    projector; application tests can substitute small fakes without starting
    a socket or constructing a full ``RpcContext``.
    """

    def __init__(
        self,
        *,
        reader: ConversationSnapshotReader,
        projector: ConversationEventProjector,
    ) -> None:
        self._reader = reader
        self._projector = projector

    def read(
        self,
        session_key: str,
        *,
        client_caps: frozenset[str] = frozenset(),
    ) -> ProjectedConversationSnapshot:
        source = self._reader.read_live_snapshot(session_key)
        projected: list[ConversationSnapshotEvent] = []
        for event in source.events:
            # A projector is an adapter-owned compatibility hook. Give it a
            # shallow copy so a legacy projection cannot mutate the live
            # stream buffer that a later subscriber will replay.
            result = self._projector(event.name, dict(event.payload), client_caps)
            if result is None:
                continue
            event_name, event_payload = result
            projected.append(
                ConversationSnapshotEvent(
                    name=event_name,
                    payload=dict(event_payload),
                )
            )

        # A terminal reset/error is the authoritative end of the live turn,
        # even if the underlying stream still retains its last task id.  Keep
        # this rule in the application seam so every transport gets the same
        # ownership result.
        terminal = any(
            event.payload.get("terminal") is True
            for event in projected
        )
        return ProjectedConversationSnapshot(
            task_id=None if terminal else source.task_id,
            stream_generation=source.stream_generation,
            current_stream_seq=source.current_stream_seq,
            events=tuple(projected),
        )


class InMemoryConversationSnapshotReader:
    """Small adapter-friendly reader for tests and local composition."""

    def __init__(self, snapshots: Mapping[str, LiveConversationSnapshot]) -> None:
        self._snapshots = snapshots

    def read_live_snapshot(self, session_key: str) -> LiveConversationSnapshot:
        return self._snapshots.get(
            session_key,
            LiveConversationSnapshot(
                task_id=None,
                stream_generation="",
                current_stream_seq=0,
                events=(),
            ),
        )


def snapshot_events(
    events: Iterable[object],
) -> tuple[ConversationSnapshotEvent, ...]:
    """Convert a concrete stream event sequence at the adapter boundary."""

    converted: list[ConversationSnapshotEvent] = []
    for event in events:
        name = getattr(event, "event_name", None)
        payload = getattr(event, "payload", None)
        if not isinstance(name, str) or not isinstance(payload, Mapping):
            raise TypeError("live conversation snapshot contains a malformed event")
        converted.append(ConversationSnapshotEvent(name=name, payload=dict(payload)))
    return tuple(converted)


__all__ = [
    "ConversationEventProjector",
    "ConversationSnapshotApplication",
    "ConversationSnapshotEvent",
    "ConversationSnapshotReader",
    "InMemoryConversationSnapshotReader",
    "LiveConversationSnapshot",
    "ProjectedConversationSnapshot",
    "snapshot_events",
]
