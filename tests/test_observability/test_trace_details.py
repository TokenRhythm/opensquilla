import pytest

from opensquilla.observability.trace_details import build_trace_details


def test_trace_details_pairs_model_and_tool_records() -> None:
    records = [
        {
            "seq": 1,
            "ts": "2026-01-01T00:00:00Z",
            "kind": "llm_request",
            "provider": "p",
            "model": "m",
            "payload": {
                "call_id": "call-1",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [],
                "config": {},
            },
        },
        {
            "seq": 2,
            "ts": "2026-01-01T00:00:01Z",
            "kind": "llm_response",
            "provider": "p",
            "model": "m",
            "payload": {
                "call_id": "call-1",
                "text": "I will inspect",
                "tool_calls": [],
                "usage": {"output_tokens": 3},
                "duration_ms": 100,
            },
        },
        {
            "seq": 3,
            "ts": "2026-01-01T00:00:02Z",
            "kind": "tool_request",
            "payload": {
                "tool_use_id": "tool-1",
                "name": "inspect",
                "arguments": {"path": "README.md"},
            },
        },
        {
            "seq": 4,
            "ts": "2026-01-01T00:00:03Z",
            "kind": "tool_response",
            "payload": {
                "tool_use_id": "tool-1",
                "name": "inspect",
                "result": "ok",
                "is_error": False,
                "duration_ms": 20,
            },
        },
    ]

    projection = build_trace_details(records, trace_id="trace-1")

    assert projection["available"] is True
    assert projection["total"] == 2
    assert projection["rows"][0]["output"]["text"] == "I will inspect"
    assert projection["rows"][0]["input"]["messages"][0]["content"] == "hello"
    assert projection["rows"][0]["started_ts"] == "2026-01-01T00:00:00Z"
    assert projection["rows"][1]["input"]["arguments"]["path"] == "README.md"
    assert projection["rows"][1]["output"]["result"] == "ok"
    assert projection["rows"][1]["started_ts"] == "2026-01-01T00:00:02Z"


def test_trace_details_preserves_monotonic_pair_timing() -> None:
    records = [
        {
            "seq": 1,
            "ts": "2026-01-01T00:00:00Z",
            "elapsed_ms": 120,
            "kind": "llm_request",
            "payload": {"call_id": "call-1", "messages": [], "tools": [], "config": {}},
        },
        {
            "seq": 2,
            "ts": "2026-01-01T00:00:01Z",
            "elapsed_ms": 2682,
            "kind": "llm_response",
            "payload": {"call_id": "call-1", "duration_ms": 2562},
        },
    ]

    rows = build_trace_details(records, trace_id="trace-1")["rows"]

    assert rows[0]["elapsed_ms"] == 2682
    assert rows[0]["started_elapsed_ms"] == 120
    assert rows[0]["ended_elapsed_ms"] == 2682


def test_trace_details_supports_cursor_and_limit() -> None:
    records = [
        {"seq": index, "kind": "context_stage", "payload": {"stage": str(index), "messages": []}}
        for index in range(1, 4)
    ]

    projection = build_trace_details(records, trace_id="trace-1", after_seq=1, limit=1)

    assert projection["count"] == 1
    assert projection["rows"][0]["seq"] == 2
    assert projection["has_more"] is True


def test_trace_details_preserves_legacy_record_order_and_clock_origin() -> None:
    records = [
        {"seq": 1, "kind": "prompt_report", "payload": {"system_chars": 10}},
        {"seq": 2, "kind": "turn_start", "payload": {"message_chars": 4}},
        {"seq": 3, "kind": "context_stage", "payload": {"stage": "session:loaded"}},
    ]

    detail = build_trace_details(records, trace_id="trace-order")
    rows = detail["rows"]

    assert [row["kind"] for row in rows] == ["prompt_report", "turn_start", "context_stage"]
    assert detail["clock_origin"] == "logger_start"
    assert rows[0]["phase"] == "context"
    assert rows[1]["phase"] == "intake"
    assert rows[1]["status"] == "success"
    assert rows[2]["status"] == "success"


def test_trace_details_keeps_input_preparation_gap_and_cursor_in_record_order() -> None:
    records = [
        {"seq": 1, "kind": "turn_start", "elapsed_ms": 0,
         "clock_origin": "turn_runner_start", "payload": {"message": "hello"}},
        {"seq": 2, "kind": "prompt_report", "elapsed_ms": 240,
         "clock_origin": "turn_runner_start", "payload": {"system_chars": 10}},
        {"seq": 3, "kind": "context_stage", "elapsed_ms": 250,
         "clock_origin": "turn_runner_start", "payload": {"stage": "session:loaded"}},
    ]
    detail = build_trace_details(reversed(records), trace_id="trace-order")
    assert detail["clock_origin"] == "turn_runner_start"
    assert [row["kind"] for row in detail["rows"]] == [
        "turn_start", "prompt_report", "context_stage",
    ]
    assert [row["elapsed_ms"] for row in detail["rows"]] == [0, 240, 250]
    assert all(row.get("duration_ms") is None for row in detail["rows"])
    first = build_trace_details(records, trace_id="trace-order", limit=1)
    second = build_trace_details(
        records, trace_id="trace-order", after_seq=first["rows"][-1]["seq"], limit=1,
    )
    assert [first["rows"][0]["seq"], second["rows"][0]["seq"]] == [1, 2]


def test_trace_details_updates_one_stable_model_row_during_generation() -> None:
    records = [
        {"seq": 1, "kind": "llm_request", "elapsed_ms": 100,
         "payload": {"call_id": "model-a", "messages": [{"role": "user", "content": "hello"}]}},
        {"seq": 2, "kind": "llm_progress", "elapsed_ms": 300,
         "payload": {"call_id": "model-a", "text": "first", "partial": True}},
        {"seq": 3, "kind": "llm_progress", "elapsed_ms": 1400,
         "payload": {"call_id": "model-a", "text": "first second", "partial": True}},
        {"seq": 4, "kind": "llm_response", "elapsed_ms": 1700,
         "payload": {"call_id": "model-a", "text": "first second final", "duration_ms": 1600}},
    ]
    request = build_trace_details(records[:1], trace_id="trace-live")["rows"][0]
    partial = build_trace_details(records[:3], trace_id="trace-live")["rows"]
    final = build_trace_details(records, trace_id="trace-live")["rows"]
    assert len(partial) == len(final) == 1
    assert request["id"] == partial[0]["id"] == final[0]["id"] == "step:model-a"
    assert request["order_seq"] == partial[0]["order_seq"] == final[0]["order_seq"] == 1
    assert partial[0]["seq"] == 3
    assert partial[0]["input_seq"] == 1
    assert partial[0]["started_elapsed_ms"] == 100
    assert partial[0]["elapsed_ms"] == 1400
    assert "ended_elapsed_ms" not in partial[0]
    assert "duration_ms" not in partial[0]
    assert partial[0]["output"]["text"] == "first second"
    assert partial[0]["status"] == "running"
    assert final[0]["status"] == "success"
    assert final[0]["output"]["text"] == "first second final"
    assert final[0]["ended_elapsed_ms"] == 1700
    updated = build_trace_details(records, trace_id="trace-live", after_seq=3)
    assert [row["id"] for row in updated["rows"]] == [request["id"]]


def test_trace_details_retains_parallel_tool_positions_when_results_arrive_in_reverse() -> None:
    records = [
        {"seq": 1, "kind": "tool_request", "payload": {"tool_use_id": "a", "arguments": {}}},
        {"seq": 2, "kind": "tool_request", "payload": {"tool_use_id": "b", "arguments": {}}},
        {"seq": 3, "kind": "tool_response", "payload": {"tool_use_id": "b", "result": "B"}},
        {"seq": 4, "kind": "tool_response", "payload": {"tool_use_id": "a", "result": "A"}},
    ]
    for prefix in (records[:2], records[:3], records):
        rows = build_trace_details(prefix, trace_id="trace-tools")["rows"]
        display = sorted(rows, key=lambda row: row["order_seq"])
        assert [row["id"] for row in display] == ["tool:a", "tool:b"]
        assert [row["order_seq"] for row in display] == [1, 2]
    assert [row["seq"] for row in rows] == [3, 4]


def test_trace_details_marks_bounded_progress_as_partial() -> None:
    records = [
        {"seq": 1, "kind": "llm_progress", "payload": {
            "call_id": "model-a", "partial": True, "text": "tail", "text_chars": 40000,
            "text_truncated": True, "text_offset": 39996,
        }},
    ]
    [row] = build_trace_details(records, trace_id="trace-live")["rows"]
    assert row["output_truncated"] is True
    assert row["output_chars"] == 40000
    assert row["output"]["text_offset"] == 39996


@pytest.mark.parametrize(
    ("kind", "phase"),
    [
        ("routing_decision", "routing"),
        ("route_plan", "routing"),
        ("provider_retry", "routing"),
        ("provider_thinking_fallback", "routing"),
        ("provider_generation_reset", "routing"),
        ("tool_approval_resolved", "approval_sandbox"),
        ("context_compaction_completed", "compaction_maintenance"),
        ("subagent_tool_completed", "subagent"),
        ("extension_checkpoint", "unknown"),
    ],
)
def test_trace_details_keeps_runtime_boundaries_and_unknown_events(kind: str, phase: str) -> None:
    record = {
        "seq": 7,
        "kind": kind,
        "elapsed_ms": 4200,
        "ts": "2026-01-01T00:00:04.200Z",
        "payload": {"status": "completed", "reason_code": "test_boundary", "extra": [1, 2]},
    }

    [row] = build_trace_details([record], trace_id="trace-boundary")["rows"]

    assert row["phase"] == phase
    assert row["status"] == "success"
    assert row["elapsed_ms"] == 4200
    assert row["ts"] == record["ts"]
    assert row["output"] == record["payload"]
    assert "duration_ms" not in row
    assert row["id"] == f"detail:7:{kind}"


def test_trace_details_routing_payload_is_redacted_and_preserves_measured_milliseconds() -> None:
    records = [
        {"seq": 1, "kind": "turn_start", "payload": {}},
        {"seq": 2, "kind": "routing_decision", "elapsed_ms": 1200,
         "provider": "initial-provider", "model": "initial-model", "payload": {
             "requested_mode": "smart", "effective_mode": "direct",
             "model": "selected-model", "provider": "selected-provider",
             "duration_ms": 12.5, "api_key": "synthetic-key-for-redaction",
             "reason": {"authorization": "synthetic-header", "reason_code": "test_choice"},
         }},
        {"seq": 3, "kind": "provider_retry", "elapsed_ms": 1600,
         "payload": {"call_id": "model-a", "attempt": 2}},
    ]

    [decision, retry] = build_trace_details(
        records, trace_id="trace-route", after_seq=1,
    )["rows"]

    assert [decision["seq"], retry["seq"]] == [2, 3]
    assert decision["duration_ms"] == 12.5
    assert decision["status"] == retry["status"] == "success"
    assert decision["model"] == "selected-model"
    assert decision["provider"] == "selected-provider"
    assert decision["attrs"]["requested_mode"] == "smart"
    assert decision["output"]["api_key"] == "[redacted]"
    assert decision["output"]["reason"]["authorization"] == "[redacted]"
    assert retry["call_id"] == "model-a"
    assert retry["attempt"] == 2
    assert "duration_ms" not in retry


def test_trace_details_retains_waiting_boundary_state_and_does_not_invent_duration() -> None:
    records = [
        {"seq": 1, "kind": "approval_request", "elapsed_ms": 300,
         "payload": {"status": "waiting", "duration_ms": -1}},
        {"seq": 2, "kind": "approval_resolved", "elapsed_ms": 2500,
         "payload": {"status": "denied", "duration_ms": 2200}},
    ]

    waiting, resolved = build_trace_details(records, trace_id="trace-approval")["rows"]

    assert waiting["status"] == "queued"
    assert "duration_ms" not in waiting
    assert resolved["status"] == "error"
    assert resolved["duration_ms"] == 2200


@pytest.mark.parametrize("duration_ms", [None, 2200])
def test_trace_details_replaces_approval_wait_with_its_resolution(duration_ms: int | None) -> None:
    wait = {
        "seq": 2, "kind": "approval_wait", "elapsed_ms": 300,
        "ts": "2026-01-01T00:00:00.300Z",
        "payload": {"approval_id": "approval-a", "tool_use_id": "tool-a", "status": "running"},
    }
    resolved = {
        "seq": 4, "kind": "approval_resolved", "elapsed_ms": 2500,
        "ts": "2026-01-01T00:00:02.500Z",
        "payload": {"approval_id": "approval-a", "tool_use_id": "tool-a", "status": "success"},
    }
    if duration_ms is not None:
        resolved["payload"]["duration_ms"] = duration_ms

    [live] = build_trace_details([wait], trace_id="trace-approval")["rows"]
    [done] = build_trace_details(
        [wait, resolved], trace_id="trace-approval", after_seq=2,
    )["rows"]

    assert live["id"] == done["id"] == "approval:approval-a"
    assert live["status"] == "running"
    assert done["status"] == "success"
    assert done["seq"] == 4
    assert done["order_seq"] == done["input_seq"] == 2
    assert done["started_elapsed_ms"] == 300
    assert done["ended_elapsed_ms"] == 2500
    assert done["started_ts"] == wait["ts"]
    assert done["input"] == wait["payload"]
    assert done["output"] == resolved["payload"]
    assert "duration_ms" not in live
    if duration_ms is None:
        assert "duration_ms" not in done
    else:
        assert done["duration_ms"] == duration_ms


def test_trace_details_does_not_pair_unrelated_or_unidentified_approvals() -> None:
    records = [
        {"seq": 1, "kind": "approval_wait", "payload": {"status": "running"}},
        {"seq": 2, "kind": "approval_resolved", "payload": {"status": "success"}},
        {"seq": 3, "kind": "approval_wait",
         "payload": {"approval_id": "approval-a", "status": "running"}},
        {"seq": 4, "kind": "approval_resolved",
         "payload": {"approval_id": "approval-b", "status": "error"}},
    ]

    rows = build_trace_details(records, trace_id="trace-approval")["rows"]

    assert len(rows) == 4
    assert len({row["id"] for row in rows}) == 4
    assert "input_seq" not in rows[1]
    assert "input_seq" not in rows[3]
    assert all("duration_ms" not in row for row in rows)


def test_trace_details_ensemble_activity_remains_checkpoints_after_completion() -> None:
    records = [
        {"seq": 1, "kind": "ensemble_progress",
         "payload": {"event_type": "candidate_start", "status": "running"}},
        {"seq": 2, "kind": "ensemble_progress",
         "payload": {"event_type": "candidate_progress", "status": "running"}},
        {"seq": 3, "kind": "ensemble_progress",
         "payload": {"event_type": "candidate_done", "status": "success"}},
        {"seq": 4, "kind": "ensemble_progress",
         "payload": {"event_type": "candidate_error", "status": "error", "error": "test"}},
    ]

    rows = build_trace_details(records, trace_id="trace-ensemble")["rows"]

    assert [row["status"] for row in rows] == ["success", "success", "success", "error"]
    assert rows[0]["output"]["status"] == "running"
    assert all(row["phase"] == "routing" for row in rows)
    assert all("duration_ms" not in row for row in rows)


@pytest.mark.parametrize("kind", [
    "agent_runtime_budget", "image_input_preflight", "tool_projection_noop",
    "tool_projection_applied",
])
def test_trace_details_runtime_preparation_is_a_context_checkpoint(kind: str) -> None:
    [row] = build_trace_details(
        [{"seq": 1, "kind": kind, "payload": {"status": "running"}}], trace_id="trace-context",
    )["rows"]

    assert row["phase"] == "context"
    assert row["status"] == "success"
    assert "duration_ms" not in row
