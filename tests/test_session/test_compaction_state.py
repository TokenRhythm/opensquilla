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


def test_extract_compaction_obligations_cap_keeps_recent_high_signal_facts() -> None:
    entries = [
        {
            "id": index,
            "role": "assistant",
            "content": f"Trace id: {index:032x}",
        }
        for index in range(70)
    ]
    entries.append(
        {
            "id": 100,
            "role": "user",
            "content": (
                "Goal: finish the current bug fix.\n"
                "Constraint: do not add the future purpose framework."
            ),
        }
    )

    obligations = extract_compaction_obligations(entries, max_obligations=8)
    by_kind = {(item.kind, item.value) for item in obligations}

    assert ("user_goal", "finish the current bug fix.") in by_kind
    assert (
        "user_constraint_or_preference",
        "do not add the future purpose framework.",
    ) in by_kind
    assert len(obligations) == 8


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


def test_structured_summary_can_block_missing_critical_obligations_when_opted_in() -> None:
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

    assert coverage.status == "fail_blocked"
    assert coverage.blocked is True
    assert coverage.missing_obligations
    assert summary.source_coverage["status"] == "fail_blocked"
