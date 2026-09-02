from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(".github/workflows")
PR_TARGET_VALIDATOR = Path(".github/scripts/validate-pr-target-branch.sh")
PR_BODY_LINT = Path(".github/scripts/validate_pr_body.py")
TEST_PATH_RE = re.compile(r"tests/[A-Za-z0-9_./-]+\.py")


def _workflow(name: str) -> dict:
    path = WORKFLOW_DIR / name
    assert path.is_file(), f"missing workflow: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _trigger_keys(data: dict) -> set[str]:
    triggers = data.get("on", {})
    if triggers is None:
        return set()
    if isinstance(triggers, str):
        return {triggers}
    return set(triggers)


def _workflow_texts() -> list[str]:
    return [path.read_text(encoding="utf-8") for path in WORKFLOW_DIR.glob("*.yml")]


def _is_windows_wsl_bash(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized.endswith("/windows/system32/bash.exe")


def _bash_executable(
    *,
    os_name: str = os.name,
    path_lookup: Callable[[str], str | None] = shutil.which,
    exists: Callable[[Path], bool] = Path.is_file,
    program_files: str | None = None,
) -> str:
    found = path_lookup("bash")
    if os_name != "nt":
        return found or "bash"

    candidates: list[Path] = []
    if found and not _is_windows_wsl_bash(found):
        candidates.append(Path(found))

    git_root = Path(program_files or os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git"
    candidates.extend(
        [
            git_root / "bin" / "bash.exe",
            git_root / "usr" / "bin" / "bash.exe",
        ]
    )

    for candidate in candidates:
        if exists(candidate):
            return str(candidate)

    raise AssertionError("Git Bash is required to run CI shell contracts on Windows")


def _validate_pr_target(
    tmp_path: Path,
    *,
    base: str,
    head: str = "feature/example",
    title: str = "Example change",
    labels: list[str] | None = None,
    changed_files: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    event_path = tmp_path / "event.json"
    changed_files_path = tmp_path / "changed-files.txt"
    if changed_files is not None:
        changed_files_path.write_text("\n".join(changed_files) + "\n", encoding="utf-8")

    event_path.write_text(
        json.dumps(
            {
                "pull_request": {
                    "base": {"ref": base},
                    "head": {"ref": head},
                    "labels": [{"name": label} for label in labels or []],
                    "title": title,
                },
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "GITHUB_EVENT_PATH": event_path.as_posix(),
            "PR_BASE_REF": base,
            "PR_HEAD_REF": head,
            "PR_LABELS": ",".join(labels or []),
            "PR_TITLE": title,
        }
    )
    if changed_files is not None:
        env["PR_CHANGED_FILES_PATH"] = changed_files_path.as_posix()
    return subprocess.run(
        [_bash_executable(), PR_TARGET_VALIDATOR.as_posix()],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_default_ci_blocks_pull_requests_and_main_pushes() -> None:
    ci_path = WORKFLOW_DIR / "ci.yml"
    if not ci_path.exists():
        return

    data = _workflow("ci.yml")
    text = ci_path.read_text(encoding="utf-8")

    assert {
        "pull_request",
        "merge_group",
        "push",
        "schedule",
        "workflow_dispatch",
    } <= _trigger_keys(data)
    assert data["on"]["merge_group"]["types"] == ["checks_requested"]
    assert "branches: [main]" in text
    assert "PYTHONPATH: ${{ github.workspace }}" in text
    assert "Configure runtime directories" in text
    assert 'OPENSQUILLA_STATE_DIR=%s/opensquilla-state\\n' in text
    assert 'OPENSQUILLA_LOG_DIR=%s/opensquilla-logs\\n' in text
    assert "OPENSQUILLA_TURN_CALL_LOG: \"0\"" in text
    assert "actionlint@v1.7.12" in text
    assert "Plan CI suites" in text
    assert "OpenTUI package tests" in text
    assert "Lint, test, and build (ubuntu-latest, 3.12)" in text
    assert "Windows compatibility smoke (3.12)" not in text
    assert "Windows high-risk" in text
    assert "Release packaging contracts" in text
    assert "CI result" in text
    assert 'push)\n              before="${{ github.event.before }}"' in text
    merge_group_case = text.split("            merge_group)", 1)[1].split(
        "              ;;", 1
    )[0]
    assert "Queue evidence was unavailable; running the full fail-closed matrix." in (
        merge_group_case
    )
    assert 'printf \'.ci/run-all\\n\' > "${changed_files}"' in merge_group_case
    assert "git diff --name-only" not in merge_group_case
    assert "queue_diff_targeted" not in text
    assert "full_fail_closed" in text
    assert (
        'git diff --no-renames --name-only "${before}" "${after}" > "${changed_files}"'
        in text
    )
    assert 'printf \'.ci/run-all\\n\' > "${changed_files}"' in text
    assert "classify-ci-changes.sh" not in text
    assert "reason_codes" in text
    assert "suite_execution_digests" in text
    assert ".github/scripts/check_ci_results.py" in text
    assert "code_changed" not in text
    assert "workflow_changed" not in text
    assert "Verify fresh full-nightly health" not in text
    assert "verify-nightly-health" not in text
    assert "steps.nightly" not in text
    assert "nightly_healthy" not in text
    pull_request_case = text.split("            pull_request)", 1)[1].split(
        "              ;;", 1
    )[0]
    assert "github.event.pull_request.base.sha" in pull_request_case
    assert "github.event.pull_request.head.sha" in pull_request_case
    assert "nightly" not in pull_request_case.lower()


def test_pr_change_selection_uses_merge_base_and_ignores_base_only_changes(
    tmp_path: Path,
) -> None:
    workflow = _workflow("ci.yml")
    checkout = next(
        step
        for step in workflow["jobs"]["plan-ci"]["steps"]
        if step.get("uses") == "actions/checkout@v4"
    )
    assert checkout["with"]["fetch-depth"] == 0

    text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
    pull_request_case = text.split("            pull_request)", 1)[1].split(
        "              ;;", 1
    )[0]
    assert "git diff --merge-base --no-renames --name-only" in pull_request_case
    assert '"${base_sha}" "${head_sha}" -- > "${changed_files}"' in pull_request_case
    assert "Unable to derive the PR-owned change set" in pull_request_case
    assert 'printf \'.ci/run-all\\n\' > "${changed_files}"' in pull_request_case

    executable_case = (
        'changed_files="${CHANGED_FILES}"\n'
        + pull_request_case.replace(
            "${{ github.event.pull_request.base.sha }}", "${BASE_SHA}"
        ).replace("${{ github.event.pull_request.head.sha }}", "${HEAD_SHA}")
    )

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def select_changed_files(base_sha: str, head_sha: str) -> list[str]:
        changed_files = tmp_path / "changed-files.txt"
        changed_files.write_text("stale partial result\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "BASE_SHA": base_sha,
                "CHANGED_FILES": changed_files.as_posix(),
                "CI_OPTIMIZATION_MODE": "enforce",
                "HEAD_SHA": head_sha,
            }
        )
        result = subprocess.run(
            [_bash_executable(), "-euo", "pipefail", "-c", executable_case],
            cwd=repo,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return changed_files.read_text(encoding="utf-8").splitlines()

    def plan_changed_files(paths: list[str]) -> dict:
        planner_input = tmp_path / "planner-input.txt"
        planner_input.write_text("\n".join(paths) + "\n", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                ".github/scripts/plan_ci.py",
                planner_input.as_posix(),
                "--repo",
                ".",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    git("init", "-b", "main")
    git("config", "user.name", "CI Test")
    git("config", "user.email", "ci@example.invalid")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "baseline")

    git("switch", "-c", "feature")
    feature_path = repo / "src/opensquilla/cli/config_cmd.py"
    feature_path.parent.mkdir(parents=True)
    feature_path.write_text("FEATURE = True\n", encoding="utf-8")
    git("add", feature_path.relative_to(repo).as_posix())
    git("commit", "-m", "feature change")
    head_sha = git("rev-parse", "HEAD")

    git("switch", "main")
    base_only_path = repo / ".github/ci/trust-policy.v1.json"
    base_only_path.parent.mkdir(parents=True)
    base_only_path.write_text("{}\n", encoding="utf-8")
    git("add", base_only_path.relative_to(repo).as_posix())
    git("commit", "-m", "base-only CI policy change")
    base_sha = git("rev-parse", "HEAD")

    changed = select_changed_files(base_sha, head_sha)
    assert changed == ["src/opensquilla/cli/config_cmd.py"]
    plan = plan_changed_files(changed)
    assert plan["full_fallback"] is False
    assert plan["reason_codes"] == ["python_targeted"]

    two_tree_changed = git(
        "diff",
        "--no-renames",
        "--name-only",
        base_sha,
        head_sha,
    ).splitlines()
    assert ".github/ci/trust-policy.v1.json" in two_tree_changed
    two_tree_plan = plan_changed_files(two_tree_changed)
    assert two_tree_plan["full_fallback"] is True
    assert "ci_policy_changed" in two_tree_plan["reason_codes"]

    git("switch", "--orphan", "unrelated")
    unrelated_path = repo / "unrelated.txt"
    unrelated_path.write_text("unrelated history\n", encoding="utf-8")
    git("add", "unrelated.txt")
    git("commit", "-m", "unrelated root")
    unrelated_sha = git("rev-parse", "HEAD")
    git("switch", "main")

    assert select_changed_files(base_sha, unrelated_sha) == [".ci/run-all"]


def test_queue_summary_explains_tree_mismatch_fallback() -> None:
    text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")

    assert 'if [[ "${REASON_CODE}" == "tree_mismatch" ]]' in text
    assert "this entry runs the full fail-closed matrix" in text
    assert "If main advanced" in text
    assert "wait for a new green PR CI run before requeueing" in text
    assert "future entry's exact base and tree" in text


def test_ci_fast_paths_keep_the_required_check_and_fail_closed() -> None:
    workflow = _workflow("ci.yml")
    jobs = workflow["jobs"]

    assert workflow["env"]["CI_OPTIMIZATION_MODE"] == (
        "${{ vars.CI_OPTIMIZATION_MODE || 'enforce' }}"
    )
    assert jobs["queue-attestation"]["name"] == "Verify reusable PR CI evidence"
    assert jobs["queue-attestation"]["if"] == (
        "${{ github.event_name == 'merge_group' }}"
    )
    assert "not a merge-group event" not in str(jobs["queue-attestation"])
    assert "fetch-depth" in str(jobs["queue-attestation"])
    assert "verify-queue" in str(jobs["queue-attestation"])
    assert "reason_code" in str(jobs["queue-attestation"])
    assert jobs["queue-attestation"]["outputs"]["combined_smoke_suites"] == (
        "${{ steps.verify.outputs.combined_smoke_suites || '[]' }}"
    )
    assert not any(
        name.startswith("nightly_") for name in jobs["queue-attestation"]["outputs"]
    )
    assert jobs["plan-ci"]["name"] == "Plan CI suites"
    assert jobs["plan-ci"]["needs"] == "queue-attestation"
    assert set(jobs["plan-ci"]["outputs"]) == {
        "required_suites",
        "desktop_matrix",
        "python_matrix",
        "platform_matrix",
        "python_targets",
        "full_fallback",
        "reason_codes",
        "plan_digest",
        "suite_execution_digests",
    }
    assert jobs["plan-ci"]["outputs"]["full_fallback"] == (
        "${{ steps.plan.outputs.full_fallback }}"
    )
    assert jobs["plan-ci"]["outputs"]["reason_codes"] == (
        "${{ steps.plan.outputs.reason_codes }}"
    )
    assert "always()" in jobs["plan-ci"]["if"]
    assert "github.event_name != 'merge_group'" in jobs["plan-ci"]["if"]
    assert "needs.queue-attestation.result != 'success'" in (
        jobs["plan-ci"]["if"]
    )
    assert not any(
        name.startswith("nightly_") for name in jobs["plan-ci"]["outputs"]
    )
    planner_consumers = {
        job_name: job
        for job_name, job in jobs.items()
        if job_name != "ci-result"
        and "plan-ci"
        in (
            [job.get("needs")]
            if isinstance(job.get("needs"), str)
            else job.get("needs", [])
        )
    }
    assert planner_consumers
    for job_name, job in planner_consumers.items():
        condition = str(job.get("if", ""))
        assert "always()" in condition, job_name
        assert "needs.plan-ci.result == 'success'" in condition, job_name
    for job_name in ("webui-chat-recovery", "desktop-recovery-e2e"):
        assert "needs.frontend-artifact.result == 'success'" in str(
            jobs[job_name]["if"]
        )
    queue_checkout = next(
        step
        for step in jobs["queue-attestation"]["steps"]
        if step.get("name") == "Check out merge-group commit"
    )
    assert queue_checkout["with"]["ref"] == "${{ github.event.merge_group.head_sha }}"
    assert "full fail-closed matrix" in str(jobs["plan-ci"])
    assert 'CI_OPTIMIZATION_MODE}" == "legacy"' in str(jobs["plan-ci"])
    summary = next(
        step
        for step in jobs["plan-ci"]["steps"]
        if step.get("name") == "Summarize canonical CI plan"
    )
    assert "Reason codes" in summary["run"]
    assert "Required suites" in summary["run"]
    assert "Python matrix" in summary["run"]
    assert "Desktop matrix" in summary["run"]
    assert "Suite cells" in summary["run"]
    assert jobs["main-canary"]["name"] == (
        "Queue/main installation and offline gateway canary"
    )
    assert "needs.queue-attestation.result == 'success'" in jobs["main-canary"]["if"]
    assert "test_gateway_silent_reply_process_e2e.py" in str(jobs["main-canary"])
    assert all(
        step.get("name") != "Run overlapping Python domain smoke"
        for step in jobs["main-canary"]["steps"]
    )
    assert jobs["ci-result"]["name"] == "CI result"
    assert "ci-evidence-v2-tree-${{ steps.attestation.outputs.tree_sha }}" in str(
        jobs["ci-result"]
    )
    assert "ci-nightly-health-v1" in str(jobs["ci-result"])
    ci_result_steps = jobs["ci-result"]["steps"]
    fast_path = next(
        step
        for step in ci_result_steps
        if step.get("name") == "Accept verified queue or main fast path"
    )
    setup_python = next(
        step for step in ci_result_steps if step.get("name") == "Set up Python"
    )
    result_gate = next(
        step
        for step in ci_result_steps
        if step.get("name") == "Check required CI results"
    )
    for condition in (fast_path["if"], setup_python["if"], result_gate["if"]):
        assert "needs.queue-attestation.result == 'success'" in condition
        assert "needs.queue-attestation.outputs.reusable == 'true'" in condition
    attestation = next(
        step
        for step in ci_result_steps
        if step.get("name") == "Create trusted CI evidence v2"
    )
    assert attestation["env"]["PLANNED_SUCCESSFUL_SUITES"] == (
        "${{ needs.plan-ci.outputs.required_suites }}"
    )
    assert attestation["env"]["PLANNED_FULL_FALLBACK"] == (
        "${{ needs.plan-ci.outputs.full_fallback }}"
    )
    assert '--successful-suites "${PLANNED_SUCCESSFUL_SUITES}"' in attestation["run"]
    assert "planner evidence metadata is incomplete" in attestation["run"]
    assert '--plan-basis "${plan_basis}"' in attestation["run"]
    assert "needs.queue-attestation.result == 'success'" in attestation["if"]
    assert "needs.queue-attestation.outputs.reusable == 'true'" in attestation["if"]
    assert "!(env.CI_OPTIMIZATION_MODE == 'enforce'" in attestation["if"]
    assert ci_result_steps.index(fast_path) < ci_result_steps.index(attestation)
    assert ci_result_steps.index(result_gate) < ci_result_steps.index(attestation)
    assert '"${MAIN_CANARY_RESULT}" != "success"' in fast_path["run"]
    assert "exit 1" in fast_path["run"]
    assert "always()" not in attestation["if"]
    assert "failure()" not in attestation["if"]
    assert "cancelled()" not in attestation["if"]
    tree_upload = next(
        step
        for step in ci_result_steps
        if step.get("name") == "Upload tree-indexed CI evidence v2"
    )
    assert tree_upload["if"] == "${{ steps.attestation.outcome == 'success' }}"


@pytest.mark.parametrize(
    ("queue_result", "queue_reusable", "canary_result", "expected_code"),
    (
        ("success", "true", "success", 0),
        ("failure", "true", "success", 1),
        ("success", "false", "success", 1),
        ("success", "true", "failure", 1),
        ("success", "true", "cancelled", 1),
        ("success", "true", "skipped", 1),
        ("success", "true", "", 1),
    ),
)
def test_ci_queue_fast_path_shell_fails_closed(
    queue_result: str,
    queue_reusable: str,
    canary_result: str,
    expected_code: int,
) -> None:
    jobs = _workflow("ci.yml")["jobs"]
    fast_path = next(
        step
        for step in jobs["ci-result"]["steps"]
        if step.get("name") == "Accept verified queue or main fast path"
    )
    env = os.environ.copy()
    env.update(
        {
            "GITHUB_EVENT_NAME": "merge_group",
            "QUEUE_RESULT": queue_result,
            "QUEUE_REUSABLE": queue_reusable,
            "QUEUE_REASON": "synthetic evidence decision",
            "MAIN_CANARY_RESULT": canary_result,
        }
    )

    completed = subprocess.run(
        [_bash_executable(), "-euo", "pipefail", "-c", fast_path["run"]],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == expected_code


def test_skill_hub_contract_is_integrated_into_canonical_ci() -> None:
    assert not (WORKFLOW_DIR / "skill-hub-contract.yml").exists()
    jobs = _workflow("ci.yml")["jobs"]
    job = jobs["skill-hub"]
    assert job["needs"] == "plan-ci"
    assert "'skill-hub'" in job["if"]
    assert job["strategy"] == {
        "fail-fast": False,
        "matrix": {"os": ["ubuntu-latest", "macos-latest", "windows-latest"]},
    }
    run = next(
        step
        for step in job["steps"]
        if step.get("name") == "Run offline Community Skill contracts"
    )["run"]
    assert set(TEST_PATH_RE.findall(run)) == {
        "tests/test_skills_manifest.py",
        "tests/test_skills_bundled_baseline.py",
        "tests/test_skills_hot_reload.py",
        "tests/test_skills_default_prompt_contract.py",
        "tests/test_skills_loader_namespaces.py",
        "tests/test_skills_tree.py",
        "tests/test_skills_hub_archive.py",
        "tests/test_skills_hub_clawhub.py",
        "tests/test_skills_hub_github.py",
        "tests/test_skills_hub_router.py",
        "tests/test_skills_hub_source.py",
        "tests/test_skills_hub_installer_security.py",
        "tests/test_skills_hub_lockfile_contract.py",
        "tests/test_skills_hub_doctor.py",
        "tests/test_skills_hash_consumers.py",
        "tests/test_skills/test_hub_management_service.py",
        "tests/test_skills/test_hub_scanner.py",
        "tests/test_skills/test_hub_transaction_recovery.py",
        "tests/test_skills/test_hub_transaction_process_gates.py",
        "tests/test_gateway/test_rpc_skills_install_visibility.py",
        "tests/test_gateway/test_rpc_skills_exact_identity.py",
        "tests/test_gateway/test_rpc_skills_coding_gate.py",
        "tests/test_gateway/test_rpc_skills_reload.py",
        "tests/test_gateway/test_skill_management_service_injection.py",
        "tests/test_tools/test_skill_view_resources.py",
        "tests/test_scripts/test_bench_skill_integrity.py",
        "tests/test_cli/test_cli_product_completeness.py",
        "tests/test_cli/test_skills_doctor_cmd.py",
        "tests/test_cli/test_skills_gateway_fallback.py",
        "tests/test_cli/test_skills_search_cmd.py",
        "tests/test_engine/turn_runner/test_provider_and_tools_stage_unit.py",
    }
    assert "not live_skill_hub" in run
    assert "skill-hub" in jobs["ci-result"]["needs"]

def test_default_ci_keeps_main_pushes_targeted_and_manual_runs_full() -> None:
    ci_path = WORKFLOW_DIR / "ci.yml"
    if not ci_path.exists():
        return
    text = ci_path.read_text(encoding="utf-8")

    assert 'before="${{ github.event.before }}"' in text
    assert 'after="${{ github.event.after }}"' in text
    assert (
        'git diff --no-renames --name-only "${before}" "${after}" > "${changed_files}"'
        in text
    )
    assert 'workflow_dispatch' in text
    assert 'printf \'.ci/run-all\\n\' > "${changed_files}"' in text


def test_ci_rejects_tracked_frontend_dist_and_builds_a_verified_artifact() -> None:
    # Generated WebUI files belong to CI artifacts and release packages, not Git.
    # Fail closed if a contributor force-adds dist, then prove the generated tree
    # is exactly what enters the wheel before sharing it with downstream jobs.
    ci_path = WORKFLOW_DIR / "ci.yml"
    if not ci_path.exists():
        return
    text = ci_path.read_text(encoding="utf-8")

    assert "Verify generated dist is not tracked" in text
    assert (
        "git ls-files 'opensquilla-webui/dist/**' 'src/opensquilla/gateway/static/dist/**'"
        in text
    )
    assert "generated Web UI dist must not be committed" in text
    assert "Build verified frontend artifact" in text
    assert "> public/.DS_Store" in text
    assert "Finder metadata survived WebUI artifact normalization" in text
    assert "npm run verify:release-dist" in text
    assert "Verify sdist-to-wheel frontend artifact round trip" in text
    assert "uv build --sdist" in text
    assert 'printf \'CI-only Finder metadata\\n\' > "${junk}"' in text
    assert "tar -tzf" in text
    assert "ignored Finder metadata leaked into the sdist" in text
    assert 'uv build --wheel --out-dir "${wheel_dir}" "${sdists[0]}"' in text
    assert "python scripts/verify_webui_artifact.py" in text
    assert "--forbid-personal-bgm" in text
    assert '--wheel "${wheels[0]}"' in text
    assert "Upload verified frontend artifact" in text
    assert "name: opensquilla-webui-dist" in text
    assert "path: opensquilla-webui/dist/" in text
    assert "Stage verified frontend artifact for Python consumers" in text
    assert "overwrite: true" in text
    workflow = _workflow("ci.yml")
    upload = next(
        step
        for step in workflow["jobs"]["frontend-artifact"]["steps"]
        if step.get("name") == "Upload verified frontend artifact"
    )
    producer = next(
        step
        for step in workflow["jobs"]["frontend-artifact"]["steps"]
        if step.get("name") == "Build verified frontend artifact"
    )
    typecheck = next(
        step
        for step in workflow["jobs"]["frontend-check"]["steps"]
        if step.get("name") == "Run frontend type checks"
    )
    setup_node = next(
        step
        for step in workflow["jobs"]["frontend-check"]["steps"]
        if step.get("name") == "Set up Node.js"
    )
    install_node = next(
        step
        for step in workflow["jobs"]["frontend-check"]["steps"]
        if step.get("name") == "Install frontend dependencies"
    )
    unit_tests = next(
        step
        for step in workflow["jobs"]["frontend-check"]["steps"]
        if step.get("name") == "Run frontend unit tests"
    )
    assert "npm run build:artifact" in producer["run"]
    assert "npm run build\n" not in producer["run"]
    assert typecheck["run"] == "npm run typecheck"
    assert "'frontend-validation'" in typecheck["if"]
    # Node is also needed to run the dependency-free staging seam when only
    # the wheel round-trip suite is selected.
    assert "if" not in setup_node
    assert install_node["if"] == typecheck["if"]
    assert unit_tests["if"] == typecheck["if"]
    assert upload["with"]["retention-days"] >= 31
    assert upload["with"]["overwrite"] is True
    assert "opensquilla-webui-dist-attempt-${{ github.run_attempt }}" not in text
    wheel = next(
        step
        for step in workflow["jobs"]["frontend-check"]["steps"]
        if step.get("name") == "Verify sdist-to-wheel frontend artifact round trip"
    )
    assert "'wheel-webui-roundtrip'" in wheel["if"]
    setup_python = next(
        step
        for step in workflow["jobs"]["frontend-check"]["steps"]
        if step.get("name") == "Set up Python"
    )
    setup_uv = next(
        step
        for step in workflow["jobs"]["frontend-check"]["steps"]
        if step.get("name") == "Set up uv"
    )
    assert setup_python["if"] == wheel["if"]
    assert setup_uv["if"] == wheel["if"]


def test_webui_text_and_docker_context_contracts_are_enforced_in_ci() -> None:
    attributes = Path(".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "opensquilla-webui/** text=auto eol=lf" in attributes
    assert "src/opensquilla/contracts/generated/v4/** text eol=lf" in attributes

    workflow = _workflow("ci.yml")
    ubuntu = workflow["jobs"]["ubuntu-quality"]
    assert ubuntu["env"]["OPENSQUILLA_DOCKERIGNORE_E2E"] == "1"
    docker_step = next(
        step
        for step in ubuntu["steps"]
        if step.get("name") == "Test Docker build-context exclusions in full CI"
    )
    assert docker_step["if"] == (
        "${{ contains(fromJSON(needs.plan-ci.outputs.required_suites), "
        "'python-full') }}"
    )
    assert "tests/test_ci/test_dockerignore_context.py" in docker_step["run"]


def test_readme_contract_check_uses_the_pinned_node_version() -> None:
    workflow = _workflow("ci.yml")
    job = workflow["jobs"]["readme-locale-check"]
    setup_node = next(
        step for step in job["steps"] if step.get("name") == "Set up Node.js"
    )
    check = next(
        step for step in job["steps"] if step.get("name") == "Check README locale parity"
    )

    assert setup_node["with"] == {
        "node-version-file": "opensquilla-webui/.node-version"
    }
    assert check["run"] == "node scripts/check-readme-locales.mjs"


def test_managed_toolchain_artifacts_cover_native_macos_architectures_and_musl() -> None:
    workflow = _workflow("managed-toolchain-artifacts.yml")
    assert _trigger_keys(workflow) == {"workflow_call", "workflow_dispatch"}
    validate = workflow["jobs"]["validate"]
    matrix = validate["strategy"]["matrix"]["include"]

    assert {entry["runner"] for entry in matrix} == {
        "ubuntu-24.04",
        "ubuntu-24.04-arm",
        "macos-15",
        "macos-15-intel",
        "windows-2022",
    }
    assert {entry["platform_key"] for entry in matrix} == {
        "linux-x64",
        "linux-arm64",
        "darwin-arm64",
        "darwin-x64",
        "windows-x64",
    }
    assert all(entry["paper_platform_key"] for entry in matrix)
    macos = {entry["runner"]: entry for entry in matrix if entry["runner"].startswith("macos-")}
    assert macos == {
        "macos-15": {
            "label": "macOS Apple Silicon real artifacts",
            "runner": "macos-15",
            "platform_key": "darwin-arm64",
            "paper_platform_key": "darwin-universal",
        },
        "macos-15-intel": {
            "label": "macOS Intel real artifacts",
            "runner": "macos-15-intel",
            "platform_key": "darwin-x64",
            "paper_platform_key": "darwin-universal",
        },
    }

    assert "OPENSQUILLA_GATEWAY_STATE_DIR" not in validate["env"]
    assert "OPENSQUILLA_TOOLCHAIN_VALIDATION_ROOT" not in validate["env"]
    assert validate["env"]["OPENSQUILLA_REQUIRE_MANAGED_TOOLCHAIN_E2E"] == "1"
    setup_uv = next(step for step in validate["steps"] if step.get("name") == "Set up uv")
    assert setup_uv["with"]["enable-cache"] is True

    configure_state = next(
        step
        for step in validate["steps"]
        if step.get("name") == "Configure isolated managed-toolchain state"
    )
    assert configure_state["shell"] == "bash"
    assert "$RUNNER_TEMP" in configure_state["run"]
    assert "OPENSQUILLA_GATEWAY_STATE_DIR=" in configure_state["run"]
    assert "OPENSQUILLA_TOOLCHAIN_VALIDATION_ROOT=" in configure_state["run"]
    assert "$GITHUB_ENV" in configure_state["run"]

    paper_smoke = next(
        step
        for step in validate["steps"]
        if step.get("name") == "Validate real pinned paper archive and capability smoke"
    )["run"]
    assert "--component paper-tex" in paper_smoke
    assert "--expect-platform-key ${{ matrix.paper_platform_key }}" in paper_smoke
    assert (
        "${{ matrix.platform_key == 'linux-x64' && '--check-runtime-hot-path' || '' }}"
        in paper_smoke
    )
    media_smoke = next(
        step
        for step in validate["steps"]
        if step.get("name") == "Validate real pinned media archives and capability smoke"
    )["run"]
    assert "--component media-ffmpeg" in media_smoke
    assert "--expect-platform-key ${{ matrix.platform_key }}" in media_smoke
    assert "--check-runtime-hot-path" not in media_smoke
    paper_compile = next(
        step
        for step in validate["steps"]
        if step.get("name") == "Compile the default four-page paper with the managed toolchain"
    )["run"]
    assert "test_meta_default_compact_contract_compiles_real_content_to_four_pages" in paper_compile

    musl = workflow["jobs"]["validate-musl-paper"]
    assert musl["runs-on"] == "ubuntu-24.04"
    assert musl["container"]["image"] == "python:3.12-alpine"
    assert musl["env"]["PYTHONPATH"] == "${{ github.workspace }}/src"
    assert musl["steps"][0] == {
        "name": "Prepare Alpine action runtime",
        "run": "apk add --no-cache fontconfig git nodejs",
    }
    smoke = next(
        step
        for step in musl["steps"]
        if step.get("name") == "Validate native musl TinyTeX archive and capability smoke"
    )
    command = smoke["run"]
    assert "validate_managed_toolchain_artifacts_stdlib.py" in command
    assert "--component paper-tex" in command
    assert "--expect-platform-key linux-musl-x64" in command
    assert "media-ffmpeg" not in command


def test_musl_toolchain_validator_bootstrap_is_stdlib_only() -> None:
    script = Path("scripts/validate_managed_toolchain_artifacts_stdlib.py")
    result = subprocess.run(
        [sys.executable, "-S", str(script), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--component {paper-tex,media-ffmpeg}" in result.stdout
    assert "--expect-platform-key" in result.stdout


def test_toolchain_validator_platform_assertion_never_overrides_detection(
    tmp_path: Path,
) -> None:
    script = Path("scripts/validate_managed_toolchain_artifacts_stdlib.py")
    root = tmp_path / "managed-toolchains"
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(script),
            "--component",
            "paper-tex",
            "--root",
            str(root),
            "--expect-platform-key",
            "not-the-native-host",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    mismatch = next(event for event in events if event["event"] == "platform_mismatch")
    assert mismatch["expected_platform_key"] == "not-the-native-host"
    assert mismatch["actual_platform_key"] != mismatch["expected_platform_key"]
    assert not (root / "packages").exists()


def test_desktop_ci_runs_primary_profile_substrate_unit_tests() -> None:
    data = _workflow("ci.yml")
    desktop_steps = data["jobs"]["desktop-check"]["steps"]
    unit_step = next(step for step in desktop_steps if step.get("name") == "Run desktop unit tests")

    assert "node scripts/test-desktop-profile-substrate.mjs" in unit_step["run"]
    assert "node scripts/test-desktop-profile-consolidation.mjs" in unit_step["run"]
    assert "node scripts/test-onboarding-flow-coordinator.mjs" in unit_step["run"]
    assert "node scripts/test-onboarding-save-telemetry.mjs" in unit_step["run"]


def test_pr_target_validator_allows_main_pull_requests(tmp_path: Path) -> None:
    result = _validate_pr_target(
        tmp_path,
        base="main",
        changed_files=["src/opensquilla/engine/agent.py"],
    )

    assert result.returncode == 0
    assert "Pull request targets main." in result.stdout


def test_pr_target_validator_blocks_dev_pull_requests(
    tmp_path: Path,
) -> None:
    result = _validate_pr_target(tmp_path, base="dev")

    assert result.returncode == 1
    assert "Ordinary pull requests should target main" in result.stderr


def test_pr_target_validator_allows_docs_only_main_pull_requests(
    tmp_path: Path,
) -> None:
    result = _validate_pr_target(
        tmp_path,
        base="main",
        head="docs/agent-testing",
        title="docs: add agent testing framework guide",
        changed_files=["docs/testing/framework.md"],
    )

    assert result.returncode == 0
    assert "Pull request targets main." in result.stdout


def test_pr_target_validator_allows_labeled_main_pull_requests_without_exception(
    tmp_path: Path,
) -> None:
    labels = [
        "allow-main-target",
        "release",
        "hotfix",
        "main-sync",
        "release-docs",
        "sync-to-main",
        "docs-preview",
    ]
    for label in labels:
        result = _validate_pr_target(
            tmp_path,
            base="main",
            head="release/0.3.2",
            labels=[label],
            changed_files=["src/opensquilla/engine/agent.py"],
        )

        assert result.returncode == 0
        assert "Pull request targets main." in result.stdout


def test_pr_target_validator_allows_staging_branch_pull_requests(
    tmp_path: Path,
) -> None:
    for base in [
        "sandbox-optimization",
        "integration/sandbox-hardening",
        "staging/sandbox-hardening",
        "release/0.3.2",
    ]:
        result = _validate_pr_target(
            tmp_path,
            base=base,
            head="pr/sandbox-run-modes-sandbox-optimization",
            changed_files=["src/opensquilla/sandbox/backend/windows_appcontainer.py"],
        )

        assert result.returncode == 0
        assert "staging/collaboration" in result.stdout
        assert "target main" in result.stdout


def test_pr_target_validator_allows_labeled_staging_pull_requests(
    tmp_path: Path,
) -> None:
    for label in ["maintainer-staging", "collaboration"]:
        result = _validate_pr_target(
            tmp_path,
            base="sandbox-review",
            head="feature/shared-sandbox-work",
            labels=[label],
            changed_files=["src/opensquilla/sandbox/policy.py"],
        )

        assert result.returncode == 0
        assert "staging/collaboration" in result.stdout


def test_pr_target_validator_blocks_unknown_target_branches(tmp_path: Path) -> None:
    result = _validate_pr_target(
        tmp_path,
        base="feature/private-target",
        head="feature/example",
        changed_files=["src/opensquilla/engine/agent.py"],
    )

    assert result.returncode == 1
    assert "Ordinary pull requests should target main" in result.stderr


def test_pr_target_validator_handles_missing_event_path() -> None:
    env = os.environ.copy()
    env.pop("GITHUB_EVENT_PATH", None)
    env.pop("PR_LABELS", None)
    env["PR_BASE_REF"] = "feature/private-target"

    result = subprocess.run(
        [_bash_executable(), PR_TARGET_VALIDATOR.as_posix()],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Ordinary pull requests should target main" in result.stderr
    assert "Traceback" not in result.stderr


def test_pr_target_branch_workflow_runs_trusted_base_validator() -> None:
    data = _workflow("pr-target-branch.yml")
    text = (WORKFLOW_DIR / "pr-target-branch.yml").read_text(encoding="utf-8")
    job = data["jobs"]["validate-target"]

    assert _trigger_keys(data) == {"pull_request", "merge_group"}
    assert data["on"]["merge_group"]["types"] == ["checks_requested"]
    assert "pull_request_target" not in text
    assert "Validate target branch" in text
    assert job["name"] == "Validate target branch"
    assert job["timeout-minutes"] == 5
    assert "github.event.repository.default_branch" in text
    assert "hashFiles('.github/scripts/validate-pr-target-branch.sh') == ''" in text
    assert "github.event.pull_request.head.sha" in text
    assert "github.event.merge_group.base_ref" in text
    assert "github.event.merge_group.head_ref" in text
    assert "pull-requests: read" in text
    assert "PR_LABELS" in text
    assert "PR_NUMBER" in text
    assert ".github/scripts/validate-pr-target-branch.sh" in text


def test_pr_target_validator_accepts_merge_group_base_ref(tmp_path: Path) -> None:
    result = _validate_pr_target(tmp_path, base="refs/heads/main")

    assert result.returncode == 0
    assert "targets main" in result.stdout

    blocked = _validate_pr_target(tmp_path, base="refs/heads/feature/private-target")

    assert blocked.returncode == 1
    assert "Ordinary pull requests should target main" in blocked.stderr


def test_pr_body_lint_workflow_warns_from_trusted_base() -> None:
    data = _workflow("pr-body-lint.yml")
    text = (WORKFLOW_DIR / "pr-body-lint.yml").read_text(encoding="utf-8")

    assert _trigger_keys(data) == {"pull_request"}
    assert "pull_request_target" not in text
    assert "Validate PR body fields" in text
    assert "github.event.repository.default_branch" in text
    assert "hashFiles('.github/scripts/validate_pr_body.py') == ''" in text
    assert "github.event.pull_request.head.sha" in text
    assert "pull-requests: read" in text
    assert PR_BODY_LINT.as_posix() in text
    assert "PR_BODY_LINT_STRICT: \"0\"" in text


def test_issue_link_sync_tracks_open_and_closed_final_prs_from_trusted_base() -> None:
    data = _workflow("issue-link-sync.yml")
    text = (WORKFLOW_DIR / "issue-link-sync.yml").read_text(encoding="utf-8")

    pull_request_target = data["on"]["pull_request_target"]
    assert set(pull_request_target["types"]) == {"opened", "reopened", "edited", "closed"}
    assert pull_request_target["branches"] == ["main"]
    assert "ref: ${{ github.event.pull_request.base.sha }}" in text
    assert "persist-credentials: false" in text
    assert "issues: write" in text
    assert ".github/scripts/issue_link_sync.py" in text


def test_bash_helper_prefers_git_bash_over_windows_wsl_bash(tmp_path: Path) -> None:
    git_bash = tmp_path / "Git" / "bin" / "bash.exe"

    result = _bash_executable(
        os_name="nt",
        path_lookup=lambda _name: r"C:\Windows\System32\bash.exe",
        exists=lambda path: path == git_bash,
        program_files=str(tmp_path),
    )

    assert result == str(git_bash)


def test_default_ci_uses_layered_job_conditions() -> None:
    data = _workflow("ci.yml")
    jobs = data["jobs"]

    assert "tui-check" in jobs
    assert "required_suites" in jobs["frontend-artifact"]["if"]
    assert "'frontend-artifact'" in jobs["frontend-artifact"]["if"]
    assert jobs["frontend-check"]["needs"] == [
        "plan-ci",
        "frontend-artifact",
    ]
    assert "'frontend-validation'" in jobs["frontend-check"]["if"]
    assert "'wheel-webui-roundtrip'" in jobs["frontend-check"]["if"]
    assert jobs["gateway-contract-windows"]["needs"] == [
        "plan-ci",
        "frontend-check",
    ]
    assert "'frontend-validation'" in jobs["gateway-contract-windows"]["if"]
    assert "'tui'" in jobs["tui-check"]["if"]
    assert "'desktop-static'" in jobs["desktop-check"]["if"]
    assert "'python-targeted'" in jobs["ubuntu-quality"]["if"]
    assert "'python-full'" in jobs["ubuntu-quality"]["if"]
    assert "'python-full'" in jobs["ubuntu-full"]["if"]
    assert "windows-compat" not in jobs
    assert "'windows-high-risk'" in jobs["windows-full"]["if"]
    assert "'macos-recovery'" in jobs["macos-recovery"]["if"]
    assert "frontend_changed == 'true'" not in jobs["desktop-recovery-e2e"]["if"]
    assert "'webui-chat-recovery'" in jobs["webui-chat-recovery"]["if"]
    assert "required_suites" in jobs["desktop-recovery-e2e"]["if"]
    assert "desktop-recovery-e2e" in jobs["desktop-recovery-e2e"]["if"]
    assert "'skill-hub'" in jobs["skill-hub"]["if"]
    assert "'release-packaging'" in jobs["release-packaging"]["if"]
    assert "tui-check" in jobs["ci-result"]["needs"]
    assert "webui-chat-recovery" in jobs["ci-result"]["needs"]
    assert "desktop-check" in jobs["ci-result"]["needs"]
    assert "ubuntu-full" in jobs["ci-result"]["needs"]
    assert "macos-recovery" in jobs["ci-result"]["needs"]
    assert "desktop-recovery-e2e" in jobs["ci-result"]["needs"]
    assert "managed-toolchain-artifacts" in jobs["ci-result"]["needs"]
    assert "gateway-contract-windows" in jobs["ci-result"]["needs"]
    artifact_e2e = jobs["managed-toolchain-artifacts"]
    assert artifact_e2e["uses"] == "./.github/workflows/managed-toolchain-artifacts.yml"
    assert "'managed-toolchain'" in artifact_e2e["if"]


def test_gateway_contract_hashes_are_compared_between_linux_and_windows() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    linux_steps = jobs["frontend-check"]["steps"]
    windows = jobs["gateway-contract-windows"]
    windows_steps = windows["steps"]

    linux_integration = next(
        step
        for step in linux_steps
        if step.get("name") == "Run real Gateway Contract toolchain integration"
    )
    linux_manifest = next(
        step
        for step in linux_steps
        if step.get("name") == "Write Linux Gateway Contract hash manifest"
    )
    upload = next(
        step
        for step in linux_steps
        if step.get("name") == "Upload Linux Gateway Contract hash manifest"
    )
    download = next(
        step
        for step in windows_steps
        if step.get("name") == "Download Linux Gateway Contract hash manifest"
    )
    compare = next(
        step
        for step in windows_steps
        if step.get("name") == "Compare Linux and Windows Contract hashes"
    )

    assert windows["runs-on"] == "windows-latest"
    assert windows["timeout-minutes"] == 60
    assert "tests/contracts" in linux_integration["run"]
    assert linux_integration["env"]["PYTHONPATH"] == (
        "${{ github.workspace }}:${{ github.workspace }}/src"
    )
    windows_verification = next(
        step
        for step in windows_steps
        if step.get("name") == "Verify Windows Contract generation and real toolchain"
    )
    assert "tests/contracts" in windows_verification["run"]
    assert windows_verification["env"]["PYTHONPATH"] == (
        "${{ github.workspace }};${{ github.workspace }}/src"
    )
    assert "--hash-manifest" in linux_manifest["run"]
    assert upload["with"]["name"] == "gateway-contract-hashes-linux"
    assert download["with"]["name"] == upload["with"]["name"]
    assert "--hash-manifest" in compare["run"]
    assert "--compare-hash-manifests" in compare["run"]


def test_ci_result_gate_covers_every_conditional_job_without_legacy_flags() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    gate = jobs["ci-result"]
    gate_step = next(
        step for step in gate["steps"] if step.get("name") == "Check required CI results"
    )

    assert gate["name"] == "CI result"
    setup_python = next(step for step in gate["steps"] if step.get("name") == "Set up Python")
    assert setup_python["with"]["python-version"] == "3.12"
    assert set(gate["needs"]) == {
        "plan-ci",
        "workflow-lint",
        "readme-locale-check",
        "frontend-artifact",
        "frontend-check",
        "gateway-contract-windows",
        "webui-chat-recovery",
        "tui-check",
        "desktop-check",
        "ubuntu-quality",
        "ubuntu-full",
        "windows-full",
        "macos-recovery",
        "desktop-recovery-e2e",
        "skill-hub",
        "release-packaging",
        "managed-toolchain-artifacts",
        "queue-attestation",
        "main-canary",
    }
    assert gate_step["run"] == "python .github/scripts/check_ci_results.py"
    assert gate_step["env"]["RESULT_PLANNER"] == "${{ needs.plan-ci.result }}"
    assert gate_step["env"]["RESULT_FRONTEND_ARTIFACT"] == (
        "${{ needs.frontend-artifact.result }}"
    )
    assert gate_step["env"]["RESULT_CONTRACT_WINDOWS"] == (
        "${{ needs.gateway-contract-windows.result }}"
    )
    assert gate_step["env"]["RESULT_UBUNTU_FULL"] == "${{ needs.ubuntu-full.result }}"
    assert gate_step["env"]["RESULT_MACOS_RECOVERY"] == (
        "${{ needs.macos-recovery.result }}"
    )
    assert gate_step["env"]["RESULT_DESKTOP_RECOVERY_E2E"] == (
        "${{ needs.desktop-recovery-e2e.result }}"
    )
    assert gate_step["env"]["RESULT_MANAGED_TOOLCHAIN_ARTIFACTS"] == (
        "${{ needs.managed-toolchain-artifacts.result }}"
    )
    assert gate_step["env"]["RESULT_SKILL_HUB"] == "${{ needs.skill-hub.result }}"
    assert not any(key.startswith("FLAG_") for key in gate_step["env"])
    assert set(gate_step["env"]) == {
        "RESULT_PLANNER",
        "RESULT_WORKFLOW_LINT",
        "RESULT_README_LOCALE",
        "RESULT_FRONTEND_ARTIFACT",
        "RESULT_FRONTEND",
        "RESULT_CONTRACT_WINDOWS",
        "RESULT_TUI",
        "RESULT_DESKTOP",
        "RESULT_UBUNTU",
        "RESULT_UBUNTU_FULL",
        "RESULT_WINDOWS_FULL",
        "RESULT_MACOS_RECOVERY",
        "RESULT_DESKTOP_RECOVERY_E2E",
        "RESULT_WEBUI_CHAT_RECOVERY",
        "RESULT_RELEASE",
        "RESULT_MANAGED_TOOLCHAIN_ARTIFACTS",
        "RESULT_SKILL_HUB",
        "REQUIRED_SUITES",
    }


def test_desktop_recovery_e2e_runs_compiled_flows_on_all_release_platforms() -> None:
    job = _workflow("ci.yml")["jobs"]["desktop-recovery-e2e"]
    steps = job["steps"]

    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["include"] == (
        "${{ fromJSON(needs.plan-ci.outputs.desktop_matrix) }}"
    )
    download = next(
        step for step in steps if step.get("name") == "Download verified frontend artifact"
    )
    setup_node = next(step for step in steps if step.get("name") == "Set up Node.js")
    verify_frontend = next(
        step
        for step in steps
        if step.get("name") == "Verify downloaded frontend artifact on consumer OS"
    )
    build = next(step for step in steps if step.get("name") == "Build Desktop TypeScript")
    session_recovery = next(
        step
        for step in steps
        if step.get("name")
        == "Run cross-platform production-dist browser session hang contract"
    )
    playwright_cache = next(
        step for step in steps if step.get("name") == "Restore Playwright browser"
    )
    electron_cache = next(
        step for step in steps if step.get("name") == "Restore Electron binary cache"
    )
    electron_cache_seed = next(
        step
        for step in steps
        if step.get("name") == "Seed Electron binary cache from nightly main"
    )
    run = next(
        step for step in steps if step.get("name") == "Run compiled Desktop recovery flows"
    )
    summary_upload = next(
        step for step in steps if step.get("name") == "Upload Desktop recovery summary"
    )
    failure_upload = next(
        step
        for step in steps
        if step.get("name") == "Upload Desktop recovery failure report"
    )

    assert steps.index(download) < steps.index(setup_node) < steps.index(verify_frontend)
    assert verify_frontend["shell"] == "bash"
    assert verify_frontend["run"] == (
        "node opensquilla-webui/scripts/verify-dist.mjs opensquilla-webui/dist"
    )
    assert build["run"] == "npm run build"
    assert session_recovery["working-directory"] == "opensquilla-webui"
    assert session_recovery["env"]["OPENSQUILLA_PLAYWRIGHT_MANAGE_WEBUI"] == "gateway"
    assert session_recovery["env"]["OPENSQUILLA_WEBUI_BASE_URL"].endswith(":18791")
    assert session_recovery["if"] == (
        "${{ (runner.os == 'Windows' || runner.os == 'macOS') && "
        "matrix.shard == 'profiles' }}"
    )
    assert "history-hydration.spec.ts" in session_recovery["run"]
    assert '--grep "terminates stalled"' in session_recovery["run"]
    assert playwright_cache["uses"] == "actions/cache/restore@v4"
    assert playwright_cache["with"]["path"] == "${{ env.PLAYWRIGHT_BROWSERS_PATH }}"
    assert job["env"]["PLAYWRIGHT_BROWSERS_PATH"] == (
        "${{ github.workspace }}/.cache/ms-playwright"
    )
    assert job["env"]["ELECTRON_CACHE"] == "${{ github.workspace }}/.cache/electron"
    assert job["env"]["OPENSQUILLA_DESKTOP_CASE_TIMEOUT_MS"] == "900000"
    assert electron_cache["uses"] == "actions/cache/restore@v4"
    assert electron_cache["with"]["path"] == "${{ env.ELECTRON_CACHE }}"
    assert "hashFiles('desktop/electron/package-lock.json')" in electron_cache["with"]["key"]
    assert electron_cache_seed["uses"] == "actions/cache/save@v4"
    assert job["env"]["OPENSQUILLA_WORKBENCH_E2E_MODE"] == (
        "${{ (github.event_name == 'pull_request' || github.event_name == 'merge_group') "
        "&& 'smoke' || 'stress' }}"
    )
    prepare = next(
        step for step in steps if step.get("name") == "Prepare Desktop recovery report"
    )
    assert "workbench_e2e_mode" in prepare["run"]
    assert "${{ runner.arch }}" in playwright_cache["with"]["key"]
    assert "steps.playwright-browser.outputs.revision" in playwright_cache["with"]["key"]
    assert "restore-keys" not in playwright_cache["with"]
    assert "xvfb-run -a node" in run["run"]
    assert "test-profile-consolidation-flow.mjs" in run["run"]
    assert "test-primary-repair-accessibility.mjs" in run["run"]
    assert "test-profile-import-flow.mjs" in run["run"]
    assert run["run"].count("'onboarding-flow:scripts/test-onboarding-flow.mjs'") == 2
    assert 'if [[ "${RUNNER_OS}" == "macOS" ]]' in run["run"]
    assert "test-desktop-cleanup-flow.mjs" in run["run"]
    assert "test-desktop-gateway-ownership.mjs" in run["run"]
    assert "test-unsafe-legacy-recovery-no-write.mjs" in run["run"]
    assert 'case "${{ matrix.shard }}" in' in run["run"]
    assert 'local log_path="${CI_REPORT_DIR}/${name}-attempt-${attempt}.log"' in run["run"]
    assert "node scripts/ci-case-telemetry.mjs run" in run["run"]
    assert '--shard "${{ matrix.shard }}"' in run["run"]
    assert '--attempt "${attempt}"' in run["run"]
    assert 'desktop-e2e-cases.jsonl' in run["run"]
    assert "classify_retryable_infrastructure_failure()" in run["run"]
    assert 'if classify_retryable_infrastructure_failure "${name}" "${first_log}"' in (
        run["run"]
    )
    assert '"windows-delete-helper-handoff-timeout-v1"' in run["run"]
    assert '"windows-isolated-acl-worker-timeout-v1"' in run["run"]
    assert '"windows-loopback-no-buffer-space-v1"' in run["run"]
    assert '"macos-electron-foreground-prerequisite-v1"' in run["run"]
    assert '"cases": {"desktop-cleanup-flow"}' in run["run"]
    assert '"cases": {"offline-document-workbench-e2e"}' in run["run"]
    assert '"classification": matches[0] if retryable else "non_retryable"' in (
        run["run"]
    )
    assert '"log_sha256": hashlib.sha256(payload).hexdigest()' in run["run"]
    assert '"TRUSTED_OVERLAY_INPUT_CONTRACT_FAILED:"' in run["run"]
    assert '"DESKTOP_E2E_PHASE_TIMEOUT:"' in run["run"]
    assert "Gateway did not become healthy" not in run["run"]
    assert "grep -Fq" not in run["run"]
    assert 'run_case "${name}" "${script}" 2' in run["run"]
    assert "exit 1" in run["run"]
    assert summary_upload["if"] == "${{ success() }}"
    assert summary_upload["with"]["name"] == (
        "desktop-recovery-e2e-${{ matrix.os }}-${{ matrix.shard }}"
        "-attempt-${{ github.run_attempt }}"
    )
    assert "desktop-e2e-cases.jsonl" in summary_upload["with"]["path"]
    assert "retry-classifications.jsonl" in summary_upload["with"]["path"]
    assert "retry-evidence" in summary_upload["with"]["path"]
    assert "*.log" not in summary_upload["with"]["path"]
    assert failure_upload["if"] == "${{ failure() }}"
    assert failure_upload["with"]["path"] == (
        "${{ runner.temp }}/desktop-recovery-e2e"
    )
    desktop_unit = next(
        step
        for step in _workflow("ci.yml")["jobs"]["desktop-check"]["steps"]
        if step.get("name") == "Run desktop unit tests"
    )
    assert "node scripts/test-ci-case-telemetry.mjs" in desktop_unit["run"]


@pytest.mark.parametrize("line_ending", ("\n", "\r\n"), ids=("lf", "crlf"))
@pytest.mark.parametrize(
    ("case_name", "runner_os", "message", "expected_signature"),
    (
        (
            "desktop-cleanup-flow",
            "Windows",
            "Error: Timed out waiting for post-exit delete-all helper completion: ; "
            "pending synthetic targets: synthetic-home\n",
            "windows-delete-helper-handoff-timeout-v1",
        ),
        (
            "offline-document-workbench-e2e",
            "Windows",
            "Traceback (most recent call last):\n"
            "    at synthetic_allowed_stack\n"
            "E           AssertionError: isolated Windows ACL hardening timed out: "
            "stdout='synthetic'\n"
            "FAILED tests/synthetic.py - AssertionError: isolated Windows ACL hardening "
            "timed out: stdout='synthetic'\n",
            "windows-isolated-acl-worker-timeout-v1",
        ),
        (
            "offline-document-workbench-e2e",
            "Windows",
            "electronApplication.evaluate: Error: "
            "ERR_NO_BUFFER_SPACE (-176) loading 'http://127.0.0.1:54108/one'\n"
            "    at synthetic_allowed_stack (native-workbench.mjs:1:1)\n"
            "Error: C:\\synthetic\\node.exe "
            "D:\\synthetic\\test-native-workbench-v2-electron.mjs failed with exit "
            "code 1\n"
            "    at synthetic_outer_stack (offline-workbench.mjs:1:1)\n",
            "windows-loopback-no-buffer-space-v1",
        ),
        (
            "offline-document-workbench-e2e",
            "macOS",
            "electronApplication.evaluate: Error: "
            "ELECTRON_FOREGROUND_PREREQUISITE_MISSING: owner is not foreground\n"
            "    at synthetic_allowed_stack (native-workbench.mjs:1:1)\n"
            "Error: /synthetic/test-native-workbench-v2-electron.mjs failed with exit "
            "code 1\n"
            "    at synthetic_outer_stack (offline-workbench.mjs:1:1)\n"
            "Error: ELECTRON_FOREGROUND_PREREQUISITE_MISSING: owner is not foreground\n",
            "macos-electron-foreground-prerequisite-v1",
        ),
    ),
)
def test_desktop_retry_classifier_accepts_only_structured_infrastructure_signatures(
    tmp_path: Path,
    case_name: str,
    runner_os: str,
    message: str,
    expected_signature: str,
    line_ending: str,
) -> None:
    run = next(
        step["run"]
        for step in _workflow("ci.yml")["jobs"]["desktop-recovery-e2e"]["steps"]
        if step.get("name") == "Run compiled Desktop recovery flows"
    )
    classifier = run.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    log = tmp_path / "attempt-1.log"
    output = tmp_path / "classifications.jsonl"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    payload = message.replace("\n", line_ending).encode()
    log.write_bytes(payload)

    accepted = subprocess.run(
        [sys.executable, "-", case_name, runner_os, str(log), str(output), str(evidence)],
        input=classifier,
        text=True,
        check=False,
    )

    assert accepted.returncode == 0
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["classification"] == expected_signature
    assert record["retryable"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", record["log_sha256"])
    assert (evidence / log.name).read_bytes() == payload

    # The same wording from a different functional case is not retryable.
    rejected = subprocess.run(
        [sys.executable, "-", "theme-flow", runner_os, str(log), str(output), str(evidence)],
        input=classifier,
        text=True,
        check=False,
    )
    assert rejected.returncode == 1
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["classification"] == "non_retryable"
    assert records[-1]["retryable"] is False


@pytest.mark.parametrize(
    "message",
    (
        "electronApplication.evaluate: Error: "
        "ERR_NO_BUFFER_SPACE (-176) loading 'http://127.0.0.1:54108/two'\n",
        "electronApplication.evaluate: Error: "
        "ERR_NO_BUFFER_SPACE (-176) loading 'http://localhost:54108/one'\n",
        "electronApplication.evaluate: Error: "
        "ERR_CONNECTION_RESET (-101) loading 'http://127.0.0.1:54108/one'\n",
        "electronApplication.evaluate: Error: "
        "ERR_NO_BUFFER_SPACE (-176) loading 'http://127.0.0.1:54108/one'\n"
        "Error: C:\\synthetic\\node.exe "
        "D:\\synthetic\\test-native-workbench-v2-electron.mjs failed with exit "
        "code 1\n"
        "AssertionError: saved document content did not match\n",
    ),
)
def test_desktop_retry_classifier_rejects_similar_windows_loopback_failures(
    tmp_path: Path,
    message: str,
) -> None:
    run = next(
        step["run"]
        for step in _workflow("ci.yml")["jobs"]["desktop-recovery-e2e"]["steps"]
        if step.get("name") == "Run compiled Desktop recovery flows"
    )
    classifier = run.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    log = tmp_path / "attempt-1.log"
    output = tmp_path / "classifications.jsonl"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    log.write_text(message, encoding="utf-8")

    rejected = subprocess.run(
        [
            sys.executable,
            "-",
            "offline-document-workbench-e2e",
            "Windows",
            str(log),
            str(output),
            str(evidence),
        ],
        input=classifier,
        text=True,
        check=False,
    )

    assert rejected.returncode == 1
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["classification"] == "non_retryable"
    assert record["retryable"] is False
    assert list(evidence.iterdir()) == []


def test_desktop_retry_classifier_rejects_generic_product_failures(tmp_path: Path) -> None:
    run = next(
        step["run"]
        for step in _workflow("ci.yml")["jobs"]["desktop-recovery-e2e"]["steps"]
        if step.get("name") == "Run compiled Desktop recovery flows"
    )
    classifier = run.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    log = tmp_path / "attempt-1.log"
    output = tmp_path / "classifications.jsonl"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    log.write_text(
        "AssertionError: expected saved document content to equal the submitted content\n"
        "Error: Gateway did not become healthy\n",
        encoding="utf-8",
    )

    rejected = subprocess.run(
        [
            sys.executable,
            "-",
            "offline-document-workbench-e2e",
            "Windows",
            str(log),
            str(output),
            str(evidence),
        ],
        input=classifier,
        text=True,
        check=False,
    )

    assert rejected.returncode == 1
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["classification"] == "non_retryable"
    assert record["retryable"] is False
    assert list(evidence.iterdir()) == []

    # A functional contract marker always wins even if a runner signature is
    # also present in the combined diagnostic log.
    log.write_text(
        "Error: ELECTRON_FOREGROUND_PREREQUISITE_MISSING: synthetic\n"
        "Error: TRUSTED_OVERLAY_INPUT_CONTRACT_FAILED: wrong submitted value\n",
        encoding="utf-8",
    )
    hard_failure = subprocess.run(
        [
            sys.executable,
            "-",
            "offline-document-workbench-e2e",
            "macOS",
            str(log),
            str(output),
            str(evidence),
        ],
        input=classifier,
        text=True,
        check=False,
    )
    assert hard_failure.returncode == 1
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["classification"] == "non_retryable"
    assert records[-1]["blocked_markers"] == [
        "trusted-overlay-input-contract-failed-v1"
    ]


@pytest.mark.parametrize(
    ("additional_failure", "expected_marker"),
    (
        (
            "AssertionError: submitted document content differs from the saved revision\n",
            "generic-assertion-error-v1",
        ),
        (
            "FATAL: renderer process crashed while committing the document\n",
            "fatal-crash-process-exit-v1",
        ),
    ),
)
def test_desktop_retry_classifier_rejects_allowed_signature_with_another_terminal_failure(
    tmp_path: Path,
    additional_failure: str,
    expected_marker: str,
) -> None:
    run = next(
        step["run"]
        for step in _workflow("ci.yml")["jobs"]["desktop-recovery-e2e"]["steps"]
        if step.get("name") == "Run compiled Desktop recovery flows"
    )
    classifier = run.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    log = tmp_path / "attempt-1.log"
    output = tmp_path / "classifications.jsonl"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    log.write_text(
        "electronApplication.evaluate: Error: "
        "ELECTRON_FOREGROUND_PREREQUISITE_MISSING: owner is not foreground\n"
        "    at synthetic_allowed_stack (native-workbench.mjs:1:1)\n"
        "Error: /synthetic/test-native-workbench-v2-electron.mjs failed with exit code 1\n"
        + additional_failure,
        encoding="utf-8",
    )

    rejected = subprocess.run(
        [
            sys.executable,
            "-",
            "offline-document-workbench-e2e",
            "macOS",
            str(log),
            str(output),
            str(evidence),
        ],
        input=classifier,
        text=True,
        check=False,
    )

    assert rejected.returncode == 1
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["classification"] == "non_retryable"
    assert record["retryable"] is False
    assert record["blocked_markers"] == [expected_marker]
    assert all("submitted document content" not in marker for marker in record["blocked_markers"])
    assert list(evidence.iterdir()) == []


def test_v1_editor_failure_evidence_is_captured_before_desktop_shutdown() -> None:
    script = Path(
        "desktop/electron/scripts/test-v1-html-agent-edit-e2e.mjs"
    ).read_text(encoding="utf-8")
    finally_block = script.index("} finally {")
    durable_check = script.index(
        "evidence.durableMutation = await readDurableMutationEvidence", finally_block
    )
    failure_capture = script.index(
        "failureEvidence = await captureFailureEvidence", finally_block
    )
    app_close = script.index(
        "await closeDesktopApp(app, 'final-electron-shutdown')", finally_block
    )

    assert durable_check < failure_capture < app_close
    assert "async function diagnosticCall" in script
    assert "const gateway = await gatewayHealthSnapshot" in script
    assert "renderer shell snapshot" in script
    assert "failure-attempt-${attempt}-${Date.now()}" in script


def test_ci_evidence_artifacts_are_replaceable_across_rerun_attempts() -> None:
    steps = _workflow("ci.yml")["jobs"]["ci-result"]["steps"]
    names = {
        "Upload tree-indexed CI evidence v2",
        "Upload PR-head-indexed CI evidence v2",
        "Upload full-nightly health evidence",
    }
    uploads = {step["name"]: step for step in steps if step.get("name") in names}

    assert set(uploads) == names
    for upload in uploads.values():
        assert upload["uses"] == "actions/upload-artifact@v4"
        assert upload["with"]["overwrite"] is True


def test_webui_chat_recovery_runs_the_verified_dist_through_gateway() -> None:
    job = _workflow("ci.yml")["jobs"]["webui-chat-recovery"]
    steps = job["steps"]
    download = next(
        step for step in steps if step.get("name") == "Download verified frontend artifact"
    )
    install_gateway = next(
        step for step in steps if step.get("name") == "Install Gateway dependencies"
    )
    run = next(
        step
        for step in steps
        if step.get("name")
        == "Run production-dist chat and Goal recovery browser contracts"
    )

    assert job["needs"] == ["plan-ci", "frontend-artifact"]
    assert download["with"]["name"] == "opensquilla-webui-dist"
    assert download["with"]["path"] == "opensquilla-webui/dist/"
    stage = next(
        step
        for step in steps
        if step.get("name") == "Stage verified frontend artifact for Gateway"
    )
    assert stage["working-directory"] == "opensquilla-webui"
    assert steps.index(download) < steps.index(install_gateway) < steps.index(run)
    assert install_gateway["run"] == "uv sync --frozen"
    assert job["env"]["OPENSQUILLA_PLAYWRIGHT_MANAGE_WEBUI"] == "gateway"
    assert job["env"]["OPENSQUILLA_WEBUI_BASE_URL"].endswith(":18791")
    assert "--workers=2" in run["run"]
    selected_specs = {
        argument
        for argument in run["run"].split()
        if argument.endswith(".spec.ts")
    }
    required_specs = {
        "assistant-activity.spec.ts",
        "composer-paste.spec.ts",
        "goal-mode.spec.ts",
        "history-hydration.spec.ts",
        "queue-steer.spec.ts",
        "session-created-card.spec.ts",
        "session-switch-transport.spec.ts",
        "share.spec.ts",
    }
    assert selected_specs == required_specs
    for spec in required_specs:
        assert (Path("opensquilla-webui/e2e") / spec).is_file()


def test_tui_check_owns_the_bun_contract() -> None:
    data = _workflow("ci.yml")
    jobs = data["jobs"]

    tui_steps = jobs["tui-check"]["steps"]
    assert any(step.get("uses") == "oven-sh/setup-bun@v2" for step in tui_steps)
    assert any("bun run test:bun" in step.get("run", "") for step in tui_steps)

    bun_test = next(step for step in tui_steps if step.get("name") == "Run OpenTUI Bun tests")
    bun_run = bun_test["run"]
    assert "for attempt in 1 2" in bun_run
    assert 'status" -ne 132' in bun_run
    assert "retrying once" in bun_run


def test_windows_high_risk_job_runs_parallel_reported_shards() -> None:
    data = _workflow("ci.yml")
    jobs = data["jobs"]
    windows_full = jobs["windows-full"]
    steps = windows_full["steps"]
    test_step = next(step for step in steps if step.get("name") == "Test Windows shard")
    upload_step = next(
        step for step in steps if step.get("name") == "Upload Windows shard report"
    )

    assert windows_full["name"] == "Windows high-risk (${{ matrix.shard }})"
    assert windows_full["timeout-minutes"] == 60
    assert windows_full["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "shard": (
                "${{ fromJSON(needs.plan-ci.outputs.python_matrix).windows }}"
            )
        },
    }
    checkout = next(step for step in steps if step.get("name") == "Check out repository")
    assert checkout["with"]["lfs"] is True
    bun_step = next(step for step in steps if step.get("name") == "Set up Bun")
    assert bun_step["if"] == "${{ matrix.shard == 'core' }}"
    assert steps[0]["name"] == "Prepare diagnostic report"
    assert "OPENSQUILLA_STATE_DIR" not in steps[0]["run"]
    assert "PATH" not in steps[0]["run"]
    assert "HOME" not in steps[0]["run"]
    assert ".github/scripts/windows_test_shards.py run" in test_step["run"]
    assert '"${{ github.event_name }}" == "pull_request"' in test_step["run"]
    assert "--maxfail=3" in test_step["run"]
    assert "--maxfail=1" not in test_step["run"]
    assert '"${{ matrix.shard }}" == "recovery-migration"' in test_step["run"]
    assert '"${{ matrix.shard }}" == "gateway-sqlite"' in test_step["run"]
    assert '"${{ matrix.shard }}" == "desktop-installer-contracts"' in test_step["run"]
    assert "worker_args+=(--workers=2)" in test_step["run"]
    assert '"${worker_args[@]}"' in test_step["run"]
    assert "set -euo pipefail" in test_step["run"]
    assert 'tee "${CI_REPORT_DIR}/pytest.log"' in test_step["run"]
    assert upload_step["if"] == "${{ always() }}"
    assert upload_step["uses"] == "actions/upload-artifact@v4"
    assert upload_step["with"]["if-no-files-found"] == "error"
    assert upload_step["with"]["retention-days"] == 14


def test_recovery_windows_shard_uses_and_always_cleans_distinct_real_volumes() -> None:
    windows_full = _workflow("ci.yml")["jobs"]["windows-full"]
    steps = windows_full["steps"]
    provision_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Provision distinct Windows test volumes"
    )
    test_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Test Windows shard"
    )
    cleanup_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Clean up Windows test volumes"
    )
    provision = steps[provision_index]
    cleanup = steps[cleanup_index]
    provision_script = provision["run"]
    cleanup_script = cleanup["run"]

    assert provision_index < test_index < cleanup_index
    assert provision["if"] == "${{ matrix.shard == 'recovery-migration' }}"
    assert provision["shell"] == "pwsh"
    assert "$env:RUNNER_TEMP" in provision_script
    assert "$volumeB = Join-Path -Path $env:LOCALAPPDATA" in provision_script
    assert "$env:SystemDrive" in provision_script
    assert "[guid]::NewGuid()" in provision_script
    assert "[System.IO.Path]::GetPathRoot($volumeA)" in provision_script
    assert "[System.IO.Path]::GetPathRoot($volumeB)" in provision_script
    assert "throw \"Windows test volume roots must use different drives\"" in provision_script
    assert "OPENSQUILLA_WINDOWS_TEST_VOLUME_A=$volumeA" in provision_script
    assert "OPENSQUILLA_WINDOWS_TEST_VOLUME_B=$volumeB" in provision_script
    assert cleanup["if"] == "${{ always() && matrix.shard == 'recovery-migration' }}"
    assert cleanup["shell"] == "pwsh"
    assert "$env:OPENSQUILLA_WINDOWS_TEST_VOLUME_A" in cleanup_script
    assert "$env:OPENSQUILLA_WINDOWS_TEST_VOLUME_B" in cleanup_script
    assert "Remove-Item -LiteralPath $testRoot -Recurse -Force" in cleanup_script


def test_windows_high_risk_job_cannot_wash_test_failures_green() -> None:
    windows_full = _workflow("ci.yml")["jobs"]["windows-full"]
    test_step = next(
        step for step in windows_full["steps"] if step.get("name") == "Test Windows shard"
    )
    serialized = json.dumps(windows_full, sort_keys=True)

    assert windows_full["strategy"]["fail-fast"] is False
    assert all("continue-on-error" not in step for step in windows_full["steps"])
    assert "--reruns" not in serialized
    assert "pytest-rerunfailures" not in serialized
    assert "continue-on-error" not in serialized
    assert "|| true" not in test_step["run"]
    assert "set -euo pipefail" in test_step["run"]
    assert "github.run_attempt" in serialized


def test_macos_recovery_runs_native_contracts_and_cannot_wash_failures_green() -> None:
    job = _workflow("ci.yml")["jobs"]["macos-recovery"]
    test_step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Test native profile recovery contracts"
    )
    upload_step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Upload macOS recovery report"
    )
    serialized = json.dumps(job, sort_keys=True)

    assert job["name"] == "macOS profile recovery and native no-replace (3.12)"
    assert job["runs-on"] == "macos-latest"
    assert job["timeout-minutes"] == 30
    assert "tests/test_recovery" in test_step["run"]
    assert "tests/test_migration/test_opensquilla_home_migration.py" in test_step["run"]
    assert "tests/test_desktop/test_electron_startup_contract.py" in test_step["run"]
    assert "set -euo pipefail" in test_step["run"]
    assert "pytest_args=(" in test_step["run"]
    assert 'uv run pytest "${pytest_args[@]}"' in test_step["run"]
    assert "maxfail_args" not in test_step["run"]
    assert "--maxfail=3" in test_step["run"]
    assert '--junitxml="${CI_REPORT_DIR}/junit.xml"' in test_step["run"]
    assert 'tee "${CI_REPORT_DIR}/pytest.log"' in test_step["run"]
    assert "status=${PIPESTATUS[0]}" in test_step["run"]
    assert 'exit "${status}"' in test_step["run"]
    assert upload_step["if"] == "${{ always() }}"
    assert upload_step["with"]["if-no-files-found"] == "error"
    assert "github.run_attempt" in upload_step["with"]["name"]
    assert "continue-on-error" not in serialized
    assert "--reruns" not in serialized
    assert "pytest-rerunfailures" not in serialized
    assert "|| true" not in test_step["run"]


def test_macos_recovery_planner_inputs_match_workflow_pytest_targets() -> None:
    config = json.loads(Path(".github/ci/suites.v1.json").read_text(encoding="utf-8"))
    expected_targets = {
        path[:-3] if path.endswith("/**") else path
        for path in config["macos_recovery_test_inputs"]
    }
    job = _workflow("ci.yml")["jobs"]["macos-recovery"]
    test_step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Test native profile recovery contracts"
    )
    array = re.search(r"pytest_args=\(\n(?P<body>.*?)\n\s*\)", test_step["run"], re.DOTALL)

    assert array is not None
    workflow_targets = {
        line.strip()
        for line in array.group("body").splitlines()
        if line.strip().startswith("tests/")
    }
    assert workflow_targets == expected_targets


def test_ubuntu_quality_keeps_targeted_pr_tests_and_full_ci_uses_balanced_matrix() -> None:
    data = _workflow("ci.yml")
    ubuntu_steps = data["jobs"]["ubuntu-quality"]["steps"]
    checkout = ubuntu_steps[0]
    test_step = next(
        step for step in ubuntu_steps if step.get("name") == "Test targeted PR suite"
    )
    ubuntu_full = data["jobs"]["ubuntu-full"]
    full_test_step = next(
        step for step in ubuntu_full["steps"] if step.get("name") == "Test Ubuntu full shard"
    )

    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"]["lfs"] == (
        "${{ contains(fromJSON(needs.plan-ci.outputs.required_suites), "
        "'python-full') }}"
    )
    assert test_step["if"] == (
        "${{ contains(fromJSON(needs.plan-ci.outputs.required_suites), "
        "'python-targeted') && !contains(fromJSON(needs.plan-ci.outputs.required_suites), "
        "'python-full') }}"
    )
    assert "required_suites" in ubuntu_full["if"]
    assert "'python-full'" in ubuntu_full["if"]
    assert "uv run pytest" in test_step["run"]
    assert "tests/test_artifacts.py" not in test_step["run"]
    assert "--ignore=tests/test_ci/test_router_artifact_manifest.py" in test_step["run"]
    assert "tests/unit" in test_step["run"]
    assert "TARGETED_PYTEST_TARGETS" in test_step["env"]
    assert 'pytest_targets+=("${target}")' in test_step["run"]
    assert "tests/test_recovery" not in test_step["run"]
    assert ubuntu_full["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "shard": (
                "${{ fromJSON(needs.plan-ci.outputs.python_matrix).ubuntu }}"
            )
        },
    }
    assert ubuntu_full["timeout-minutes"] == 20
    assert ".github/scripts/windows_test_shards.py run" in full_test_step["run"]
    assert '"${{ matrix.shard }}" == "gateway-sqlite"' in full_test_step["run"]
    assert "worker_args+=(--workers=2)" in full_test_step["run"]
    assert "maxfail_args+=(--maxfail=3)" in full_test_step["run"]
    assert '"${maxfail_args[@]}"' in full_test_step["run"]
    assert "--reruns" not in json.dumps(ubuntu_full, sort_keys=True)
    assert all("continue-on-error" not in step for step in ubuntu_full["steps"])


def test_manual_workflows_reference_existing_test_files() -> None:
    for text in _workflow_texts():
        for raw_path in TEST_PATH_RE.findall(text):
            assert Path(raw_path).is_file(), f"workflow references missing test: {raw_path}"


def test_webui_browser_workflow_is_manual_and_opt_in() -> None:
    data = _workflow("webui-browser-smoke.yml")
    text = (WORKFLOW_DIR / "webui-browser-smoke.yml").read_text(encoding="utf-8")

    assert _trigger_keys(data) == {"workflow_dispatch"}
    assert 'OPENSQUILLA_WEBUI_BROWSER_E2E: "1"' in text
    assert "tests/functional/test_webui_browser_e2e.py" in text
    assert "playwright install chromium" in text


def test_manual_browser_workflow_builds_the_verified_webui_from_source() -> None:
    data = _workflow("webui-browser-smoke.yml")
    steps = data["jobs"]["webui-browser-smoke"]["steps"]
    setup_node = next(step for step in steps if step.get("name") == "Set up Node")
    install = next(
        step for step in steps if step.get("name") == "Install Web UI dependencies"
    )
    build = next(step for step in steps if step.get("name") == "Build and verify Web UI")

    assert setup_node["with"]["node-version-file"] == "opensquilla-webui/.node-version"
    assert setup_node["with"]["cache-dependency-path"] == (
        "opensquilla-webui/package-lock.json"
    )
    assert install == {
        "name": "Install Web UI dependencies",
        "working-directory": "opensquilla-webui",
        "run": "npm ci",
    }
    assert build == {
        "name": "Build and verify Web UI",
        "working-directory": "opensquilla-webui",
        "run": "npm run build",
    }
    test_index = next(
        index
        for index, step in enumerate(steps)
        if "tests/functional/test_webui_browser_e2e.py" in step.get("run", "")
    )
    assert steps.index(install) < steps.index(build) < test_index


def test_llm_workflow_is_single_manual_smoke() -> None:
    data = _workflow("llm-e2e.yml")
    text = (WORKFLOW_DIR / "llm-e2e.yml").read_text(encoding="utf-8")

    assert _trigger_keys(data) == {"workflow_dispatch"}
    assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in text
    assert "tests/functional/test_llm_smoke.py" in text
    assert "llm_costly" not in text
    assert "tests/functional/test_webui_llm_e2e.py" not in text


def test_live_release_e2e_workflow_is_manual_and_separates_private_inputs() -> None:
    data = _workflow("live-release-e2e.yml")
    text = (WORKFLOW_DIR / "live-release-e2e.yml").read_text(encoding="utf-8")

    assert _trigger_keys(data) == {"workflow_dispatch"}
    assert "tests/functional/test_gateway_llm_e2e.py" in text
    assert "tests/functional/test_live_channel_telegram_smoke.py" in text
    assert "test_webui_browser_chat_e2e.py" not in text
    assert "OPENSQUILLA_WEBUI_BROWSER_CHAT_E2E" not in text
    assert "playwright install chromium" not in text
    assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in text
    assert (
        "OPENSQUILLA_LIVE_TELEGRAM_BOT_TOKEN: "
        "${{ secrets.OPENSQUILLA_LIVE_TELEGRAM_BOT_TOKEN }}"
    ) in text
    assert (
        "OPENSQUILLA_LIVE_TELEGRAM_CHAT_ID: "
        "${{ secrets.OPENSQUILLA_LIVE_TELEGRAM_CHAT_ID }}"
    ) in text
    assert "tests/private" not in text


def test_default_ci_stays_offline_and_does_not_run_live_gates() -> None:
    text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")

    assert "OPENROUTER_API_KEY" not in text
    assert "OPENSQUILLA_LIVE_TELEGRAM" not in text
    assert "OPENSQUILLA_GATEWAY_LLM_E2E" not in text
    assert "OPENSQUILLA_WEBUI_BROWSER_E2E" not in text
    assert "OPENSQUILLA_WEBUI_BROWSER_CHAT_E2E" not in text
    assert "test_gateway_llm_e2e.py" not in text
    assert "test_live_channel_telegram_smoke.py" not in text


def test_live_release_e2e_fails_fast_when_required_provider_secret_is_missing() -> None:
    text = (WORKFLOW_DIR / "live-release-e2e.yml").read_text(encoding="utf-8")

    assert "Fail if OpenRouter secret is missing" in text
    assert 'if [ -z "$OPENROUTER_API_KEY" ]; then' in text
    assert "OPENROUTER_API_KEY GitHub secret is required" in text
    assert "Fail if Telegram secrets are missing when channel smoke is enabled" in text
    assert 'if [ -z "$OPENSQUILLA_LIVE_TELEGRAM_BOT_TOKEN" ]' in text
    assert 'if [ -z "$OPENSQUILLA_LIVE_TELEGRAM_CHAT_ID" ]' in text


def test_wheelhouse_release_publishes_only_recommended_router_profile() -> None:
    text = (WORKFLOW_DIR / "wheelhouse-release.yml").read_text(encoding="utf-8")

    assert "      profile:\n" not in text
    assert "RELEASE_PROFILE: recommended" in text
    assert "opensquilla-release-assets-python-${{ env.RELEASE_PROFILE }}" in text
    assert "opensquilla-release-assets-${{ env.RELEASE_PROFILE }}" in text
    assert "--profile \"${RELEASE_PROFILE}\"" not in text
    assert "- core" not in text


def test_release_jobs_share_one_rerun_stable_verified_webui_artifact() -> None:
    workflow = _workflow("wheelhouse-release.yml")
    jobs = workflow["jobs"]
    artifact_name = "opensquilla-release-webui-dist"
    build_steps = jobs["build-control-ui"]["steps"]
    upload = next(
        step
        for step in build_steps
        if step.get("name") == "Upload source-owned Web UI artifact"
    )
    release_build = next(
        step for step in build_steps if step.get("name") == "Build and verify Web UI"
    )
    detect = next(
        step for step in build_steps if step.get("name") == "Detect Web UI artifact contract"
    )
    legacy = next(
        step for step in build_steps if step.get("name") == "Validate legacy committed Web UI"
    )

    assert upload["with"]["name"] == artifact_name
    assert upload["with"]["path"] == "opensquilla-webui/dist/"
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] >= 31
    assert upload["with"]["overwrite"] is True
    assert "npm run verify:release-dist" in release_build["run"]
    assert release_build["if"] == "steps.webui-contract.outputs.mode != 'legacy-committed'"
    assert "legacy-committed" in detect["run"]
    assert "legacy-source-built" in detect["run"]
    assert "scripts/stage-dist.mjs" in detect["run"]
    assert "src/opensquilla/gateway/static/dist/index.html" in detect["run"]
    assert legacy["if"] == "steps.webui-contract.outputs.mode == 'legacy-committed'"
    assert jobs["build-control-ui"]["outputs"]["webui_mode"] == (
        "${{ steps.webui-contract.outputs.mode }}"
    )
    assert 'data.get("tracks") == []' in legacy["run"]
    for job_name in (
        "build-release-assets",
        "build-desktop-macos",
        "build-desktop-windows",
    ):
        job = jobs[job_name]
        assert job["needs"] == "build-control-ui"
        download = next(
            step
            for step in job["steps"]
            if step.get("name") == "Download verified Web UI artifact"
        )
        assert download["with"]["name"] == artifact_name
        assert "needs.build-control-ui.outputs.webui_mode" in download["with"]["path"]
        assert "opensquilla-webui/dist/" in download["with"]["path"]
        assert "src/opensquilla/gateway/static/dist/" in download["with"]["path"]
        stage = next(
            step
            for step in job["steps"]
            if step.get("name") == "Stage verified Web UI artifact for packaging"
            or step.get("name") == "Stage verified Web UI artifact for Desktop"
        )
        assert stage["if"] == "needs.build-control-ui.outputs.webui_mode == 'source-built'"

    all_uploads = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if step.get("uses") == "actions/upload-artifact@v4"
    ]
    assert all_uploads
    assert all(step["with"].get("overwrite") is True for step in all_uploads)

    wheel_steps = jobs["build-release-assets"]["steps"]
    verify = next(
        step
        for step in wheel_steps
        if step.get("name") == "Verify wheel contains the exact Web UI artifact"
    )
    assert "python scripts/verify_webui_artifact.py" in verify["run"]
    assert "--forbid-personal-bgm" in verify["run"]
    assert '--wheel "${wheels[0]}"' in verify["run"]
    assert "legacy wheel Web UI differs from committed artifact" in verify["run"]
    smoke = next(
        step
        for step in wheel_steps
        if step.get("name") == "Smoke versioned release artifacts"
    )
    assert 'if Path("scripts/verify_webui_artifact.py").is_file()' in smoke["run"]


def test_container_release_smoke_serves_control_ui_entry_assets() -> None:
    data = _workflow("docker-image.yml")
    steps = data["jobs"]["build-and-publish"]["steps"]
    smoke = next(step for step in steps if step.get("name") == "Smoke pushed image HEALTHCHECK")
    script = smoke["run"]

    assert "http://127.0.0.1:18791/control/" in script
    assert 'parsed.netloc == "127.0.0.1:18791"' in script
    assert 'path.endswith(".js")' in script
    assert 'path.endswith(".css")' in script
    assert 'docker exec "${container_id}" curl --fail --silent --show-error' in script
    build = next(step for step in steps if step.get("name") == "Build multi-arch image")
    assert build["with"]["build-args"] == "OPENSQUILLA_FORBID_PERSONAL_BGM=1\n"


def test_wheelhouse_release_hydrates_current_router_bundle() -> None:
    text = (WORKFLOW_DIR / "wheelhouse-release.yml").read_text(encoding="utf-8")

    assert "models/v4.2_phase3_inference" in text
    assert 'root / "bge_onnx" / "model.onnx"' in text
    assert 'root / "features" / "tfidf.pkl"' in text
    assert 'root / "lgbm_main.bin"' in text
    assert 'root / "mlp" / "model.onnx"' in text
    assert 'root / "router.runtime.yaml"' in text
    assert "intent_head.joblib" not in text
    assert "router_model.onnx" not in text


def test_linux_desktop_recovery_e2e_scripts_preserve_x11_authority() -> None:
    """The xvfb display needs ``DISPLAY`` and ``XAUTHORITY`` to survive scrubbing.

    These harnesses strip credential-shaped variables from the Electron child
    environment, and ``XAUTHORITY`` matches that pattern.  Dropping it makes the
    ubuntu Desktop recovery E2E job fail with ``Missing X server or $DISPLAY``,
    so every harness that scrubs must exempt the X11 variables.
    """

    data = _workflow("ci.yml")
    steps = data["jobs"]["desktop-recovery-e2e"]["steps"]
    step = next(
        item for item in steps if item.get("name") == "Run compiled Desktop recovery flows"
    )
    run = step["run"]
    assert "xvfb-run" in run, "the Linux branch must provide a virtual display"

    scripts = re.findall(r"'[a-z0-9-]+:(scripts/[A-Za-z0-9_./-]+\.mjs)'", run)
    assert scripts, "no Desktop recovery E2E scripts were found in ci.yml"

    exemption = "name === 'DISPLAY' || name === 'XAUTHORITY'"
    for relative in scripts:
        path = Path("desktop/electron") / relative
        assert path.is_file(), f"missing Desktop recovery E2E script: {path}"
        source = path.read_text(encoding="utf-8")
        if "CREDENTIAL|AUTH" not in source:
            continue
        assert exemption in source, (
            f"{path} scrubs credential-shaped environment variables without exempting "
            "DISPLAY/XAUTHORITY, so the ubuntu Desktop recovery E2E job will fail with "
            "'Missing X server or $DISPLAY'"
        )


def test_desktop_cleanup_flow_allows_windows_helper_release_latency() -> None:
    source = Path(
        "desktop/electron/scripts/test-desktop-cleanup-flow.mjs"
    ).read_text(encoding="utf-8")

    assert "process.platform === 'win32' ? 90_000 : 30_000" in source
    assert "pending synthetic targets" in source
