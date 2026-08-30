"""Gateway adapter for the transport-neutral conversation snapshot seam."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, cast

from opensquilla.application.conversation_runtime import (
    ConversationEventProjector,
    ConversationSnapshotApplication,
    LiveConversationSnapshot,
    ProjectedConversationSnapshot,
    snapshot_events,
)


class SessionStreamSnapshotAdapter:
    """Expose only live-snapshot data required by the Application Module."""

    def __init__(self, streams: object) -> None:
        self._streams = streams

    def read_live_snapshot(self, session_key: str) -> LiveConversationSnapshot:
        # Keep the old handler's failure behaviour at this boundary: a broken
        # stream registry must not silently turn a real error into an empty
        # successful snapshot.  The concrete registry supplies these fields;
        # casts narrow its opaque shape without importing it into the
        # application module.
        read = getattr(self._streams, "live_snapshot")
        snapshot = read(session_key)
        return LiveConversationSnapshot(
            task_id=cast(str | None, getattr(snapshot, "task_id")),
            stream_generation=cast(str, getattr(snapshot, "stream_generation")),
            current_stream_seq=cast(int, getattr(snapshot, "current_stream_seq")),
            events=snapshot_events(
                cast(Iterable[object], getattr(snapshot, "events"))
            ),
        )


def build_conversation_snapshot_application(
    streams: object,
    *,
    projector: ConversationEventProjector,
) -> ConversationSnapshotApplication:
    """Compose the snapshot application without importing Gateway types into it."""

    return ConversationSnapshotApplication(
        reader=SessionStreamSnapshotAdapter(streams),
        projector=projector,
    )


def build_v4_conversation_snapshot_application(
    streams: object,
) -> ConversationSnapshotApplication:
    """Compose the v4 adapter without exposing projection to the RPC handler."""

    from opensquilla.gateway.protocol import project_session_event_for_client

    def project(
        event_name: str,
        payload: Mapping[str, Any],
        caps: frozenset[str],
    ) -> tuple[str, dict[str, Any]] | None:
        projected = project_session_event_for_client(
            event_name,
            dict(payload),
            client_caps=caps,
        )
        if projected is None:
            return None
        projected_name, projected_payload = projected
        if not isinstance(projected_payload, Mapping):
            # The current live stream only carries object payloads.  Keep the
            # old renderer's failure boundary explicit if a future producer
            # violates that invariant instead of silently inventing fields.
            raise TypeError(
                f"conversation event {projected_name!r} payload must be an object"
            )
        return projected_name, dict(projected_payload)

    return build_conversation_snapshot_application(streams, projector=project)


def snapshot_result_to_v4(
    session_key: str,
    snapshot: ProjectedConversationSnapshot,
) -> dict[str, Any]:
    """Render the existing v4 response envelope at the adapter boundary."""

    return {
        "key": session_key,
        "task_id": snapshot.task_id,
        "stream_generation": snapshot.stream_generation,
        "current_stream_seq": snapshot.current_stream_seq,
        "events": [
            {
                "event": event.name,
                "payload": dict(event.payload),
            }
            for event in snapshot.events
        ],
    }


__all__ = [
    "SessionStreamSnapshotAdapter",
    "build_conversation_snapshot_application",
    "build_v4_conversation_snapshot_application",
    "snapshot_result_to_v4",
]
