from __future__ import annotations

import json
import runpy
import subprocess
from pathlib import Path
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

BASELINE_SUITES = {"dependency-audit", "readme-locale", "workflow-lint"}

AUDIT = runpy.run_path(".github/scripts/audit_dependencies.py", run_name="dependency_audit")
AuditError = AUDIT["AuditError"]


def test_python_audit_covers_optional_platform_and_multiple_locked_versions() -> None:
    expected = {("pywin32", "311"), ("platform-package", "1.0"), ("platform-package", "2.0")}
    report = {"dependencies": [
        {"name": name, "version": version, "vulns": []} for name, version in expected
    ]}
    assert AUDIT["validate_python_report"](report, expected)["coverage_complete"] is True
    report["dependencies"].pop()
    with pytest.raises(AuditError, match="coverage mismatch"):
        AUDIT["validate_python_report"](report, expected)


def test_python_audit_deduplicates_the_same_advisory_across_platform_variants() -> None:
    package = {"name": "cleaner", "version": "1", "vulns": [{"id": "PYSEC-test"}]}
    report = {"dependencies": [package, package]}
    assert AUDIT["validate_python_report"](report, {("cleaner", "1")})["vulnerability_count"] == 1


def test_npm_audit_refuses_to_scan_a_stale_lockfile() -> None:
    manifest = {"dependencies": {"runtime": "^2"}}
    lock = {"packages": {"": {"dependencies": {"runtime": "^1"}}}}
    with pytest.raises(AuditError, match="disagree"):
        AUDIT["validate_npm_manifest"](manifest, lock)


@pytest.mark.parametrize("record", [
    {"name": "missing", "skip_reason": "not found"},
    {"name": "missing", "version": "1", "skip_reason": "not found", "vulns": []},
    {"name": "missing", "version": "1"},
])
def test_python_audit_never_treats_skipped_packages_as_clean(record: dict) -> None:
    with pytest.raises(AuditError):
        AUDIT["validate_python_report"]({"dependencies": [record]}, {("missing", "1")})


def _npm_evidence() -> tuple[dict, dict]:
    lock = {"lockfileVersion": 3, "packages": {
        "": {"name": "app", "version": "1"},
        "node_modules/dev-only": {"version": "1", "dev": True},
        "node_modules/optional": {"version": "1", "optional": True},
    }}
    report = {
        "auditReportVersion": 2, "vulnerabilities": {},
        "metadata": {"vulnerabilities": {"total": 0}, "dependencies": {"total": 2}},
    }
    return report, lock


def test_npm_audit_requires_full_development_and_optional_coverage() -> None:
    report, lock = _npm_evidence()
    assert AUDIT["validate_npm_report"](report, lock)["package_locations"] == 2
    report["metadata"]["dependencies"]["total"] = 1
    with pytest.raises(AuditError, match="coverage mismatch"):
        AUDIT["validate_npm_report"](report, lock)


def test_npm_report_keeps_dev_findings_and_deduplicates_advisory_count() -> None:
    report, lock = _npm_evidence()
    report["vulnerabilities"] = {
        "dev-only": {"via": [{"source": 123, "url": "https://example.invalid/advisory"}],
                     "nodes": ["node_modules/dev-only"]},
        "optional": {"via": ["dev-only"], "nodes": ["node_modules/optional"]},
    }
    report["metadata"]["vulnerabilities"]["total"] = 2
    result = AUDIT["validate_npm_report"](report, lock)
    assert result["vulnerability_count"] == 2
    assert result["distinct_advisory_count"] == 1
    assert result["findings"][0]["packages"][0]["dev"] is True


@pytest.mark.parametrize(
    "mutation", ["network", "missing-count", "missing-nodes", "unknown-schema"],
)
def test_npm_audit_rejects_incomplete_or_network_error_reports(mutation: str) -> None:
    report, lock = _npm_evidence()
    if mutation == "network":
        report["error"] = {"code": "ENETUNREACH"}
    elif mutation == "missing-count":
        del report["metadata"]
    elif mutation == "unknown-schema":
        report["auditReportVersion"] = 100
    else:
        report["vulnerabilities"] = {"absent": {"via": [], "nodes": ["absent"]}}
        report["metadata"]["vulnerabilities"]["total"] = 1
    with pytest.raises(AuditError):
        AUDIT["validate_npm_report"](report, lock)


def test_artifact_audit_excludes_only_local_project_and_explicit_build_only(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({
        "schemaVersion": 1, "kind": "pyinstaller", "lockSha256": "abc",
        "packages": [
            {"name": "OpenSquilla", "version": "1", "bundled": True},
            {"name": "pyinstaller", "version": "6", "bundled": False},
            {"name": "some_project", "version": "2", "bundled": True},
        ],
    }), encoding="utf-8")
    target = tmp_path / "pylock.toml"
    exclusions = AUDIT["inventory_pylock"](inventory, target, "abc")
    assert AUDIT["pylock_inventory"](target) == {("some-project", "2")}
    assert {entry["name"] for entry in exclusions} == {"opensquilla", "pyinstaller"}
    with pytest.raises(AuditError, match="SHA256"):
        AUDIT["inventory_pylock"](inventory, target, "different-lock")


def _python_export_repo(tmp_path: Path, *, conflicting: bool = True) -> tuple[Path, Path]:
    repo, output = tmp_path / "repo", tmp_path / "out"
    repo.mkdir()
    output.mkdir()
    manifest = '[project]\nname = "OpenSquilla"\n'
    if conflicting:
        manifest += (
            '[tool.uv]\nconflicts = [[{ group = "dev" }, '
            '{ group = "legacy-contract-codegen" }]]\n'
        )
    (repo / "pyproject.toml").write_text(manifest, encoding="utf-8")
    lock = '[[package]]\nname = "opensquilla"\nversion = "1"\nsource = {editable = "."}\n'
    for name, version in [("generator", "1"), ("generator", "2"), ("pywin32", "311")]:
        lock += (
            f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n'
            'source = {registry = "https://pypi.org/simple"}\n'
        )
    (repo / "uv.lock").write_text(lock, encoding="utf-8")
    return repo, output


EXPORT_POLICY = {"timeout_seconds": 1, "uv_version": "0.12.15", "pip_audit_version": "2.10.1"}
EXPORT_PACKAGES = {("generator", "1"), ("generator", "2"), ("pywin32", "311")}


@pytest.mark.parametrize("conflicting", [False, True])
@pytest.mark.parametrize("vulnerable_legacy", [False, True])
def test_locked_audit_includes_every_exported_generator_and_platform_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflicting: bool, vulnerable_legacy: bool,
) -> None:
    repo, output = _python_export_repo(tmp_path, conflicting=conflicting)
    export_calls = []

    def fake_run(args: list[str], *, cwd: Path, **kwargs: Any) -> subprocess.CompletedProcess:
        assert "--locked" in args
        if "export" in args:
            assert args[:5] == ["uvx", "--from", "uv==0.12.15", "uv", "export"]
            export_calls.append(args)
            packages = EXPORT_PACKAGES
            if "--no-group" in args:
                assert args[args.index("--no-group") + 1] == "legacy-contract-codegen"
                assert "--all-groups" in args and "--all-extras" in args
                packages = EXPORT_PACKAGES - {("generator", "1")}
            elif "--only-group" in args:
                assert args[args.index("--only-group") + 1] == "legacy-contract-codegen"
                packages = {("generator", "1")}
            AUDIT["write_inventory_pylock"](Path(args[args.index("--output-file") + 1]), packages)
            return subprocess.CompletedProcess(args, 0, "", "locked export diagnostic")
        assert args[:4] == ["uvx", "--from", "pip-audit==2.10.1", "pip-audit"]
        assert "--strict" in args and "--ignore-vuln" not in args
        assert not (cwd / "pyproject.toml").exists() and not (cwd / "uv.lock").exists()
        assert list(cwd.glob("*.toml")) == [cwd / "pylock.toml"]
        assert AUDIT["pylock_inventory"](cwd / "pylock.toml") == EXPORT_PACKAGES
        report = {"dependencies": [
            {"name": name, "version": version, "vulns": (
                [{"id": "PYSEC-legacy-only"}] if vulnerable_legacy and version == "1" else []
            )} for name, version in sorted(EXPORT_PACKAGES)
        ]}
        (output / "python.json").write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(args, int(vulnerable_legacy), "", "")

    monkeypatch.setitem(AUDIT["audit_python"].__globals__, "run_command", fake_run)
    result = AUDIT["audit_python"](repo, output, EXPORT_POLICY, None)
    assert result["package_versions"] == 3 and result["coverage_complete"] is True
    assert result["vulnerability_count"] == int(vulnerable_legacy)
    assert len(export_calls) == (2 if conflicting else 1)
    provenance = json.loads((output / "python-export-provenance.json").read_text())
    assert provenance["uv_lock_sha256"] == AUDIT["sha256"](repo / "uv.lock")
    assert provenance["inventory_sha256"] == AUDIT["sha256"](output / "pylock.toml")
    assert provenance["package_versions"] == 3
    for exported in provenance["exports"]:
        path = output / exported["file"]
        assert exported["sha256"] == AUDIT["sha256"](path)
        assert exported["package_versions"] == len(AUDIT["pylock_inventory"](path))


@pytest.mark.parametrize("failure", [
    "ordinary-export", "legacy-export", "missing-legacy", "missing-platform", "extra-package",
])
def test_locked_export_union_fails_closed_before_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    repo, output = _python_export_repo(tmp_path)
    calls = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert "export" in args, "incomplete exports must never reach pip-audit"
        calls.append(args)
        legacy = "--only-group" in args
        label = "legacy" if legacy else "ordinary"
        if failure == f"{label}-export":
            return subprocess.CompletedProcess(args, 2, "", "locked export failed")
        packages = {("generator", "1")} if legacy else EXPORT_PACKAGES - {("generator", "1")}
        if failure == "missing-legacy" and legacy:
            packages = {("generator", "2")}
        if failure == "missing-platform":
            packages.discard(("pywin32", "311"))
        if failure == "extra-package":
            packages.add(("unlocked-package", "1"))
        AUDIT["write_inventory_pylock"](Path(args[args.index("--output-file") + 1]), packages)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setitem(AUDIT["audit_python"].__globals__, "run_command", fake_run)
    with pytest.raises(AuditError, match="exited 2|coverage mismatch"):
        AUDIT["audit_python"](repo, output, EXPORT_POLICY, None)
    assert calls
    assert not (output / "python.json").exists()
    if failure.endswith("-export"):
        label = failure.removesuffix("-export")
        assert "locked export failed" in (output / f"python-export-{label}.stderr.txt").read_text()


@pytest.mark.parametrize("source", [
    '{git = "https://example.invalid/pkg"}', '{editable = "../other"}',
    '{path = "some.whl"}', '{registry = ""}', '{}',
])
def test_registry_coverage_rejects_unhandled_sources_instead_of_dropping_them(
    tmp_path: Path, source: str,
) -> None:
    repo, _ = _python_export_repo(tmp_path)
    with (repo / "uv.lock").open("a", encoding="utf-8") as stream:
        stream.write(f'\n[[package]]\nname = "unknown"\nversion = "1"\nsource = {source}\n')
    with pytest.raises(AuditError, match="Unsupported locked package source"):
        AUDIT["registry_lock_inventory"](repo, {"project": {"name": "opensquilla"}})


def test_unknown_group_conflict_requires_an_explicit_coverage_review(tmp_path: Path) -> None:
    repo, output = _python_export_repo(tmp_path)
    manifest = repo / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace('group = "dev"', 'group = "other"'))
    with pytest.raises(AuditError, match="dependency-group conflict"):
        AUDIT["export_python_inventory"](repo, output, EXPORT_POLICY)


@pytest.mark.parametrize("failure", ["network", "malformed", "missing-platform", "vulnerability"])
def test_python_audit_command_failure_never_returns_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    repo, output = tmp_path / "repo", tmp_path / "out"
    repo.mkdir()
    output.mkdir()
    (repo / "uv.lock").write_text("lock", encoding="utf-8")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({
        "schemaVersion": 1, "kind": "wheelhouse",
        "lockSha256": AUDIT["sha256"](repo / "uv.lock"),
        "packages": [{"name": "pywin32", "version": "311", "bundled": True}],
    }), encoding="utf-8")

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert "--locked" in args and "--strict" in args
        assert "--fix" not in args and "--ignore-vuln" not in args
        if failure == "network":
            return subprocess.CompletedProcess(args, 1, "", "network unavailable")
        report = {"dependencies": [{"name": "pywin32", "version": "311", "vulns": []}]}
        if failure == "malformed":
            (output / "python.json").write_text("not JSON", encoding="utf-8")
        else:
            if failure == "missing-platform":
                report["dependencies"] = []
            if failure == "vulnerability":
                report["dependencies"][0]["vulns"] = [{"id": "PYSEC-test"}]
            (output / "python.json").write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(args, int(failure == "vulnerability"), "", "")

    monkeypatch.setitem(AUDIT["audit_python"].__globals__, "run_command", fake_run)
    policy = {"timeout_seconds": 1, "pip_audit_version": "2.10.1"}
    if failure == "vulnerability":
        assert AUDIT["audit_python"](repo, output, policy, inventory)["vulnerability_count"] == 1
    else:
        with pytest.raises(AuditError):
            AUDIT["audit_python"](repo, output, policy, inventory)


def test_dependency_audit_tool_timeout_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(AUDIT["shutil"], "which", lambda _: "tool")

    def timeout(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("tool", 1)

    monkeypatch.setattr(AUDIT["subprocess"], "run", timeout)
    with pytest.raises(AuditError, match="timed out"):
        AUDIT["run_command"](["tool"], cwd=tmp_path, timeout=1)


@pytest.mark.parametrize("scenario,expected", [
    ("clean", 0), ("vulnerable", 1), ("network", 2), ("input-changed", 2),
])
def test_dependency_audit_cli_records_fail_closed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, expected: int,
) -> None:
    repo, output = tmp_path / "repo", tmp_path / "evidence"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("project", encoding="utf-8")
    (repo / "uv.lock").write_text("lock", encoding="utf-8")

    def fake_command(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, "a" * 40, "")

    def fake_audit(*args: Any) -> dict:
        if scenario == "network":
            raise AuditError("network unavailable")
        if scenario == "input-changed":
            (repo / "uv.lock").write_text("different lock", encoding="utf-8")
        return {"coverage_complete": True, "vulnerability_count": int(scenario == "vulnerable")}

    globals_ = AUDIT["main"].__globals__
    monkeypatch.setitem(globals_, "run_command", fake_command)
    monkeypatch.setitem(globals_, "audit_python", fake_audit)
    assert AUDIT["main"]([
        "--repo", str(repo), "--output-dir", str(output), "--python-only",
    ]) == expected
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["exit_code"] == expected
    assert summary["started_at"] and summary["finished_at"]
    assert "uv.lock" in summary["inputs"]
    assert bool(summary["errors"]) == (expected == 2)


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


def _partial_env() -> dict[str, str]:
    suites = set(KNOWN_SUITES) - {"python-targeted", "frontend-validation"}
    return {
        **_env_for(suites), "GITHUB_EVENT_NAME": "merge_group", "QUEUE_PARTIAL": "true",
        "QUEUE_EVIDENCE_RESULT": "success", "QUEUE_CANARY_RESULT": "success",
        "QUEUE_SOURCE_RUN_ID": "123", "QUEUE_REUSED_SUITES": '["frontend-validation"]',
    }


def test_partial_gate_keeps_shared_frontend_job_and_requires_full_coverage() -> None:
    env = _partial_env()
    assert env["RESULT_FRONTEND"] == "success"  # Wheel roundtrip still runs.
    assert env["RESULT_CONTRACT_WINDOWS"] == "skipped"
    assert check_ci_results(env) == []


@pytest.mark.parametrize("key,value", [
    ("QUEUE_EVIDENCE_RESULT", "failure"), ("QUEUE_EVIDENCE_RESULT", "skipped"),
    ("QUEUE_CANARY_RESULT", "cancelled"), ("QUEUE_CANARY_RESULT", "failure"),
    ("QUEUE_SOURCE_RUN_ID", ""), ("QUEUE_SOURCE_RUN_ID", "0"),
    ("GITHUB_EVENT_NAME", "pull_request"), ("QUEUE_REUSED_SUITES", "[]"),
    ("QUEUE_REUSED_SUITES", '["windows-high-risk"]'),
    ("QUEUE_REUSED_SUITES", '["frontend-validation","frontend-validation"]'),
    ("QUEUE_REUSED_SUITES", "null"), ("QUEUE_REUSED_SUITES", '[{}]'),
    ("RESULT_WINDOWS_FULL", "skipped"), ("RESULT_WINDOWS_FULL", "failure"),
    ("RESULT_WINDOWS_NSIS", "skipped"), ("RESULT_WINDOWS_NSIS", "failure"),
    ("RESULT_WINDOWS_NSIS", "cancelled"), ("RESULT_WINDOWS_NSIS", ""),
    ("QUEUE_REUSED_SUITES", '["frontend-validation","windows-nsis-regression"]'),
    ("RESULT_FRONTEND", "failure"), ("RESULT_CONTRACT_WINDOWS", "failure"),
    ("RESULT_PLANNER", "cancelled"),
    ("QUEUE_PARTIAL", ""), ("QUEUE_PARTIAL", "false"), ("QUEUE_PARTIAL", "invalid"),
])
def test_partial_gate_cannot_hide_missing_failed_or_cancelled_checks(key: str, value: str) -> None:
    env = _partial_env()
    env[key] = value
    assert check_ci_results(env)


def test_partial_gate_rejects_an_unaccounted_suite_even_with_green_remaining_jobs() -> None:
    env = _partial_env()
    required = set(json.loads(env["REQUIRED_SUITES"])) - {"windows-high-risk"}
    env.update(_env_for(required))
    assert any("partition" in error for error in check_ci_results(env))


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", "", "neutral"])
def test_required_windows_acceptance_cannot_be_hidden_by_green_other_jobs(result: str) -> None:
    env = _env_for(BASELINE_SUITES | {"windows-nsis-regression"})
    assert check_ci_results(env) == []
    env["RESULT_WINDOWS_NSIS"] = result
    assert any("Windows packaged install" in error for error in check_ci_results(env))


def test_partial_gate_rejects_omitted_windows_acceptance() -> None:
    env = _partial_env()
    suites = set(json.loads(env["REQUIRED_SUITES"])) - {"windows-nsis-regression"}
    env.update(_env_for(suites))
    assert any("partition" in error for error in check_ci_results(env))


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", ""])
def test_frontend_requires_complete_verification_profile(result: str) -> None:
    env = _env_for(BASELINE_SUITES | {"frontend-validation"})
    env["RESULT_CONTRACT_VERIFICATION_LINUX"] = result
    assert any("Complete Gateway Contract verification" in error for error in check_ci_results(env))


def test_wheel_only_does_not_require_verification_profile() -> None:
    env = _env_for(BASELINE_SUITES | {"wheel-webui-roundtrip"})
    assert env["RESULT_CONTRACT_VERIFICATION_LINUX"] == "skipped"
    assert check_ci_results(env) == []


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
