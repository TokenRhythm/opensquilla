from __future__ import annotations

import json
import runpy
from typing import Any

import pytest

GATE_MODULE: dict[str, Any] = runpy.run_path(
    ".github/scripts/check_ci_results.py", run_name="check_ci_results"
)
JOB_RESULT_LABELS: dict[str, str] = GATE_MODULE["JOB_RESULT_LABELS"]
KNOWN_SUITES: frozenset[str] = GATE_MODULE["KNOWN_SUITES"]
SUITE_RESULT_REQUIREMENTS: dict[str, tuple[str, ...]] = GATE_MODULE[
    "SUITE_RESULT_REQUIREMENTS"
]
check_ci_results = GATE_MODULE["check_ci_results"]

BASELINE_SUITES = {"readme-locale", "workflow-lint"}


def _env_for(suites: set[str]) -> dict[str, str]:
    required_results = {
        variable
        for suite in suites
        for variable in SUITE_RESULT_REQUIREMENTS[suite]
    }
    env = {
        "RESULT_PLANNER": "success",
        "REQUIRED_SUITES": json.dumps(sorted(suites)),
    }
    env.update(
        {
            variable: "success" if variable in required_results else "skipped"
            for variable in JOB_RESULT_LABELS
        }
    )
    return env


def test_ci_result_gate_accepts_baseline_plan() -> None:
    assert check_ci_results(_env_for(BASELINE_SUITES)) == []


def test_ci_result_gate_accepts_complete_full_plan() -> None:
    assert check_ci_results(_env_for(set(KNOWN_SUITES))) == []


@pytest.mark.parametrize("suite", sorted(KNOWN_SUITES))
def test_ci_result_gate_has_an_executable_mapping_for_every_suite(suite: str) -> None:
    suites = BASELINE_SUITES | {suite}

    assert check_ci_results(_env_for(suites)) == []


def test_ci_result_gate_requires_successful_planner_job() -> None:
    for result in ("skipped", "failure", "cancelled", ""):
        env = _env_for(BASELINE_SUITES)
        env["RESULT_PLANNER"] = result

        errors = check_ci_results(env)

        assert any("Plan CI suites" in error and "success" in error for error in errors)


def test_ci_result_gate_rejects_missing_required_suites_without_legacy_fallback() -> None:
    env = _env_for(BASELINE_SUITES)
    env.pop("REQUIRED_SUITES")
    env.update(
        {
            "FLAG_FULL_REQUIRED": "true",
            "FLAG_DOCS_ONLY": "false",
            "FLAG_PYTHON_CHANGED": "true",
        }
    )

    errors = check_ci_results(env)

    assert any("required_suites is missing" in error for error in errors)


@pytest.mark.parametrize(
    "suites",
    [set(), {"workflow-lint"}, {"readme-locale"}],
    ids=("empty", "missing-readme", "missing-workflow-lint"),
)
def test_ci_result_gate_requires_every_baseline_suite(suites: set[str]) -> None:
    env = _env_for(suites)

    errors = check_ci_results(env)

    assert any("omitted baseline suites" in error for error in errors)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not-json", "valid JSON"),
        (json.dumps({"workflow-lint": True}), "list of strings"),
        (json.dumps(["workflow-lint", 1]), "list of strings"),
        (json.dumps(["unknown-suite"]), "unknown suites"),
        (json.dumps(["workflow-lint", "workflow-lint"]), "duplicate-free"),
        (json.dumps(["workflow-lint", "readme-locale"]), "sorted"),
    ],
)
def test_ci_result_gate_rejects_invalid_planner_suite_contract(
    raw: str, message: str
) -> None:
    env = _env_for(BASELINE_SUITES)
    env["REQUIRED_SUITES"] = raw

    assert any(message in error for error in check_ci_results(env))


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", ""])
def test_ci_result_gate_rejects_incomplete_selected_job(result: str) -> None:
    env = _env_for(BASELINE_SUITES | {"skill-hub"})
    env["RESULT_SKILL_HUB"] = result

    errors = check_ci_results(env)

    assert any("Skill Hub contract matrix" in error and "success" in error for error in errors)


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", ""])
def test_ci_result_gate_rejects_unplanned_job_execution_or_missing_result(result: str) -> None:
    env = _env_for(BASELINE_SUITES)
    env["RESULT_SKILL_HUB"] = result

    errors = check_ci_results(env)

    assert any("Skill Hub contract matrix" in error and "skipped" in error for error in errors)


def test_ci_result_gate_requires_both_python_full_jobs() -> None:
    env = _env_for(BASELINE_SUITES | {"python-full"})
    env["RESULT_UBUNTU"] = "skipped"
    env["RESULT_UBUNTU_FULL"] = "failure"

    errors = check_ci_results(env)

    assert any("Ubuntu quality gate" in error for error in errors)
    assert any("Ubuntu full test matrix" in error for error in errors)


@pytest.mark.parametrize("suite", ["frontend-validation", "wheel-webui-roundtrip"])
def test_ci_result_gate_requires_shared_frontend_check_for_each_consumer(suite: str) -> None:
    env = _env_for(BASELINE_SUITES | {suite})
    env["RESULT_FRONTEND"] = "skipped"

    errors = check_ci_results(env)

    assert any("Frontend validation and wheel WebUI roundtrip" in error for error in errors)


def test_ci_result_gate_accepts_one_shared_frontend_check_for_both_suites() -> None:
    env = _env_for(
        BASELINE_SUITES | {"frontend-validation", "wheel-webui-roundtrip"}
    )

    assert env["RESULT_FRONTEND"] == "success"
    assert check_ci_results(env) == []


def test_frontend_validation_requires_windows_contract_determinism() -> None:
    env = _env_for(BASELINE_SUITES | {"frontend-validation"})
    env["RESULT_CONTRACT_WINDOWS"] = "skipped"

    errors = check_ci_results(env)

    assert any("Gateway Contract determinism on Windows" in error for error in errors)


def test_wheel_only_plan_does_not_run_windows_contract_determinism() -> None:
    env = _env_for(BASELINE_SUITES | {"wheel-webui-roundtrip"})

    assert env["RESULT_CONTRACT_WINDOWS"] == "skipped"
    assert check_ci_results(env) == []


def test_ci_result_gate_checks_frontend_artifact_independently() -> None:
    env = _env_for(
        BASELINE_SUITES | {"frontend-artifact", "webui-chat-recovery"}
    )
    env["RESULT_FRONTEND_ARTIFACT"] = "skipped"

    errors = check_ci_results(env)

    assert any("Frontend artifact" in error and "success" in error for error in errors)
    assert not any("WebUI chat recovery" in error for error in errors)


def test_ci_result_gate_rejects_missing_suite_result_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = check_ci_results.__globals__["SUITE_RESULT_REQUIREMENTS"]
    monkeypatch.delitem(mapping, "tui")

    errors = check_ci_results(_env_for(BASELINE_SUITES))

    assert any("mappings are missing suites: tui" in error for error in errors)


def test_ci_result_gate_rejects_unknown_job_result_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = check_ci_results.__globals__["SUITE_RESULT_REQUIREMENTS"]
    monkeypatch.setitem(mapping, "tui", ("RESULT_NOT_A_JOB",))

    errors = check_ci_results(_env_for(BASELINE_SUITES))

    assert any("unknown job results: RESULT_NOT_A_JOB" in error for error in errors)


def test_ci_result_gate_contract_has_no_legacy_or_dead_lane() -> None:
    assert "BOOLEAN_FLAGS" not in GATE_MODULE
    assert "windows-compat" not in KNOWN_SUITES
    assert "RESULT_WINDOWS_SMOKE" not in JOB_RESULT_LABELS
    assert set(SUITE_RESULT_REQUIREMENTS) == set(KNOWN_SUITES)
    assert {
        variable
        for variables in SUITE_RESULT_REQUIREMENTS.values()
        for variable in variables
    } == set(JOB_RESULT_LABELS)
