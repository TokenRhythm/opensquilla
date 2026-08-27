from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import live_long_task_release_gate as gate


def _passing_result(
    *,
    requests: int = 1,
    tokens: int = 10,
    cost: float = 0.01,
) -> gate.DriverResult:
    return gate.DriverResult(
        status="passed",
        stage="terminal",
        physical_requests=requests,
        latency_ms=25,
        usage={"total_tokens": tokens},
        cost={"billed_cost_usd": cost},
        counts={
            "provider_legs": requests,
            "accounted_provider_legs": requests,
        },
        metrics={"first_token_ms": 10.0},
    )


def _two_cases() -> list[gate.CaseSpec]:
    return [
        gate.CaseSpec(
            case_id="deepseek-direct-one",
            provider="deepseek",
            model="deepseek-v4-flash",
            scenario="direct",
            repeat_index=1,
        ),
        gate.CaseSpec(
            case_id="tokenrhythm-direct-one",
            provider="tokenrhythm",
            model="deepseek-v4-flash",
            scenario="direct",
            repeat_index=1,
        ),
    ]


def test_mandatory_matrix_has_fixed_provider_model_and_repeat_coverage() -> None:
    cases = gate.build_mandatory_matrix()

    assert len(cases) == 74
    direct = [case for case in cases if case.scenario == "direct"]
    assert len(direct) == 18
    assert {(case.provider, case.model) for case in direct} == {
        ("deepseek", "deepseek-v4-flash"),
        ("deepseek", "deepseek-v4-pro"),
        ("tokenrhythm", "deepseek-v4-flash"),
        ("tokenrhythm", "deepseek-v4-pro"),
        ("openrouter", "deepseek/deepseek-v4-flash"),
        ("openrouter", "deepseek/deepseek-v4-pro"),
    }
    assert len([case for case in cases if case.scenario == "router"]) == 6
    assert len([case for case in cases if case.scenario == "long_reasoning"]) == 4
    assert len([case for case in cases if case.scenario in gate.FAULT_SCENARIOS]) == 24
    assert len([case for case in cases if case.scenario == "fallback"]) == 6
    assert len([case for case in cases if case.scenario in gate.BROWSER_SCENARIOS]) == 10
    assert len({case.case_id for case in cases}) == len(cases)

    router = [case for case in cases if case.scenario == "router"]
    assert {(case.provider, case.model, case.repeat_index) for case in router} == {
        (provider, models[0], repeat_index)
        for provider, models in gate.MODEL_MATRIX.items()
        for repeat_index in (1, 2)
    }
    fallback = [case for case in cases if case.scenario == "fallback"]
    assert {(case.provider, case.fallback_provider, case.repeat_index) for case in fallback} == {
        (provider, fallback_provider, repeat_index)
        for provider, fallback_provider in (
            ("tokenrhythm", "deepseek"),
            ("deepseek", "openrouter"),
            ("openrouter", "tokenrhythm"),
        )
        for repeat_index in (1, 2)
    }
    browser = [case for case in cases if case.scenario in gate.BROWSER_SCENARIOS]
    assert {case.scenario for case in browser} == set(gate.BROWSER_SCENARIOS)
    assert {(case.provider, case.model, case.repeat_index) for case in browser} == {
        ("tokenrhythm", "deepseek-v4-pro", 1)
    }

    for provider in gate.MODEL_MATRIX:
        faults = [
            case
            for case in cases
            if case.provider == provider and case.scenario in gate.FAULT_SCENARIOS
        ]
        assert len(faults) == 8
        assert {
            case.repeat_index
            for case in faults
            if case.scenario in {"fault_429_retry_after", "fault_reset_before_first_token"}
        } == {1, 2}
        assert all(
            case.repeat_index == 1
            for case in faults
            if case.scenario not in {"fault_429_retry_after", "fault_reset_before_first_token"}
        )

    assert gate.minimum_physical_requests(cases) == 117
    assert gate.BudgetLimits().physical_requests - gate.minimum_physical_requests(cases) == 3


def test_repeat_override_applies_to_each_selected_matrix_row() -> None:
    cases = gate.select_cases(
        gate.build_mandatory_matrix(repeat_override=1),
        providers=["deepseek"],
        scenarios=["direct"],
    )

    assert [(case.model, case.repeat_index) for case in cases] == [
        ("deepseek-v4-flash", 1),
        ("deepseek-v4-pro", 1),
    ]


def test_all_required_rows_must_pass() -> None:
    calls: list[str] = []

    def executor(case: gate.CaseSpec, _remaining: gate.RemainingBudget) -> gate.DriverResult:
        calls.append(case.case_id)
        if len(calls) == 2:
            return gate.DriverResult(
                status="skipped",
                stage="provider",
                physical_requests=0,
                latency_ms=0,
                usage={},
                cost={},
                counts={},
                metrics={},
                failure_class="skipped",
            )
        return _passing_result()

    report, exit_code = gate.run_gate(_two_cases(), executor=executor)

    assert exit_code == gate.EXIT_FAILED
    assert report["summary"] == {
        "status": "failed",
        "required": 2,
        "passed": 1,
        "failed": 0,
        "skipped": 1,
        "inconclusive": 0,
        "pending": 0,
    }


def test_budget_exhaustion_leaves_unexecuted_required_rows_pending() -> None:
    report, exit_code = gate.run_gate(
        _two_cases(),
        executor=lambda _case, _remaining: _passing_result(requests=1),
        limits=gate.BudgetLimits(
            wall_seconds=60,
            billed_cost_usd=30,
            physical_requests=1,
            billed_tokens=100,
        ),
    )

    assert exit_code == gate.EXIT_BUDGET
    assert report["summary"]["status"] == "budget_exhausted"
    assert report["summary"]["passed"] == 1
    assert report["summary"]["pending"] == 1
    assert report["totals"]["physical_requests"] == 1


def test_last_passing_row_cannot_exceed_a_hard_budget() -> None:
    report, exit_code = gate.run_gate(
        _two_cases()[:1],
        executor=lambda _case, _remaining: _passing_result(requests=2),
        limits=gate.BudgetLimits(
            wall_seconds=60,
            billed_cost_usd=30,
            physical_requests=1,
            billed_tokens=100,
        ),
    )

    assert report["summary"]["passed"] == 1
    assert report["summary"]["status"] == "budget_exhausted"
    assert exit_code == gate.EXIT_BUDGET


def test_exact_budget_after_completed_failed_row_remains_assertion_exit_1() -> None:
    failed = gate.DriverResult(
        status="failed",
        stage="terminal",
        physical_requests=1,
        latency_ms=1,
        usage={"total_tokens": 1},
        cost={"billed_cost_usd": 0.0},
        counts={"provider_legs": 1, "accounted_provider_legs": 1},
        metrics={},
        failure_class="assertion",
    )

    report, exit_code = gate.run_gate(
        _two_cases()[:1],
        executor=lambda _case, _remaining: failed,
        limits=gate.BudgetLimits(
            wall_seconds=60,
            billed_cost_usd=30,
            physical_requests=1,
            billed_tokens=100,
        ),
    )

    assert exit_code == gate.EXIT_FAILED
    assert report["summary"]["status"] == "failed"
    assert report["summary"]["failed"] == 1


def test_exact_budget_after_final_passing_row_is_exit_0() -> None:
    report, exit_code = gate.run_gate(
        _two_cases()[:1],
        executor=lambda _case, _remaining: _passing_result(requests=1),
        limits=gate.BudgetLimits(
            wall_seconds=60,
            billed_cost_usd=30,
            physical_requests=1,
            billed_tokens=100,
        ),
    )

    assert exit_code == gate.EXIT_PASSED
    assert report["summary"]["status"] == "passed"


def test_passed_row_without_scenario_evidence_fails_closed() -> None:
    case = gate.CaseSpec(
        case_id="deepseek-long-answer-one",
        provider="deepseek",
        model="deepseek-v4-pro",
        scenario="long_answer",
        repeat_index=1,
    )

    report, exit_code = gate.run_gate(
        [case],
        executor=lambda _case, _remaining: _passing_result(),
    )

    assert exit_code == gate.EXIT_FAILED
    assert report["rows"][0]["stage"] == "scenario_assertion"
    assert report["rows"][0]["failure_class"] == "assertion"
    # The failed assertion still accounts for the provider request and cost.
    assert report["totals"]["physical_requests"] == 1
    assert report["totals"]["billed_tokens"] == 10


def test_passed_row_without_durable_usage_leg_reconciliation_fails_closed() -> None:
    case = _two_cases()[0]
    result = _passing_result()
    result = gate.DriverResult(
        **{
            **result.__dict__,
            "counts": {"provider_legs": 1},
        }
    )

    report, exit_code = gate.run_gate(
        [case],
        executor=lambda _case, _remaining: result,
    )

    assert exit_code == gate.EXIT_FAILED
    assert report["rows"][0]["stage"] == "scenario_assertion"


def test_long_answer_requires_real_relative_performance_evidence() -> None:
    case = gate.CaseSpec(
        case_id="deepseek-long-answer-relative-evidence",
        provider="deepseek",
        model="deepseek-v4-pro",
        scenario="long_answer",
        repeat_index=1,
    )
    result = gate.DriverResult(
        status="passed",
        stage="browser",
        physical_requests=1,
        latency_ms=25,
        usage={"total_tokens": 20_000},
        cost={"billed_cost_usd": 0.01},
        counts={
            "provider_legs": 1,
            "accounted_provider_legs": 1,
            "output_bytes": 18 * 1024,
            "incremental_chunks": 10,
            "mounted_rows": 2,
            "dom_nodes": 1_000,
        },
        metrics={
            "input_next_paint_p95_ms": 20,
            "input_next_paint_max_ms": 40,
            "max_main_thread_task_ms": 50,
            "peak_heap_delta_bytes": 1_000,
            "post_gc_heap_delta_bytes": 1_000,
            "post_gc_growth_bytes": 1_000,
            "anchor_drift_px": 0,
            "bottom_gap_px": 0,
        },
    )

    report, exit_code = gate.run_gate(
        [case],
        executor=lambda _case, _remaining: result,
    )

    assert exit_code == gate.EXIT_FAILED
    assert report["rows"][0]["stage"] == "scenario_assertion"


def test_configuration_failure_suppresses_only_that_provider() -> None:
    cases = [
        gate.CaseSpec(
            case_id="deepseek-one",
            provider="deepseek",
            model="deepseek-v4-flash",
            scenario="direct",
            repeat_index=1,
        ),
        gate.CaseSpec(
            case_id="deepseek-two",
            provider="deepseek",
            model="deepseek-v4-pro",
            scenario="long_reasoning",
            repeat_index=1,
        ),
        gate.CaseSpec(
            case_id="openrouter-one",
            provider="openrouter",
            model="deepseek/deepseek-v4-flash",
            scenario="direct",
            repeat_index=1,
        ),
    ]
    called: list[str] = []

    def executor(case: gate.CaseSpec, _remaining: gate.RemainingBudget) -> gate.DriverResult:
        called.append(case.case_id)
        if case.provider == "deepseek":
            return gate.DriverResult(
                status="failed",
                stage="provider_preflight",
                physical_requests=0,
                latency_ms=0,
                usage={},
                cost={},
                counts={},
                metrics={},
                failure_class="model-unavailable",
                driver_exit_code=gate.EXIT_CONFIGURATION,
            )
        return _passing_result()

    report, exit_code = gate.run_gate(cases, executor=executor)

    assert exit_code == gate.EXIT_CONFIGURATION
    assert called == ["deepseek-one", "openrouter-one"]
    assert [row["status"] for row in report["rows"]] == ["failed", "skipped", "passed"]
    assert report["rows"][1]["failure_class"] == "provider-unavailable"


def test_provider_unavailable_suppresses_later_expensive_rows_without_exit_2() -> None:
    cases = [
        gate.CaseSpec(
            case_id="deepseek-direct-one",
            provider="deepseek",
            model="deepseek-v4-flash",
            scenario="direct",
            repeat_index=1,
        ),
        gate.CaseSpec(
            case_id="deepseek-long-reasoning-one",
            provider="deepseek",
            model="deepseek-v4-pro",
            scenario="long_reasoning",
            repeat_index=1,
        ),
    ]
    calls: list[str] = []

    def executor(case: gate.CaseSpec, _remaining: gate.RemainingBudget) -> gate.DriverResult:
        calls.append(case.case_id)
        return gate.DriverResult(
            status="failed",
            stage="provider_preflight",
            physical_requests=0,
            latency_ms=0,
            usage={},
            cost={},
            counts={},
            metrics={},
            failure_class="provider-unavailable",
        )

    report, exit_code = gate.run_gate(cases, executor=executor)

    assert exit_code == gate.EXIT_FAILED
    assert calls == ["deepseek-direct-one"]
    assert [row["status"] for row in report["rows"]] == ["failed", "skipped"]


def test_resume_does_not_repeat_passed_case_but_retries_failed_case() -> None:
    cases = _two_cases()
    first_rows = [
        gate._result_row(cases[0], _passing_result(), 1),
        gate._result_row(
            cases[1],
            gate.DriverResult(
                status="failed",
                stage="terminal",
                physical_requests=1,
                latency_ms=20,
                usage={"total_tokens": 5},
                cost={"billed_cost_usd": 0.01},
                counts={"provider_legs": 1, "accounted_provider_legs": 1},
                metrics={},
                failure_class="assertion",
            ),
            1,
        ),
    ]
    called: list[str] = []

    def executor(case: gate.CaseSpec, _remaining: gate.RemainingBudget) -> gate.DriverResult:
        called.append(case.case_id)
        return _passing_result()

    report, exit_code = gate.run_gate(
        cases,
        executor=executor,
        resumed_rows=first_rows,
        resumed_wall_time_ms=100,
    )

    assert exit_code == gate.EXIT_PASSED
    assert called == [cases[1].case_id]
    assert [row["run_attempt"] for row in report["rows"] if row["case_id"] == cases[1].case_id] == [
        1,
        2,
    ]
    assert report["summary"]["passed"] == 2
    assert report["totals"]["physical_requests"] == 3


def test_driver_result_rejects_raw_or_unbounded_data() -> None:
    valid = {
        "status": "passed",
        "stage": "terminal",
        "physical_requests": 1,
        "latency_ms": 10,
        "usage": {"total_tokens": 3},
        "cost": {"billed_cost_usd": 0.01},
        "counts": {"provider_legs": 1, "accounted_provider_legs": 1},
        "metrics": {},
        "failure_class": None,
    }

    with pytest.raises(ValueError, match="unsupported fields"):
        gate.parse_driver_result({**valid, "response": "must not survive"})
    with pytest.raises(ValueError, match="at least one physical request"):
        gate.parse_driver_result({**valid, "physical_requests": 0})
    with pytest.raises(ValueError, match="non-zero"):
        gate.parse_driver_result(valid, driver_exit_code=1)
    with pytest.raises(ValueError, match="finite"):
        gate.parse_driver_result({**valid, "cost": {"billed_cost_usd": float("nan")}})
    with pytest.raises(ValueError, match="failure_class"):
        gate.parse_driver_result({**valid, "failure_class": "assertion"})
    with pytest.raises(ValueError, match="usage.total_tokens"):
        gate.parse_driver_result({**valid, "usage": {}})
    with pytest.raises(ValueError, match="cost.billed_cost_usd"):
        gate.parse_driver_result({**valid, "cost": {}})
    with pytest.raises(ValueError, match="provider_legs"):
        gate.parse_driver_result({**valid, "counts": {}})


def test_resume_uses_highest_attempt_even_if_rows_are_reordered() -> None:
    cases = _two_cases()[:1]
    passed = gate._result_row(cases[0], _passing_result(), 2)
    failed = gate._result_row(
        cases[0],
        gate.DriverResult(
            status="failed",
            stage="terminal",
            physical_requests=1,
            latency_ms=20,
            usage={"total_tokens": 5},
            cost={"billed_cost_usd": 0.01},
            counts={"provider_legs": 1, "accounted_provider_legs": 1},
            metrics={},
            failure_class="assertion",
        ),
        1,
    )
    called: list[str] = []

    report, exit_code = gate.run_gate(
        cases,
        executor=lambda case, _remaining: called.append(case.case_id) or _passing_result(),
        resumed_rows=[passed, failed],
    )

    assert exit_code == gate.EXIT_PASSED
    assert called == []
    assert report["summary"]["passed"] == 1


def test_resume_revalidates_scenario_evidence_before_skipping_passed_row(
    tmp_path: Path,
) -> None:
    cases = _two_cases()[:1]
    incomplete = gate.DriverResult(
        status="passed",
        stage="terminal",
        physical_requests=1,
        latency_ms=1,
        usage={"total_tokens": 1},
        cost={"billed_cost_usd": 0.0},
        counts={"provider_legs": 1},
        metrics={},
    )
    report = gate._build_report(
        cases,
        [gate._result_row(cases[0], incomplete, 1)],
        gate.BudgetLimits(),
        wall_time_ms=1,
        budget_exhausted=False,
    )
    path = tmp_path / "resume.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="durable usage ledger"):
        gate._load_resume(path, cases)


def test_checkpoint_preserves_completed_rows_before_interrupt() -> None:
    cases = _two_cases()
    checkpoints: list[dict[str, object]] = []
    calls = 0

    def executor(
        _case: gate.CaseSpec,
        _remaining: gate.RemainingBudget,
    ) -> gate.DriverResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return _passing_result()

    with pytest.raises(KeyboardInterrupt):
        gate.run_gate(cases, executor=executor, checkpoint=checkpoints.append)

    assert len(checkpoints) == 1
    assert checkpoints[0]["summary"] == {
        "status": "failed",
        "required": 2,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "inconclusive": 0,
        "pending": 1,
    }
    assert len(checkpoints[0]["rows"]) == 1  # type: ignore[arg-type]


def test_subprocess_executor_keeps_secret_out_of_argv_case_and_result(
    tmp_path: Path,
) -> None:
    driver = tmp_path / "synthetic_driver.py"
    driver.write_text(
        """
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--case-file", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
case_text = args.case_file.read_text(encoding="utf-8")
secret = os.environ.get("DEEPSEEK_API_KEY", "")
assert secret
assert secret not in case_text
assert all(secret not in value for value in [str(args.case_file), str(args.output)])
for name in ("BASELINE", "CANDIDATE", "RESILIENCE"):
    report_path = os.environ.get(f"OPENSQUILLA_LONG_TASK_PERF_{name}_PATH", "")
    assert report_path
    assert report_path not in case_text
args.output.write_text(json.dumps({
    "status": "passed",
    "stage": "terminal",
    "physical_requests": 1,
    "latency_ms": 1,
    "usage": {"total_tokens": 2},
    "cost": {"billed_cost_usd": 0.001},
    "counts": {"provider_legs": 1, "accounted_provider_legs": 1},
    "metrics": {},
    "failure_class": None,
}), encoding="utf-8")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    case = _two_cases()[0]
    secret = "synthetic-secret-only-in-child-env"
    performance_reports = {
        name: tmp_path / f"{name}.json" for name in ("baseline", "candidate", "resilience")
    }
    executor = gate.SubprocessCaseExecutor(
        driver=driver,
        secrets={"DEEPSEEK_API_KEY": secret},
        performance_reports=performance_reports,
    )

    result = executor(
        case,
        gate.RemainingBudget(
            wall_ms=10_000,
            billed_cost_usd=1,
            physical_requests=2,
            billed_tokens=100,
        ),
    )

    assert result.status == "passed"
    assert secret not in json.dumps(result.__dict__)


def test_malformed_driver_result_is_exit_1_not_configuration(
    tmp_path: Path,
) -> None:
    driver = tmp_path / "malformed_driver.py"
    driver.write_text(
        """
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--case-file", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.write_text("not-json", encoding="utf-8")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    cases = _two_cases()[:1]
    executor = gate.SubprocessCaseExecutor(
        driver=driver,
        secrets={"DEEPSEEK_API_KEY": "synthetic-secret"},
    )

    report, exit_code = gate.run_gate(cases, executor=executor)

    assert exit_code == gate.EXIT_FAILED
    assert report["rows"][0]["stage"] == "driver_protocol"
    assert report["rows"][0]["failure_class"] == "implementation"


@pytest.mark.parametrize(
    ("remaining_wall_ms", "expected_stage", "expected_exit"),
    [
        (4 * 60 * 60 * 1000, "case_timeout", gate.EXIT_FAILED),
        (10_000, "budget", gate.EXIT_BUDGET),
    ],
)
def test_case_timeout_is_distinct_from_global_budget_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remaining_wall_ms: int,
    expected_stage: str,
    expected_exit: int,
) -> None:
    driver = tmp_path / "timeout_driver.py"
    driver.write_text("# synthetic\n", encoding="utf-8")
    executor = gate.SubprocessCaseExecutor(
        driver=driver,
        secrets={"DEEPSEEK_API_KEY": "synthetic-secret"},
    )

    def timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="synthetic", timeout=1)

    monkeypatch.setattr(gate.subprocess, "run", timeout)
    result = executor(
        _two_cases()[0],
        gate.RemainingBudget(
            wall_ms=remaining_wall_ms,
            billed_cost_usd=1,
            physical_requests=2,
            billed_tokens=100,
        ),
    )

    assert result.stage == expected_stage
    assert result.driver_exit_code == expected_exit


def test_cli_missing_required_env_is_exit_2_and_report_is_not_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "report.json"
    for env_name in gate.CREDENTIAL_ENV_BY_PROVIDER.values():
        monkeypatch.delenv(env_name, raising=False)

    exit_code = gate.main(
        [
            "--output",
            str(output),
            "--driver",
            str(tmp_path / "unused.py"),
            "--provider",
            "deepseek",
            "--scenario",
            "direct",
            "--repeat",
            "1",
        ]
    )

    assert exit_code == gate.EXIT_CONFIGURATION
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["status"] == "failed"
    assert report["summary"]["passed"] == 0
    assert report["summary"]["skipped"] == 2
    assert all(row["failure_class"] == "missing-credential" for row in report["rows"])
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600


def test_cli_rejects_an_impossible_request_budget_before_credentials_or_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "impossible-budget.json"
    for env_name in gate.CREDENTIAL_ENV_BY_PROVIDER.values():
        monkeypatch.delenv(env_name, raising=False)

    exit_code = gate.main(
        [
            "--output",
            str(output),
            "--provider",
            "deepseek",
            "--scenario",
            "direct",
            "--repeat",
            "1",
            "--max-requests",
            "1",
        ]
    )

    assert exit_code == gate.EXIT_CONFIGURATION
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["preflight"] == {
        "minimum_physical_requests": 2,
        "configured_physical_requests": 1,
        "budget_reachable": False,
    }
    assert report["summary"]["status"] == "failed"


def test_default_driver_is_checked_in() -> None:
    assert gate.DEFAULT_CASE_DRIVER.is_file()
