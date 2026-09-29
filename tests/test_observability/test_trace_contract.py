from __future__ import annotations

import pytest

from opensquilla.observability.trace import (
    JsonlTraceSink,
    MemoryTraceSink,
    PrivacyGuardSink,
    TraceContext,
    TraceEvent,
    load_trace_events,
    write_trace_event,
)
from opensquilla.observability.trace_projection import build_trace_projection


def test_trace_context_child_inherits_parent_identity() -> None:
    parent = TraceContext.new(
        trace_id="trace-1",
        session_key="agent:main:test",
        session_id="session-1",
        turn_id="turn-1",
        task_id="task-1",
        agent_id="main",
    )

    child = parent.child(run_id="child-run", agent_id="child")

    assert child.trace_id == "trace-1"
    assert child.session_key == "agent:main:test"
    assert child.session_id == "session-1"
    assert child.turn_id == "turn-1"
    assert child.task_id == "task-1"
    assert child.parent_run_id == "task-1"
    assert child.run_id == "child-run"
    assert child.agent_id == "child"


def test_trace_event_serializes_required_contract_fields() -> None:
    context = TraceContext.new(
        trace_id="trace-1",
        session_key="agent:main:test",
        turn_id="turn-1",
        agent_id="main",
    )
    event = TraceEvent(
        kind="turn_start",
        context=context,
        privacy="diagnostic",
        seq=7,
        attrs={"source": "test"},
        payload={"message_hash": "abc123"},
    )

    payload = event.to_dict()

    assert payload["schema_version"] == 1
    assert payload["kind"] == "turn_start"
    assert payload["privacy"] == "diagnostic"
    assert payload["trace_id"] == "trace-1"
    assert payload["session_key"] == "agent:main:test"
    assert payload["turn_id"] == "turn-1"
    assert payload["agent_id"] == "main"
    assert payload["seq"] == 7
    assert payload["attrs"] == {"source": "test"}
    assert payload["payload"] == {"message_hash": "abc123"}


def test_trace_projection_groups_phases_without_exposing_payload() -> None:
    context = TraceContext.new(trace_id="trace-projection", turn_id="turn-1", run_id="run-1")
    events = [
        TraceEvent(kind="turn_start", context=context, seq=1),
        TraceEvent(
            kind="route.resolved",
            context=context,
            seq=2,
            attrs={"requested_mode": "router", "effective_mode": "ensemble", "model": "m"},
        ),
        TraceEvent(
            kind="tool_call",
            context=context,
            seq=3,
            attrs={"tool_name": "search", "status": "completed"},
            payload={"secret_prompt": "must not be projected"},
        ),
        TraceEvent(kind="turn_end", context=context, seq=4),
    ]

    projection = build_trace_projection(events)

    assert projection["trace_id"] == "trace-projection"
    assert projection["status"] == "success"
    assert projection["complete"] is True
    assert projection["requested_mode"] == "router"
    assert projection["effective_mode"] == "ensemble"
    assert [phase["phase"] for phase in projection["phases"]] == [
        "intake",
        "routing",
        "tool_execution",
        "finalize",
    ]
    tool_row = projection["spans"][2]
    assert tool_row["tool_name"] == "search"
    assert tool_row["payload_keys"] == ["secret_prompt"]
    assert "secret_prompt" not in tool_row


def test_trace_projection_after_seq_returns_only_new_rows() -> None:
    context = TraceContext.new(trace_id="trace-cursor")
    events = [
        TraceEvent(kind="turn_start", context=context, seq=1),
        TraceEvent(kind="model_start", context=context, seq=2),
        TraceEvent(kind="turn_end", context=context, seq=3),
    ]

    projection = build_trace_projection(events, after_seq=1, limit=1)

    assert projection["current_seq"] == 3
    assert [row["seq"] for row in projection["spans"]] == [2]
    assert projection["total"] == 3
    assert projection["has_more"] is True


def test_trace_projection_keeps_elapsed_position_separate_from_duration() -> None:
    context = TraceContext.new(trace_id="trace-timing-units")
    events = [
        TraceEvent(
            kind="context_stage",
            context=context,
            seq=1,
            attrs={"elapsed_ms": 1250},
        ),
        TraceEvent(
            kind="tool_response",
            context=context,
            seq=2,
            attrs={"elapsed_ms": 4300, "duration_ms": 87},
        ),
    ]

    rows = build_trace_projection(events)["spans"]

    # A turn-relative elapsed position is useful for ordering but must not be
    # rendered as a 1.25-second interval. Only duration_ms supplies width.
    assert rows[0]["elapsed_ms"] == 1250
    assert rows[0]["duration_ms"] is None
    assert rows[1]["elapsed_ms"] == 4300
    assert rows[1]["duration_ms"] == 87


@pytest.mark.parametrize(
    ("kind", "phase"),
    [
        ("routing_decision", "routing"),
        ("provider_retry", "routing"),
        ("provider_thinking_fallback", "routing"),
        ("provider_generation_reset", "routing"),
        ("tool_approval_resolved", "approval_sandbox"),
        ("context_compaction_completed", "compaction_maintenance"),
        ("subagent_tool_completed", "subagent"),
    ],
)
def test_trace_projection_classifies_boundaries_before_model_and_tool_names(
    kind: str, phase: str,
) -> None:
    event = TraceEvent(kind=kind, context=TraceContext.new(trace_id="trace-boundary"), seq=1)

    [row] = build_trace_projection([event])["spans"]

    assert row["phase"] == phase
    assert row["status"] == "success"
    assert row["duration_ms"] is None


def test_privacy_guard_blocks_raw_events_by_default() -> None:
    sink = MemoryTraceSink()
    guarded = PrivacyGuardSink(sink)
    context = TraceContext.new(trace_id="trace-1", session_key="agent:main:test")

    guarded.write(TraceEvent(kind="turn_start", context=context, privacy="diagnostic"))

    assert len(sink.events) == 1
    with pytest.raises(ValueError, match="raw trace event"):
        guarded.write(
            TraceEvent(
                kind="llm_request",
                context=context,
                privacy="raw",
                payload={"messages": [{"role": "user", "content": "secret"}]},
            )
        )


def test_memory_trace_sink_filters_by_trace_id() -> None:
    sink = MemoryTraceSink()
    sink.write(TraceEvent(kind="turn_start", context=TraceContext.new(trace_id="trace-a")))
    sink.write(TraceEvent(kind="turn_start", context=TraceContext.new(trace_id="trace-b")))

    assert [event.trace_id for event in sink.by_trace_id("trace-b")] == ["trace-b"]


def test_jsonl_trace_sink_persists_and_loads_by_trace_id(tmp_path) -> None:
    sink = JsonlTraceSink(log_dir=tmp_path)
    sink.write(
        TraceEvent(
            kind="turn_start",
            context=TraceContext.new(
                trace_id="trace-a",
                session_key="agent:main:test",
                turn_id="turn-1",
            ),
            seq=1,
        )
    )
    write_trace_event(
        TraceEvent(
            kind="turn_start",
            context=TraceContext.new(trace_id="trace-b", turn_id="turn-2"),
            seq=1,
        ),
        log_dir=tmp_path,
    )

    [path] = list(tmp_path.glob("traces-*.jsonl"))
    assert path.exists()
    events = load_trace_events("trace-a", log_dir=tmp_path)

    assert [event.kind for event in events] == ["turn_start"]
    assert events[0].trace_id == "trace-a"
    assert events[0].context.session_key == "agent:main:test"
    assert events[0].context.turn_id == "turn-1"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"trace_id": ""}, "trace_id must be non-empty"),
        ({"trace_id": "trace-1", "session_key": " "}, "session_key must be non-empty"),
    ],
)
def test_trace_context_rejects_invalid_identity(kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        TraceContext.new(**kwargs)


def test_trace_context_child_rejects_blank_parent_override() -> None:
    parent = TraceContext.new(trace_id="trace-1", turn_id="turn-1")

    with pytest.raises(ValueError, match="parent_run_id must be non-empty"):
        parent.child(parent_run_id="")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"kind": ""}, "kind must be non-empty"),
        ({"schema_version": 99}, "unsupported trace schema_version"),
        ({"privacy": "unsafe"}, "invalid trace privacy"),
    ],
)
def test_trace_event_rejects_invalid_contract(kwargs: dict[str, object], message: str) -> None:
    context = TraceContext.new(trace_id="trace-1")
    base = {"kind": "turn_start", "context": context}
    base.update(kwargs)

    with pytest.raises(ValueError, match=message):
        TraceEvent(**base)  # type: ignore[arg-type]
