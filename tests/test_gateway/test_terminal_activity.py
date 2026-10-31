import json

import pytest

from opensquilla.gateway.terminal_activity import (
    build_terminal_activity_snapshot,
    terminal_activity_snapshot,
)


def event(name: str, order: int, **payload: object) -> dict[str, object]:
    return {
        "event_name": f"session.event.{name}",
        "stream_seq": order,
        "payload": {
            "stream_seq": order,
            "emitted_at": 10_000 + order,
            **payload,
        },
    }


def test_v2_activity_snapshot_preserves_interleaving_and_sanitizes_payloads() -> None:
    events = [
        event("provider_activity", 4, phase="requesting", started_at=4_000),
        event("provider_activity", 5, phase="reasoning", started_at=5_000),
        event("thinking_start", 6, block_id="reasoning-1", block_index=0),
        event("thinking", 7, block_id="reasoning-1", text="A😀"),
        event("thinking_end", 8, block_id="reasoning-1", status="completed"),
        event("text_delta", 31, text="First narration"),
        event(
            "tool_use_start",
            41,
            tool_use_id="tool-1",
            tool_name="skill_view",
            arguments={"path": "C:/private/secret.txt"},
        ),
        event(
            "tool_result",
            47,
            tool_use_id="tool-1",
            tool_name="skill_view",
            result="TOP SECRET RESULT",
            is_error=False,
        ),
        event("provider_activity", 50, phase="requesting", started_at=50_000),
        event("provider_activity", 51, phase="reasoning", started_at=51_000),
        event("thinking", 52, block_id="reasoning-2", block_index=1, text="second"),
        event("thinking_end", 53, block_id="reasoning-2", status="completed"),
        event("text_delta", 140, text="Second narration"),
        event(
            "tool_use_start",
            154,
            tool_use_id="tool-2",
            tool_name="write_file",
            input={"file_path": "C:/private/output.html"},
        ),
        event(
            "tool_result",
            3741,
            tool_use_id="tool-2",
            tool_name="write_file",
            result="private file body",
            is_error=False,
        ),
        event("provider_activity", 3744, phase="requesting"),
        event("provider_activity", 3746, phase="reasoning"),
        event("thinking", 3747, block_id="reasoning-3", block_index=2, text="third"),
        event("thinking_end", 3766, block_id="reasoning-3", status="completed"),
        event("text_delta", 3767, text="马上发布"),
        event(
            "tool_use_start",
            3768,
            tool_use_id="tool-3",
            tool_name="publish_artifact",
            arguments={"artifact": "private"},
        ),
        event(
            "tool_result",
            3803,
            tool_use_id="tool-3",
            tool_name="publish_artifact",
            result="private publication receipt",
            is_error=False,
        ),
        event("text_delta", 3805, text="Final answer"),
        event("done", 3806, reason="completed"),
    ]

    snapshot = build_terminal_activity_snapshot(
        events,
        task_id="task-1",
        turn_id="turn-1",
    )

    assert snapshot is not None
    assert snapshot["version"] == 2
    assert snapshot["complete"] is True
    entries = snapshot["entries"]
    assert [entry["order"] for entry in entries] == sorted(
        entry["order"] for entry in entries
    )
    assert [
        (entry["type"], entry.get("phase"), entry["order"])
        for entry in entries
        if entry["type"] in {"phase", "reasoning", "segment"}
    ] == [
        ("phase", "requesting", 4),
        ("phase", "reasoning", 5),
        ("reasoning", None, 6),
        ("phase", "writing", 31),
        ("segment", None, 31),
        ("segment", None, 41),
        ("phase", "requesting", 50),
        ("phase", "reasoning", 51),
        ("reasoning", None, 52),
        ("phase", "writing", 140),
        ("segment", None, 140),
        ("segment", None, 154),
        ("phase", "requesting", 3744),
        ("phase", "reasoning", 3746),
        ("reasoning", None, 3747),
        ("phase", "writing", 3767),
        ("segment", None, 3767),
        ("segment", None, 3768),
        ("phase", "writing", 3805),
        ("segment", None, 3805),
    ]
    reasoning = [entry for entry in entries if entry["type"] == "reasoning"]
    assert reasoning[0]["text_start_utf16"] == 0
    assert reasoning[0]["text_end_utf16"] == 3
    assert [
        (entry["text_start_utf16"], entry["text_end_utf16"])
        for entry in reasoning
    ] == [(0, 3), (4, 10), (11, 16)]
    assert snapshot["reasoning_utf16_length"] == 16
    tool = next(entry for entry in entries if entry["id"] == "tool:tool-1")
    assert tool["order"] == 41
    assert tool["ended_at"] == 10_047

    serialized = json.dumps(snapshot)
    for forbidden in (
        "C:/private",
        "TOP SECRET",
        "private file body",
        "private publication receipt",
        "First narration",
        "马上发布",
        "A😀",
        "arguments",
    ):
        assert forbidden not in serialized


def test_v2_validator_rejects_extra_fields_even_without_a_checksum() -> None:
    snapshot = build_terminal_activity_snapshot(
        [event("provider_activity", 1, phase="requesting")],
        task_id="task-1",
        turn_id="turn-1",
    )
    assert snapshot is not None
    snapshot.pop("checksum", None)
    snapshot["entries"][0]["raw_label"] = "C:/private/secret.txt"

    assert terminal_activity_snapshot(
        snapshot,
        task_id="task-1",
        turn_id="turn-1",
    ) is None


def test_v2_order_ignores_timestamp_inversion() -> None:
    snapshot = build_terminal_activity_snapshot(
        [
            event("provider_activity", 2, phase="requesting", started_at=9_000),
            event("provider_activity", 3, phase="fallback", started_at=1_000),
        ],
        task_id="task-1",
        turn_id="turn-1",
    )
    assert snapshot is not None
    assert [(entry["phase"], entry["order"]) for entry in snapshot["entries"]] == [
        ("requesting", 2),
        ("fallback", 3),
    ]
    assert snapshot["entries"][0]["ended_at"] == 9_000


def test_v2_resolved_interrupts_keep_request_order_without_private_context() -> None:
    approval_request = {
        "event_name": "exec.approval.requested",
        "stream_seq": 10,
        "payload": {
            "stream_seq": 10,
            "emitted_at": 20_010,
            "approval_id": "approval-1",
            "namespace": "exec",
            "tool_name": "",
            "approval_kind": "sandbox_path",
            "command": "type C:/private/secret.txt",
            "display_target": "C:/private/secret.txt",
        },
    }
    approval_result = {
        "event_name": "exec.approval.resolved",
        "stream_seq": 30,
        "payload": {
            "stream_seq": 30,
            "emitted_at": 20_030,
            "approval_id": "approval-1",
            "approved": True,
            "resolution": "approved",
        },
    }
    snapshot = build_terminal_activity_snapshot(
        [
            approval_request,
            event(
                "tool_use_start",
                15,
                tool_use_id="clarify-tool",
                tool_name="clarify",
            ),
            event(
                "tool_result",
                20,
                tool_use_id="clarify-tool",
                tool_name="clarify",
                result=json.dumps({
                    "kind": "user_input",
                    "paused": True,
                    "request_id": "clarify-1",
                }),
            ),
            event(
                "tool_result",
                25,
                tool_use_id="clarify-tool",
                tool_name="clarify",
                result=json.dumps({
                    "kind": "user_input",
                    "paused": False,
                    "status": "answered",
                    "request_id": "clarify-1",
                }),
            ),
            approval_result,
            event("done", 40),
        ],
        task_id="task-1",
        turn_id="turn-1",
    )

    assert snapshot is not None
    assert snapshot["complete"] is True
    assert terminal_activity_snapshot(
        snapshot,
        task_id="task-1",
        turn_id="turn-1",
    ) == snapshot
    interrupts = [
        entry for entry in snapshot["entries"] if entry["type"] == "interrupt"
    ]
    assert [(entry["interrupt_type"], entry["order"]) for entry in interrupts] == [
        ("approval", 10),
        ("clarify", 20),
    ]
    serialized = json.dumps(snapshot)
    assert "C:/private" not in serialized
    assert "command" not in serialized
    assert "display_target" not in serialized


def test_v2_generation_reset_without_referenceable_text_fails_closed() -> None:
    snapshot = build_terminal_activity_snapshot(
        [
            event(
                "answer_generation_reset",
                2,
                authoritative_text_snapshot="cannot safely reference this",
            ),
            event("done", 3),
        ],
        task_id="task-1",
        turn_id="turn-1",
    )
    assert snapshot is None


def test_v2_compaction_updates_in_place_and_normalizes_wire_states() -> None:
    snapshot = build_terminal_activity_snapshot(
        [
            event(
                "compaction",
                4,
                compaction_id="compact-1",
                status="started",
                source="automatic",
            ),
            event(
                "compaction",
                8,
                compaction_id="compact-1",
                status="observed",
            ),
            event(
                "compaction",
                12,
                compaction_id="compact-1",
                status="emergency_ephemeral",
            ),
            event("done", 20),
        ],
        task_id="task-1",
        turn_id="turn-1",
    )
    assert snapshot is not None
    maintenance = [
        entry for entry in snapshot["entries"] if entry["type"] == "maintenance"
    ]
    assert maintenance == [{
        "type": "maintenance",
        "id": "compact-1",
        "order": 4,
        "maintenance_type": "context_compaction",
        "state": "completed",
        "at": 10_004,
        "ended_at": 10_012,
        "source": "automatic",
    }]


def test_wait_status_is_scoped_to_retry_wait_and_survives_history():
    events = [
        event("provider_activity", 1, phase="retry_wait", reason="provider_overloaded",
              http_status=503, retry_after_ms=35000),
        event("provider_activity", 2, phase="retrying",
              reason="provider_overloaded", http_status=503),
        event("provider_activity", 3, phase="requesting", http_status=429),
        event("done", 4),
    ]
    snapshot = build_terminal_activity_snapshot(events, task_id="t", turn_id="t", terminal_at=20000)
    assert snapshot is not None
    wait, retrying, requesting = snapshot["entries"]
    assert wait["http_status"] == 503
    assert wait["reason"] == "provider_overloaded"
    assert "http_status" not in retrying
    assert "http_status" not in requesting
    assert terminal_activity_snapshot(snapshot, task_id="t", turn_id="t") == snapshot


def test_fallback_models_survive_history_as_each_physical_hop():
    snapshot = build_terminal_activity_snapshot([
        event("provider_activity", 1, phase="fallback", from_model="model/A", model="model/B"),
        event("provider_activity", 2, phase="fallback", from_model="model/B", model="model/C"),
        event("provider_activity", 3, phase="fallback", model="本地 模型"),
        event("provider_activity", 4, phase="fallback"),  # Older events have no model facts.
        event("provider_activity", 5, phase="requesting", model="model/C", from_model="model/B"),
        event("done", 6),
    ], task_id="t", turn_id="t", terminal_at=20000)
    assert snapshot is not None
    entries = snapshot["entries"]
    assert [(entry.get("from_model"), entry.get("model")) for entry in entries] == [
        ("model/A", "model/B"), ("model/B", "model/C"),
        (None, "本地 模型"), (None, None), (None, "model/C"),
    ]
    assert terminal_activity_snapshot(snapshot, task_id="t", turn_id="t") == snapshot


@pytest.mark.parametrize("model", ["", "x" * 257, "model\nerror", "model\u202eerror", 42])
def test_fallback_snapshot_omits_invalid_live_models_and_rejects_invalid_stored_models(model):
    snapshot = build_terminal_activity_snapshot([
        event("provider_activity", 1, phase="fallback", from_model=model, model=model),
        event("done", 2),
    ], task_id="t", turn_id="t", terminal_at=20000)
    assert snapshot is not None
    entry = snapshot["entries"][0]
    assert "from_model" not in entry and "model" not in entry
    assert terminal_activity_snapshot(snapshot, task_id="t", turn_id="t") == snapshot
    snapshot.pop("checksum")
    entry["from_model"] = model
    assert terminal_activity_snapshot(snapshot, task_id="t", turn_id="t") is None



@pytest.mark.parametrize("status", ["failed", "skipped"])
def test_compaction_failure_detail_survives_history_without_stale_wait(status):
    events = [
        event("compaction", 1, compaction_id="compact-1", status="observed",
              recovery={"phase": "retry_wait", "reason": "rate_limited", "http_status": 429,
                        "retry_after_ms": 35000, "raw_body": "SECRET"}),
        event("compaction", 2, compaction_id="compact-1", status=status,
              skip_reason="summary_failed",
              failure_detail={"failure_kind": "rate_limited", "http_status": 429,
                              "reason_code": "PRIVATE"}),
        event("done", 3),
    ]
    snapshot = build_terminal_activity_snapshot(events, task_id="t", turn_id="t", terminal_at=20000)
    assert snapshot is not None
    maintenance, = snapshot["entries"]
    assert maintenance["failure_detail"] == {"failure_kind": "rate_limited", "http_status": 429}
    assert maintenance["state"] == status
    assert maintenance["reason"] == "summary_failed"
    assert "recovery" not in maintenance
    assert "SECRET" not in repr(snapshot) and "PRIVATE" not in repr(snapshot)
    assert terminal_activity_snapshot(snapshot, task_id="t", turn_id="t") == snapshot


@pytest.mark.parametrize("reason", ["within_budget", "within_compaction_budget"])
def test_benign_compaction_skip_never_inherits_failure(reason):
    snapshot = build_terminal_activity_snapshot([
        event("compaction", 1, compaction_id="compact-benign", status="failed",
              failure_detail={"failure_kind": "rate_limited", "http_status": 429}),
        event("compaction", 2, compaction_id="compact-benign", status="skipped", reason=reason,
              failure_detail={"failure_kind": "rate_limited", "http_status": 429}),
        event("done", 3),
    ], task_id="t", turn_id="t", terminal_at=20000)
    assert snapshot is not None
    maintenance, = snapshot["entries"]
    assert maintenance["state"] == "skipped"
    assert "failure_detail" not in maintenance
    # An older or externally supplied snapshot is sanitized at read time too.
    maintenance["failure_detail"] = {"failure_kind": "rate_limited", "http_status": 429}
    restored = terminal_activity_snapshot(snapshot, task_id="t", turn_id="t")
    assert restored is not None
    assert "failure_detail" not in restored["entries"][0]


def test_compaction_success_clears_failed_attempt_status():
    snapshot = build_terminal_activity_snapshot([
        event("compaction", 1, compaction_id="compact-2", status="observed",
              recovery={"phase": "retry_wait", "reason": "rate_limited", "http_status": 429}),
        event("compaction", 2, compaction_id="compact-2", status="completed"),
        event("done", 3),
    ], task_id="t", turn_id="t", terminal_at=20000)
    assert snapshot is not None
    maintenance, = snapshot["entries"]
    assert "recovery" not in maintenance
    assert "failure_detail" not in maintenance


def test_compaction_heartbeat_does_not_erase_waiting_facts():
    snapshot = build_terminal_activity_snapshot([
        event("compaction", 1, compaction_id="compact-3", status="observed",
              recovery={"phase": "retry_wait", "reason": "rate_limited", "http_status": 429}),
        event("compaction", 2, compaction_id="compact-3", status="observed", heartbeat=True),
        event("done", 3),
    ], task_id="t", turn_id="t", terminal_at=20000)
    assert snapshot is not None
    maintenance, = snapshot["entries"]
    assert maintenance["recovery"]["http_status"] == 429


def _failed_second_call_events(**done: object) -> list[dict[str, object]]:
    """Call 1 commits reasoning, text and a tool; call 2 streams, then errors."""
    return [
        event("provider_activity", 1, phase="requesting"),
        event("thinking", 2, block_id="r1", block_index=0, text="first reasoning"),
        event("thinking_end", 3, block_id="r1", status="completed"),
        event("text_delta", 4, text="Before tool."),
        event("tool_use_start", 5, tool_use_id="call", tool_name="record"),
        event("tool_result", 6, tool_use_id="call", tool_name="record", is_error=False),
        event("provider_activity", 7, phase="requesting"),
        event("thinking", 8, block_id="r2", block_index=1, text="second partial reasoning"),
        event("thinking_end", 9, block_id="r2", status="completed"),
        event("text_delta", 10, text="After tool, partial text."),
        event("error", 11, code="request_error"),
        event("done", 12, **done),
    ]


def test_v2_snapshot_with_uncommitted_failed_call_output_is_incomplete() -> None:
    snapshot = build_terminal_activity_snapshot(
        _failed_second_call_events(
            reasoning_content="first reasoning",
            text_snapshot="Before tool.",
        ),
        task_id="task-1",
        turn_id="turn-1",
    )

    assert snapshot is not None
    assert snapshot["complete"] is False
    # The streamed order is still recorded, only the completeness claim is dropped.
    assert [entry["order"] for entry in snapshot["entries"]] == sorted(
        entry["order"] for entry in snapshot["entries"]
    )


def test_v2_snapshot_with_uncommitted_reasoning_only_is_incomplete() -> None:
    events = _failed_second_call_events(
        reasoning_content="first reasoning",
        text_snapshot="Before tool.After tool, partial text.",
    )

    snapshot = build_terminal_activity_snapshot(events, task_id="task-1", turn_id="turn-1")

    assert snapshot is not None
    assert snapshot["complete"] is False


def test_v2_snapshot_with_uncommitted_text_only_is_incomplete() -> None:
    events = _failed_second_call_events(
        reasoning_content="first reasoning\nsecond partial reasoning",
        text_snapshot="Before tool.",
    )

    snapshot = build_terminal_activity_snapshot(events, task_id="task-1", turn_id="turn-1")

    assert snapshot is not None
    assert snapshot["complete"] is False


def test_v2_snapshot_matching_the_committed_done_payload_stays_complete() -> None:
    events = _failed_second_call_events(
        reasoning_content="first reasoning\nsecond partial reasoning",
        text_snapshot="Before tool.After tool, partial text.",
    )

    snapshot = build_terminal_activity_snapshot(events, task_id="task-1", turn_id="turn-1")

    assert snapshot is not None
    assert snapshot["complete"] is True


def test_v2_snapshot_without_committed_values_in_done_is_unchanged() -> None:
    snapshot = build_terminal_activity_snapshot(
        _failed_second_call_events(),
        task_id="task-1",
        turn_id="turn-1",
    )

    assert snapshot is not None
    assert snapshot["complete"] is True
