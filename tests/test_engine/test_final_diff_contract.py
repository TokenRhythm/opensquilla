from __future__ import annotations

from opensquilla.engine.final_diff_contract import (
    build_final_diff_contract_observation,
    classify_final_diff_path,
    final_diff_contract_recovery_message,
)


def test_classify_final_diff_paths_for_source_test_and_scratch() -> None:
    assert classify_final_diff_path("src/app.py") == "source"
    assert classify_final_diff_path("tests/test_app.py") == "test-like"
    assert classify_final_diff_path("all_test_cases.php") == "test-like"
    assert classify_final_diff_path("debug_case.php") == "scratch"
    assert classify_final_diff_path("debug_issue.mjs") == "scratch"
    assert classify_final_diff_path("repro_case.cjs") == "scratch"
    assert classify_final_diff_path(".phpunit.cache/test-results") == "scratch"
    assert classify_final_diff_path("src/debug/helpers.py") == "source"
    assert classify_final_diff_path("packages/parser/src/index.mjs") == "source"


def test_observation_flags_polluted_final_diff_with_source_patch() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=[
            "src/app.py",
            "debug_case.php",
            "tests/test_app.py",
        ],
        write_records=[{"relative_path": "src/app.py"}],
        mutation_records=[
            {
                "paths": [
                    {"relative_path": "debug_case.php", "classification": "scratch"},
                ]
            }
        ],
    )

    assert observation.source_paths == ["src/app.py"]
    assert observation.scratch_paths == ["debug_case.php"]
    assert observation.test_like_paths == ["tests/test_app.py"]
    assert observation.mutation_overlap_paths == ["debug_case.php"]
    assert observation.suspicious is True
    assert "scratch_artifact_in_final_diff" in observation.triggers


def test_observation_uses_configured_hidden_scratch_paths() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=[".artifacts/repro.py"],
        known_scratch_paths=[".artifacts/repro.py"],
        mutation_records=[
            {
                "paths": [
                    {
                        "relative_path": ".artifacts/repro.py",
                        "classification": "scratch",
                    }
                ]
            }
        ],
    )

    assert observation.source_paths == []
    assert observation.scratch_paths == [".artifacts/repro.py"]
    assert observation.candidate_source_paths == []
    assert observation.candidate_source_missing_paths == []
    assert observation.triggers == [
        "final_diff_without_source",
        "scratch_artifact_in_final_diff",
    ]


def test_scratch_only_records_without_diff_do_not_claim_lost_workspace_writes() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=[],
        known_scratch_paths=[".artifacts/direct.py", ".artifacts/shell.py"],
        write_records=[
            {
                "relative_path": ".artifacts/direct.py",
                "classification": "scratch",
            }
        ],
        mutation_records=[
            {
                "paths": [
                    {
                        "relative_path": ".artifacts/shell.py",
                        "classification": "scratch",
                    }
                ]
            }
        ],
        mutation_receipts=[
            {
                "relative_path": ".artifacts/direct.py",
                "classification": "scratch",
                "changed": True,
            }
        ],
    )

    assert observation.candidate_source_paths == []
    assert observation.triggers == []
    assert observation.suspicious is False


def test_observation_flags_candidate_source_drift_to_test_only_patch() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=["tests/test_issue.py"],
        write_records=[{"relative_path": "src/parser.py"}],
    )

    assert observation.source_paths == []
    assert observation.candidate_source_paths == ["src/parser.py"]
    assert observation.candidate_source_missing_paths == ["src/parser.py"]
    assert observation.triggers[:2] == [
        "final_diff_without_source",
        "candidate_source_drift",
    ]
    message = final_diff_contract_recovery_message(observation)
    assert "current repository diff looks suspicious" in message
    assert "src/parser.py" in message
    assert "tests/test_issue.py" in message


def test_observation_flags_root_debug_module_with_source_patch() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=[
            "debug_issue.mjs",
            "packages/babel-parser/src/util/expression-scope.js",
            "test_detailed.mjs",
            "test_issue.mjs",
            "test_trace.mjs",
        ],
        write_records=[
            {"relative_path": "packages/babel-parser/src/util/expression-scope.js"}
        ],
    )

    assert observation.source_paths == [
        "packages/babel-parser/src/util/expression-scope.js"
    ]
    assert observation.scratch_paths == ["debug_issue.mjs"]
    assert observation.test_like_paths == [
        "test_detailed.mjs",
        "test_issue.mjs",
        "test_trace.mjs",
    ]
    assert observation.triggers == [
        "scratch_artifact_in_final_diff",
        "test_like_heavy_final_diff",
    ]


def test_observation_accepts_source_only_diff() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=["src/parser.py"],
        write_records=[{"relative_path": "src/parser.py"}],
    )

    assert observation.suspicious is False
    assert observation.primary_reason == "final_diff_contract_ok"


def test_observation_flags_changed_source_receipt_lost_before_final() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=[],
        mutation_receipts=[
            {
                "relative_path": "src/parser.py",
                "classification": "source",
                "changed": True,
            }
        ],
    )

    assert observation.suspicious is True
    assert observation.changed_source_receipt_paths == ["src/parser.py"]
    assert observation.lost_source_mutation_paths == ["src/parser.py"]
    assert observation.primary_reason == "source_mutation_lost_before_final"
    assert "source_mutation_lost_before_final" in observation.triggers
    assert (
        observation.to_event_details()["lost_source_mutation_paths"] == ["src/parser.py"]
    )


def test_observation_includes_lost_candidate_recovery() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=[],
        mutation_receipts=[
            {
                "relative_path": "src/parser.py",
                "classification": "source",
                "changed": True,
            }
        ],
        source_diff_candidates=[
            {
                "candidate_id": "srcdiff-1",
                "paths": ["src/parser.py"],
                "lost": True,
                "restored": False,
                "patch_sha256": "abc",
            }
        ],
    )

    assert observation.primary_reason == "source_mutation_lost_before_final"
    assert observation.recoverable_candidate_ids == ["srcdiff-1"]
    assert observation.to_event_details()["recoverable_candidate_ids"] == ["srcdiff-1"]
    message = final_diff_contract_recovery_message(observation)
    assert "Recoverable source diff candidate(s): srcdiff-1" in message
    assert "restore_source_diff_candidate" not in message


def test_observation_omits_candidate_recovery_when_source_diff_remains() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=["src/parser.py"],
        mutation_receipts=[
            {
                "relative_path": "src/parser.py",
                "classification": "source",
                "changed": True,
            }
        ],
        source_diff_candidates=[
            {
                "candidate_id": "srcdiff-1",
                "paths": ["src/parser.py"],
                "lost": True,
                "restored": False,
            }
        ],
    )

    assert observation.lost_source_mutation_paths == []
    assert observation.recoverable_candidate_ids == []


def test_observation_ignores_unrelated_lost_candidate() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=[],
        mutation_receipts=[
            {
                "relative_path": "src/parser.py",
                "classification": "source",
                "changed": True,
            }
        ],
        source_diff_candidates=[
            {
                "candidate_id": "srcdiff-2",
                "paths": ["src/other.py"],
                "lost": True,
                "restored": False,
            }
        ],
    )

    assert observation.lost_source_mutation_paths == ["src/parser.py"]
    assert observation.recoverable_candidate_ids == []


def test_observation_does_not_flag_source_receipt_when_source_diff_remains() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=["src/parser.py"],
        mutation_receipts=[
            {
                "relative_path": "src/parser.py",
                "classification": "source",
                "changed": True,
            }
        ],
    )

    assert "source_mutation_lost_before_final" not in observation.triggers
    assert observation.lost_source_mutation_paths == []


def test_observation_ignores_test_like_receipt_for_source_loss() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=[],
        mutation_receipts=[
            {
                "relative_path": "tests/test_parser.py",
                "classification": "test-like",
                "changed": True,
            }
        ],
    )

    assert "source_mutation_lost_before_final" not in observation.triggers
    assert observation.changed_source_receipt_paths == []


def test_observation_records_read_source_paths_missing_from_final_diff() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=["src/app.py"],
        read_records=[
            {"relative_path": "src/app.py"},
            {"relative_path": "src/config.py"},
            {"relative_path": "tests/test_app.py"},
        ],
        write_records=[{"relative_path": "src/app.py"}],
    )

    assert observation.suspicious is False
    assert observation.read_source_paths == ["src/app.py", "src/config.py"]
    assert observation.read_source_missing_paths == ["src/config.py"]
    assert observation.to_event_details()["read_source_missing_paths"] == [
        "src/config.py"
    ]


def test_observation_flags_diagnostic_source_like_paths_with_source_patch() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=[
            "src/Fixer/PhpTag/BlankLineAfterOpeningTagFixer.php",
            ".php_cs.php",
            "check_whitespace.php",
            "long_test_path/a/b/c/d/e/file.txt",
            "this/is/a/very/deep/path/structure/with/file.txt",
        ],
        write_records=[
            {"relative_path": "src/Fixer/PhpTag/BlankLineAfterOpeningTagFixer.php"}
        ],
    )

    assert observation.source_paths == [
        "src/Fixer/PhpTag/BlankLineAfterOpeningTagFixer.php",
        ".php_cs.php",
        "check_whitespace.php",
        "long_test_path/a/b/c/d/e/file.txt",
        "this/is/a/very/deep/path/structure/with/file.txt",
    ]
    assert observation.diagnostic_source_like_paths == [
        ".php_cs.php",
        "check_whitespace.php",
        "long_test_path/a/b/c/d/e/file.txt",
        "this/is/a/very/deep/path/structure/with/file.txt",
    ]
    assert observation.actionable_source_paths == [
        "src/Fixer/PhpTag/BlankLineAfterOpeningTagFixer.php"
    ]
    assert observation.triggers == ["diagnostic_source_like_in_final_diff"]
    assert observation.primary_reason == "diagnostic_source_like_in_final_diff"
    assert observation.to_event_details()["diagnostic_source_like_count"] == 4
    assert observation.to_event_details()["actionable_source_count"] == 1
    assert observation.to_event_details()["diagnostic_source_like_only"] is False
    message = final_diff_contract_recovery_message(observation)
    assert "diagnostic/source-like paths" in message
    assert "long_test_path/a/b/c/d/e/file.txt" in message


def test_observation_flags_diagnostic_source_like_only_diff() -> None:
    observation = build_final_diff_contract_observation(
        diff_paths=[
            "analyze_output.py",
            "this/is/a/very/deep/path/structure/that/goes/on/file.txt",
        ],
        read_records=[
            {"relative_path": "src/printer.rs"},
            {"relative_path": "src/input.rs"},
        ],
    )

    assert observation.source_paths == [
        "analyze_output.py",
        "this/is/a/very/deep/path/structure/that/goes/on/file.txt",
    ]
    assert observation.diagnostic_source_like_paths == [
        "analyze_output.py",
        "this/is/a/very/deep/path/structure/that/goes/on/file.txt",
    ]
    assert observation.actionable_source_paths == []
    assert observation.triggers == ["diagnostic_source_like_in_final_diff"]
    assert observation.primary_reason == "diagnostic_source_like_in_final_diff"
    details = observation.to_event_details()
    assert details["diagnostic_source_like_count"] == 2
    assert details["actionable_source_count"] == 0
    assert details["diagnostic_source_like_only"] is True
    assert details["candidate_actionable_source_missing_paths"] == [
        "src/printer.rs",
        "src/input.rs",
    ]
    assert details["read_actionable_source_missing_paths"] == [
        "src/printer.rs",
        "src/input.rs",
    ]
    assert details["candidate_actionable_source_missing_count"] == 2
    assert details["read_actionable_source_missing_count"] == 2
