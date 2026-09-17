from __future__ import annotations

import json
import re
import runpy
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / ".github/ci/trust-policy.v1.json"
MODULE: dict[str, Any] = runpy.run_path(
    str(ROOT / ".github/scripts/ci_attestation.py"), run_name="ci_attestation"
)
AttestationError = MODULE["AttestationError"]
policy_digest = MODULE["policy_digest"]

LOCAL_GITHUB_PATH_RE = re.compile(r"\.github/[A-Za-z0-9_.\-/]+")


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write(repo: Path, relative: str, value: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _manifest() -> dict[str, Any]:
    value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _seed_policy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "CI Test")
    _git(repo, "config", "user.email", "ci@example.invalid")
    manifest = _manifest()
    for relative in manifest["merge_critical_inputs"]:
        if relative == ".github/ci/trust-policy.v1.json":
            value = json.dumps(manifest, indent=2) + "\n"
        else:
            value = f"synthetic merge-critical input: {relative}\n"
        _write(repo, relative, value)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "seed trust policy")
    return repo


def _local_workflow_dependencies(
    relative: str,
    visited: set[str],
    *,
    root: Path = ROOT,
) -> set[str]:
    if relative in visited:
        return set()
    visited.add(relative)
    workflow = yaml.safe_load((root / relative).read_text(encoding="utf-8"))
    dependencies = {relative}

    def visit(value: object, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)
            return
        if not isinstance(value, str):
            return
        if key == "uses" and value.startswith("./.github/"):
            dependency = value[2:]
            dependency_path = root / dependency
            if dependency_path.is_dir():
                action_files = [
                    candidate
                    for candidate in ("action.yml", "action.yaml")
                    if (dependency_path / candidate).is_file()
                ]
                assert len(action_files) == 1, (
                    f"local action must have exactly one metadata file: {dependency}"
                )
                dependency = f"{dependency.rstrip('/')}/{action_files[0]}"
                dependency_path = root / dependency
            assert dependency_path.is_file(), (
                f"local workflow dependency does not exist: {dependency}"
            )
            dependencies.add(dependency)
            if dependency.endswith((".yml", ".yaml")):
                dependencies.update(
                    _local_workflow_dependencies(
                        dependency,
                        visited,
                        root=root,
                    )
                )
        else:
            # Scan every scalar, not only ``run``. This deliberately catches
            # repository-local executors passed indirectly through env/with.
            dependencies.update(LOCAL_GITHUB_PATH_RE.findall(value))

    visit(workflow)
    return dependencies


def test_local_workflow_dependency_scan_follows_env_and_composite_actions(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        """
name: Synthetic CI
env:
  GATE: .github/scripts/gate.py
jobs:
  required:
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/local-check
      - run: python \"$GATE\"
""".lstrip(),
    )
    _write(
        tmp_path,
        ".github/actions/local-check/action.yml",
        """
name: Local check
runs:
  using: composite
  steps:
    - run: python .github/scripts/action-check.py
      shell: bash
""".lstrip(),
    )

    assert _local_workflow_dependencies(
        ".github/workflows/ci.yml",
        visited=set(),
        root=tmp_path,
    ) == {
        ".github/actions/local-check/action.yml",
        ".github/scripts/action-check.py",
        ".github/scripts/gate.py",
        ".github/workflows/ci.yml",
    }


def test_trust_policy_manifest_is_exact_sorted_and_self_governing() -> None:
    manifest = _manifest()
    paths = manifest["merge_critical_inputs"]

    assert manifest["schema_version"] == 1
    assert paths == sorted(set(paths))
    assert ".github/ci/trust-policy.v1.json" in paths
    assert ".github/scripts/windows_test_durations.json" not in paths
    assert all(path.startswith(".github/") and "*" not in path for path in paths)
    assert all((ROOT / path).is_file() for path in paths)


def test_trust_policy_covers_local_required_ci_execution_closure() -> None:
    declared = set(_manifest()["merge_critical_inputs"])
    discovered = _local_workflow_dependencies(
        ".github/workflows/ci.yml", visited=set()
    )
    discovered.update(
        {
            ".github/ci/suites.v1.json",
            ".github/ci/trust-policy.v1.json",
            ".github/scripts/windows_test_assignments.json",
        }
    )

    assert discovered <= declared, (
        f"undeclared merge-critical inputs: {sorted(discovered - declared)}"
    )


def test_policy_digest_ignores_noncritical_workflow_and_duration_data(
    tmp_path: Path,
) -> None:
    repo = _seed_policy_repo(tmp_path)
    baseline = policy_digest(repo)

    _write(repo, ".github/workflows/docs-only.yml", "name: Docs\n")
    _write(repo, ".github/scripts/windows_test_durations.json", "{}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "change noncritical CI inputs")

    assert policy_digest(repo) == baseline

    _write(repo, ".github/workflows/ci.yml", "name: Changed required CI\n")
    _git(repo, "add", ".github/workflows/ci.yml")
    _git(repo, "commit", "-m", "change required CI")

    assert policy_digest(repo) != baseline


def test_policy_digest_rejects_missing_manifest_input(tmp_path: Path) -> None:
    repo = _seed_policy_repo(tmp_path)
    _git(repo, "rm", ".github/scripts/check_ci_results.py")
    _git(repo, "commit", "-m", "remove required gate")

    with pytest.raises(AttestationError, match="inputs are missing"):
        policy_digest(repo)
