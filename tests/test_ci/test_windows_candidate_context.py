"""Keep producer provenance distinct from the verification harness revision."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run_context(*, producer_path: str, producer_sha: str):
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is required for the native candidate context helper")
    source = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
    ).strip()
    # Mock only GitHub's run lookup. The helper reads real immutable Git data.
    response = json.dumps({"path": producer_path, "head_sha": producer_sha})
    code = "\n".join((
        "$ErrorActionPreference = 'Stop'",
        "function gh {",
        "  $global:LASTEXITCODE = 0",
        "  '" + response.replace("'", "''") + "'",
        "}",
        "$env:GITHUB_WORKFLOW_SHA = '" + "c" * 40 + "'",
        "$env:GITHUB_REPOSITORY = 'TokenRhythm/opensquilla'",
        ".github/scripts/windows-candidate-context.ps1 "
        f"-SourceSha {source} -SourceRunId 123 | ConvertTo-Json -Compress",
    ))
    return source, subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-Command", code],
        cwd=ROOT, text=True, capture_output=True, timeout=30,
    )


def test_reused_candidate_uses_producer_workflow_and_immutable_source_version():
    source, result = run_context(
        producer_path=".github/workflows/wheelhouse-release.yml", producer_sha="b" * 40,
    )
    assert result.returncode == 0, result.stderr
    package = json.loads(subprocess.check_output(
        ["git", "show", f"{source}:desktop/electron/package.json"], cwd=ROOT, text=True,
    ))
    assert json.loads(result.stdout) == {"WorkflowSha": "b" * 40, "Version": package["version"]}


@pytest.mark.parametrize(("path", "sha", "message"), [
    (".github/workflows/ci.yml", "b" * 40, "Release Assets run"),
    (".github/workflows/wheelhouse-release.yml", "mutable-main", "workflow SHA"),
])
def test_candidate_context_rejects_wrong_producer_or_mutable_workflow(path, sha, message):
    _, result = run_context(producer_path=path, producer_sha=sha)
    assert result.returncode != 0
    assert message in result.stderr


def test_candidate_context_is_part_of_required_native_execution_inputs():
    path = ".github/scripts/windows-candidate-context.ps1"
    policy = json.loads((ROOT / ".github/ci/trust-policy.v1.json").read_text())
    suites = json.loads((ROOT / ".github/ci/suites.v1.json").read_text())
    assert path in policy["merge_critical_inputs"]
    assert path in suites["suites"]["windows-nsis-regression"]["execution_inputs"]
