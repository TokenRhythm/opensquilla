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
CLASSIFIER = Path(".github/scripts/classify-ci-changes.sh")
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

    raise AssertionError("Git Bash is required to run the CI change classifier on Windows")


def _classify_changed_files(
    tmp_path: Path,
    paths: list[str],
    *,
    line_ending: str = "\n",
) -> dict[str, str]:
    changed_file = tmp_path / "changed-files.txt"
    output_file = tmp_path / "github-output.txt"
    changed_file.write_text(
        line_ending.join(paths) + line_ending,
        encoding="utf-8",
        newline="",
    )

    env = os.environ.copy()
    env["GITHUB_OUTPUT"] = output_file.as_posix()
    subprocess.run(
        [_bash_executable(), CLASSIFIER.as_posix(), changed_file.as_posix()],
        check=True,
        env=env,
        text=True,
        capture_output=True,
    )

    outputs: dict[str, str] = {}
    for line in output_file.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        outputs[key] = value
    return outputs


def _expected_classifier_outputs(**overrides: str) -> dict[str, str]:
    outputs = {
        "docs_only": "false",
        "runtime_changed": "false",
        "test_changed": "false",
        "ci_changed": "false",
        "dependency_changed": "false",
        "release_changed": "false",
        "windows_full_required": "false",
        "frontend_changed": "false",
        "tui_changed": "false",
        "desktop_changed": "false",
        "python_changed": "false",
        "python_full_required": "false",
        "platform_sensitive_changed": "false",
        "build_wheel_required": "false",
        "toolchain_artifact_changed": "false",
        "full_required": "false",
        "pytest_targets": "",
    }
    if overrides.get("full_required") == "true":
        outputs["python_full_required"] = "true"
        if "pytest_targets" not in overrides:
            outputs["pytest_targets"] = "tests"
    outputs.update(overrides)
    return outputs


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
    assert "Classify changed files" in text
    assert "OpenTUI package tests" in text
    assert "Lint, test, and build (ubuntu-latest, 3.12)" in text
    assert "Windows compatibility smoke (3.12)" in text
    assert "Windows high-risk" in text
    assert "Release packaging contracts" in text
    assert "CI result" in text
    assert 'push)\n              before="${{ github.event.before }}"' in text
    assert 'merge_group)\n              base="${{ github.event.merge_group.base_sha }}"' in text
    assert 'head="${{ github.event.merge_group.head_sha }}"' in text
    assert 'git diff --name-only "${base}" "${head}" > "${changed_files}"' in text
    assert "Merge-group diff is unavailable or empty; running the full CI matrix." in text
    assert 'git diff --name-only "${before}" "${after}" > "${changed_files}"' in text
    assert 'printf \'.ci/run-all\\n\' > "${changed_files}"' in text
    assert "runtime_changed" in text
    assert "test_changed" in text
    assert "ci_changed" in text
    assert "dependency_changed" in text
    assert "release_changed" in text
    assert "windows_full_required" in text
    assert "frontend_changed" in text
    assert "tui_changed" in text
    assert "desktop_changed" in text
    assert "python_changed" in text
    assert "python_full_required" in text
    assert "platform_sensitive_changed" in text
    assert "build_wheel_required" in text
    assert "full_required" in text
    assert ".github/scripts/check_ci_results.py" in text
    assert "code_changed" not in text
    assert "workflow_changed" not in text
    assert text.count(
        '"${{ github.event_name }}" == "pull_request" || '
        '"${{ github.event_name }}" == "merge_group"'
    ) == 3


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
    assert jobs["classify-changes"]["needs"] == "queue-attestation"
    assert "always()" in jobs["classify-changes"]["if"]
    assert "github.event_name != 'merge_group'" in jobs["classify-changes"]["if"]
    classify_consumers = {
        job_name: job
        for job_name, job in jobs.items()
        if job_name != "ci-result"
        and "classify-changes"
        in (
            [job.get("needs")]
            if isinstance(job.get("needs"), str)
            else job.get("needs", [])
        )
    }
    assert classify_consumers
    for job_name, job in classify_consumers.items():
        condition = str(job.get("if", ""))
        assert "always()" in condition, job_name
        assert "needs.classify-changes.result == 'success'" in condition, job_name
    for job_name in ("webui-chat-recovery", "desktop-recovery-e2e"):
        assert "needs.frontend-check.result == 'success'" in str(
            jobs[job_name]["if"]
        )
    queue_checkout = next(
        step
        for step in jobs["queue-attestation"]["steps"]
        if step.get("name") == "Check out merge-group commit"
    )
    assert queue_checkout["with"]["ref"] == "${{ github.event.merge_group.head_sha }}"
    assert "full fail-closed matrix" in str(jobs["classify-changes"])
    assert 'CI_OPTIMIZATION_MODE}" == "legacy"' in str(jobs["classify-changes"])
    assert jobs["main-canary"]["name"] == "Main installation and offline gateway canary"
    assert "test_gateway_silent_reply_process_e2e.py" in str(jobs["main-canary"])
    assert jobs["ci-result"]["name"] == "CI result"
    assert "ci-attestation-${{ steps.attestation.outputs.tree_sha }}" in str(
        jobs["ci-result"]
    )


def test_skill_hub_contract_uses_classifier_gate_without_changing_required_names() -> None:
    workflow = _workflow("skill-hub-contract.yml")
    jobs = workflow["jobs"]
    assert jobs["detect"]["name"] == "Detect Skill Hub contract changes"
    assert jobs["skill-hub-contract"]["needs"] == "detect"
    assert jobs["skill-hub-contract"]["if"] == (
        "${{ needs.detect.outputs.run_contract == 'true' }}"
    )
    text = (WORKFLOW_DIR / "skill-hub-contract.yml").read_text(encoding="utf-8")
    assert "classify-ci-changes.sh" in text
    assert "github.event.pull_request.base.sha" in text
    assert "github.event.pull_request.head.sha" in text
    assert "run_contract" in text


def test_ci_change_classifier_routes_platform_neutral_gateway_changes(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "src/opensquilla/gateway/task_runtime.py",
            "tests/test_gateway/test_task_runtime.py",
        ],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        test_changed="true",
        python_changed="true",
        build_wheel_required="true",
        pytest_targets=(
            "tests/test_gateway,tests/test_gateway*.py,tests/functional/test_gateway_*_e2e.py"
        ),
    )


def test_ci_change_classifier_routes_platform_neutral_provider_changes(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["src/opensquilla/provider/registry.py"],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        python_changed="true",
        build_wheel_required="true",
        pytest_targets=(
            "tests/test_provider,tests/test_provider*.py,tests/test_*router*.py,"
            "tests/test_cross_provider_tiers.py"
        ),
    )


@pytest.mark.parametrize(
    "path",
    [
        "src/opensquilla/engine/runtime.py",
        "src/opensquilla/application/approval_rpc.py",
        "src/opensquilla/agents/registry.py",
        "src/opensquilla/safety/injection_guard.py",
    ],
)
def test_ci_change_classifier_runs_all_python_shards_for_shared_core(
    tmp_path: Path,
    path: str,
) -> None:
    outputs = _classify_changed_files(tmp_path, [path])

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        python_changed="true",
        python_full_required="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_keeps_native_router_changes_platform_sensitive(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["src/opensquilla/squilla_router/inference.py"],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
    )

def test_default_ci_keeps_main_pushes_targeted_and_manual_runs_full() -> None:
    ci_path = WORKFLOW_DIR / "ci.yml"
    if not ci_path.exists():
        return
    text = ci_path.read_text(encoding="utf-8")

    assert 'before="${{ github.event.before }}"' in text
    assert 'after="${{ github.event.after }}"' in text
    assert 'git diff --name-only "${before}" "${after}" > "${changed_files}"' in text
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
    assert "git ls-files 'src/opensquilla/gateway/static/dist/**'" in text
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
    assert "overwrite: true" in text
    workflow = _workflow("ci.yml")
    upload = next(
        step
        for step in workflow["jobs"]["frontend-check"]["steps"]
        if step.get("name") == "Upload verified frontend artifact"
    )
    assert upload["with"]["retention-days"] >= 31
    assert upload["with"]["overwrite"] is True
    assert "opensquilla-webui-dist-attempt-${{ github.run_attempt }}" not in text
    wheel = next(
        step
        for step in workflow["jobs"]["frontend-check"]["steps"]
        if step.get("name") == "Verify sdist-to-wheel frontend artifact round trip"
    )
    assert "build_wheel_required == 'true'" in wheel["if"]
    assert "full_required == 'true'" in wheel["if"]


def test_webui_text_and_docker_context_contracts_are_enforced_in_ci() -> None:
    attributes = Path(".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "opensquilla-webui/** text=auto eol=lf" in attributes

    workflow = _workflow("ci.yml")
    ubuntu = workflow["jobs"]["ubuntu-quality"]
    assert ubuntu["env"]["OPENSQUILLA_DOCKERIGNORE_E2E"] == "1"
    docker_step = next(
        step
        for step in ubuntu["steps"]
        if step.get("name") == "Test Docker build-context exclusions in full CI"
    )
    assert docker_step["if"] == (
        "${{ needs.classify-changes.outputs.full_required == 'true' }}"
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

    assert _trigger_keys(data) == {"pull_request", "merge_group"}
    assert data["on"]["merge_group"]["types"] == ["checks_requested"]
    assert "pull_request_target" not in text
    assert "Validate target branch" in text
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


def test_ci_change_classifier_allows_root_and_docs_markdown_only(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "README.md",
            "CHANGELOG.md",
            "docs/features/skills.md",
            ".github/pull_request_template.md",
        ],
    )

    assert outputs == _expected_classifier_outputs(docs_only="true")


def test_classifier_helper_prefers_git_bash_over_windows_wsl_bash(tmp_path: Path) -> None:
    git_bash = tmp_path / "Git" / "bin" / "bash.exe"

    result = _bash_executable(
        os_name="nt",
        path_lookup=lambda _name: r"C:\Windows\System32\bash.exe",
        exists=lambda path: path == git_bash,
        program_files=str(tmp_path),
    )

    assert result == str(git_bash)


def test_ci_change_classifier_accepts_crlf_changed_files(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["README.md", "docs/features/skills.md"],
        line_ending="\r\n",
    )

    assert outputs["docs_only"] == "true"
    assert outputs["runtime_changed"] == "false"
    assert outputs["windows_full_required"] == "false"
    assert outputs["python_changed"] == "false"
    assert outputs["full_required"] == "false"


def test_ci_change_classifier_treats_runtime_markdown_as_runtime(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["src/opensquilla/identity/templates/bootstrap/AGENTS.md"],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_fails_closed_for_unclassified_tests(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["tests/test_ci/test_workflows.py"],
    )

    assert outputs == _expected_classifier_outputs(
        test_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
    )


def test_ci_change_classifier_builds_webui_source_into_the_runtime_wheel(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["opensquilla-webui/src/views/ChatView.vue"],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        frontend_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_fails_closed_for_force_added_webui_dist(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["src/opensquilla/gateway/static/dist/assets/index-example.js"],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        frontend_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_routes_source_and_forced_dist_to_the_same_guard(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "opensquilla-webui/src/views/ChatView.vue",
            "src/opensquilla/gateway/static/dist/assets/index-example.js",
        ],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        frontend_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_tracks_ci_dependency_and_release_changes(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [".github/workflows/ci.yml", ".github/scripts/classify-ci-changes.sh", "uv.lock"],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        test_changed="true",
        ci_changed="true",
        dependency_changed="true",
        release_changed="true",
        windows_full_required="true",
        frontend_changed="true",
        tui_changed="true",
        desktop_changed="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
        toolchain_artifact_changed="true",
        full_required="true",
    )


def test_ci_change_classifier_requires_real_artifacts_for_toolchain_surfaces(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "src/opensquilla/skills/toolchains/registry.py",
            "src/opensquilla/skills/toolchains/manager.py",
            "src/opensquilla/skills/toolchains/runtime.py",
            "scripts/validate_managed_toolchain_artifacts.py",
            "scripts/validate_managed_toolchain_artifacts_stdlib.py",
        ],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
        toolchain_artifact_changed="true",
    )


def test_ci_change_classifier_requires_real_artifacts_for_paper_contracts(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "src/opensquilla/skills/runtime_env.py",
            "src/opensquilla/skills/bundled/meta-paper-write/SKILL.md",
            "src/opensquilla/skills/bundled/paper-artifact-runtime/scripts/run.py",
            "src/opensquilla/skills/bundled/paper-citation-integrity-gate/scripts/audit.py",
            "src/opensquilla/skills/bundled/paper-delivery-summary/SKILL.md",
            "src/opensquilla/skills/bundled/paper-latex-sanitizer/scripts/sanitize.py",
            "src/opensquilla/skills/bundled/paper-length-gate/scripts/audit.py",
            "src/opensquilla/skills/bundled/paper-quality-gate/scripts/audit.py",
            "src/opensquilla/skills/bundled/meta-short-drama/SKILL.md",
            "src/opensquilla/skills/bundled/subtitle-burner/scripts/burn.py",
            "src/opensquilla/skills/bundled/video-still-animator/scripts/animate.py",
            "tests/test_skills/test_meta_paper_skills.py",
            "tests/test_skills/test_managed_toolchains.py",
        ],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        test_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
        toolchain_artifact_changed="true",
    )


@pytest.mark.parametrize(
    "paper_surface",
    [
        "src/opensquilla/skills/bundled/meta-paper-write/SKILL.md",
        "src/opensquilla/skills/bundled/paper-artifact-runtime/SKILL.md",
        "src/opensquilla/skills/bundled/paper-artifact-runtime/scripts/run.py",
        "src/opensquilla/skills/bundled/paper-citation-integrity-gate/SKILL.md",
        "src/opensquilla/skills/bundled/paper-citation-integrity-gate/scripts/audit.py",
        "src/opensquilla/skills/bundled/paper-delivery-summary/SKILL.md",
        "src/opensquilla/skills/bundled/paper-delivery-summary/scripts/render.py",
        "src/opensquilla/skills/bundled/paper-latex-sanitizer/SKILL.md",
        "src/opensquilla/skills/bundled/paper-latex-sanitizer/scripts/sanitize.py",
        "src/opensquilla/skills/bundled/paper-length-gate/SKILL.md",
        "src/opensquilla/skills/bundled/paper-length-gate/scripts/audit.py",
        "src/opensquilla/skills/bundled/paper-quality-gate/SKILL.md",
        "src/opensquilla/skills/bundled/paper-quality-gate/scripts/audit.py",
        "src/opensquilla/skills/bundled/paper-refbib-stub/SKILL.md",
        "src/opensquilla/skills/bundled/paper-refbib-stub/scripts/json_to_bib.py",
        "src/opensquilla/skills/bundled/paper-source-readiness-gate/SKILL.md",
        "src/opensquilla/skills/bundled/paper-source-readiness-gate/scripts/audit.py",
    ],
)
def test_each_paper_truthfulness_surface_requires_real_artifacts(
    tmp_path: Path,
    paper_surface: str,
) -> None:
    outputs = _classify_changed_files(tmp_path, [paper_surface])

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
        toolchain_artifact_changed="true",
    )


def test_ci_change_classifier_requires_real_artifacts_for_dependency_changes(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(tmp_path, ["uv.lock"])

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        dependency_changed="true",
        release_changed="true",
        windows_full_required="true",
        python_changed="true",
        build_wheel_required="true",
        toolchain_artifact_changed="true",
    )


def test_ci_change_classifier_tracks_release_surface_changes(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            ".github/workflows/wheelhouse-release.yml",
            "scripts/build_wheelhouse_zip.py",
            "README.release.md",
            "RELEASES.md",
            "tests/test_scripts/test_build_wheelhouse_zip.py",
        ],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        test_changed="true",
        ci_changed="true",
        release_changed="true",
        windows_full_required="true",
        python_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_tracks_tui_changes_without_windows_full(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["src/opensquilla/cli/tui/opentui/package/src/composer.mjs"],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        tui_changed="true",
        python_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_tracks_development_companion_changes(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "packages/opensquilla-tui-host/src/opensquilla_tui_host/api.py",
            "scripts/build_tui_host_companion.py",
        ],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        tui_changed="true",
        python_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_fails_closed_for_unclassified_runtime_paths(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["src/opensquilla/future_profile_store/transaction.py"],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_fails_closed_for_unknown_root_paths(tmp_path: Path) -> None:
    outputs = _classify_changed_files(tmp_path, ["future-runtime-policy.json"])

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_covers_state_and_installation_boundaries(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "src/opensquilla/session/manager.py",
            "src/opensquilla/scheduler/persistence.py",
            "src/opensquilla/memory/store.py",
            "src/opensquilla/uninstall/actions.py",
            "tests/test_recovery/test_new_contract.py",
            "tests/test_uninstall/test_actions.py",
        ],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        test_changed="true",
        release_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
    )

def test_ci_change_classifier_tracks_platform_sensitive_changes(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["tests/test_tools/test_shell_process_isolation.py"],
    )

    assert outputs == _expected_classifier_outputs(
        test_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
    )


def test_ci_change_classifier_runs_windows_full_for_native_source_snapshot(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["tests/test_migration/test_source_snapshot_windows.py"],
    )

    assert outputs == _expected_classifier_outputs(
        test_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
    )


def test_ci_change_classifier_runs_windows_full_for_native_source_snapshot_implementation(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["src/opensquilla/migration/source_snapshot_windows.py"],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_runs_full_for_its_own_windows_gate(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [".github/workflows/ci.yml"],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        test_changed="true",
        ci_changed="true",
        dependency_changed="true",
        release_changed="true",
        windows_full_required="true",
        frontend_changed="true",
        tui_changed="true",
        desktop_changed="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
        toolchain_artifact_changed="true",
        full_required="true",
    )


def test_ci_change_classifier_fails_closed_for_future_ci_surfaces(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            ".github/workflows/future-profile-safety.yml",
            ".github/scripts/future_profile_gate.py",
        ],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        test_changed="true",
        ci_changed="true",
        dependency_changed="true",
        release_changed="true",
        windows_full_required="true",
        frontend_changed="true",
        tui_changed="true",
        desktop_changed="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
        toolchain_artifact_changed="true",
        full_required="true",
    )


def test_ci_change_classifier_runs_windows_release_gates_for_profile_verifier(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [".github/scripts/verify-release-profile-preservation.py"],
    )

    assert outputs == _expected_classifier_outputs(
        ci_changed="true",
        release_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
    )


def test_ci_change_classifier_tracks_packaged_update_policy_probe_as_release_surface(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["desktop/electron/scripts/test-packaged-update-policy.mjs"],
    )

    assert outputs == _expected_classifier_outputs(
        release_changed="true",
        windows_full_required="true",
        desktop_changed="true",
        platform_sensitive_changed="true",
    )


def test_ci_change_classifier_runs_windows_full_for_persistence_risk(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "src/opensquilla/persistence/migrator.py",
            "tests/test_persistence/test_migrator.py",
            "migrations/V999__example.py",
        ],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        test_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_runs_windows_full_for_provider_onboarding_risk(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "src/opensquilla/provider/registry.py",
            "src/opensquilla/onboarding/provider_specs.py",
            "tests/test_onboarding/test_mutations.py",
            "tests/test_provider/test_spec_substrate.py",
        ],
    )

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        test_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
    )


def test_ci_change_classifier_runs_windows_full_for_gateway_functional_e2e(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "tests/functional/test_gateway_non_image_attachment_materialization_e2e.py",
            "tests/functional/test_gateway_attachment_history_e2e.py",
        ],
    )

    assert outputs == _expected_classifier_outputs(
        test_changed="true",
        windows_full_required="true",
        python_changed="true",
        platform_sensitive_changed="true",
    )


def test_ci_change_classifier_tracks_desktop_changes(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["desktop/electron/src/main.ts"],
    )

    # A desktop change gates the desktop-check Node tests and, as a platform-
    # sensitive surface, the Windows full suite — but not the Python quality gate.
    assert outputs == _expected_classifier_outputs(
        desktop_changed="true",
        platform_sensitive_changed="true",
        windows_full_required="true",
    )


def test_ci_change_classifier_run_all_requires_full_ci(tmp_path: Path) -> None:
    outputs = _classify_changed_files(tmp_path, [".ci/run-all"])

    assert outputs == _expected_classifier_outputs(
        runtime_changed="true",
        test_changed="true",
        ci_changed="true",
        dependency_changed="true",
        release_changed="true",
        windows_full_required="true",
        frontend_changed="true",
        tui_changed="true",
        desktop_changed="true",
        python_changed="true",
        platform_sensitive_changed="true",
        build_wheel_required="true",
        toolchain_artifact_changed="true",
        full_required="true",
    )


def test_default_ci_uses_layered_job_conditions() -> None:
    data = _workflow("ci.yml")
    jobs = data["jobs"]

    assert "tui-check" in jobs
    assert "frontend_changed == 'true'" in jobs["frontend-check"]["if"]
    assert "full_required == 'true'" in jobs["frontend-check"]["if"]
    assert "tui_changed == 'true'" in jobs["tui-check"]["if"]
    assert "desktop_changed == 'true'" in jobs["desktop-check"]["if"]
    assert "python_changed == 'true'" in jobs["ubuntu-quality"]["if"]
    assert "full_required == 'true'" in jobs["ubuntu-full"]["if"]
    assert "python_full_required == 'true'" in jobs["ubuntu-full"]["if"]
    assert jobs["windows-compat"]["if"] == (
        "${{ always() && needs.classify-changes.result == 'success' && "
        "(needs.classify-changes.outputs.python_changed == 'true' || "
        "needs.classify-changes.outputs.platform_sensitive_changed == 'true' || "
        "needs.classify-changes.outputs.dependency_changed == 'true' || "
        "needs.classify-changes.outputs.release_changed == 'true') && "
        "needs.classify-changes.outputs.windows_full_required != 'true' && "
        "needs.classify-changes.outputs.full_required != 'true' }}"
    )
    assert "windows_full_required == 'true'" in jobs["windows-full"]["if"]
    assert "platform_sensitive_changed == 'true'" in jobs["macos-recovery"]["if"]
    assert "desktop_changed == 'true'" in jobs["macos-recovery"]["if"]
    assert "frontend_changed == 'true'" not in jobs["desktop-recovery-e2e"]["if"]
    assert "frontend_changed == 'true'" in jobs["webui-chat-recovery"]["if"]
    assert "platform_sensitive_changed == 'true'" in jobs["desktop-recovery-e2e"]["if"]
    assert "desktop_changed == 'true'" in jobs["desktop-recovery-e2e"]["if"]
    assert "platform_sensitive_changed == 'true'" in jobs["webui-chat-recovery"]["if"]
    assert "release_changed == 'true'" in jobs["release-packaging"]["if"]
    assert "tui-check" in jobs["ci-result"]["needs"]
    assert "webui-chat-recovery" in jobs["ci-result"]["needs"]
    assert "desktop-check" in jobs["ci-result"]["needs"]
    assert "ubuntu-full" in jobs["ci-result"]["needs"]
    assert "macos-recovery" in jobs["ci-result"]["needs"]
    assert "desktop-recovery-e2e" in jobs["ci-result"]["needs"]
    assert "managed-toolchain-artifacts" in jobs["ci-result"]["needs"]
    artifact_e2e = jobs["managed-toolchain-artifacts"]
    assert artifact_e2e["uses"] == "./.github/workflows/managed-toolchain-artifacts.yml"
    assert "toolchain_artifact_changed == 'true'" in artifact_e2e["if"]
    assert "full_required == 'true'" in artifact_e2e["if"]


def test_ci_result_gate_covers_every_conditional_job_and_classifier_flag() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    gate = jobs["ci-result"]
    gate_step = next(
        step for step in gate["steps"] if step.get("name") == "Check required CI results"
    )

    assert gate["name"] == "CI result"
    setup_python = next(step for step in gate["steps"] if step.get("name") == "Set up Python")
    assert setup_python["with"]["python-version"] == "3.12"
    assert set(gate["needs"]) == {
        "classify-changes",
        "workflow-lint",
        "readme-locale-check",
        "frontend-check",
        "webui-chat-recovery",
        "tui-check",
        "desktop-check",
        "ubuntu-quality",
        "ubuntu-full",
        "windows-compat",
        "windows-full",
        "macos-recovery",
        "desktop-recovery-e2e",
        "release-packaging",
        "managed-toolchain-artifacts",
        "queue-attestation",
        "main-canary",
    }
    assert gate_step["run"] == "python .github/scripts/check_ci_results.py"
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
    assert set(key for key in gate_step["env"] if key.startswith("FLAG_")) == {
        "FLAG_DOCS_ONLY",
        "FLAG_RUNTIME_CHANGED",
        "FLAG_TEST_CHANGED",
        "FLAG_CI_CHANGED",
        "FLAG_DEPENDENCY_CHANGED",
        "FLAG_RELEASE_CHANGED",
        "FLAG_WINDOWS_FULL_REQUIRED",
        "FLAG_FRONTEND_CHANGED",
        "FLAG_TUI_CHANGED",
        "FLAG_DESKTOP_CHANGED",
        "FLAG_PYTHON_CHANGED",
        "FLAG_PYTHON_FULL_REQUIRED",
        "FLAG_PLATFORM_SENSITIVE_CHANGED",
        "FLAG_BUILD_WHEEL_REQUIRED",
        "FLAG_TOOLCHAIN_ARTIFACT_CHANGED",
        "FLAG_FULL_REQUIRED",
    }


def test_desktop_recovery_e2e_runs_compiled_flows_on_all_release_platforms() -> None:
    job = _workflow("ci.yml")["jobs"]["desktop-recovery-e2e"]
    steps = job["steps"]

    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["include"] == [
        {"os": "ubuntu-latest", "shard": "all"},
        {"os": "macos-latest", "shard": "all"},
        {"os": "windows-latest", "shard": "profiles"},
        {"os": "windows-latest", "shard": "ownership"},
        {"os": "windows-latest", "shard": "workbench"},
    ]
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
        step for step in steps if step.get("name") == "Cache Playwright browser"
    )
    run = next(
        step for step in steps if step.get("name") == "Run compiled Desktop recovery flows"
    )
    upload = next(
        step for step in steps if step.get("name") == "Upload Desktop recovery report"
    )

    assert steps.index(download) < steps.index(setup_node) < steps.index(verify_frontend)
    assert verify_frontend["shell"] == "bash"
    assert verify_frontend["run"] == (
        "node opensquilla-webui/scripts/verify-dist.mjs "
        "src/opensquilla/gateway/static/dist"
    )
    assert build["run"] == "npm run build"
    assert session_recovery["working-directory"] == "opensquilla-webui"
    assert session_recovery["env"]["OPENSQUILLA_PLAYWRIGHT_MANAGE_WEBUI"] == "gateway"
    assert session_recovery["env"]["OPENSQUILLA_WEBUI_BASE_URL"].endswith(":18791")
    assert "history-hydration.spec.ts" in session_recovery["run"]
    assert '--grep "terminates stalled"' in session_recovery["run"]
    assert playwright_cache["uses"] == "actions/cache@v4"
    assert playwright_cache["with"]["path"] == "${{ env.PLAYWRIGHT_BROWSERS_PATH }}"
    assert job["env"]["PLAYWRIGHT_BROWSERS_PATH"] == (
        "${{ github.workspace }}/.cache/ms-playwright"
    )
    assert "${{ runner.arch }}" in playwright_cache["with"]["key"]
    assert "opensquilla-webui/package-lock.json" in playwright_cache["with"]["key"]
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
    assert "is_retryable_windows_failure()" in run["run"]
    assert '[[ "${RUNNER_OS}" == "Windows" ]] || return 1' in run["run"]
    assert "grep -Fq 'Gateway did not become healthy'" in run["run"]
    assert (
        "grep -Fq 'Timed out waiting for post-exit delete-all helper completion'"
        in run["run"]
    )
    assert 'if is_retryable_windows_failure "${first_log}"' in run["run"]
    assert 'run_case "${name}" "${script}" 2' in run["run"]
    assert "exit 1" in run["run"]
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["name"] == (
        "desktop-recovery-e2e-${{ matrix.os }}-${{ matrix.shard }}"
        "-attempt-${{ github.run_attempt }}"
    )


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

    assert job["needs"] == ["classify-changes", "frontend-check"]
    assert download["with"]["name"] == "opensquilla-webui-dist"
    assert download["with"]["path"] == "src/opensquilla/gateway/static/dist/"
    assert steps.index(download) < steps.index(install_gateway) < steps.index(run)
    assert install_gateway["run"] == "uv sync --frozen"
    assert job["env"]["OPENSQUILLA_PLAYWRIGHT_MANAGE_WEBUI"] == "gateway"
    assert job["env"]["OPENSQUILLA_WEBUI_BASE_URL"].endswith(":18791")
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
        "share.spec.ts",
    }
    assert selected_specs == required_specs
    for spec in required_specs:
        assert (Path("opensquilla-webui/e2e") / spec).is_file()


def test_windows_smoke_does_not_install_bun_by_default() -> None:
    data = _workflow("ci.yml")
    jobs = data["jobs"]

    windows_steps = jobs["windows-compat"]["steps"]
    assert all(step.get("uses") != "oven-sh/setup-bun@v2" for step in windows_steps)
    assert all("OpenTUI" not in step.get("name", "") for step in windows_steps)
    assert "lfs" not in windows_steps[0].get("with", {})

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
    assert windows_full["timeout-minutes"] == 45
    assert windows_full["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "shard": [
                "core",
                "gateway-sqlite",
                "recovery-migration",
                "desktop-installer-contracts",
            ]
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
        "${{ needs.classify-changes.outputs.full_required == 'true' }}"
    )
    assert test_step["if"] == (
        "${{ needs.classify-changes.outputs.full_required != 'true' && "
        "needs.classify-changes.outputs.python_full_required != 'true' }}"
    )
    assert "full_required == 'true'" in ubuntu_full["if"]
    assert "python_full_required == 'true'" in ubuntu_full["if"]
    assert "uv run pytest" in test_step["run"]
    assert "tests/test_artifacts.py" not in test_step["run"]
    assert "--ignore=tests/test_ci/test_router_artifact_manifest.py" in test_step["run"]
    assert "tests/test_recovery" in test_step["run"]
    assert "tests/test_migration/test_opensquilla_home_migration.py" in test_step["run"]
    assert ubuntu_full["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "shard": [
                "core",
                "gateway-sqlite",
                "recovery-migration",
                "desktop-installer-contracts",
            ]
        },
    }
    assert ubuntu_full["timeout-minutes"] == 20
    assert ".github/scripts/windows_test_shards.py run" in full_test_step["run"]
    assert "--maxfail" not in full_test_step["run"]
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
    upload = next(step for step in build_steps if step.get("name") == "Upload Web UI artifact")
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
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] >= 31
    assert upload["with"]["overwrite"] is True
    assert "npm run verify:release-dist" in release_build["run"]
    assert release_build["if"] == "steps.webui-contract.outputs.mode == 'source-built'"
    assert "legacy-committed" in detect["run"]
    assert "src/opensquilla/gateway/static/dist/index.html" in detect["run"]
    assert legacy["if"] == "steps.webui-contract.outputs.mode == 'legacy-committed'"
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
        assert download["with"] == {
            "name": artifact_name,
            "path": "src/opensquilla/gateway/static/dist/",
        }

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
