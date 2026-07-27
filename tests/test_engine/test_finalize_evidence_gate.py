"""Unit tests for the pure finalize-time red-evidence gate module.

The tracker/classifiers here are shared verbatim between the live agent loop
and offline transcript replay (``scripts/experiments/replay_finalize_gate.py``),
so these tests pin the exact detector semantics that both callers rely on.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from opensquilla.engine.finalize_evidence_gate import (
    FINALIZE_EVIDENCE_GATE_CHALLENGE_LIMIT,
    FinalizeEvidenceTracker,
    classify_gate_command,
    command_execution_profiles,
    command_removal_targets,
    execution_signals_from_result,
    finalize_evidence_challenge_message,
    finalize_evidence_gate_key,
    green_profiles_deselect_red,
    green_profiles_recover_red,
    has_stash_reversal,
    is_detector_findings_exit,
    looks_repro_artifact_path,
    scan_stash_effects,
)

# ---------------------------------------------------------------------------
# Path / command classifiers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("repro.py", True),
        ("reproduce_issue.rb", True),
        ("debug_overlap.java", True),
        ("verify_fix.sh", True),
        ("/tmp/squilla-scratch/test_fix.py", True),
        ("/tmp/squilla-scratch/repro_case.tcl", True),
        ("/tmp/anything.js", True),
        ("/tmp/notes.txt", False),
        ("src/main.py", False),
        ("tests/test_parser.py", False),
        ("", False),
    ],
)
def test_looks_repro_artifact_path(path: str, expected: bool) -> None:
    assert looks_repro_artifact_path(path) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # Marker must be an exact stem token: real source files whose names
        # merely contain a marker substring are source edits.
        ("preprocessing.py", False),
        ("inspectdb.py", False),
        ("DebugOverlaps.java", False),
        # Nested repository paths are always source edits, whatever the name.
        ("app/views/debug.py", False),
        ("lib/debug.py", False),
        ("src/utils/reproduce_helper.py", False),
        ("/testbed/app/reproduce.py", False),
        # Shallow paths with an exact marker token still qualify.
        ("./repro.py", True),
        ("debug-issue.py", True),
        ("/testbed/reproduce.py", True),
    ],
)
def test_looks_repro_artifact_path_requires_anchored_shallow_marker(
    path: str, expected: bool
) -> None:
    assert looks_repro_artifact_path(path) is expected


def test_nested_marker_named_write_counts_as_source_edit() -> None:
    # A fix landing in app/views/debug.py must arm the gate, not register
    # a scratch artifact (which would silently disarm it for the whole run).
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write("app/views/debug.py", iteration=1)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.source_edit_seen is True
    assert observation.triggers == ["no_execution_after_final_edit"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat foo.py | grep bar", "inspection"),
        ("git status && git diff", "inspection"),
        ("cd /testbed && ls -la", "inspection"),
        ("python repro.py", "execution"),
        ("cd /testbed && pytest -x tests/", "execution"),
        ("timeout 60 cargo test 2>&1 | tail -30", "execution"),
        ("sudo npm test", "execution"),
        ("bundle exec rspec spec/foo_spec.rb", "execution"),
        # Unknown heads err toward execution (suppresses the gate).
        ("./gradlew test", "execution"),
    ],
)
def test_classify_gate_command(command: str, expected: str) -> None:
    assert classify_gate_command(command) == expected


def test_heredoc_body_is_not_segment_split() -> None:
    command = (
        "cat > /tmp/squilla-scratch/repro.py << 'EOF'\n"
        "import subprocess; subprocess.run(['pytest'])\n"
        "EOF"
    )
    # The heredoc body contains executable-looking text, but the command as a
    # whole is a file write via cat: inspection-only.
    assert classify_gate_command(command) == "inspection"


def test_command_after_heredoc_terminator_is_classified() -> None:
    command = (
        "cat > notes.md << EOF\n"
        "run pytest tests/ later\n"
        "EOF\n"
        "python repro.py"
    )
    assert classify_gate_command(command) == "execution"


def test_quoted_heredoc_operator_does_not_swallow_following_lines() -> None:
    # ``<< EOF`` inside a quoted string is not a heredoc opener; the pytest
    # line after it must still classify as execution.
    command = 'echo "see << EOF marker"\npytest tests/'
    assert classify_gate_command(command) == "execution"


def test_newline_separates_segments_like_semicolon() -> None:
    assert classify_gate_command("git status\npytest tests/") == "execution"
    assert command_removal_targets("git stash\nrm /tmp/repro.py") == ["/tmp/repro.py"]


def test_quoted_newline_stays_inside_one_segment() -> None:
    profiles = command_execution_profiles("python -c 'import x\nprint(1)'")
    assert len(profiles) == 1


def test_escaped_separator_stays_inside_segment() -> None:
    # A backslash-escaped ``;`` (as in ``find ... -exec ... \;``) must not
    # start a new segment: here ``pytest`` stays an argument of echo.
    assert classify_gate_command("echo skip \\; pytest tests/") == "inspection"


def test_bit_shift_is_not_mistaken_for_heredoc() -> None:
    assert classify_gate_command('python -c "print(1 << 2)"') == "execution"


def test_command_removal_targets() -> None:
    assert command_removal_targets("rm -f /tmp/repro.py") == ["/tmp/repro.py"]
    assert command_removal_targets("cd /testbed && rm -rf a.py b.py") == ["a.py", "b.py"]
    assert command_removal_targets("pytest tests/") == []


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # ``env`` and ``exec`` are wrappers: the wrapped program decides.
        ("env FOO=1 pytest tests/", "execution"),
        ("env -i PATH=/usr/bin python repro.py", "execution"),
        ("exec pytest tests/", "execution"),
        # Bare ``env`` prints the environment; nothing runs.
        ("env", "inspection"),
        # ``command -v prog`` probes for existence; it runs nothing.
        ("command -v pytest", "inspection"),
        ("command pytest tests/", "execution"),
    ],
)
def test_wrapper_head_classification(command: str, expected: str) -> None:
    assert classify_gate_command(command) == expected


def test_has_stash_reversal() -> None:
    assert has_stash_reversal("git stash && python repro.py") is True
    assert has_stash_reversal("git stash pop && pytest tests/") is False
    assert has_stash_reversal("git stash apply && pytest tests/") is False
    assert has_stash_reversal("pytest tests/") is False


def test_has_stash_reversal_parses_git_global_flags() -> None:
    assert has_stash_reversal("git -C /repo stash && pytest tests/") is True
    # ``-c key=val`` takes a value token; ``stash`` is still the subcommand.
    assert has_stash_reversal("git -c core.pager=cat stash && pytest tests/") is True


def test_stash_inspection_subcommands_carry_no_event() -> None:
    assert has_stash_reversal("git stash list && pytest tests/") is False
    assert has_stash_reversal("git stash show -p && pytest tests/") is False


def test_scan_stash_effects_threads_state_across_calls() -> None:
    assert scan_stash_effects("git stash", initially_stashed=False) == (False, True)
    assert scan_stash_effects("pytest tests/", initially_stashed=True) == (True, True)
    assert scan_stash_effects("git stash pop", initially_stashed=True) == (False, False)
    assert scan_stash_effects("git stash pop && pytest tests/", initially_stashed=True) == (
        False,
        False,
    )


# ---------------------------------------------------------------------------
# Detector findings demotion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "exit_code", "timed_out", "expected"),
    [
        ("rubocop app.rb", 1, False, True),
        ("cd /testbed && ruff check src/", 1, False, True),
        # Interpreter-launched and module-launched detectors resolve to the
        # real program.
        ("php php-cs-fixer fix --dry-run src.php", 1, False, True),
        ("python -m mypy src/", 1, False, True),
        # Exit >= 2 is a real tool error: stays red.
        ("rubocop app.rb", 2, False, False),
        # Mixed with a non-detector execution segment: stays red.
        ("ruff check . && pytest tests/", 1, False, False),
        ("pytest tests/", 1, False, False),
        ("rubocop app.rb", 1, True, False),
        ("rubocop app.rb", None, False, False),
    ],
)
def test_is_detector_findings_exit(
    command: str, exit_code: int | None, timed_out: bool, expected: bool
) -> None:
    assert is_detector_findings_exit(command, exit_code, timed_out) is expected


# ---------------------------------------------------------------------------
# Execution signals
# ---------------------------------------------------------------------------


def test_execution_signals_prefer_sidecar_masked_pipeline_failure() -> None:
    red, exit_code, timed_out, reason = execution_signals_from_result(
        tool_name="exec_command",
        content_text="exit_code=0\nerror[E0308]: mismatched types",
        execution_status={
            "version": 1,
            "status": "error",
            "exit_code": 0,
            "timed_out": False,
            "reason": "masked_pipeline_failure",
        },
        is_error=True,
    )
    assert red is True
    assert exit_code == 0
    assert timed_out is False
    assert reason == "masked_pipeline_failure"


def test_execution_signals_sidecar_success() -> None:
    red, exit_code, timed_out, reason = execution_signals_from_result(
        tool_name="exec_command",
        content_text="exit_code=0\nall good",
        execution_status={"version": 1, "status": "success", "exit_code": 0},
        is_error=False,
    )
    assert red is False
    assert exit_code == 0
    assert reason is None


def test_execution_signals_rederived_from_text_without_sidecar() -> None:
    red, exit_code, timed_out, reason = execution_signals_from_result(
        tool_name="exec_command",
        content_text="exit_code=2\nFAILED tests/test_x.py::test_y",
        execution_status=None,
        is_error=False,
    )
    assert red is True
    assert exit_code == 2
    assert reason == "nonzero_exit"


def test_execution_signals_fall_back_to_is_error() -> None:
    red, exit_code, timed_out, reason = execution_signals_from_result(
        tool_name="background_process",
        content_text="opaque output",
        execution_status=None,
        is_error=True,
    )
    assert red is True
    assert exit_code is None
    assert timed_out is False
    assert reason is None


# ---------------------------------------------------------------------------
# Deselection profiles
# ---------------------------------------------------------------------------


def test_green_deselects_red_via_k_not() -> None:
    red = command_execution_profiles("cd /testbed && pytest tests/test_a.py")
    green = command_execution_profiles(
        "cd /testbed && pytest tests/test_a.py -k 'not test_bad'"
    )
    assert green_profiles_deselect_red(green, red) is True


def test_green_deselects_red_via_deselect_flag() -> None:
    red = command_execution_profiles("pytest tests/test_a.py")
    green = command_execution_profiles(
        "pytest tests/test_a.py --deselect tests/test_a.py::test_bad"
    )
    assert green_profiles_deselect_red(green, red) is True


def test_green_rerun_without_narrowing_does_not_deselect() -> None:
    red = command_execution_profiles("pytest tests/test_a.py")
    green = command_execution_profiles("pytest tests/test_a.py -v")
    assert green_profiles_deselect_red(green, red) is False


def test_green_positive_selection_is_not_deselection() -> None:
    red = command_execution_profiles("pytest tests/test_a.py")
    green = command_execution_profiles("pytest tests/test_a.py -k 'test_good'")
    assert green_profiles_deselect_red(green, red) is False


def test_green_different_program_does_not_deselect() -> None:
    red = command_execution_profiles("pytest tests/test_a.py")
    green = command_execution_profiles("tox -e py311 -k 'not test_bad'")
    assert green_profiles_deselect_red(green, red) is False


def test_interpreter_module_launch_profiles_as_real_program() -> None:
    (profile,) = command_execution_profiles("python -m pytest tests/test_a.py")
    assert profile.head == "pytest"
    # The module token names the program, not a test target.
    assert profile.positionals == frozenset({"tests/test_a.py"})


def test_deselection_matches_across_module_and_direct_launch() -> None:
    red = command_execution_profiles("python -m pytest tests/test_a.py")
    green = command_execution_profiles("pytest tests/test_a.py -k 'not test_bad'")
    assert green_profiles_deselect_red(green, red) is True

    red = command_execution_profiles("pytest tests/test_a.py")
    green = command_execution_profiles("python -m pytest tests/test_a.py -k 'not test_bad'")
    assert green_profiles_deselect_red(green, red) is True


# ---------------------------------------------------------------------------
# Tracker triggers
# ---------------------------------------------------------------------------


def _tracker_with_source_edit() -> FinalizeEvidenceTracker:
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write("src/main.py", iteration=1)
    return tracker


def test_trigger_red_execution_after_final_edit() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_execution(
        "cd /testbed && pytest tests/test_a.py",
        red=True,
        exit_code=1,
        failure_anchors=["FAILED tests/test_a.py::test_x"],
        iteration=2,
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers[0] == "red_execution_after_final_edit"
    assert observation.should_challenge is True
    assert observation.red_command == "cd /testbed && pytest tests/test_a.py"
    assert observation.red_exit_code == 1
    assert observation.red_failure_anchors == ["FAILED tests/test_a.py::test_x"]


def test_no_triggers_without_workspace_diff() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_execution("pytest tests/", red=True, exit_code=1, iteration=2)
    observation = tracker.build_observation(has_workspace_diff=False)
    assert observation.triggers == []
    assert observation.should_challenge is False
    assert observation.primary_reason == "finalize_evidence_ok"


def test_no_triggers_without_source_edit() -> None:
    tracker = FinalizeEvidenceTracker()
    tracker.observe_execution("pytest tests/", red=True, exit_code=1, iteration=1)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []


def test_trigger_red_repro_outstanding_after_final_edit() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=True, exit_code=1, iteration=3
    )
    tracker.observe_execution("pytest tests/test_other.py", red=False, exit_code=0, iteration=4)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == ["red_repro_outstanding_after_final_edit"]
    assert observation.red_artifact_paths == ["/tmp/squilla-scratch/repro.py"]


def test_repro_going_green_clears_outstanding() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=True, exit_code=1, iteration=3
    )
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=False, exit_code=0, iteration=4
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []


def test_source_edit_resets_post_edit_window() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_execution("pytest tests/", red=True, exit_code=1, iteration=2)
    tracker.observe_write("src/main.py", iteration=3)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == ["no_execution_after_final_edit"]
    assert observation.post_edit_execution_count == 0


def test_scratch_write_does_not_reset_window() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_execution("pytest tests/", red=False, exit_code=0, iteration=2)
    tracker.observe_write("/data/custom-scratch/inspect_state.py", iteration=3, scratch=True)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []
    assert observation.post_edit_execution_count == 1


def test_trigger_red_evidence_deselected_after_final_edit() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_execution("pytest tests/test_a.py", red=True, exit_code=1, iteration=2)
    tracker.observe_execution(
        "pytest tests/test_a.py -k 'not test_bad'", red=False, exit_code=0, iteration=3
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == ["red_evidence_deselected_after_final_edit"]
    assert observation.red_command == "pytest tests/test_a.py"


def test_command_form_error_red_cannot_be_deselected() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_execution("pytest tests/test_a.py", red=True, exit_code=127, iteration=2)
    tracker.observe_execution(
        "pytest tests/test_a.py -k 'not test_bad'", red=False, exit_code=0, iteration=3
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []


def test_trigger_red_majority_after_final_edit() -> None:
    tracker = _tracker_with_source_edit()
    for index in range(5):
        tracker.observe_execution(
            f"pytest tests/test_{index}.py", red=True, exit_code=1, iteration=2 + index
        )
    for index in range(4):
        tracker.observe_execution(
            f"python check_{index}.py", red=False, exit_code=0, iteration=7 + index
        )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == ["red_majority_after_final_edit"]
    assert observation.post_edit_red_count == 5
    assert observation.post_edit_execution_count == 9


def test_red_minority_does_not_trigger_majority() -> None:
    tracker = _tracker_with_source_edit()
    for index in range(4):
        tracker.observe_execution(
            f"pytest tests/test_{index}.py", red=True, exit_code=1, iteration=2 + index
        )
    for index in range(5):
        tracker.observe_execution(
            f"python check_{index}.py", red=False, exit_code=0, iteration=6 + index
        )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []


def test_trigger_never_green_repro_deleted() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=True, exit_code=1, iteration=3
    )
    tracker.observe_execution("rm /tmp/squilla-scratch/repro.py", red=False, iteration=4)
    tracker.observe_execution("pytest tests/test_other.py", red=False, exit_code=0, iteration=5)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == ["never_green_repro_deleted"]
    assert observation.deleted_never_green_repro_paths == ["/tmp/squilla-scratch/repro.py"]


def test_ever_green_repro_deleted_is_fine() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=True, exit_code=1, iteration=3
    )
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=False, exit_code=0, iteration=4
    )
    tracker.observe_execution("rm /tmp/squilla-scratch/repro.py", red=False, iteration=5)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []


def test_trigger_no_execution_after_final_edit() -> None:
    tracker = _tracker_with_source_edit()
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == ["no_execution_after_final_edit"]


def test_stash_reversal_red_is_not_recorded() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_execution(
        "git stash && python repro.py", red=True, exit_code=1, iteration=2
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    # The expected-red stash run neither fires a red trigger nor counts as
    # post-edit verification.
    assert observation.triggers == ["no_execution_after_final_edit"]
    assert observation.post_edit_execution_count == 0


def test_stash_state_carries_across_separate_calls() -> None:
    # ``git stash`` in one call and ``pytest`` in the next behave exactly
    # like ``git stash && pytest`` in one call: the red run happens on the
    # reverted tree and is expected, so it is not failure evidence.
    tracker = _tracker_with_source_edit()
    tracker.observe_execution("git stash", red=False, exit_code=0, iteration=2)
    tracker.observe_execution("pytest tests/test_a.py", red=True, exit_code=1, iteration=3)
    tracker.observe_execution("git stash pop", red=False, exit_code=0, iteration=4)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == ["no_execution_after_final_edit"]
    assert observation.post_edit_execution_count == 0

    # After the fix is restored, a red run counts again.
    tracker.observe_execution("pytest tests/test_a.py", red=True, exit_code=1, iteration=5)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers[0] == "red_execution_after_final_edit"


def test_green_run_alongside_rm_does_not_credit_deleted_artifact() -> None:
    # ``rm repro.py && pytest tests/`` must not attribute the green pytest
    # result to the deleted script: the never-green deletion still fires.
    tracker = _tracker_with_source_edit()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=True, exit_code=1, iteration=3
    )
    tracker.observe_execution(
        "rm /tmp/squilla-scratch/repro.py && pytest tests/",
        red=False,
        exit_code=0,
        iteration=4,
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == ["never_green_repro_deleted"]
    assert observation.deleted_never_green_repro_paths == ["/tmp/squilla-scratch/repro.py"]


def test_detector_findings_exit_is_demoted_in_tracker() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_execution("rubocop app.rb", red=True, exit_code=1, iteration=2)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []
    assert observation.post_edit_execution_count == 1
    assert observation.post_edit_red_count == 0


def test_inspection_command_is_not_recorded() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_execution("git diff && git status", red=True, exit_code=1, iteration=2)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == ["no_execution_after_final_edit"]


# ---------------------------------------------------------------------------
# Non-verification executions (denied / harness errors / form errors)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_reason", "exit_code"),
    [
        # Sandbox / policy blocks and approval denials: nothing ran.
        ("denied", None),
        ("approval_denied", None),
        # Harness-internal tool errors and cancellations: no signal.
        ("runtime_error", None),
        ("cancelled", None),
        # Harness-level blocks minted with "The tool was not run".
        ("provider_context_projection_reused", None),
        ("projected_diagnostic_requires_retrieval", None),
        ("tool_failure_loop_exhausted", None),
        ("tool_run_budget_exhausted", None),
        ("invalid_tool_arguments", None),
        # Launched or awaiting approval: no outcome in either polarity.
        ("background_running", None),
        ("approval_pending", None),
        # Command not found / not executable: says nothing about the patch.
        ("nonzero_exit", 127),
        ("nonzero_exit", 126),
    ],
)
def test_non_verification_red_is_not_recorded(
    status_reason: str, exit_code: int | None
) -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_execution(
        "pytest tests/test_a.py",
        red=True,
        exit_code=exit_code,
        status_reason=status_reason,
        iteration=2,
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    # Like the stash skip: neither red evidence nor post-edit verification.
    assert observation.triggers == ["no_execution_after_final_edit"]
    assert observation.post_edit_execution_count == 0


@pytest.mark.parametrize(
    ("status_reason", "exit_code", "timed_out"),
    [
        # Genuine test/build failures must stay red.
        ("nonzero_exit", 1, False),
        ("nonzero_exit", 2, False),
        # Adapter-detected masked pipeline failures are legitimate red.
        ("masked_pipeline_failure", 0, False),
        # A timeout can be a patch-induced hang: stays red.
        ("tool_timeout", None, True),
        ("killed", None, False),
    ],
)
def test_genuine_verification_failures_stay_red(
    status_reason: str, exit_code: int | None, timed_out: bool
) -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_execution(
        "pytest tests/test_a.py",
        red=True,
        exit_code=exit_code,
        timed_out=timed_out,
        status_reason=status_reason,
        iteration=2,
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers[0] == "red_execution_after_final_edit"
    assert observation.post_edit_red_count == 1


def test_denied_rm_does_not_mark_artifact_deleted() -> None:
    # A policy-denied ``rm`` deleted nothing: the repro still exists and must
    # stay an outstanding red, not become a never-green deletion.
    tracker = _tracker_with_source_edit()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=True, exit_code=1, iteration=3
    )
    tracker.observe_execution(
        "rm /tmp/squilla-scratch/repro.py",
        red=True,
        status_reason="denied",
        iteration=4,
    )
    tracker.observe_execution("pytest tests/test_other.py", red=False, exit_code=0, iteration=5)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == ["red_repro_outstanding_after_final_edit"]
    assert observation.deleted_never_green_repro_paths == []


def test_rm_before_form_error_exit_still_marks_artifact_deleted() -> None:
    # Two-tier skip placement: exit 126/127 skips the execution *record*, but
    # side effects from earlier segments of the same call really happened —
    # in ``rm repro.py && ./missing.sh`` the rm ran before the 127.
    tracker = _tracker_with_source_edit()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=True, exit_code=1, iteration=3
    )
    tracker.observe_execution(
        "rm /tmp/squilla-scratch/repro.py && ./missing.sh",
        red=True,
        exit_code=127,
        iteration=4,
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert "never_green_repro_deleted" in observation.triggers
    assert observation.deleted_never_green_repro_paths == [
        "/tmp/squilla-scratch/repro.py"
    ]


def test_denied_git_stash_does_not_flip_stash_state() -> None:
    # A policy-denied ``git stash`` stashed nothing: the fix is still in the
    # tree, so a following red run is genuine evidence about the workspace
    # and must not be skipped as a stash-reversal bug check.
    tracker = _tracker_with_source_edit()
    tracker.observe_execution("git stash", red=True, status_reason="denied", iteration=2)
    tracker.observe_execution("pytest tests/test_a.py", red=True, exit_code=1, iteration=3)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers[0] == "red_execution_after_final_edit"
    assert observation.post_edit_red_count == 1


# ---------------------------------------------------------------------------
# Scratch-note writes (non-source, non-artifact)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/squilla-scratch/FIX_SUMMARY.md",
        "/tmp/notes.txt",
        "/var/tmp/OBSERVATIONS.md",
    ],
)
def test_nonsource_scratch_note_write_does_not_reset_window(path: str) -> None:
    # coreutils-6682 live defect: a plain write of FIX_SUMMARY.md under the
    # scratch dir was treated as the final source edit, producing a false
    # no_execution_after_final_edit challenge.
    tracker = _tracker_with_source_edit()
    tracker.observe_execution("pytest tests/", red=False, exit_code=0, iteration=2)
    tracker.observe_write(path, iteration=3)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []
    assert observation.post_edit_execution_count == 1


def test_repo_doc_write_still_counts_as_source_edit() -> None:
    # Non-script files under the repository tree (changelogs, docs) are real
    # edits and must arm/reset the gate as before.
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write("docs/CHANGES.rst", iteration=1)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.source_edit_seen is True
    assert observation.triggers == ["no_execution_after_final_edit"]


@pytest.mark.parametrize(
    "path",
    [
        # A repo path merely containing a ``tmp`` directory segment.
        "/testbed/tests/tmp/expected_output.txt",
        # A relative ``tmp/`` path resolves under the repo cwd, not /tmp.
        "tmp/config.yml",
    ],
)
def test_repo_tmp_like_path_still_counts_as_source_edit(path: str) -> None:
    # The scratch-note skip is anchored to absolute /tmp, /var/tmp, and
    # squilla-scratch dirs; it must not over-match tmp-named repo paths.
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write(path, iteration=1)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.source_edit_seen is True


def test_pathless_write_still_counts_as_source_edit() -> None:
    # ``apply_patch`` carries no single path argument; it must keep counting
    # as a source edit.
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write(None, iteration=1)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.source_edit_seen is True


# ---------------------------------------------------------------------------
# Green reruns never auto-discharge reds (supersession deliberately deferred)
# ---------------------------------------------------------------------------


def test_superset_green_rerun_does_not_discharge_red_majority() -> None:
    # Deliberate polarity choice: a broader green suite run does NOT discharge
    # repeated reds of a narrower selection — failing self-repros stay binding
    # (the retired patch-evidence protocol showed the harm of teaching the
    # model that unrelated/broader greens override its own red). Only an exact
    # green rerun of the tracked artifact clears it (``ever_green``); anything
    # else goes through the challenge's escape hatch.
    tracker = _tracker_with_source_edit()
    for index in range(5):
        tracker.observe_execution(
            "gradle test --tests org.foo.BarTest",
            red=True,
            exit_code=1,
            iteration=2 + index,
        )
    tracker.observe_execution("gradle test", red=False, exit_code=0, iteration=7)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == ["red_majority_after_final_edit"]


def test_narrower_green_rerun_does_not_discharge_red_majority() -> None:
    tracker = _tracker_with_source_edit()
    for index in range(5):
        tracker.observe_execution(
            "pytest tests/test_a.py", red=True, exit_code=1, iteration=2 + index
        )
    tracker.observe_execution(
        "pytest tests/test_a.py -k 'not test_bad'", red=False, exit_code=0, iteration=7
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    # A deselecting green discharges nothing: both the deselection and the
    # red majority fire.
    assert "red_evidence_deselected_after_final_edit" in observation.triggers
    assert "red_majority_after_final_edit" in observation.triggers


# ---------------------------------------------------------------------------
# Dedup key and challenge messages
# ---------------------------------------------------------------------------


def _observation_with_red(command: str):
    tracker = _tracker_with_source_edit()
    tracker.observe_execution(command, red=True, exit_code=1, iteration=2)
    return tracker.build_observation(has_workspace_diff=True)


def test_gate_key_stable_for_same_state_and_distinct_for_new_red() -> None:
    first = _observation_with_red("pytest tests/test_a.py")
    second = _observation_with_red("pytest tests/test_a.py")
    third = _observation_with_red("pytest tests/test_b.py")
    assert finalize_evidence_gate_key(first) == finalize_evidence_gate_key(second)
    assert finalize_evidence_gate_key(first) != finalize_evidence_gate_key(third)


def test_gate_key_ignores_volatile_post_edit_execution_count() -> None:
    # Regression: the dedup key must be derived from the red *evidence* only, not
    # from progress counters. If it hashed ``post_edit_execution_count`` the
    # caller's ``keys.add(key)`` guard would mint a fresh key on every execution
    # and re-fire the one-shot challenge indefinitely for the same failing state.
    base = _observation_with_red("pytest tests/test_a.py")
    later = replace(base, post_edit_execution_count=base.post_edit_execution_count + 5)
    assert later.post_edit_execution_count != base.post_edit_execution_count
    assert finalize_evidence_gate_key(base) == finalize_evidence_gate_key(later)


def test_challenge_messages_include_one_shot_notice() -> None:
    # Every challenge variant tells the model the check is one-shot for this
    # exact failing state, so a recurring challenge is not read as pressure to
    # keep re-finalizing.
    red = _observation_with_red("pytest tests/test_a.py")

    deleted_tracker = _tracker_with_source_edit()
    deleted_tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=2)
    deleted_tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=True, exit_code=1, iteration=3
    )
    deleted_tracker.observe_execution(
        "rm /tmp/squilla-scratch/repro.py", red=False, iteration=4
    )
    deleted_tracker.observe_execution("pytest tests/", red=False, exit_code=0, iteration=5)
    deleted = deleted_tracker.build_observation(has_workspace_diff=True)

    no_execution = _tracker_with_source_edit().build_observation(has_workspace_diff=True)

    for observation in (red, deleted, no_execution):
        message = finalize_evidence_challenge_message(observation)
        assert "one-shot for this exact failing state" in message
        assert "it will not repeat for the same evidence" in message


def test_challenge_message_red_execution_polarity() -> None:
    observation = _observation_with_red("pytest tests/test_a.py")
    message = finalize_evidence_challenge_message(observation)
    assert message.startswith("[Finalize evidence check]")
    assert "pytest tests/test_a.py" in message
    assert "exit code 1" in message
    assert "binding evidence" in message
    assert "do not override it" in message
    assert "Do not finalize yet" in message
    # Escape hatch: an invalid command may be justified instead of chased.
    assert "explicitly justify" in message


def test_challenge_message_never_green_repro_deleted() -> None:
    tracker = _tracker_with_source_edit()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=True, exit_code=1, iteration=3
    )
    tracker.observe_execution("rm /tmp/squilla-scratch/repro.py", red=False, iteration=4)
    tracker.observe_execution("pytest tests/", red=False, exit_code=0, iteration=5)
    message = finalize_evidence_challenge_message(
        tracker.build_observation(has_workspace_diff=True)
    )
    assert "/tmp/squilla-scratch/repro.py" in message
    assert "deleted before a passing run" in message
    assert "binding evidence" in message


def test_challenge_message_no_execution() -> None:
    tracker = _tracker_with_source_edit()
    message = finalize_evidence_challenge_message(
        tracker.build_observation(has_workspace_diff=True)
    )
    assert "no execution-level command ran after your final source edit" in message


def test_challenge_messages_never_use_protocol_polarity() -> None:
    """The retired patch-evidence protocol harmed runs by demanding minimality
    and devaluing self-written repros; no challenge message may echo either."""

    tracker = _tracker_with_source_edit()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=True, exit_code=1, iteration=3
    )
    strict_red_first = _strict_tracker_with_source_edit()
    strict_red_first.observe_execution(
        "pytest tests/test_a.py", red=False, exit_code=0, iteration=2
    )
    observations = [
        tracker.build_observation(has_workspace_diff=True),
        _observation_with_red("pytest tests/test_a.py"),
        _tracker_with_source_edit().build_observation(has_workspace_diff=True),
        # Strict shapes: red-first-missing and zero-verification.
        strict_red_first.build_observation(has_workspace_diff=True),
        _strict_tracker_with_source_edit().build_observation(has_workspace_diff=True),
    ]
    for observation in observations:
        message = finalize_evidence_challenge_message(observation).lower()
        assert "minimal" not in message
        assert "localized" not in message
        assert "not sufficient" not in message


def test_challenge_limit_constant() -> None:
    assert FINALIZE_EVIDENCE_GATE_CHALLENGE_LIMIT == 2


# ---------------------------------------------------------------------------
# Strict mode: zero_verification trigger; red-first tracked but report-only
# ---------------------------------------------------------------------------


def _strict_tracker_with_source_edit() -> FinalizeEvidenceTracker:
    tracker = FinalizeEvidenceTracker(strict=True)
    tracker.observe_write("src/main.py", iteration=1)
    return tracker


def test_strict_triggers_never_fire_without_strict() -> None:
    tracker = _tracker_with_source_edit()
    observation = tracker.build_observation(has_workspace_diff=True)
    assert "red_first_missing" not in observation.triggers
    assert "zero_verification" not in observation.triggers
    assert observation.strict is False
    # Evidence fields are still populated for offline comparison.
    assert observation.verification_command_count == 0
    assert observation.red_first_satisfied is False


def test_strict_zero_verification_fires_on_zero_execution_run() -> None:
    observation = _strict_tracker_with_source_edit().build_observation(
        has_workspace_diff=True
    )
    assert observation.triggers == [
        "no_execution_after_final_edit",
        "zero_verification",
    ]
    assert observation.primary_reason == "no_execution_after_final_edit"
    assert observation.verification_command_count == 0


def test_strict_zero_verification_ignores_inspection_and_denied_commands() -> None:
    tracker = _strict_tracker_with_source_edit()
    tracker.observe_execution("grep -r foo src/", red=False, exit_code=0, iteration=2)
    tracker.observe_execution(
        "pytest tests/", red=True, exit_code=1, status_reason="denied", iteration=3
    )
    tracker.observe_execution("./missing.sh", red=True, exit_code=127, iteration=4)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert "zero_verification" in observation.triggers
    assert observation.verification_command_count == 0


def test_strict_green_only_run_does_not_challenge() -> None:
    # Green-only runs are routinely legitimate (direct fix + existing suite
    # green), so red-first state is reported for diagnostics but never gates.
    # Replay over real runs measured 41% of stable solved traces as
    # never-red; a trigger here would challenge correct finishes constantly.
    tracker = _strict_tracker_with_source_edit()
    tracker.observe_execution("pytest tests/test_a.py", red=False, exit_code=0, iteration=2)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []
    assert observation.should_challenge is False
    assert observation.verification_command_count == 1
    assert observation.red_first_satisfied is False
    assert observation.red_first_candidate_count == 0


def test_strict_red_first_satisfied_by_pre_edit_red_then_green() -> None:
    tracker = FinalizeEvidenceTracker(strict=True)
    tracker.observe_execution("pytest tests/test_a.py", red=True, exit_code=1, iteration=1)
    tracker.observe_write("src/main.py", iteration=2)
    tracker.observe_execution("pytest tests/test_a.py", red=False, exit_code=0, iteration=3)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []
    assert observation.red_first_satisfied is True
    assert observation.red_first_candidate_count == 1


def test_strict_red_first_satisfied_by_post_edit_red_then_green() -> None:
    # Legitimate edit-then-reproduce flow: the red arrives after the first
    # source edit and later goes green.
    tracker = _strict_tracker_with_source_edit()
    tracker.observe_execution("pytest tests/test_a.py", red=True, exit_code=1, iteration=2)
    tracker.observe_execution("pytest tests/test_a.py", red=False, exit_code=0, iteration=3)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []
    assert observation.red_first_satisfied is True


def test_strict_red_first_satisfied_by_stash_reverted_red() -> None:
    # ``git stash && pytest`` red demonstrates the failure without the patch;
    # the green re-run after ``git stash pop`` completes red-first.
    tracker = _strict_tracker_with_source_edit()
    tracker.observe_execution(
        "git stash && pytest tests/test_a.py", red=True, exit_code=1, iteration=2
    )
    tracker.observe_execution(
        "git stash pop && pytest tests/test_a.py", red=False, exit_code=0, iteration=3
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []
    assert observation.red_first_satisfied is True
    assert observation.red_first_candidate_count == 1


def test_strict_red_first_not_satisfied_by_deselecting_green() -> None:
    tracker = _strict_tracker_with_source_edit()
    tracker.observe_execution("pytest tests/test_a.py", red=True, exit_code=1, iteration=2)
    tracker.observe_execution(
        "pytest tests/test_a.py -k 'not test_bad'", red=False, exit_code=0, iteration=3
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.red_first_satisfied is False
    # The base deselection trigger still fires and stays primary; red-first
    # state itself adds nothing.
    assert observation.primary_reason == "red_evidence_deselected_after_final_edit"


def test_strict_red_first_satisfied_by_artifact_green_rerun() -> None:
    tracker = _strict_tracker_with_source_edit()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=True, exit_code=1, iteration=3
    )
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py", red=False, exit_code=0, iteration=4
    )
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []
    assert observation.red_first_satisfied is True


def test_strict_red_first_matches_focused_red_to_wider_green() -> None:
    tracker = FinalizeEvidenceTracker(strict=True)
    tracker.observe_execution(
        "pytest tests/test_a.py::test_bad", red=True, exit_code=1, iteration=1
    )
    tracker.observe_write("src/main.py", iteration=2)
    tracker.observe_execution("pytest tests/", red=False, exit_code=0, iteration=3)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.triggers == []
    assert observation.red_first_satisfied is True


def test_strict_red_first_unmatched_green_does_not_satisfy() -> None:
    tracker = FinalizeEvidenceTracker(strict=True)
    tracker.observe_execution("pytest tests/test_a.py", red=True, exit_code=1, iteration=1)
    tracker.observe_write("src/main.py", iteration=2)
    tracker.observe_execution("make build", red=False, exit_code=0, iteration=3)
    observation = tracker.build_observation(has_workspace_diff=True)
    assert observation.red_first_satisfied is False
    assert observation.triggers == []


def test_strict_triggers_suppressed_without_diff_or_source_edit() -> None:
    no_diff = _strict_tracker_with_source_edit().build_observation(
        has_workspace_diff=False
    )
    assert no_diff.triggers == []
    no_edit = FinalizeEvidenceTracker(strict=True).build_observation(
        has_workspace_diff=True
    )
    assert no_edit.triggers == []


def test_strict_and_base_observations_identical_apart_from_strict_fields() -> None:
    # Default-off safety: for the same event stream the strict tracker only
    # ever APPENDS triggers; every base field matches the base tracker.
    def feed(tracker: FinalizeEvidenceTracker) -> None:
        tracker.observe_write("src/main.py", iteration=1)
        tracker.observe_execution(
            "pytest tests/test_a.py", red=True, exit_code=1, iteration=2
        )

    base = FinalizeEvidenceTracker()
    strict = FinalizeEvidenceTracker(strict=True)
    feed(base)
    feed(strict)
    base_details = base.build_observation(has_workspace_diff=True).to_event_details()
    strict_details = strict.build_observation(has_workspace_diff=True).to_event_details()
    assert strict_details["triggers"][: len(base_details["triggers"])] == (
        base_details["triggers"]
    )
    for key, value in base_details.items():
        if key in {"triggers", "strict", "should_challenge", "primary_reason"}:
            continue
        assert strict_details[key] == value, key


def test_green_profiles_recover_red_requires_same_runner() -> None:
    red = command_execution_profiles("pytest tests/test_a.py")
    green = command_execution_profiles("tox")
    assert green_profiles_recover_red(green, red) is False


def test_green_profiles_recover_red_rejects_added_deselection() -> None:
    red = command_execution_profiles("pytest tests/test_a.py")
    green = command_execution_profiles("pytest tests/test_a.py -k 'not test_bad'")
    assert green_profiles_recover_red(green, red) is False


def test_green_profiles_recover_red_matches_module_and_direct_launch() -> None:
    red = command_execution_profiles("python -m pytest tests/test_a.py")
    green = command_execution_profiles("pytest tests/test_a.py")
    assert green_profiles_recover_red(green, red) is True


def test_green_profiles_recover_red_covers_directory_prefix() -> None:
    red = command_execution_profiles("pytest tests/test_a.py::test_bad")
    green = command_execution_profiles("pytest tests")
    assert green_profiles_recover_red(green, red) is True


def test_strict_challenge_message_zero_verification() -> None:
    observation = _strict_tracker_with_source_edit().build_observation(
        has_workspace_diff=True
    )
    assert "zero_verification" in observation.triggers
    message = finalize_evidence_challenge_message(observation)
    assert message.startswith("[Finalize evidence check]")
    assert "no execution-level command ran at any point" in message
    assert "binding evidence" in message


def test_strict_gate_key_differs_between_strict_and_base_state() -> None:
    base = _tracker_with_source_edit().build_observation(has_workspace_diff=True)
    strict = _strict_tracker_with_source_edit().build_observation(
        has_workspace_diff=True
    )
    assert finalize_evidence_gate_key(base) != finalize_evidence_gate_key(strict)
