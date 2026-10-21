from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, Field

from opensquilla.session.compaction_state import (
    CompactionObligation,
    StructuredCompactionSummary,
    build_structured_summary_from_text,
    extract_compaction_obligations,
    render_structured_summary,
    verify_summary_coverage,
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
            "tool_calls": [{"id": "call_exec_1", "name": "exec_command", "arguments": {
                "command": "uv run pytest tests/test_session/test_manager.py",
            }}],
        },
        {
            "role": "tool",
            "tool_call_id": "call_exec_1",
            "content": (
                "exit_code=1\n"
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


@pytest.mark.parametrize("role", ["assistant", "tool"])
def test_closing_markup_tags_do_not_become_compaction_file_facts(role: str) -> None:
    obligations = extract_compaction_obligations([
        {
            "role": role,
            "content": (
                "<think>Review the parser.</think>\n"
                "<analysis>Keep the verified source.</analysis>\n"
                "<task-state>Done.</task-state>\n"
                "Verified source: `src/parser/scan.py`."
            ),
        },
    ])
    summary, _coverage = build_structured_summary_from_text(
        "The verified source is src/parser/scan.py.", obligations,
    )

    assert [item.value for item in obligations if item.kind == "file_path"] == [
        "src/parser/scan.py",
    ]
    assert summary.files_and_artifacts == []
    assert "src/parser/scan.py" in render_structured_summary(summary)


@pytest.mark.parametrize("path", [
    "/think", "/tmp/synthetic-state.txt", "./state.txt", "../state.txt",
    "src/parser/scan.py", "C:/synthetic/state.txt", r"C:\state.txt",
])
def test_real_paths_remain_compaction_file_facts(path: str) -> None:
    obligations = extract_compaction_obligations([
        {"role": "assistant", "content": f"Verified source: `{path}`."},
    ])

    assert [item.value for item in obligations if item.kind == "file_path"] == [path]


def test_tool_workset_is_diagnostic_not_a_new_summary_sidecar() -> None:
    entries = [
        {
            "role": "assistant",
            "content": "Next I will run uv run pytest tests/test_session/test_compaction.py.",
            "tool_calls": [
                {"id": "call_done", "name": "exec_command", "arguments": {
                    "command": "uv run pytest tests/test_session/test_compaction.py",
                }},
                {"id": "call_waiting", "type": "function"},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_done",
            "content": "exit_code=0\nuv run pytest tests/test_session/test_compaction.py passed",
        },
    ]

    obligations = extract_compaction_obligations(entries)
    summary, _coverage = build_structured_summary_from_text(
        "One test completed and another tool call is awaiting its result.",
        obligations,
    )

    assert any(item.kind == "command" for item in obligations)
    assert [item.value for item in obligations if item.kind == "pending_tool_or_approval_id"] == [
        "call_waiting",
    ]
    assert summary.executed_commands_and_tests == []
    assert summary.pending_tool_and_approval_ids == []
    # A historical promise does not establish that this command is still pending.
    assert summary.open_steps == []
    assert summary.next_action is None
    assert "call_done" not in summary.pending_tool_and_approval_ids


@pytest.mark.parametrize("role", ["user", "assistant", "tool"])
def test_command_mentions_are_not_promoted_to_executed_history(role: str) -> None:
    command = "pnpm test --filter connection-recovery"
    entries = [{"role": role, "content": f"Please run `{command}` after approval."}]
    if role == "tool":
        entries = [
            {"role": "assistant", "content": "Read the instructions.", "tool_calls": [
                {"id": "read_guide", "name": "read_file", "arguments": {"path": "guide.md"}},
            ]},
            {**entries[0], "tool_call_id": "read_guide"},
        ]
    summary, _ = build_structured_summary_from_text(
        "Awaiting approval; tests have not run.", extract_compaction_obligations(entries),
        block_missing_critical=True,
    )
    assert summary.executed_commands_and_tests == []
    assert command not in render_structured_summary(summary)
    assert "Executed Commands and Tests:" not in render_structured_summary(summary)


@pytest.mark.parametrize("status", [
    {"status": "unknown", "reason": "awaiting_approval", "preservation_class": "ephemeral"},
    {"status": "error", "reason": "approval_denied"},
    {"status": "error", "reason": "runtime_unavailable"},
    {"status": "cancelled", "reason": "cancelled_before_dispatch"},
    {"status": "timeout", "reason": "queue_timeout"},
])
def test_unexecuted_shell_requests_do_not_become_completed_commands(status: dict) -> None:
    entries = [{"role": "assistant", "content": "Checking tests.", "tool_calls": [
        {"type": "tool_use", "tool_use_id": "exec_1", "name": "exec_command",
         "input": {"command": "pnpm test"}},
        {"type": "tool_result", "tool_use_id": "exec_1", "result": "pnpm test not executed",
         "execution_status": status},
    ]}]
    summary, _ = build_structured_summary_from_text(
        "The shell operation did not execute.", extract_compaction_obligations(entries),
    )
    assert summary.executed_commands_and_tests == []


@pytest.mark.parametrize("exit_code", [0, 2])
def test_actual_shell_receipt_preserves_input_not_commands_mentioned_in_output(
    exit_code: int,
) -> None:
    output = f"exit_code={exit_code}\nFor future checks try npm run lint"
    if exit_code:
        output += "\nError: validation failed"
    entries = [
        {"role": "assistant", "content": "Run validation.", "tool_calls": [
            {"id": "exec_1", "function": {"name": "exec_command",
             "arguments": '{"command": "pnpm test"}'}},
        ]},
        {"role": "tool", "tool_call_id": "exec_1",
         "content": output},
    ]
    obligations = extract_compaction_obligations(entries)
    summary, _ = build_structured_summary_from_text(
        "Validation returned an exit code.", obligations,
    )
    assert [item.value for item in obligations if item.kind == "command"] == ["pnpm test"]
    assert summary.executed_commands_and_tests == []
    assert "npm run lint" not in render_structured_summary(summary)
    if exit_code:
        assert any("validation failed" in item.value for item in obligations)
        assert summary.known_failures == []
        assert "passed" not in render_structured_summary(summary)


def test_neutral_command_reference_stays_neutral_across_two_checkpoints() -> None:
    first, _ = build_structured_summary_from_text(
        "Approval is pending.", extract_compaction_obligations([
            {"role": "user", "content": "After approval run pnpm test"},
        ]),
    )
    second, _ = build_structured_summary_from_text(
        "Approval is still pending; no command was run.", extract_compaction_obligations([
            {"role": "assistant", "content": render_structured_summary(first)},
        ]),
        block_missing_critical=True,
    )
    assert first.executed_commands_and_tests == second.executed_commands_and_tests == []
    assert "pnpm test" not in render_structured_summary(second)


@pytest.mark.parametrize("result", ["exit_code=0\nDone", "pnpm test passed (quoted README text)"])
def test_legacy_shell_result_requires_actual_receipt_instead_of_completion_prose(
    result: str,
) -> None:
    entries = [{"role": "assistant", "content": "", "tool_calls": [
        {"type": "tool_use", "tool_use_id": "exec_1", "name": "exec_command",
         "input": {"command": "pnpm test"}},
        {"type": "tool_result", "tool_use_id": "exec_1", "result": result,
         "execution_status": {"status": "unknown", "reason": "legacy_missing_status"}},
    ]}]
    obligations = extract_compaction_obligations(entries)
    summary, _ = build_structured_summary_from_text("Prior tool result.", obligations)
    expected = ["pnpm test"] if result.startswith("exit_code=") else []
    assert [item.value for item in obligations if item.kind == "command"] == expected
    assert summary.executed_commands_and_tests == []


@pytest.mark.parametrize("structured", [False, True])
def test_resolved_plans_and_questions_are_not_backfilled_as_current_state(structured) -> None:
    old_plan = "Next I will compare the two adapters."
    old_question = "Which adapter is active?"
    prior = (
        render_structured_summary(StructuredCompactionSummary(
            next_action=old_plan,
            open_steps=[old_plan],
            unresolved_questions=[old_question],
            critical_carry_forward=[
                f"current_plan_or_next_action: {old_plan}",
                f"unresolved_question: {old_question}",
            ],
        ))
        if structured else f"{old_plan}\n{old_question}"
    )
    entries = [
        {"role": "assistant", "content": prior},
        {
            "role": "assistant",
            "content": "Both adapters were compared. The silver adapter is active. "
            "The next task is to prepare the release note for src/synthetic/main.py.",
            "tool_calls": [{"id": "call_release_note", "type": "function"}],
        },
    ]
    updated = "Adapter comparison is complete; silver is active. The release note is underway."
    obligations = extract_compaction_obligations(entries)
    summary, coverage = build_structured_summary_from_text(
        updated, obligations, block_missing_critical=True,
    )
    rendered = render_structured_summary(summary)

    assert summary.current_status == updated
    assert summary.next_action is None
    assert summary.open_steps == []
    assert summary.unresolved_questions == []
    assert old_plan not in rendered
    assert old_question not in rendered
    assert "src/synthetic/main.py" not in rendered
    assert summary.pending_tool_and_approval_ids == []
    assert coverage.status == "fail_reported"
    assert not coverage.blocked


def test_legacy_obligation_callers_cannot_force_superseded_state_back_into_summary() -> None:
    obligations = [
        CompactionObligation(kind="current_plan_or_next_action", value="Next I will compare."),
        CompactionObligation(kind="unresolved_question", value="Which route is active?"),
        CompactionObligation(kind="important_identifier", value="att_synthetic123"),
    ]
    updated = "Comparison is complete; the silver route is active. Keep att_synthetic123."
    summary, coverage = build_structured_summary_from_text(
        updated, obligations, block_missing_critical=True,
    )
    assert summary.critical_carry_forward == []
    assert summary.next_action is None
    assert summary.unresolved_questions == []
    assert coverage.status == "pass"
    assert coverage.checked_obligations == 1
    assert verify_summary_coverage(
        render_structured_summary(summary), obligations, block_missing_critical=True,
    ).status == "pass"


def test_canonical_nested_tool_workset_does_not_override_summary() -> None:
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

    assert any(item.kind == "failed_command_or_error" and "assertion failed" in item.value
               for item in obligations)
    assert any(item.kind == "command" and item.value.startswith("uv run pytest tests/test_session")
               for item in obligations)
    assert [item.value for item in obligations if item.kind == "pending_tool_or_approval_id"] == [
        "call_waiting",
    ]
    assert summary.known_failures == []
    assert summary.executed_commands_and_tests == []
    assert summary.pending_tool_and_approval_ids == []


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
    assert any(item.kind == "user_goal" and item.value == "ship the current compaction fix."
               for item in obligations)
    assert any(item.kind == "user_constraint_or_preference"
               and item.value == "preserve pending operation continuity." for item in obligations)
    assert [item.value for item in obligations if item.kind == "pending_tool_or_approval_id"] == [
        "approval_pending_current",
        "call_pending_tool",
    ]
    assert summary.user_goal == ""
    assert summary.constraints_and_preferences == []
    assert summary.pending_tool_and_approval_ids == []
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
    assert any(
        item.kind == "user_goal" and item.value == "complete the active bounded selection repair."
        for item in obligations
    )
    assert any(item.kind == "user_constraint_or_preference"
               and item.value == "retain the active pending operation." for item in obligations)
    assert [item.value for item in obligations if item.kind == "pending_tool_or_approval_id"] == [
        "call_active_pending",
    ]
    assert summary.user_goal == ""
    assert summary.constraints_and_preferences == []
    assert summary.pending_tool_and_approval_ids == []
    assert all(
        item.value != "superseded historical objective 0." for item in obligations
    )


def test_structured_summary_reports_missing_obligations_without_backfill() -> None:
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

    assert coverage.status == "fail_reported"
    assert coverage.blocked is False
    assert coverage.missing_obligations
    assert coverage.critical_carry_forward == summary.critical_carry_forward == []

    rendered = render_structured_summary(summary)
    assert "Critical Carry Forward:" not in rendered
    assert "src/opensquilla/session/models.py" not in rendered
    assert "call_exec_2" not in rendered


def test_structured_summary_does_not_reintroduce_extracted_decisions_and_identifiers() -> None:
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

    assert coverage.status == "fail_reported"
    assert summary.decisions_and_rationale == []
    assert summary.important_identifiers == []


def test_legacy_blocking_option_cannot_force_backfill() -> None:
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

    assert coverage.status == "fail_reported"
    assert coverage.blocked is False
    assert coverage.missing_obligations
    assert summary.source_coverage["status"] == "fail_reported"
    assert "src/opensquilla/session/models.py" not in render_structured_summary(summary)


def test_many_missing_goals_neither_block_nor_grow_checkpoint() -> None:
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

    assert coverage.status == "fail_reported"
    assert coverage.blocked is False
    assert coverage.missing_obligations
    assert summary.source_coverage["status"] == "fail_reported"
    assert len(render_structured_summary(summary)) < 120


@pytest.mark.parametrize("backfill", [False, True])
@pytest.mark.parametrize("blocking", [False, True])
def test_coverage_legacy_flags_are_diagnostic_only(backfill: bool, blocking: bool) -> None:
    report = verify_summary_coverage(
        "Use the corrected current task.",
        [CompactionObligation(kind="user_goal", value="obsolete task", critical=True)],
        backfill_missing=backfill,
        block_missing_critical=blocking,
    )
    assert report.status == "fail_reported"
    assert report.checked_obligations == 1
    assert report.covered_obligations == 0
    assert report.missing_obligations == ["user_goal: obsolete task"]
    assert report.blocked is False
    assert report.critical_carry_forward == []


def test_coverage_diagnostics_bound_work_and_labels() -> None:
    class OversizedObligations:
        def __iter__(self):
            for _ in range(64):
                yield CompactionObligation(kind="file_path", value="x" * 100_000)
            raise AssertionError("diagnostics scanned past the bounded sample")

    report = verify_summary_coverage("A usable checkpoint.", OversizedObligations())
    assert report.status == "fail_reported"
    assert report.checked_obligations == 64
    assert len(report.missing_obligations) == 32
    assert all(len(label) <= 306 for label in report.missing_obligations)
    assert not report.blocked


def test_diagnostic_failure_does_not_discard_model_summary() -> None:
    class UnavailableObligations:
        def __iter__(self):
            raise RuntimeError("diagnostic source unavailable")

    body = "Current task completed; do not repeat the retired migration."
    summary, report = build_structured_summary_from_text(body, UnavailableObligations())
    assert summary.current_status == body
    assert report.status == "unknown"
    assert report.blocked is False
    assert body in render_structured_summary(summary)


def test_coverage_does_not_swallow_cancellation() -> None:
    class CancelledObligations:
        def __iter__(self):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        verify_summary_coverage("A checkpoint.", CancelledObligations())


class _FrozenV1Summary(BaseModel):
    """The pre-P1 payload field types, independent of the production model."""

    schema_version: int = 1
    user_goal: str = ""
    current_status: str = ""
    next_action: str | None = None
    completed_steps: list[str] = Field(default_factory=list)
    open_steps: list[str] = Field(default_factory=list)
    files_and_artifacts: list[dict[str, str]] = Field(default_factory=list)
    tool_results_to_remember: list[dict[str, str]] = Field(default_factory=list)
    decisions_and_rationale: list[dict[str, str]] = Field(default_factory=list)
    known_failures: list[dict[str, str]] = Field(default_factory=list)
    executed_commands_and_tests: list[str] = Field(default_factory=list)
    pending_tool_and_approval_ids: list[str] = Field(default_factory=list)
    important_identifiers: list[str] = Field(default_factory=list)
    constraints_and_preferences: list[str] = Field(default_factory=list)
    do_not_repeat: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    critical_carry_forward: list[str] = Field(default_factory=list)
    source_coverage: dict[str, Any] = Field(default_factory=dict)


def test_new_checkpoint_is_readable_by_frozen_v1_payload_model() -> None:
    body = "## Current Work\nThe corrected task is active.\n## Next Step\nVerify the current files."
    summary, _ = build_structured_summary_from_text(
        body,
        [CompactionObligation(kind="failed_command_or_error", value="retired failure")],
        block_missing_critical=True,
    )
    summary.source_coverage.update({"replaces_prior_context": True})
    payload = summary.model_dump(mode="json")
    old_reader = _FrozenV1Summary.model_validate(payload)
    assert old_reader.current_status == body
    assert old_reader.schema_version == 1
    assert old_reader.known_failures == []
    assert old_reader.source_coverage["replaces_prior_context"] is True
    assert render_structured_summary(old_reader.model_dump()) == render_structured_summary(payload)
