from opensquilla.gateway import session_streams
from opensquilla.gateway.session_streams import (
    SessionStreamRegistry,
    get_session_streams,
    reset_session_streams,
)


def test_session_stream_registry_records_monotonic_stream_seq() -> None:
    registry = SessionStreamRegistry(
        max_events_per_session=5,
        stream_generation="gateway-generation-a",
    )

    first = registry.record("agent:main:test", "session.event.text_delta", {"text": "a"})
    second = registry.record("agent:main:test", "session.event.done", {"reason": "stop"})

    assert first["stream_seq"] == 1
    assert second["stream_seq"] == 2
    assert first["stream_generation"] == "gateway-generation-a"
    assert second["stream_generation"] == "gateway-generation-a"
    assert second["session_key"] == "agent:main:test"
    assert registry.current_seq("agent:main:test") == 2


def test_session_stream_registry_replays_events_after_cursor() -> None:
    registry = SessionStreamRegistry(max_events_per_session=5)
    registry.record("agent:main:test", "session.event.text_delta", {"text": "a"})
    registry.record("agent:main:test", "session.event.text_delta", {"text": "b"})

    replay = registry.replay("agent:main:test", 1)

    assert replay.current_stream_seq == 2
    assert replay.replay_complete is True
    assert [event.payload["text"] for event in replay.events] == ["b"]


def test_session_stream_registry_preserves_original_emitted_at_on_replay(
    monkeypatch,
) -> None:
    registry = SessionStreamRegistry(max_events_per_session=5)
    monkeypatch.setattr(session_streams, "_epoch_time_ms", lambda: 1_234)
    recorded = registry.record(
        "agent:main:test",
        "session.event.done",
        {"reason": "stop"},
    )

    monkeypatch.setattr(session_streams, "_epoch_time_ms", lambda: 9_999)
    replay = registry.replay("agent:main:test", 0)

    assert recorded["emitted_at"] == 1_234
    assert replay.events[0].payload["emitted_at"] == 1_234


def test_session_stream_registry_reports_incomplete_replay() -> None:
    registry = SessionStreamRegistry(max_events_per_session=2)
    registry.record("agent:main:test", "session.event.text_delta", {"text": "a"})
    registry.record("agent:main:test", "session.event.text_delta", {"text": "b"})
    registry.record("agent:main:test", "session.event.text_delta", {"text": "c"})

    replay = registry.replay("agent:main:test", 0)

    assert replay.current_stream_seq == 3
    assert replay.replay_complete is False
    assert [event.stream_seq for event in replay.events] == [2, 3]


def test_session_stream_registry_preserves_meta_step_control_events() -> None:
    registry = SessionStreamRegistry(max_events_per_session=5)
    registry.record(
        "agent:main:test",
        "session.event.tool_use_start",
        {"tool_name": "meta-step:writing_plan", "tool_use_id": "meta_step_writing_plan"},
    )
    registry.record(
        "agent:main:test",
        "session.event.tool_result",
        {
            "tool_name": "meta-step:writing_plan",
            "tool_use_id": "meta_step_writing_plan",
            "result": "ok",
        },
    )
    for index in range(10):
        registry.record(
            "agent:main:test",
            "session.event.text_delta",
            {"text": f"chunk-{index}"},
        )

    replay = registry.replay("agent:main:test", 0)

    tool_events = [
        event for event in replay.events
        if event.payload.get("tool_name") == "meta-step:writing_plan"
    ]
    assert [event.event_name for event in tool_events] == [
        "session.event.tool_use_start",
        "session.event.tool_result",
    ]


def test_session_stream_registry_reports_reset_when_client_cursor_is_ahead() -> None:
    registry = SessionStreamRegistry(max_events_per_session=5)

    replay = registry.replay("agent:main:after-restart", 5)

    assert replay.current_stream_seq == 0
    assert replay.replay_complete is False
    assert replay.gap_reason == "stream_buffer_reset"
    assert replay.events == []


def test_session_stream_registry_reports_generation_change_without_replaying_old_cursor() -> None:
    registry = SessionStreamRegistry(
        max_events_per_session=5,
        stream_generation="gateway-generation-new",
    )
    registry.record("agent:main:after-restart", "session.event.text_delta", {"text": "new"})

    replay = registry.replay(
        "agent:main:after-restart",
        5_000,
        "gateway-generation-old",
    )

    assert replay.stream_generation == "gateway-generation-new"
    assert replay.current_stream_seq == 1
    assert replay.replay_complete is False
    assert replay.gap_reason == "stream_generation_changed"
    assert replay.events == []


def test_session_stream_registry_promotes_legacy_safe_integer_cursor() -> None:
    registry = SessionStreamRegistry(
        max_events_per_session=5,
        stream_generation="gateway-generation-new",
    )
    session_key = "agent:main:legacy-after-restart"

    assert registry.promote_legacy_cursor(session_key, 4_200) is True
    event = registry.record(
        session_key,
        "session.event.text_delta",
        {"text": "visible"},
    )

    assert event["stream_seq"] == 4_201
    assert event["stream_generation"] == "gateway-generation-new"


def test_session_stream_registry_rejects_unsafe_legacy_cursor_promotion() -> None:
    registry = SessionStreamRegistry(max_events_per_session=5)
    session_key = "agent:main:unsafe-legacy-cursor"

    assert registry.promote_legacy_cursor(session_key, 1 << 53) is False
    assert registry.record(
        session_key,
        "session.event.text_delta",
        {"text": "first"},
    )["stream_seq"] == 1


def test_live_turn_snapshot_compacts_high_frequency_deltas_without_losing_state() -> None:
    registry = SessionStreamRegistry(max_events_per_session=5)
    session_key = "agent:main:long-turn"
    task_id = "task-long"

    registry.record(
        session_key,
        "session.event.state_change",
        {"task_id": task_id, "to_state": "thinking"},
    )
    registry.record(
        session_key,
        "session.event.thinking",
        {"task_id": task_id, "text": "Plan"},
    )
    registry.record(
        session_key,
        "session.event.thinking",
        {"task_id": task_id, "text": "ning"},
    )
    registry.record(
        session_key,
        "session.event.tool_use_start",
        {"task_id": task_id, "tool_use_id": "call-1", "tool_name": "exec_command"},
    )
    for fragment in ("{", '"cmd"', ":", '"pwd"', "}"):
        registry.record(
            session_key,
            "session.event.tool_use_delta",
            {
                "task_id": task_id,
                "tool_use_id": "call-1",
                "json_fragment": fragment,
            },
        )
    registry.record(
        session_key,
        "session.event.tool_result",
        {
            "task_id": task_id,
            "tool_use_id": "call-1",
            "tool_name": "exec_command",
            "result": "/workspace",
        },
    )
    registry.record(
        session_key,
        "session.event.text_delta",
        {"task_id": task_id, "text": "Hello"},
    )
    registry.record(
        session_key,
        "session.event.text_delta",
        {"task_id": task_id, "text": " world"},
    )

    replay = registry.replay(session_key, 0)
    assert len(replay.events) <= 5
    assert not any(event.event_name == "session.event.text_delta" for event in replay.events)

    snapshot = registry.live_snapshot(session_key)

    assert snapshot.current_stream_seq == registry.current_seq(session_key)
    assert snapshot.task_id == task_id
    assert [event.event_name for event in snapshot.events] == [
        "session.event.state_change",
        "session.event.thinking",
        "session.event.tool_use_start",
        "session.event.tool_use_delta",
        "session.event.tool_result",
        "session.event.text_delta",
    ]
    assert snapshot.events[1].payload["text"] == "Planning"
    assert snapshot.events[3].payload["json_fragment"] == '{"cmd":"pwd"}'
    assert snapshot.events[5].payload["text"] == "Hello world"


def test_live_turn_snapshot_is_replaced_by_the_next_task_and_cleared_on_terminal() -> None:
    registry = SessionStreamRegistry(max_events_per_session=5)
    session_key = "agent:main:sequential-turns"

    registry.record(
        session_key,
        "session.event.text_delta",
        {"task_id": "task-old", "text": "old"},
    )
    registry.record(
        session_key,
        "session.event.text_delta",
        {"task_id": "task-new", "text": "new"},
    )

    snapshot = registry.live_snapshot(session_key)
    assert snapshot.task_id == "task-new"
    assert [event.payload["text"] for event in snapshot.events] == ["new"]

    registry.record(
        session_key,
        "session.event.done",
        {"task_id": "task-new", "reason": "completed"},
    )

    terminal_snapshot = registry.live_snapshot(session_key)
    assert terminal_snapshot.task_id is None
    assert terminal_snapshot.events == []
    assert terminal_snapshot.current_stream_seq == registry.current_seq(session_key)


def test_session_stream_registry_preserves_compaction_boundaries_over_heartbeats() -> None:
    registry = SessionStreamRegistry(max_events_per_session=2)
    session_key = "agent:main:compaction-lifecycle"
    compaction_id = "compaction-replay"
    registry.record(
        session_key,
        "session.event.compaction",
        {"status": "started", "compaction_id": compaction_id, "sequence": 1},
    )
    registry.record(
        session_key,
        "session.event.compaction",
        {
            "status": "observed",
            "compaction_id": compaction_id,
            "sequence": 2,
            "heartbeat": True,
        },
    )
    registry.record(
        session_key,
        "session.event.compaction",
        {
            "status": "observed",
            "compaction_id": compaction_id,
            "sequence": 3,
            "heartbeat": True,
        },
    )
    registry.record(
        session_key,
        "session.event.compaction",
        {"status": "completed", "compaction_id": compaction_id, "sequence": 4},
    )

    replay = registry.replay(session_key, 0)

    assert replay.current_stream_seq == 4
    assert replay.replay_complete is True
    assert replay.gap_reason is None
    assert [event.payload["status"] for event in replay.events] == ["started", "completed"]
    assert [event.payload["sequence"] for event in replay.events] == [1, 4]
    assert all(event.payload.get("heartbeat") is not True for event in replay.events)


def test_live_turn_snapshot_preserves_active_compaction_state() -> None:
    registry = SessionStreamRegistry(max_events_per_session=2)
    session_key = "agent:main:active-compaction"
    compaction_id = "compaction-live"
    registry.record(
        session_key,
        "session.event.compaction",
        {"status": "started", "compaction_id": compaction_id, "sequence": 1},
    )
    registry.record(
        session_key,
        "session.event.compaction",
        {
            "status": "observed",
            "compaction_id": compaction_id,
            "sequence": 2,
            "heartbeat": True,
            "phase": "summarizing",
        },
    )

    snapshot = registry.live_snapshot(session_key)

    assert snapshot.current_stream_seq == 2
    assert [event.event_name for event in snapshot.events] == [
        "session.event.compaction",
        "session.event.compaction",
    ]
    assert [event.payload["status"] for event in snapshot.events] == [
        "started",
        "observed",
    ]
    assert all(
        event.payload["compaction_id"] == compaction_id
        for event in snapshot.events
    )


def test_provider_activity_pulses_are_lossy_in_replay_but_keep_phase_boundaries() -> None:
    registry = SessionStreamRegistry(max_events_per_session=2)
    session_key = "agent:main:provider-activity"
    common = {"task_id": "task-live", "activity_id": "activity-1"}
    registry.record(
        session_key,
        "session.event.provider_activity",
        {**common, "phase": "requesting", "heartbeat": False},
    )
    registry.record(
        session_key,
        "session.event.provider_activity",
        {**common, "phase": "reasoning", "heartbeat": False},
    )
    registry.record(
        session_key,
        "session.event.provider_activity",
        {**common, "phase": "reasoning", "heartbeat": True, "pulse": 1},
    )
    latest = registry.record(
        session_key,
        "session.event.provider_activity",
        {**common, "phase": "reasoning", "heartbeat": True, "pulse": 2},
    )

    replay = registry.replay(session_key, 0)
    assert [event.payload["phase"] for event in replay.events] == [
        "requesting",
        "reasoning",
    ]
    assert all(event.payload.get("heartbeat") is not True for event in replay.events)

    snapshot = registry.live_snapshot(session_key)
    activity = [
        event
        for event in snapshot.events
        if event.event_name == "session.event.provider_activity"
    ]
    assert [event.payload["phase"] for event in activity] == [
        "requesting",
        "reasoning",
    ]
    assert activity[-1].payload["pulse"] == 2
    assert activity[-1].stream_seq == latest["stream_seq"]


def test_reset_session_streams_starts_a_fresh_embedded_gateway_generation() -> None:
    try:
        first = reset_session_streams(stream_generation="embedded-generation-a")
        first.record("agent:main:test", "session.event.text_delta", {"text": "old"})

        second = reset_session_streams(stream_generation="embedded-generation-b")

        assert get_session_streams() is second
        assert second.stream_generation == "embedded-generation-b"
        assert second.current_seq("agent:main:test") == 0
        assert second.live_snapshot("agent:main:test").events == []
        assert first.stream_generation == "embedded-generation-a"
        assert first.current_seq("agent:main:test") == 1
    finally:
        reset_session_streams()
