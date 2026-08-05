from __future__ import annotations

from opensquilla.session.compaction_state import (
    StructuredCompactionSummary,
    build_structured_summary_from_text,
    extract_compaction_obligations,
    render_structured_summary,
)


def test_render_structured_summary_uses_stable_sections_not_raw_json() -> None:
    summary = StructuredCompactionSummary(
        user_goal="Improve compaction continuity.",
        current_status="Metadata fields are being introduced.",
        next_action="Wire structured rendering into summary replay.",
        completed_steps=["Locked existing summary_text behavior."],
        open_steps=["Persist structured payload."],
        files_and_artifacts=[
            {
                "path": "src/opensquilla/session/models.py",
                "status": "changed",
                "why": "stores summary metadata",
            }
        ],
        tool_results_to_remember=[{"tool": "pytest", "result": "targeted summary tests pass"}],
        decisions_and_rationale=[
            {
                "decision": "keep summary_text",
                "rationale": "old sessions must continue replaying",
            }
        ],
        known_failures=[{"command": "pytest old", "error": "missing summary_payload column"}],
        executed_commands_and_tests=["uv run pytest tests/test_session/test_compaction.py"],
        pending_tool_and_approval_ids=["call_waiting_1"],
        important_identifiers=["cmp_123"],
        constraints_and_preferences=["do not enable coverage blocking by default"],
        do_not_repeat=["do not force-add ignored docs"],
        unresolved_questions=["which provider-native state ships first?"],
        critical_carry_forward=["summary_text remains compatibility fallback"],
        source_coverage={"coverage_status": "unknown"},
    )

    rendered = render_structured_summary(summary)

    assert rendered.startswith("[Structured Compaction Summary]")
    assert "Goal:\nImprove compaction continuity." in rendered
    assert "Files and Artifacts:" in rendered
    assert "- path: src/opensquilla/session/models.py" in rendered
    assert "Known Failures:" in rendered
    assert "Executed Commands and Tests:" in rendered
    assert "Pending Tool and Approval IDs:" in rendered
    assert "Critical Carry Forward:" in rendered
    assert '{"' not in rendered
    assert "source_coverage" not in rendered


def test_extract_compaction_obligations_keeps_high_signal_facts_bounded() -> None:
    entries = [
        {
            "role": "user",
            "content": (
                "Goal: keep the long task alive.\n"
                "Constraint: preserve changed file paths in the final report.\n"
                "Decision: keep provider-specific state diagnostic-only until wired.\n"
                "Trace id: 550e8400-e29b-41d4-a716-446655440000.\n"
                "Please update docs/Long Task Report.md.\n"
                "Artifact: final continuity report.pdf.\n"
                "Question: should provider-native state stay opt-in?"
            ),
        },
        {
            "role": "assistant",
            "content": "Next I will run uv run pytest tests/test_session/test_manager.py.",
            "tool_calls": [{"id": "call_exec_1", "type": "function"}],
        },
        {
            "role": "tool",
            "tool_call_id": "call_exec_1",
            "content": (
                "Command failed: uv run pytest tests/test_session/test_manager.py\n"
                "Exit code 1\n"
                "File src/opensquilla/session/models.py\n"
                "Error: missing summary_payload column"
            ),
        },
        {
            "role": "assistant",
            "content": "Do not repeat git reset --hard; keep summary_text compatible.",
        },
    ]

    obligations = extract_compaction_obligations(entries, max_obligations=20)
    by_kind = {(item.kind, item.value) for item in obligations}

    assert ("user_goal", "keep the long task alive.") in by_kind
    assert (
        "user_constraint_or_preference",
        "preserve changed file paths in the final report.",
    ) in by_kind
    assert ("file_path", "docs/Long Task Report.md") in by_kind
    assert ("file_path", "src/opensquilla/session/models.py") in by_kind
    assert ("artifact_path_or_name", "final continuity report.pdf") in by_kind
    assert (
        "decision_or_rationale",
        "keep provider-specific state diagnostic-only until wired.",
    ) in by_kind
    assert (
        "important_identifier",
        "550e8400-e29b-41d4-a716-446655440000",
    ) in by_kind
    assert ("command", "uv run pytest tests/test_session/test_manager.py") in by_kind
    assert ("failed_command_or_error", "Error: missing summary_payload column") in by_kind
    assert ("tool_result_id", "call_exec_1") in by_kind
    assert (
        "do_not_repeat_action",
        "Do not repeat git reset --hard; keep summary_text compatible.",
    ) in by_kind
    assert all(len(item.value) <= 240 for item in obligations)


def test_structured_summary_exposes_command_and_pending_tool_workset() -> None:
    entries = [
        {
            "role": "assistant",
            "content": "Next I will run uv run pytest tests/test_session/test_compaction.py.",
            "tool_calls": [
                {"id": "call_done", "type": "function"},
                {"id": "call_waiting", "type": "function"},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_done",
            "content": "uv run pytest tests/test_session/test_compaction.py passed",
        },
    ]

    obligations = extract_compaction_obligations(entries)
    summary, _coverage = build_structured_summary_from_text(
        "One test completed and another tool call is awaiting its result.",
        obligations,
    )

    assert any(
        command.startswith("uv run pytest tests/test_session/test_compaction.py")
        for command in summary.executed_commands_and_tests
    )
    assert summary.pending_tool_and_approval_ids == ["call_waiting"]
    assert summary.open_steps == [
        "Next I will run uv run pytest tests/test_session/test_compaction.py."
    ]
    assert "call_done" not in summary.pending_tool_and_approval_ids


def test_canonical_nested_tool_results_populate_failure_and_pending_workset() -> None:
    entries = [
        {
            "role": "assistant",
            "content": "Checking the implementation.",
            "tool_calls": [
                {
                    "type": "tool_use",
                    "tool_use_id": "call_failed",
                    "name": "exec_command",
                    "input": {"command": "uv run pytest tests/test_session"},
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call_failed",
                    "result": (
                        "uv run pytest tests/test_session failed\n"
                        "Error: assertion failed in src/opensquilla/session/manager.py"
                    ),
                    "is_error": True,
                    "execution_status": {
                        "status": "error",
                        "reason": "nonzero_exit",
                    },
                },
                {
                    "type": "tool_use",
                    "tool_use_id": "call_waiting",
                    "name": "request_approval",
                    "input": {},
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call_waiting",
                    "result": "Approval is still pending.",
                    "execution_status": {
                        "status": "unknown",
                        "reason": "awaiting_approval",
                        "preservation_class": "ephemeral",
                    },
                },
            ],
        }
    ]

    obligations = extract_compaction_obligations(entries)
    summary, _coverage = build_structured_summary_from_text(
        "The check failed and an approval remains pending.",
        obligations,
    )

    assert any(
        "assertion failed" in item.get("detail", "").lower()
        for item in summary.known_failures
    )
    assert any(
        command.startswith("uv run pytest tests/test_session")
        for command in summary.executed_commands_and_tests
    )
    assert summary.pending_tool_and_approval_ids == ["call_waiting"]
    assert "call_failed" not in summary.pending_tool_and_approval_ids


def test_extract_compaction_obligations_prioritizes_goals_when_bounded() -> None:
    entries = [
        {
            "role": "user",
            "content": (
                "Goal: preserve task continuity.\n"
                "Constraint: keep coverage blocking disabled by default.\n"
                "Touch docs/a.md docs/b.md docs/c.md docs/d.md docs/e.md docs/f.md."
            ),
        }
    ]

    obligations = extract_compaction_obligations(entries, max_obligations=2)

    assert [(item.kind, item.value) for item in obligations] == [
        ("user_goal", "preserve task continuity."),
        ("user_constraint_or_preference", "keep coverage blocking disabled by default."),
    ]


def test_extract_compaction_obligations_keeps_current_workset_after_old_noise() -> None:
    entries = [
        {
            "role": "user",
            "content": (
                ("Goal: superseded migration goal.\nConstraint: superseded compatibility rule.\n")
                if index == 0
                else ""
            )
            + f"Touch src/legacy/noise_{index}.py.",
        }
        for index in range(64)
    ]
    entries.extend(
        [
            {
                "role": "user",
                "content": (
                    "Goal: ship the current compaction fix.\n"
                    "Constraint: preserve pending operation continuity."
                ),
            },
            {
                "role": "assistant",
                "content": "Waiting for the current operations.",
                "tool_calls": [
                    {"id": "call_pending_tool", "type": "function"},
                    {"id": "approval_pending_current", "type": "function"},
                ],
            },
        ]
    )

    obligations = extract_compaction_obligations(entries, max_obligations=64)
    summary, _coverage = build_structured_summary_from_text(
        "The current task remains in progress.",
        obligations,
    )

    assert len(obligations) == 64
    assert summary.user_goal == "ship the current compaction fix."
    assert summary.constraints_and_preferences[0] == (
        "preserve pending operation continuity."
    )
    assert summary.pending_tool_and_approval_ids == [
        "approval_pending_current",
        "call_pending_tool",
    ]
    assert not any(
        item.kind == "file_path" and item.value == "src/legacy/noise_0.py"
        for item in obligations
    )


def test_extract_compaction_obligations_reserves_current_workset_after_goal_noise() -> None:
    entries = [
        {
            "role": "user",
            "content": f"Goal: superseded historical objective {index}.",
        }
        for index in range(70)
    ]
    entries.extend(
        [
            {
                "role": "user",
                "content": (
                    "Goal: complete the active bounded selection repair.\n"
                    "Constraint: retain the active pending operation."
                ),
            },
            {
                "role": "assistant",
                "content": "The active tool operation is still pending.",
                "tool_calls": [{"id": "call_active_pending", "type": "function"}],
            },
        ]
    )

    obligations = extract_compaction_obligations(entries, max_obligations=64)
    summary, _coverage = build_structured_summary_from_text(
        "The active repair is still underway.",
        obligations,
    )

    assert len(obligations) == 64
    assert summary.user_goal == "complete the active bounded selection repair."
    assert summary.constraints_and_preferences[0] == (
        "retain the active pending operation."
    )
    assert summary.pending_tool_and_approval_ids == ["call_active_pending"]
    assert all(
        item.value != "superseded historical objective 0." for item in obligations
    )


def test_structured_summary_backfills_missing_obligations_without_blocking() -> None:
    obligations = extract_compaction_obligations(
        [
            {
                "role": "user",
                "content": (
                    "Goal: finish compaction continuity. Keep src/opensquilla/session/models.py."
                ),
            },
            {
                "role": "tool",
                "tool_call_id": "call_exec_2",
                "content": (
                    "Command failed: uv run pytest tests/test_session/test_manager.py\nError: boom"
                ),
            },
        ]
    )

    summary, coverage = build_structured_summary_from_text(
        "The session is being compacted.",
        obligations,
    )

    assert coverage.status == "pass_with_backfill"
    assert coverage.blocked is False
    assert coverage.missing_obligations
    assert any(
        "src/opensquilla/session/models.py" in item for item in coverage.critical_carry_forward
    )
    assert any("call_exec_2" in item for item in coverage.critical_carry_forward)
    assert summary.critical_carry_forward == coverage.critical_carry_forward

    rendered = render_structured_summary(summary)
    assert "Critical Carry Forward:" in rendered
    assert "src/opensquilla/session/models.py" in rendered
    assert "call_exec_2" in rendered


def test_structured_summary_preserves_decisions_and_identifiers() -> None:
    obligations = extract_compaction_obligations(
        [
            {
                "role": "user",
                "content": (
                    "Decision: keep provider-specific state diagnostic-only until wired.\n"
                    "Trace id: 550e8400-e29b-41d4-a716-446655440000."
                ),
            }
        ]
    )

    summary, coverage = build_structured_summary_from_text(
        "The session is being compacted.",
        obligations,
    )

    assert coverage.status == "pass_with_backfill"
    assert summary.decisions_and_rationale == [
        {"detail": "keep provider-specific state diagnostic-only until wired."}
    ]
    assert summary.important_identifiers == ["550e8400-e29b-41d4-a716-446655440000"]


def test_structured_summary_coverage_checks_the_replayed_backfill() -> None:
    obligations = extract_compaction_obligations(
        [
            {
                "role": "user",
                "content": (
                    "Goal: finish compaction continuity. Keep src/opensquilla/session/models.py."
                ),
            }
        ]
    )

    summary, coverage = build_structured_summary_from_text(
        "The session is being compacted.",
        obligations,
        block_missing_critical=True,
    )

    assert coverage.status == "pass"
    assert coverage.blocked is False
    assert coverage.missing_obligations == []
    assert summary.source_coverage["status"] == "pass"
    assert "src/opensquilla/session/models.py" in render_structured_summary(summary)


def test_structured_summary_blocks_when_bounded_replay_still_misses_critical_facts() -> None:
    obligations = extract_compaction_obligations(
        [
            {
                "role": "user",
                "content": "\n".join(
                    f"Goal: preserve independent objective {index}"
                    for index in range(40)
                ),
            }
        ]
    )

    summary, coverage = build_structured_summary_from_text(
        "The session is being compacted.",
        obligations,
        block_missing_critical=True,
    )

    assert coverage.status == "fail_blocked"
    assert coverage.blocked is True
    assert coverage.missing_obligations
    assert summary.source_coverage["status"] == "fail_blocked"
