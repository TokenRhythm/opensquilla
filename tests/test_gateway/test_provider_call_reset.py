from __future__ import annotations

import pytest

from opensquilla.contracts.gateway_transport import (
    ANSWER_GENERATION_RESET_CAPABILITY,
    PROVIDER_CALL_RESET_CAPABILITY,
)
from opensquilla.gateway.protocol import project_session_event_for_client
from opensquilla.gateway.session_streams import SessionStreamRegistry

_KEY = "agent:main:attempt-recovery"
_IDENTITY = {"task_id": "task-recovery", "assistant_message_id": "assistant-recovery"}


def _record(
    registry: SessionStreamRegistry,
    name: str,
    **payload: object,
) -> None:
    registry.record(_KEY, f"session.event.{name}", {**_IDENTITY, **payload})


def _reset(registry: SessionStreamRegistry, failed: str, epoch: int) -> None:
    _record(
        registry,
        "answer_generation_reset",
        reset_scope="provider_call",
        rollback_model_call_id=failed,
        old_generation_epoch=epoch,
        new_generation_epoch=epoch + 1,
        terminal=False,
        authoritative_text_snapshot="committed prefix",
    )


def test_live_call_reset_keeps_ordered_prefix_and_completed_tools() -> None:
    registry = SessionStreamRegistry()
    _record(
        registry,
        "thinking",
        text="accepted reasoning",
        model_call_id="1.0",
        block_id="block-one",
        generation_epoch=0,
    )
    _record(
        registry,
        "text_delta",
        text="committed prefix",
        model_call_id="1.0",
        presentation="intermediate",
        generation_epoch=0,
    )
    _record(registry, "tool_use_start", tool_use_id="tool-one", generation_epoch=0)
    _record(registry, "tool_result", tool_use_id="tool-one", result="completed")
    _record(
        registry, "thinking_start", block_id="block-two", model_call_id="2.0", generation_epoch=0
    )
    _record(
        registry,
        "thinking",
        text="failed reasoning",
        model_call_id="2.0",
        block_id="block-two",
        generation_epoch=0,
    )
    _record(registry, "text_delta", text="failed prefix", model_call_id="2.0", generation_epoch=0)
    _reset(registry, "2.0", 0)
    _record(registry, "text_delta", text="accepted suffix", model_call_id="2.1", generation_epoch=1)
    _record(registry, "text_delta", text="late old suffix", model_call_id="2.0", generation_epoch=0)

    snapshot = registry.live_snapshot(_KEY)
    assert [event.event_name.removeprefix("session.event.") for event in snapshot.events] == [
        "thinking",
        "text_delta",
        "tool_use_start",
        "tool_result",
        "answer_generation_reset",
        "text_delta",
    ]
    assert [event.payload["text"] for event in snapshot.events if "text" in event.payload] == [
        "accepted reasoning",
        "committed prefix",
        "accepted suffix",
    ]
    assert snapshot.events[1].payload["presentation"] == "intermediate"


def test_repeated_call_reset_preserves_prefix_across_generation_boundaries() -> None:
    registry = SessionStreamRegistry()
    _record(
        registry, "text_delta", text="committed prefix", model_call_id="1.0", generation_epoch=0
    )
    _record(registry, "text_delta", text="failed one", model_call_id="2.0", generation_epoch=0)
    _reset(registry, "2.0", 0)
    _record(registry, "text_delta", text="failed two", model_call_id="2.1", generation_epoch=1)
    _reset(registry, "2.1", 1)
    _record(registry, "text_delta", text="accepted suffix", model_call_id="2.2", generation_epoch=2)
    _reset(registry, "2.0", 0)
    _reset(registry, "2.2", 0)
    _reset(registry, "1.0", 2)

    snapshot = registry.live_snapshot(_KEY)
    assert [event.payload["text"] for event in snapshot.events if "text" in event.payload] == [
        "committed prefix",
        "accepted suffix",
    ]
    assert len([event for event in snapshot.events if event.event_name.endswith("reset")]) == 2
    replay = registry.replay(_KEY, 0)
    assert [event.payload["text"] for event in replay.events if "text" in event.payload] == [
        "committed prefix",
        "accepted suffix",
    ]


def test_call_reset_replay_keeps_successful_prefix_without_failed_attempt() -> None:
    registry = SessionStreamRegistry()
    _record(
        registry, "text_delta", text="committed prefix", model_call_id="1.0", generation_epoch=0
    )
    _record(registry, "tool_result", tool_use_id="tool-one", result="completed")
    _record(registry, "text_delta", text="failed prefix", model_call_id="2.0", generation_epoch=0)
    _reset(registry, "2.0", 0)
    _record(registry, "text_delta", text="accepted suffix", model_call_id="2.1", generation_epoch=1)

    replay = registry.replay(_KEY, 0)
    assert replay.replay_complete
    assert [event.payload["text"] for event in replay.events if "text" in event.payload] == [
        "committed prefix",
        "accepted suffix",
    ]
    assert replay.events[1].event_name == "session.event.tool_result"


def test_call_reset_does_not_hide_missing_successful_prefix_in_replay() -> None:
    registry = SessionStreamRegistry(max_events_per_session=3)
    _record(
        registry, "text_delta", text="committed prefix", model_call_id="1.0", generation_epoch=0
    )
    _record(registry, "tool_result", tool_use_id="tool-one", result="completed")
    _record(registry, "text_delta", text="failed prefix", model_call_id="2.0", generation_epoch=0)
    _reset(registry, "2.0", 0)
    _record(registry, "text_delta", text="accepted suffix", model_call_id="2.1", generation_epoch=1)

    replay = registry.replay(_KEY, 0)
    assert not replay.replay_complete
    assert replay.gap_reason == "buffer_window_missed"
    snapshot = registry.live_snapshot(_KEY)
    assert snapshot.events[0].payload["text"] == "committed prefix"


@pytest.mark.parametrize("kind", ["thinking", "thinking_start", "thinking_end"])
def test_call_reset_marks_trimmed_reasoning_prefix_as_replay_gap(kind: str) -> None:
    registry = SessionStreamRegistry(max_events_per_session=3)
    _record(registry, "tool_result", tool_use_id="tool-one", result="completed")
    _record(
        registry, kind, text="accepted reasoning", block_id="accepted",
        model_call_id="1.0", generation_epoch=0,
    )
    _record(
        registry, "thinking_start", block_id="failed", model_call_id="2.0", generation_epoch=0,
    )
    _reset(registry, "2.0", 0)
    _record(
        registry, "thinking", text="new reasoning", block_id="replacement",
        model_call_id="2.1", generation_epoch=1,
    )

    replay = registry.replay(_KEY, 0)

    assert not replay.replay_complete
    assert replay.gap_reason == "buffer_window_missed"
    assert any(
        event.payload.get("text") == "accepted reasoning"
        for event in registry.live_snapshot(_KEY).events
    )


@pytest.mark.parametrize("identity_field", ["task_id", "turn_id", "assistant_message_id"])
@pytest.mark.parametrize("evict", [False, True])
def test_replay_does_not_apply_previous_turn_epoch_to_successor(
    identity_field: str, evict: bool,
) -> None:
    registry = SessionStreamRegistry()

    def record(identity: str, kind: str, **payload: object) -> dict[str, object]:
        return registry.record(
            _KEY, f"session.event.{kind}", {identity_field: identity, **payload},
        )

    record("previous", "text_delta", text="failed", model_call_id="1.0", generation_epoch=0)
    record(
        "previous", "answer_generation_reset", reset_scope="provider_call",
        rollback_model_call_id="1.0", old_generation_epoch=0, new_generation_epoch=1,
    )
    record("previous", "text_delta", text="recovered", model_call_id="1.1", generation_epoch=1)
    terminal = record("previous", "done", text="recovered", generation_epoch=1)
    if evict:
        registry._evict_replay_cache(_KEY)
    record("successor", "text_delta", text="next answer", model_call_id="1.0", generation_epoch=0)

    replay = registry.replay(_KEY, int(terminal["stream_seq"]))

    assert replay.replay_complete
    assert [event.payload.get("text") for event in replay.events] == ["next answer"]


def test_call_reset_replay_with_successor_retains_reused_attempt_identity() -> None:
    registry = SessionStreamRegistry()
    _record(registry, "text_delta", text="failed", model_call_id="1.0", generation_epoch=0)
    _reset(registry, "1.0", 0)
    _record(registry, "text_delta", text="recovered", model_call_id="1.1", generation_epoch=1)
    _record(registry, "done", text="recovered", generation_epoch=1)
    registry.record(
        _KEY, "session.event.text_delta", {
            "task_id": "successor-task", "assistant_message_id": "successor-answer",
            "text": "next answer", "model_call_id": "1.0", "generation_epoch": 0,
        },
    )

    replay = registry.replay(_KEY, 0)

    assert replay.replay_complete
    assert [event.payload.get("text") for event in replay.events] == [
        None, "recovered", "recovered", "next answer",
    ]


def test_replay_after_reset_reports_trimmed_replacement_reasoning() -> None:
    registry = SessionStreamRegistry(max_events_per_session=3)
    _record(registry, "tool_result", tool_use_id="tool-one", result="completed")
    _record(registry, "thinking", text="failed", model_call_id="2.0", generation_epoch=0)
    _reset(registry, "2.0", 0)
    reset_cursor = registry.current_seq(_KEY)
    for index in range(2):
        _record(
            registry, "thinking", text=f"replacement {index}", block_id=f"block-{index}",
            model_call_id="2.1", generation_epoch=1,
        )

    replay = registry.replay(_KEY, reset_cursor)

    assert not replay.replay_complete
    assert replay.gap_reason == "buffer_window_missed"
    assert [event.payload.get("text") for event in replay.events] == ["replacement 1"]
    assert [
        event.payload["text"] for event in registry.live_snapshot(_KEY).events
        if "text" in event.payload
    ] == ["replacement 0", "replacement 1"]


@pytest.mark.parametrize("kind", ["text_delta", "thinking", "thinking_start", "thinking_end"])
@pytest.mark.parametrize("caps", [(), (ANSWER_GENERATION_RESET_CAPABILITY,)])
def test_legacy_projection_hides_scoped_attempt_content(kind: str, caps: tuple[str, ...]) -> None:
    payload = {**_IDENTITY, "model_call_id": "2.0", "text": "provisional"}
    assert (
        project_session_event_for_client(
            f"session.event.{kind}",
            payload,
            client_caps=caps,
        )
        is None
    )
    assert project_session_event_for_client(
        f"session.event.{kind}",
        payload,
        client_caps=(PROVIDER_CALL_RESET_CAPABILITY,),
    ) == (f"session.event.{kind}", payload)


@pytest.mark.parametrize("kind", ["text_delta", "thinking", "thinking_start", "thinking_end"])
@pytest.mark.parametrize("caps", [(), (ANSWER_GENERATION_RESET_CAPABILITY,)])
def test_projection_streams_calls_that_cannot_be_rolled_back(
    kind: str, caps: tuple[str, ...],
) -> None:
    payload = {
        **_IDENTITY, "model_call_id": "2.0", "text": "live CLI content",
        "provider_call_resettable": False,
    }
    event_name = f"session.event.{kind}"
    assert project_session_event_for_client(event_name, payload, client_caps=caps) == (
        event_name, payload,
    )
    # This describes the producing turn, not the observing client's version.
    # CLI observers of a recoverable WebUI turn must still withhold drafts.
    assert project_session_event_for_client(
        event_name, {**payload, "provider_call_resettable": True}, client_caps=caps,
    ) is None


def test_projection_keeps_legacy_producers_and_canonical_terminals() -> None:
    text = {"text": "unscoped legacy content"}
    assert project_session_event_for_client("session.event.text_delta", text) == (
        "session.event.text_delta",
        text,
    )
    reset = {
        **_IDENTITY,
        "reset_scope": "provider_call",
        "rollback_model_call_id": "2.0",
        "old_generation_epoch": 0,
        "new_generation_epoch": 1,
    }
    projected_reset = project_session_event_for_client(
        "session.event.answer_generation_reset", reset,
    )
    assert projected_reset is not None
    assert projected_reset[1]["reset_scope"] == "answer"
    done = {**_IDENTITY, "text": "canonical result", "text_snapshot": "canonical result"}
    assert project_session_event_for_client("session.event.done", done) == (
        "session.event.done",
        done,
    )
    error = {**_IDENTITY, "code": "response_incomplete", "message": "safe failure"}
    assert project_session_event_for_client("session.event.error", error) == (
        "session.event.error",
        error,
    )


def test_v1_client_receives_call_terminal_reset_as_one_legacy_error() -> None:
    projected = project_session_event_for_client(
        "session.event.answer_generation_reset",
        {
            "reset_scope": "provider_call",
            "terminal": True,
            "terminal_text_snapshot": "safe failure",
            "terminal_error_message": "internal",
        },
        client_caps=(ANSWER_GENERATION_RESET_CAPABILITY,),
    )
    assert projected is not None
    assert projected[0] == "session.event.error"
    assert projected[1]["message"] == "safe failure"
    assert "terminal_error_message" not in projected[1]


def test_snapshot_projection_keeps_legacy_and_modern_observers_independent() -> None:
    from opensquilla.gateway.adapters.conversation_runtime import (
        build_v4_conversation_snapshot_application,
    )

    registry = SessionStreamRegistry()
    _record(
        registry, "text_delta", text="committed prefix", model_call_id="1.0", generation_epoch=0
    )
    _record(registry, "tool_result", tool_use_id="tool-one", result="completed")
    _record(registry, "text_delta", text="failed prefix", model_call_id="2.0", generation_epoch=0)
    _reset(registry, "2.0", 0)
    _record(registry, "text_delta", text="accepted suffix", model_call_id="2.1", generation_epoch=1)
    application = build_v4_conversation_snapshot_application(registry)
    legacy = application.read(_KEY)
    modern = application.read(_KEY, client_caps=frozenset({PROVIDER_CALL_RESET_CAPABILITY}))

    assert [event.name for event in legacy.events] == [
        "session.event.tool_result", "session.event.answer_generation_reset",
    ]
    assert legacy.events[-1].payload["reset_scope"] == "answer"
    assert [event.payload["text"] for event in modern.events if "text" in event.payload] == [
        "committed prefix",
        "accepted suffix",
    ]
    assert len(registry.live_snapshot(_KEY).events) == len(modern.events)


def test_call_reset_wire_round_trips_scope_and_failed_physical_attempt() -> None:
    from opensquilla.engine.types import AnswerGenerationResetEvent
    from opensquilla.gateway.protocol import deserialize_public_event, serialize_public_event

    payload = serialize_public_event(
        AnswerGenerationResetEvent(
            reset_scope="provider_call",
            rollback_model_call_id="4.0",
            old_generation_epoch=2,
            new_generation_epoch=3,
        )
    )
    parsed = deserialize_public_event(payload)
    assert parsed.reset_scope == "provider_call"
    assert parsed.rollback_model_call_id == "4.0"


@pytest.mark.parametrize("caps", [(), (ANSWER_GENERATION_RESET_CAPABILITY,)])
def test_legacy_projection_advances_generation_before_retry_terminal(caps: tuple[str, ...]) -> None:
    reset = {
        **_IDENTITY, "reset_scope": "provider_call", "rollback_model_call_id": "4.0",
        "old_generation_epoch": 0, "new_generation_epoch": 1, "terminal": False,
        "authoritative_text_snapshot": "accepted prefix", "preserve_completed_tools": True,
    }
    projected = project_session_event_for_client(
        "session.event.answer_generation_reset", reset,
        client_caps=caps,
    )
    assert projected is not None
    assert projected[1]["reset_scope"] == "answer"
    assert "rollback_model_call_id" not in projected[1]
    assert projected[1]["new_generation_epoch"] == 1
    assert projected[1]["authoritative_text_snapshot"] == "accepted prefix"
    assert projected[1]["preserve_completed_tools"]
    assert reset["reset_scope"] == "provider_call"


@pytest.mark.parametrize("kind", ["done", "error", "tool_result", "run_heartbeat"])
def test_capabilityless_projection_preserves_generation_fencing(kind: str) -> None:
    payload = {**_IDENTITY, "generation_epoch": 1, "text_snapshot": "accepted prefix"}
    projected = project_session_event_for_client(f"session.event.{kind}", payload)
    assert projected is not None
    assert projected[1]["generation_epoch"] == 1
    assert payload["generation_epoch"] == 1
    assert project_session_event_for_client(
        f"session.event.{kind}", payload, client_caps=(PROVIDER_CALL_RESET_CAPABILITY,),
    ) == (f"session.event.{kind}", payload)
