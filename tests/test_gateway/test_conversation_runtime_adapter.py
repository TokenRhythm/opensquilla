"""Tests for the Gateway side of the conversation snapshot seam."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from opensquilla.gateway.adapters.conversation_runtime import (
    SessionStreamSnapshotAdapter,
    build_conversation_snapshot_application,
    snapshot_result_to_v4,
)


def test_stream_adapter_projects_only_the_snapshot_port() -> None:
    source_payload = {"text": "hello"}
    streams = SimpleNamespace(
        live_snapshot=lambda key: SimpleNamespace(
            task_id="task-1",
            stream_generation="generation-1",
            current_stream_seq=4,
            events=[
                SimpleNamespace(
                    event_name="session.event.text_delta",
                    payload=source_payload,
                    stream_seq=4,
                )
            ],
        )
    )

    snapshot = SessionStreamSnapshotAdapter(streams).read_live_snapshot("session-a")

    assert snapshot.task_id == "task-1"
    assert snapshot.current_stream_seq == 4
    assert snapshot.events[0].name == "session.event.text_delta"
    assert snapshot.events[0].payload == source_payload
    assert snapshot.events[0].payload is not source_payload


def test_application_and_renderer_keep_the_existing_v4_shape() -> None:
    streams = SimpleNamespace(
        live_snapshot=lambda _key: SimpleNamespace(
            task_id="task-1",
            stream_generation="generation-1",
            current_stream_seq=2,
            events=[
                SimpleNamespace(
                    event_name="session.event.answer",
                    payload={"text": "ok"},
                    stream_seq=2,
                ),
                SimpleNamespace(
                    event_name="session.event.error",
                    payload={"terminal": True},
                    stream_seq=3,
                ),
            ],
        )
    )

    def projector(
        name: str,
        payload: dict[str, Any],
        caps: frozenset[str],
    ) -> tuple[str, dict[str, Any]] | None:
        if name == "session.event.answer" and "answers" not in caps:
            return None
        return name, payload

    application = build_conversation_snapshot_application(
        streams,
        projector=projector,
    )
    result = application.read("session-a", client_caps=frozenset({"answers"}))

    assert snapshot_result_to_v4("session-a", result) == {
        "key": "session-a",
        "task_id": None,
        "stream_generation": "generation-1",
        "current_stream_seq": 2,
        "events": [
            {"event": "session.event.answer", "payload": {"text": "ok"}},
            {"event": "session.event.error", "payload": {"terminal": True}},
        ],
    }
