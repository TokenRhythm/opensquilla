from __future__ import annotations

import copy
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/nsis_packaged_proof.py"
MODULE = runpy.run_path(str(SCRIPT))
PROBES = MODULE["PROBES"]
verify_reports = MODULE["verify_reports"]
IDENTITY = {
    "GITHUB_SHA": "a" * 40, "GITHUB_WORKFLOW_SHA": "b" * 40,
    "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
}
CANDIDATE = {
    "sourceSha": IDENTITY["GITHUB_SHA"], "workflowSha": IDENTITY["GITHUB_WORKFLOW_SHA"],
    "auditRunId": "123", "runAttempt": "2", "signing": "unsigned regression only",
    "version": "0.5.5", "installerName": "OpenSquilla-0.5.5-win-x64.exe",
    **{field: str(index) * 64 for index, field in enumerate(MODULE["HASH_FIELDS"], 1)},
}
FIRST_SEND = {
    "reportType": "packaged-first-send", "schemaVersion": 2, "ok": True, "iterations": 20,
    "viewports": {"wide": 10, "tight": 10}, "rpc": {"chatSend": 40, "uniqueSessions": 20},
    "provider": {"chatRequestCount": 40}, "externalRendererRequests": 0,
    "acceptance": {"cleanupSucceeded": True, "failures": []},
}
SMOKE = (
    "Packaged Safe execution passed with windows_default; write and network denials verified.\n"
    "OpenSquilla packaged gateway smoke passed.\n"
)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _proof(probe: str) -> dict[str, Any]:
    return {
        "schema_version": 1, "probe": probe, "success": True,
        "identity": copy.deepcopy(IDENTITY), "candidate": copy.deepcopy(CANDIDATE),
        "installedIdentity": copy.deepcopy(CANDIDATE),
        "runner": {"os": "Windows", "environment": "github-hosted"},
        "stages": [
            {"stage": stage, "status": "passed",
             "startedAt": f"2026-10-17T08:00:{index * 2:02d}.0000000+00:00",
             "finishedAt": f"2026-10-17T08:00:{index * 2 + 1:02d}.0000000+00:00"}
            for index, stage in enumerate(MODULE["STAGES"])
        ],
    }


def _reports(tmp_path: Path) -> dict[str, Path]:
    paths = {probe: tmp_path / probe / "packaged-proof.json" for probe in PROBES}
    for probe, path in paths.items():
        _write(path, _proof(probe))
    _write(paths["dependency-audit"].parent / "dependency-audit/summary.json", {
        "schema_version": 1, "mode": "artifact", "exit_code": 0, "errors": [],
        "commit": CANDIDATE["sourceSha"],
        "inputs": {"python_inventory": CANDIDATE["dependencyInventorySha256"]},
        "results": {"python": {"vulnerability_count": 0}},
    })
    (paths["gateway-smoke"].parent / "gateway-smoke.log").write_text(SMOKE, encoding="utf-8")
    _write(paths["first-interaction"].parent / "first-send.log", FIRST_SEND)
    return paths


def test_three_complete_checks_cover_one_installed_candidate(tmp_path: Path) -> None:
    _reports(tmp_path)
    result = verify_reports(tmp_path, expected_identity=IDENTITY)
    assert result == {
        "coverage_complete": True, "probes": list(PROBES), "identity": IDENTITY,
        "candidate": CANDIDATE, "checks": 3,
    }


def test_current_consumer_can_use_verified_successful_prior_producer_attempt(
    tmp_path: Path,
) -> None:
    paths = _reports(tmp_path)
    for probe, path in paths.items():
        proof = _proof(probe)
        proof["candidate"]["runAttempt"] = proof["installedIdentity"]["runAttempt"] = "1"
        _write(path, proof)
    assert verify_reports(tmp_path, expected_identity=IDENTITY)["coverage_complete"]


@pytest.mark.parametrize("problem", ["missing", "extra", "duplicate", "unknown"])
def test_missing_duplicate_and_unknown_checks_cannot_pass(tmp_path: Path, problem: str) -> None:
    paths = _reports(tmp_path)
    path = paths["gateway-smoke"]
    if problem == "missing":
        path.unlink()
    elif problem == "extra":
        _write(tmp_path / "extra/packaged-proof.json", _proof("gateway-smoke"))
    else:
        _write(path, _proof("dependency-audit" if problem == "duplicate" else "unexpected"))
    with pytest.raises(ValueError, match="exactly three|packaged probe"):
        verify_reports(tmp_path)


@pytest.mark.parametrize("key", list(IDENTITY))
def test_current_execution_identity_cannot_reuse_other_run_reports(
    tmp_path: Path, key: str,
) -> None:
    _reports(tmp_path)
    expected = {**IDENTITY, key: "9" * (40 if key.endswith("SHA") else 1)}
    with pytest.raises(ValueError, match="identity mismatch"):
        verify_reports(tmp_path, expected_identity=expected)


@pytest.mark.parametrize("field,value", [
    ("sourceSha", "c" * 40), ("workflowSha", "c" * 40), ("auditRunId", "124"),
    ("runAttempt", "3"), ("runAttempt", "0"), ("signing", "signed"),
    *[(field, "invalid") for field in MODULE["HASH_FIELDS"]],
])
def test_candidate_must_bind_hashes_and_successful_current_run_producer(
    tmp_path: Path, field: str, value: str,
) -> None:
    for probe, path in _reports(tmp_path).items():
        proof = _proof(probe)
        proof["candidate"][field] = proof["installedIdentity"][field] = value
        _write(path, proof)
    with pytest.raises(ValueError, match="immutable candidate"):
        verify_reports(tmp_path)


@pytest.mark.parametrize("problem", [
    "failed", "installed-hash", "different-candidate", "different-identity", "runner",
    "missing-uninstall", "reordered", "duplicate-stage", "failed-check", "failed-uninstall",
    "reversed-time", "overlap", "missing-timezone",
])
def test_failed_or_incomplete_native_execution_cannot_pass(tmp_path: Path, problem: str) -> None:
    paths = _reports(tmp_path)
    proof = _proof("gateway-smoke")
    if problem == "failed":
        proof["success"] = False
    elif problem == "installed-hash":
        proof["installedIdentity"]["gatewaySha256"] = "c" * 64
    elif problem == "different-candidate":
        proof["candidate"]["gatewaySha256"] = "c" * 64
    elif problem == "different-identity":
        proof["identity"]["GITHUB_RUN_ATTEMPT"] = "1"
    elif problem == "runner":
        proof["runner"]["os"] = "Linux"
    elif problem == "missing-uninstall":
        proof["stages"].pop()
    elif problem == "reordered":
        proof["stages"].reverse()
    elif problem == "duplicate-stage":
        proof["stages"].append(copy.deepcopy(proof["stages"][-1]))
    elif problem.startswith("failed-"):
        proof["stages"][3 if problem == "failed-check" else 4]["status"] = "failed"
    elif problem == "reversed-time":
        proof["stages"][1]["finishedAt"] = proof["stages"][0]["finishedAt"]
    elif problem == "overlap":
        proof["stages"][1]["startedAt"] = proof["stages"][0]["startedAt"]
    else:
        proof["stages"][1]["startedAt"] = "2026-10-17T08:00:02"
    _write(paths["gateway-smoke"], proof)
    with pytest.raises(ValueError, match="incomplete|stages|stage failed"):
        verify_reports(tmp_path)


@pytest.mark.parametrize("problem", ["missing", "wrong-inventory", "vulnerability", "error"])
def test_actual_installed_dependency_inventory_audit_is_required(
    tmp_path: Path, problem: str,
) -> None:
    paths = _reports(tmp_path)
    path = paths["dependency-audit"].parent / "dependency-audit/summary.json"
    if problem == "missing":
        path.unlink()
    else:
        audit = json.loads(path.read_text())
        if problem == "wrong-inventory":
            audit["inputs"]["python_inventory"] = "0" * 64
        elif problem == "vulnerability":
            audit["results"]["python"]["vulnerability_count"] = 1
        else:
            audit["errors"].append({"message": "audit incomplete"})
        _write(path, audit)
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_reports(tmp_path)


@pytest.mark.parametrize("output", ["", SMOKE.splitlines()[1], SMOKE.splitlines()[0], SMOKE * 2])
def test_gateway_and_windows_default_safe_smoke_must_both_complete_once(
    tmp_path: Path, output: str,
) -> None:
    paths = _reports(tmp_path)
    (paths["gateway-smoke"].parent / "gateway-smoke.log").write_text(output, encoding="utf-8")
    with pytest.raises(ValueError, match="Gateway/Safe smoke"):
        verify_reports(tmp_path)


@pytest.mark.parametrize("field,value", [
    ("ok", False), ("iterations", 19), ("viewports", {"wide": 19, "tight": 1}),
    ("rpc", {"chatSend": 39, "uniqueSessions": 20}),
    ("rpc", {"chatSend": 40, "uniqueSessions": 19}),
    ("provider", {"chatRequestCount": 39}), ("externalRendererRequests", 1),
    ("acceptance", {"cleanupSucceeded": False, "failures": []}),
    ("acceptance", {"cleanupSucceeded": True, "failures": ["assertion failed"]}),
])
def test_all_twenty_first_interaction_rounds_and_cleanup_are_required(
    tmp_path: Path, field: str, value: Any,
) -> None:
    paths = _reports(tmp_path)
    _write(paths["first-interaction"].parent / "first-send.log", {**FIRST_SEND, field: value})
    with pytest.raises(ValueError, match="all 20 iterations"):
        verify_reports(tmp_path)


@pytest.mark.parametrize("copies", [0, 2])
def test_first_send_requires_exactly_one_final_report(tmp_path: Path, copies: int) -> None:
    paths = _reports(tmp_path)
    (paths["first-interaction"].parent / "first-send.log").write_text(
        "Diagnostic output {unstructured}\n" + json.dumps(FIRST_SEND) * copies, encoding="utf-8",
    )
    with pytest.raises(ValueError, match="one complete"):
        verify_reports(tmp_path)


def test_cli_writes_coverage_only_when_all_required_evidence_passes(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    paths = _reports(reports)
    output = tmp_path / "coverage.json"
    command = [sys.executable, str(SCRIPT), "verify", "--reports-dir", str(reports),
               "--output", str(output)]
    result = subprocess.run(command, env={**os.environ, **IDENTITY}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["coverage_complete"]
    output.unlink()
    paths["gateway-smoke"].unlink()
    result = subprocess.run(command, env={**os.environ, **IDENTITY}, capture_output=True, text=True)
    assert result.returncode == 2 and "exactly three" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("probe,failed_stage", [
    *[(probe, "") for probe in PROBES],
    *[(probe, "check") for probe in PROBES],
    ("gateway-smoke", "installer-identity"), ("gateway-smoke", "install"),
    ("gateway-smoke", "installed-identity"), ("gateway-smoke", "uninstall"),
])
def test_native_workflow_records_selected_check_and_cleanup_on_failures(
    tmp_path: Path, probe: str, failed_stage: str,
) -> None:
    """Execute real PowerShell orchestration with process boundaries mocked, never install apps."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell is required for the native orchestration contract")
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/windows-nsis-upgrade-regression.yml").read_text(
            encoding="utf-8"
        ),
    )
    steps = workflow["jobs"]["packaged-security"]["steps"]
    script = next(s["run"] for s in steps
                  if s.get("name") == "Verify independent installed packaged check")
    # Native version APIs require a real executable. All external process calls
    # and this one native file-version query are replaced with controlled fixtures.
    version_line = next(line for line in script.splitlines() if "$installedVersion =" in line)
    script = script.replace(version_line, "      $installedVersion = '0.5.5'")
    candidate = tmp_path / "candidate"
    _write(candidate / "audit-candidate.json", CANDIDATE)
    (candidate / CANDIDATE["installerName"]).write_bytes(b"synthetic installer, never launched")
    _write(tmp_path / "desktop/electron/package.json", {"version": "0.5.5"})
    _write(tmp_path / "first-send.json", FIRST_SEND)
    prelude = r"""
$global:LASTEXITCODE = 0
function git { $env:GITHUB_SHA }
function python {
  $global:LASTEXITCODE = 0
  $stage = if ($args[0] -like '*audit_dependencies.py') { 'check' }
    elseif ($args -contains '--root') { 'installed-identity' } else { 'installer-identity' }
  Add-Content -LiteralPath "$env:FIXTURE_ROOT/commands.log" -Value $stage
  if ($env:FAILED_STAGE -eq $stage) { $global:LASTEXITCODE = 7; return }
  if ($stage -ne 'check') { Get-Content "$env:FIXTURE_ROOT/candidate/audit-candidate.json" -Raw }
}
function Start-Process {
  param($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru)
  if ($WindowStyle -ne 'Hidden' -or -not $PassThru) { throw 'Unsafe process arguments' }
  $stage = if ($FilePath -like '*Uninstall*') { 'uninstall' } else { 'install' }
  Add-Content -LiteralPath "$env:FIXTURE_ROOT/commands.log" -Value $stage
  if ($stage -eq 'install') {
    $uninstaller = "$env:FIXTURE_ROOT/dist/desktop-electron/win-unpacked/Uninstall.exe"
    New-Item -ItemType File $uninstaller | Out-Null
  }
  $process = [pscustomobject]@{ ExitCode = $(if ($env:FAILED_STAGE -eq $stage) { 7 } else { 0 }) }
  $process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($timeout) $true }
  return $process
}
function npm {
  Add-Content -LiteralPath "$env:FIXTURE_ROOT/commands.log" -Value 'check'
  $global:LASTEXITCODE = if ($env:FAILED_STAGE -eq 'check') { 7 } else { 0 }
  'Packaged Safe execution passed with windows_default; write and network denials verified.'
  'OpenSquilla packaged gateway smoke passed.'
}
function node {
  Add-Content -LiteralPath "$env:FIXTURE_ROOT/commands.log" -Value 'check'
  $global:LASTEXITCODE = if ($env:FAILED_STAGE -eq 'check') { 7 } else { 0 }
  Get-Content "$env:FIXTURE_ROOT/first-send.json" -Raw
}
"""
    path = tmp_path / "orchestration.ps1"
    path.write_text(prelude + script, encoding="utf-8")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(path)], cwd=tmp_path,
        env={**os.environ, **IDENTITY, "FIXTURE_ROOT": str(tmp_path),
             "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
             "RUNNER_OS": "Windows", "RUNNER_TEMP": str(tmp_path / "runner-temp"),
             "GITHUB_WORKSPACE": str(tmp_path), "PACKAGED_PROBE": probe,
             "FAILED_STAGE": failed_stage}, capture_output=True, text=True,
    )
    proof = json.loads((tmp_path / f"runner-temp/nsis-packaged-proof/{probe}/packaged-proof.json")
                       .read_text(encoding="utf-8-sig"))
    assert (result.returncode == 0) is (failed_stage == ""), result.stderr
    assert proof["success"] is (failed_stage == "")
    expected = list(MODULE["STAGES"])
    if failed_stage:
        expected = expected[:expected.index(failed_stage) + 1]
        if failed_stage in {"installed-identity", "check"}:
            expected.append("uninstall")
    assert [stage["stage"] for stage in proof["stages"]] == expected
    assert (tmp_path / "commands.log").read_text().splitlines() == expected
    assert [stage["stage"] for stage in proof["stages"] if stage["status"] == "failed"] == (
        [failed_stage] if failed_stage else []
    )
    assert all(stage["startedAt"] <= stage["finishedAt"] for stage in proof["stages"])
