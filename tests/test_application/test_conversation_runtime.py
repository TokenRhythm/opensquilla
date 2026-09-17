"""Tests for the transport-independent conversation snapshot seam."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from opensquilla.application.conversation_runtime import (
    ConversationSnapshotApplication,
    ConversationSnapshotEvent,
    InMemoryConversationSnapshotReader,
    LiveConversationSnapshot,
    snapshot_events,
)


@dataclass(frozen=True)
class ConcreteEvent:
    event_name: str
    payload: dict[str, Any]
    stream_seq: int = 0


def snapshot(
    *events: ConversationSnapshotEvent,
    task_id: str | None = "task-1",
) -> LiveConversationSnapshot:
    return LiveConversationSnapshot(
        task_id=task_id,
        stream_generation="generation-1",
        current_stream_seq=len(events),
        events=events,
    )


def identity_projector(
    name: str,
    payload: Mapping[str, Any],
    _caps: frozenset[str],
) -> tuple[str, Mapping[str, Any]]:
    return name, payload


@pytest.mark.asyncio
async def test_read_projects_events_and_clears_terminal_task_owner() -> None:
    terminal_payload = {"terminal": True, "message": "done"}
    reader = InMemoryConversationSnapshotReader(
        {
            "session-a": snapshot(
                ConversationSnapshotEvent("session.event.answer", {"text": "ok"}),
                ConversationSnapshotEvent("session.event.error", terminal_payload),
            )
        }
    )
    app = ConversationSnapshotApplication(reader=reader, projector=identity_projector)

    result = app.read("session-a", client_caps=frozenset({"events.v2"}))

    assert result.task_id is None
    assert result.stream_generation == "generation-1"
    assert result.current_stream_seq == 2
    assert [event.name for event in result.events] == [
        "session.event.answer",
        "session.event.error",
    ]
    assert result.events[1].payload == terminal_payload


def test_read_skips_events_filtered_by_the_capability_projector() -> None:
    reader = InMemoryConversationSnapshotReader(
        {"session-a": snapshot(ConversationSnapshotEvent("private", {"value": 1}))}
    )

    def projector(
        name: str,
        payload: Mapping[str, Any],
        caps: frozenset[str],
    ) -> tuple[str, Mapping[str, Any]] | None:
        if name == "private" and "private-events" not in caps:
            return None
        return name, payload

    result = ConversationSnapshotApplication(reader=reader, projector=projector).read(
        "session-a"
    )

    assert result.task_id == "task-1"
    assert result.events == ()


def test_read_copies_projected_payload_without_mutating_the_source() -> None:
    payload = {"nested": {"value": 1}}
    source = snapshot(ConversationSnapshotEvent("event", payload))
    reader = InMemoryConversationSnapshotReader({"session-a": source})

    def projector(
        name: str,
        event_payload: Mapping[str, Any],
        _caps: frozenset[str],
    ) -> tuple[str, Mapping[str, Any]]:
        mutable = event_payload
        assert isinstance(mutable, dict)
        mutable["adapter_seen"] = True
        return name, mutable

    result = ConversationSnapshotApplication(reader=reader, projector=projector).read(
        "session-a"
    )

    assert result.events[0].payload["adapter_seen"] is True
    assert "adapter_seen" not in payload


def test_missing_session_is_an_empty_non_running_snapshot() -> None:
    app = ConversationSnapshotApplication(
        reader=InMemoryConversationSnapshotReader({}),
        projector=identity_projector,
    )

    result = app.read("missing")

    assert result.task_id is None
    assert result.stream_generation == ""
    assert result.current_stream_seq == 0
    assert result.events == ()


def test_snapshot_events_converts_concrete_stream_events_at_the_adapter_boundary() -> None:
    payload = {"text": "hello"}
    events = snapshot_events(
        [
            ConcreteEvent("session.event.text_delta", payload, 1),
            ConcreteEvent("session.event.answer", {"terminal": True}, 2),
        ]
    )

    assert events == (
        ConversationSnapshotEvent("session.event.text_delta", payload),
        ConversationSnapshotEvent("session.event.answer", {"terminal": True}),
    )
    assert events[0].payload is not payload


def test_snapshot_events_rejects_malformed_event_shapes() -> None:
    with pytest.raises(TypeError, match="malformed event"):
        snapshot_events(
            [
                ConcreteEvent("", {}),
                type("Bad", (), {"event_name": 1, "payload": {}})(),
            ]
        )

    with pytest.raises(TypeError, match="malformed event"):
        snapshot_events(
            [type("BadPayload", (), {"event_name": "event", "payload": []})()]
        )
