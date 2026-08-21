from __future__ import annotations

import io
import json
import runpy
import subprocess
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

MODULE: dict[str, Any] = runpy.run_path(
    ".github/scripts/ci_attestation.py", run_name="ci_attestation"
)
AttestationError = MODULE["AttestationError"]
SafeArtifactRedirectHandler = MODULE["_SafeArtifactRedirectHandler"]
create_attestation = MODULE["create_attestation"]
policy_digest = MODULE["policy_digest"]
validate_candidate = MODULE["validate_candidate"]
verify_queue = MODULE["verify_queue"]
list_attestation_artifacts = MODULE["_list_attestation_artifacts"]
plan_paths = MODULE["_plan_paths"]
composition_is_safe = MODULE["_composition_is_safe"]
reconstructed_queue_tree = MODULE["_reconstructed_queue_tree"]
verify_nightly_health = MODULE["verify_nightly_health"]
wait_for_base_successful_ci = MODULE["_wait_for_base_successful_ci"]
verify_queue_command = MODULE["_verify_queue_command"]


def _artifact_redirect(newurl: str) -> urllib.request.Request:
    request = urllib.request.Request(
        "https://api.github.com/repos/opensquilla/opensquilla/actions/artifacts/1/zip",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer synthetic-token",
            "User-Agent": "opensquilla-ci-attestation",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    redirected = SafeArtifactRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        Message(),
        newurl,
    )
    assert redirected is not None
    return redirected


def test_artifact_redirect_strips_api_credentials_cross_origin() -> None:
    redirected = _artifact_redirect(
        "https://productionresultssa.blob.core.windows.net/actions-results/attestation.zip"
    )

    assert redirected.get_header("Authorization") is None
    assert redirected.get_header("Accept") is None
    assert redirected.get_header("X-Github-Api-Version") is None


def test_artifact_redirect_preserves_api_credentials_same_origin() -> None:
    redirected = _artifact_redirect("https://api.github.com/artifact-download")

    assert redirected.get_header("Authorization") == "Bearer synthetic-token"
    assert redirected.get_header("Accept") == "application/vnd.github+json"


def test_artifact_redirect_rejects_non_https_target() -> None:
    with pytest.raises(AttestationError, match="must use HTTPS"):
        _artifact_redirect("http://artifact-storage.example.invalid/attestation.zip")


def test_verify_queue_details_default_combined_smoke_to_empty_json(
    tmp_path: Path,
) -> None:
    details: dict[str, object] = {}

    reusable, reason, source_run = verify_queue(
        repo=tmp_path,
        repository="opensquilla/opensquilla",
        event={},
        token="synthetic-token",
        api_url="https://api.github.com",
        current_run_id=999,
        details=details,
    )

    assert reusable is False
    assert reason == "not a merge_group event"
    assert source_run is None
    assert details["combined_smoke_suites"] == "[]"


def test_verify_queue_command_emits_canonical_combined_smoke_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text("{}\n", encoding="utf-8")
    output_path = tmp_path / "github-output.txt"

    def fake_verify_queue(**kwargs: Any) -> tuple[bool, str, int]:
        kwargs["details"].update(
            reason_code="reusable_base_advance",
            combined_smoke_suites='["python-targeted"]',
        )
        return True, "trusted overlap", 123

    monkeypatch.setenv("GH_TOKEN", "synthetic-token")
    monkeypatch.setitem(
        verify_queue_command.__globals__, "verify_queue", fake_verify_queue
    )

    result = verify_queue_command(
        SimpleNamespace(
            repo=tmp_path,
            repository="opensquilla/opensquilla",
            event_path=event_path,
            run_id=999,
            api_url="https://api.github.com",
            github_output=output_path,
        )
    )

    assert result == 0
    outputs = dict(
        line.split("=", 1)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    )
    assert outputs["reusable"] == "true"
    assert outputs["reason_code"] == "reusable_base_advance"
    assert outputs["combined_smoke_suites"] == '["python-targeted"]'


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(repo: Path, relative: str, value: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _seed_suite_execution_input_fixtures(repo: Path) -> None:
    """Give the synthetic Git repo one harmless file for every suite input glob."""

    config = json.loads(
        (repo / ".github/ci/suites.v1.json").read_text(encoding="utf-8")
    )
    patterns = {
        pattern
        for suite in config["suites"].values()
        for pattern in suite["execution_inputs"]
    }
    for pattern in sorted(patterns):
        if pattern == "**":
            continue
        candidate = pattern
        if candidate.endswith("/**"):
            candidate = f"{candidate[:-2]}fixture.txt"
        candidate = candidate.replace("**", "fixture").replace("*", "fixture")
        if not (repo / candidate).exists():
            _write(repo, candidate, "synthetic suite input\n")


def _merge_preview_repo(
    tmp_path: Path, *, advance_base: bool = False, change_ci_executor: bool = False
) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "CI Test")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _write(repo, ".github/workflows/ci.yml", "name: CI\n")
    _write(repo, ".github/scripts/classify-ci-changes.sh", "#!/bin/sh\n")
    _write(repo, ".github/scripts/windows_test_shards.py", "TEST_SHARDS = 1\n")
    for relative in (
        ".github/ci/suites.v1.json",
        ".github/scripts/plan_ci.py",
    ):
        _write(repo, relative, Path(relative).read_text(encoding="utf-8"))
    _write(repo, "pyproject.toml", "[project]\nname='fixture'\nversion='0'\n")
    _write(repo, "src/example.py", "BASE = True\n")
    _seed_suite_execution_input_fixtures(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    _git(repo, "switch", "-c", "feature")
    _write(repo, "src/example.py", "BASE = True\nFEATURE = True\n")
    _git(repo, "add", "src/example.py")
    if change_ci_executor:
        _write(repo, ".github/scripts/windows_test_shards.py", "TEST_SHARDS = 2\n")
        _git(repo, "add", ".github/scripts/windows_test_shards.py")
    _git(repo, "commit", "-m", "feature")
    head_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "main")
    if advance_base:
        _write(repo, "src/base_update.py", "UPDATED_BASE = True\n")
        _git(repo, "add", "src/base_update.py")
        _git(repo, "commit", "-m", "advance base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge preview")
    merge_sha = _git(repo, "rev-parse", "HEAD")
    return repo, base_sha, head_sha, merge_sha


def _event(base_sha: str, head_sha: str, merge_sha: str) -> dict[str, Any]:
    return {
        "pull_request": {
            "number": 42,
            "base": {"ref": "main", "sha": base_sha},
            "head": {
                "ref": "feature",
                "sha": head_sha,
                "repo": {"full_name": "opensquilla/opensquilla"},
            },
        },
        "merge_group": {
            "base_sha": base_sha,
            "head_sha": merge_sha,
            "base_ref": "refs/heads/main",
            "head_ref": "refs/heads/gh-readonly-queue/main/pr-42-synthetic",
        },
    }


def _run(attestation: dict[str, object]) -> dict[str, Any]:
    return {
        "id": attestation["workflow_run_id"],
        "run_attempt": attestation["workflow_run_attempt"],
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "head_sha": attestation["head_sha"],
        "path": ".github/workflows/ci.yml",
        "repository": {"full_name": "opensquilla/opensquilla"},
        "pull_requests": [
            {
                "number": 42,
                "head": {"sha": attestation["head_sha"]},
                "base": {"ref": "main", "sha": attestation["base_sha"]},
            }
        ],
    }


def _archive(attestation: dict[str, object]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("ci-attestation.json", json.dumps(attestation))
    return buffer.getvalue()


def _evidence_metadata(
    repo: Path, paths: list[str] | None = None
) -> dict[str, object]:
    plan = plan_paths(repo, paths or ["src/example.py"])
    return {
        "successful_suites": plan["required_suites"],
        "planner_digest": plan["plan_digest"],
        "suite_execution_digests": plan["suite_execution_digests"],
        "platform_matrix": plan["platform_matrix"],
    }


def _named_archive(name: str, value: dict[str, object]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(name, json.dumps(value))
    return buffer.getvalue()


def test_create_attestation_pins_merge_parents_tree_and_policy(tmp_path: Path) -> None:
    repo, base_sha, head_sha, merge_sha = _merge_preview_repo(
        tmp_path, change_ci_executor=True
    )
    attestation = create_attestation(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=_event(base_sha, head_sha, merge_sha),
        workflow_run_id=123,
        workflow_run_attempt=2,
        workflow_ref="opensquilla/opensquilla/.github/workflows/ci.yml@refs/pull/42/merge",
        optimization_mode="shadow",
        **_evidence_metadata(repo),
    )

    assert attestation["base_sha"] == base_sha
    assert attestation["head_sha"] == head_sha
    assert attestation["tested_merge_sha"] == merge_sha
    assert attestation["tested_tree_sha"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert attestation["trust_policy_digest"] == policy_digest(repo)
    assert attestation["trust_policy_digest"] != policy_digest(repo, base_sha)
    assert attestation["validation_profile"] == "suite-evidence-v2"
    assert attestation["schema_version"] == 2
    assert attestation["evidence_kind"] == "root"
    assert attestation["successful_suites"]


def test_create_command_records_full_merge_group_root_plan(tmp_path: Path) -> None:
    repo, base_sha, head_sha, _merge_sha = _merge_preview_repo(tmp_path)
    _git(repo, "switch", "-C", "queue", base_sha)
    _git(repo, "cherry-pick", head_sha)
    queue_sha = _git(repo, "rev-parse", "HEAD")
    event = {
        "merge_group": {
            "base_sha": base_sha,
            "head_sha": queue_sha,
            "base_ref": "refs/heads/main",
            "head_ref": "refs/heads/gh-readonly-queue/main/pr-42-synthetic",
        }
    }
    event_path = tmp_path / "merge-group-event.json"
    output_path = tmp_path / "ci-attestation.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(Path(".github/scripts/ci_attestation.py").resolve()),
            "create",
            "--repo",
            str(repo),
            "--repository",
            "opensquilla/opensquilla",
            "--event-path",
            str(event_path),
            "--run-id",
            "777",
            "--run-attempt",
            "1",
            "--workflow-ref",
            "opensquilla/opensquilla/.github/workflows/ci.yml@refs/heads/main",
            "--optimization-mode",
            "enforce",
            "--output",
            str(output_path),
        ],
        check=True,
        cwd=Path.cwd(),
    )
    attestation = json.loads(output_path.read_text(encoding="utf-8"))
    full_plan = plan_paths(repo, [".ci/run-all"], ref=attestation["tested_tree_sha"])

    assert attestation["evidence_kind"] == "root"
    assert attestation["plan_basis"] == "full_fallback"
    assert attestation["successful_suites"] == full_plan["required_suites"]
    assert attestation["platform_matrix"] == full_plan["platform_matrix"]
    validate_candidate(
        attestation=attestation,
        run={
            "id": 777,
            "run_attempt": 1,
            "event": "merge_group",
            "status": "completed",
            "conclusion": "success",
            "head_sha": queue_sha,
            "path": ".github/workflows/ci.yml",
            "repository": {"full_name": "opensquilla/opensquilla"},
        },
        repository="opensquilla/opensquilla",
        queue_tree_sha=attestation["tested_tree_sha"],
        queue_base_sha=base_sha,
        queue_policy_digest=attestation["trust_policy_digest"],
        repo=repo,
    )


def test_create_attestation_uses_tested_base_when_event_base_is_stale(
    tmp_path: Path,
) -> None:
    repo, tested_base_sha, head_sha, merge_sha = _merge_preview_repo(
        tmp_path, advance_base=True
    )
    event_base_sha = _git(repo, "rev-parse", f"{head_sha}^")

    assert event_base_sha != tested_base_sha
    attestation = create_attestation(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=_event(event_base_sha, head_sha, merge_sha),
        workflow_run_id=123,
        workflow_run_attempt=1,
        workflow_ref="opensquilla/opensquilla/.github/workflows/ci.yml@refs/pull/42/merge",
        optimization_mode="enforce",
        **_evidence_metadata(repo),
    )

    assert attestation["base_sha"] == tested_base_sha


def test_create_attestation_rejects_merge_for_another_head(tmp_path: Path) -> None:
    repo, base_sha, head_sha, merge_sha = _merge_preview_repo(tmp_path)

    with pytest.raises(AttestationError, match="tested merge head"):
        create_attestation(
            repo=repo,
            repository="opensquilla/opensquilla",
            event=_event(base_sha, "0" * 40, merge_sha),
            workflow_run_id=123,
            workflow_run_attempt=1,
            workflow_ref="opensquilla/opensquilla/.github/workflows/ci.yml@refs/pull/42/merge",
            optimization_mode="enforce",
            **_evidence_metadata(repo),
        )


def test_validate_candidate_rejects_non_green_or_mismatched_runs(tmp_path: Path) -> None:
    repo, base_sha, head_sha, merge_sha = _merge_preview_repo(tmp_path)
    attestation = create_attestation(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=_event(base_sha, head_sha, merge_sha),
        workflow_run_id=123,
        workflow_run_attempt=1,
        workflow_ref="opensquilla/opensquilla/.github/workflows/ci.yml@refs/pull/42/merge",
        optimization_mode="enforce",
        **_evidence_metadata(repo),
    )
    run = _run(attestation)
    current_pr = {
        "number": 42,
        **_event(base_sha, head_sha, merge_sha)["pull_request"],
    }

    validate_candidate(
        attestation=attestation,
        run=run,
        repository="opensquilla/opensquilla",
        queue_tree_sha=str(attestation["tested_tree_sha"]),
        queue_base_sha=base_sha,
        queue_policy_digest=str(attestation["trust_policy_digest"]),
        current_pull_request=current_pr,
    )

    for field, value in (
        ("conclusion", "failure"),
        ("event", "workflow_dispatch"),
        ("path", ".github/workflows/untrusted.yml"),
    ):
        tampered = dict(run)
        tampered[field] = value
        with pytest.raises(AttestationError):
            validate_candidate(
                attestation=attestation,
                run=tampered,
                repository="opensquilla/opensquilla",
                queue_tree_sha=str(attestation["tested_tree_sha"]),
                queue_base_sha=base_sha,
                queue_policy_digest=str(attestation["trust_policy_digest"]),
                current_pull_request=current_pr,
            )

    stale_base = dict(run)
    stale_base["pull_requests"] = [
        {
            "number": 42,
            "head": {"sha": attestation["head_sha"]},
            "base": {"ref": "main", "sha": "0" * 40},
        }
    ]
    validate_candidate(
        attestation=attestation,
        run=stale_base,
        repository="opensquilla/opensquilla",
        queue_tree_sha=str(attestation["tested_tree_sha"]),
        queue_base_sha=base_sha,
        queue_policy_digest=str(attestation["trust_policy_digest"]),
        current_pull_request=current_pr,
    )

    wrong_ref = dict(run)
    wrong_ref["pull_requests"] = [
        {
            "number": 42,
            "head": {"sha": attestation["head_sha"]},
            "base": {"ref": "release", "sha": base_sha},
        }
    ]
    with pytest.raises(AttestationError):
        validate_candidate(
            attestation=attestation,
            run=wrong_ref,
            repository="opensquilla/opensquilla",
            queue_tree_sha=str(attestation["tested_tree_sha"]),
            queue_base_sha=base_sha,
            queue_policy_digest=str(attestation["trust_policy_digest"]),
            current_pull_request=current_pr,
        )

    merge_sha_run = dict(run)
    merge_sha_run["head_sha"] = attestation["tested_merge_sha"]
    validate_candidate(
        attestation=attestation,
        run=merge_sha_run,
        repository="opensquilla/opensquilla",
        queue_tree_sha=str(attestation["tested_tree_sha"]),
        queue_base_sha=base_sha,
        queue_policy_digest=str(attestation["trust_policy_digest"]),
        current_pull_request=current_pr,
    )


def test_validate_candidate_rejects_self_reported_incomplete_suite_coverage(
    tmp_path: Path,
) -> None:
    repo, base_sha, head_sha, merge_sha = _merge_preview_repo(tmp_path)
    attestation = create_attestation(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=_event(base_sha, head_sha, merge_sha),
        workflow_run_id=123,
        workflow_run_attempt=1,
        workflow_ref="opensquilla/opensquilla/.github/workflows/ci.yml@refs/pull/42/merge",
        optimization_mode="enforce",
        **_evidence_metadata(repo),
    )
    tampered = dict(attestation)
    tampered["successful_suites"] = ["workflow-lint"]
    tampered["suite_execution_digests"] = {
        "workflow-lint": attestation["suite_execution_digests"]["workflow-lint"]
    }
    tampered["platform_matrix"] = [
        cell
        for cell in attestation["platform_matrix"]
        if cell["suite"] == "workflow-lint"
    ]

    with pytest.raises(AttestationError, match="canonical source plan"):
        validate_candidate(
            attestation=tampered,
            run=_run(tampered),
            repository="opensquilla/opensquilla",
            queue_tree_sha=str(tampered["tested_tree_sha"]),
            queue_base_sha=base_sha,
            queue_policy_digest=str(tampered["trust_policy_digest"]),
            current_pull_request={
                "number": 42,
                **_event(base_sha, head_sha, merge_sha)["pull_request"],
            },
            repo=repo,
        )


def test_validate_candidate_rejects_expired_root_evidence(tmp_path: Path) -> None:
    repo, base_sha, head_sha, merge_sha = _merge_preview_repo(tmp_path)
    attestation = create_attestation(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=_event(base_sha, head_sha, merge_sha),
        workflow_run_id=123,
        workflow_run_attempt=1,
        workflow_ref="opensquilla/opensquilla/.github/workflows/ci.yml@refs/pull/42/merge",
        optimization_mode="enforce",
        **_evidence_metadata(repo),
    )
    attestation["root_issued_at"] = (
        datetime.now(UTC) - timedelta(hours=73)
    ).isoformat()

    with pytest.raises(AttestationError, match="too old"):
        validate_candidate(
            attestation=attestation,
            run=_run(attestation),
            repository="opensquilla/opensquilla",
            queue_tree_sha=str(attestation["tested_tree_sha"]),
            queue_base_sha=base_sha,
            queue_policy_digest=str(attestation["trust_policy_digest"]),
            current_pull_request={
                "number": 42,
                **_event(base_sha, head_sha, merge_sha)["pull_request"],
            },
        )


def test_validate_candidate_accepts_derivable_lineage_and_rejects_full_depth(
    tmp_path: Path,
) -> None:
    repo, base_sha, head_sha, merge_sha = _merge_preview_repo(tmp_path)
    attestation = create_attestation(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=_event(base_sha, head_sha, merge_sha),
        workflow_run_id=123,
        workflow_run_attempt=1,
        workflow_ref="opensquilla/opensquilla/.github/workflows/ci.yml@refs/pull/42/merge",
        optimization_mode="enforce",
        **_evidence_metadata(repo),
    )
    current_pr = {
        "number": 42,
        **_event(base_sha, head_sha, merge_sha)["pull_request"],
    }
    derivable = {
        **attestation,
        "evidence_kind": "derived",
        "lineage": list(range(1001, 1008)),
    }

    validate_candidate(
        attestation=derivable,
        run=_run(derivable),
        repository="opensquilla/opensquilla",
        queue_tree_sha=str(derivable["tested_tree_sha"]),
        queue_base_sha=base_sha,
        queue_policy_digest=str(derivable["trust_policy_digest"]),
        current_pull_request=current_pr,
    )

    full_depth = {**derivable, "lineage": list(range(1001, 1009))}
    with pytest.raises(AttestationError, match="cannot be extended"):
        validate_candidate(
            attestation=full_depth,
            run=_run(full_depth),
            repository="opensquilla/opensquilla",
            queue_tree_sha=str(full_depth["tested_tree_sha"]),
            queue_base_sha=base_sha,
            queue_policy_digest=str(full_depth["trust_policy_digest"]),
            current_pull_request=current_pr,
        )


def _composition_fixture(
    tmp_path: Path,
    *,
    base_delta_path: str,
    source_path: str = "src/opensquilla/provider/example.py",
) -> tuple[Path, dict[str, object], str]:
    repo = tmp_path / "composition"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "CI Test")
    _git(repo, "config", "user.email", "ci@example.invalid")
    for relative in (
        ".github/ci/suites.v1.json",
        ".github/scripts/plan_ci.py",
    ):
        _write(repo, relative, Path(relative).read_text(encoding="utf-8"))
    _write(repo, source_path, "VALUE = 1\n")
    _write(repo, "README.md", "base readme\n")
    _write(repo, "docs/base.md", "base\n")
    _seed_suite_execution_input_fixtures(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    original_base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "-c", "feature")
    _write(repo, source_path, "VALUE = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feature")
    head_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "-c", "source-preview", "main")
    _git(repo, "merge", "--no-ff", "feature", "-m", "source preview")
    source_merge = _git(repo, "rev-parse", "HEAD")
    source_event = _event(original_base, head_sha, source_merge)
    attestation = create_attestation(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=source_event,
        workflow_run_id=123,
        workflow_run_attempt=1,
        workflow_ref="opensquilla/opensquilla/.github/workflows/ci.yml@refs/pull/42/merge",
        optimization_mode="enforce",
        **_evidence_metadata(repo, [source_path]),
    )

    _git(repo, "switch", "main")
    _write(repo, base_delta_path, "advanced base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "advance base")
    queue_base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "merge", "--no-ff", "feature", "-m", "queue preview")
    return repo, attestation, queue_base


def test_base_advance_composition_has_no_overlap_trust_root() -> None:
    assert MODULE["COMPOSITION_COMBINED_SMOKE_TRUST_ROOT"] == frozenset()


def test_base_advance_composition_accepts_disjoint_risk_domains(tmp_path: Path) -> None:
    repo, attestation, queue_base = _composition_fixture(
        tmp_path, base_delta_path="docs/queue.md"
    )

    safe, reason, combined_smoke_suites = composition_is_safe(
        repo=repo, attestation=attestation, queue_base_sha=queue_base
    )

    assert safe is True
    assert "disjoint" in reason
    assert combined_smoke_suites == ()


def test_base_advance_composition_accepts_disjoint_source_code_domains(
    tmp_path: Path,
) -> None:
    repo, attestation, queue_base = _composition_fixture(
        tmp_path,
        source_path="opensquilla-webui/src/components/example.ts",
        base_delta_path="src/opensquilla/provider/base.py",
    )

    safe, reason, combined_smoke_suites = composition_is_safe(
        repo=repo, attestation=attestation, queue_base_sha=queue_base
    )

    assert safe is True
    assert "disjoint" in reason
    assert combined_smoke_suites == ()


def test_base_advance_composition_rejects_python_targeted_overlap(
    tmp_path: Path,
) -> None:
    repo, attestation, queue_base = _composition_fixture(
        tmp_path, base_delta_path="src/opensquilla/provider/base.py"
    )

    safe, reason, combined_smoke_suites = composition_is_safe(
        repo=repo, attestation=attestation, queue_base_sha=queue_base
    )

    assert safe is False
    assert reason == "base delta overlaps unsupported source suites: python-targeted"
    assert combined_smoke_suites == ()


@pytest.mark.parametrize(
    ("source_path", "base_delta_path", "unsupported_suite"),
    [
        (
            "src/opensquilla/engine/runtime.py",
            "src/opensquilla/engine/agent.py",
            "python-full",
        ),
        (
            "src/opensquilla/sandbox/windows_backend.py",
            "src/opensquilla/sandbox/windows_policy.py",
            "windows-high-risk",
        ),
        (
            "opensquilla-webui/src/components/FeaturePanel.vue",
            "opensquilla-webui/src/components/BasePanel.vue",
            "frontend",
        ),
        (
            "desktop/electron/scripts/test-native-workbench-a.mjs",
            "desktop/electron/scripts/test-native-workbench-b.mjs",
            "desktop-recovery-e2e",
        ),
    ],
)
def test_base_advance_composition_rejects_unsupported_overlapping_risk_domains(
    tmp_path: Path,
    source_path: str,
    base_delta_path: str,
    unsupported_suite: str,
) -> None:
    repo, attestation, queue_base = _composition_fixture(
        tmp_path,
        source_path=source_path,
        base_delta_path=base_delta_path,
    )

    safe, reason, combined_smoke_suites = composition_is_safe(
        repo=repo, attestation=attestation, queue_base_sha=queue_base
    )

    assert safe is False
    assert "unsupported" in reason
    assert unsupported_suite in reason
    assert combined_smoke_suites == ()


def test_base_advance_composition_rejects_source_execution_input_drift(
    tmp_path: Path,
) -> None:
    repo, attestation, queue_base = _composition_fixture(
        tmp_path, base_delta_path="scripts/build_wheelhouse_zip.py"
    )

    safe, reason, combined_smoke_suites = composition_is_safe(
        repo=repo, attestation=attestation, queue_base_sha=queue_base
    )

    assert safe is False
    assert "execution inputs" in reason
    assert combined_smoke_suites == ()


def test_base_advance_composition_rejects_baseline_execution_input_drift(
    tmp_path: Path,
) -> None:
    repo, attestation, queue_base = _composition_fixture(
        tmp_path,
        source_path="docs/feature.md",
        base_delta_path="README.md",
    )

    safe, reason, combined_smoke_suites = composition_is_safe(
        repo=repo, attestation=attestation, queue_base_sha=queue_base
    )

    assert safe is False
    assert reason == "base delta changed source suite execution inputs: readme-locale"
    assert combined_smoke_suites == ()


def _verify_composed_fixture(
    *,
    repo: Path,
    attestation: dict[str, object],
    queue_base: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[bool, str, int | None, dict[str, object]]:
    queue_head = _git(repo, "rev-parse", "HEAD")
    head_sha = str(attestation["head_sha"])
    event = _event(queue_base, head_sha, queue_head)
    current_pr = {"number": 42, **event["pull_request"]}
    run = _run(attestation)
    run["created_at"] = "2026-08-20T00:00:00Z"
    archive = _archive(attestation)

    def fake_json(url: str, _token: str) -> dict[str, Any]:
        if "actions/artifacts" in url:
            return {
                "artifacts": [
                    {
                        "id": 1,
                        "expired": False,
                        "size_in_bytes": len(archive),
                        "created_at": "2026-08-20T00:00:00Z",
                        "archive_download_url": "https://api.github.com/artifact.zip",
                        "workflow_run": {"id": 123},
                    }
                ]
            }
        if "/pulls/42" in url:
            return current_pr
        if "actions/workflows/ci.yml/runs" in url:
            return {"workflow_runs": [run]}
        if "/actions/runs?" in url:
            return {
                "workflow_runs": [
                    {
                        "path": ".github/workflows/ci.yml",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": queue_base,
                    }
                ]
            }
        if url.endswith("/actions/runs/123"):
            return run
        raise AssertionError(f"unexpected API request: {url}")

    monkeypatch.setitem(verify_queue.__globals__, "_request_json", fake_json)
    monkeypatch.setitem(
        verify_queue.__globals__,
        "_request_bytes",
        lambda _url, _token: archive,
    )
    monkeypatch.setitem(
        verify_queue.__globals__,
        "verify_nightly_health",
        lambda **_kwargs: (True, "latest full nightly is green and fresh"),
    )
    details: dict[str, object] = {}
    reusable, reason, source_run = verify_queue(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=event,
        token="synthetic-token",
        api_url="https://api.github.com",
        current_run_id=999,
        details=details,
    )
    return reusable, reason, source_run, details


def test_verify_queue_rejects_python_targeted_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, attestation, queue_base = _composition_fixture(
        tmp_path, base_delta_path="src/opensquilla/provider/base.py"
    )

    reusable, reason, source_run, details = _verify_composed_fixture(
        repo=repo,
        attestation=attestation,
        queue_base=queue_base,
        monkeypatch=monkeypatch,
    )

    assert reusable is False
    assert "unsupported source suites: python-targeted" in reason
    assert source_run is None
    assert details["combined_smoke_suites"] == "[]"


def test_verify_queue_disjoint_composition_has_no_combined_smoke_suites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, attestation, queue_base = _composition_fixture(
        tmp_path, base_delta_path="docs/queue.md"
    )

    reusable, _reason, source_run, details = _verify_composed_fixture(
        repo=repo,
        attestation=attestation,
        queue_base=queue_base,
        monkeypatch=monkeypatch,
    )

    assert reusable is True
    assert source_run == 123
    assert details["reason_code"] == "reusable_base_advance"
    assert details["combined_smoke_suites"] == "[]"


def test_verify_queue_rejects_overlap_outside_combined_smoke_trust_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, attestation, queue_base = _composition_fixture(
        tmp_path,
        source_path="src/opensquilla/engine/runtime.py",
        base_delta_path="src/opensquilla/engine/agent.py",
    )

    reusable, reason, source_run, details = _verify_composed_fixture(
        repo=repo,
        attestation=attestation,
        queue_base=queue_base,
        monkeypatch=monkeypatch,
    )

    assert reusable is False
    assert "unsupported source suites: python-full" in reason
    assert source_run is None
    assert details["combined_smoke_suites"] == "[]"


def test_squash_binding_reconstructs_the_tested_tree(tmp_path: Path) -> None:
    repo, base_sha, head_sha, _merge_sha = _merge_preview_repo(tmp_path)

    assert reconstructed_queue_tree(
        repo, queue_base_sha=base_sha, head_sha=head_sha
    ) == _git(repo, "rev-parse", "HEAD^{tree}")


def test_verify_queue_reuses_only_exact_trusted_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_sha, head_sha, merge_sha = _merge_preview_repo(tmp_path)
    event = _event(base_sha, head_sha, merge_sha)
    attestation = create_attestation(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=event,
        workflow_run_id=123,
        workflow_run_attempt=1,
        workflow_ref="opensquilla/opensquilla/.github/workflows/ci.yml@refs/pull/42/merge",
        optimization_mode="shadow",
        **_evidence_metadata(repo),
    )
    run = _run(attestation)
    run["created_at"] = "2026-08-20T00:00:00Z"
    current_pr = {"number": 42, **event["pull_request"]}

    def fake_json(url: str, _token: str) -> dict[str, Any]:
        if "actions/artifacts" in url:
            return {
                "artifacts": [
                    {
                        "id": 1,
                        "expired": False,
                        "size_in_bytes": len(_archive(attestation)),
                        "created_at": "2026-08-13T00:00:00Z",
                        "archive_download_url": "https://api.github.com/artifact.zip",
                        "workflow_run": {"id": 123},
                    }
                ]
            }
        if "/pulls/42" in url:
            return current_pr
        if "actions/workflows/ci.yml/runs" in url:
            return {"workflow_runs": [run]}
        return run

    monkeypatch.setitem(verify_queue.__globals__, "_request_json", fake_json)
    monkeypatch.setitem(
        verify_queue.__globals__, "_request_bytes", lambda _url, _token: _archive(attestation)
    )
    monkeypatch.setitem(
        verify_queue.__globals__,
        "verify_nightly_health",
        lambda **_kwargs: (True, "latest full nightly is green and fresh"),
    )

    details: dict[str, object] = {}
    reusable, reason, source_run = verify_queue(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=event,
        token="synthetic-token",
        api_url="https://api.github.com",
        current_run_id=999,
        details=details,
    )

    assert reusable is True
    assert reason == "matching trusted exact-tree CI evidence"
    assert source_run == 123
    assert details["reason_code"] == "reusable_exact"
    assert details["candidate_count"] == 1
    assert details["artifact_name"].startswith("ci-evidence-v2-tree-")
    assert details["combined_smoke_suites"] == "[]"

    _write(repo, ".github/workflows/ci.yml", "name: Changed CI\n")
    _git(repo, "add", ".github/workflows/ci.yml")
    _git(repo, "commit", "-m", "change policy")
    changed_event = _event(base_sha, head_sha, _git(repo, "rev-parse", "HEAD"))
    reusable, reason, source_run = verify_queue(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=changed_event,
        token="synthetic-token",
        api_url="https://api.github.com",
        current_run_id=1000,
    )

    assert reusable is False
    assert "CI policy changed" in reason
    assert source_run is None


def test_verify_queue_skips_full_depth_lineage_and_uses_root_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_sha, head_sha, merge_sha = _merge_preview_repo(tmp_path)
    event = _event(base_sha, head_sha, merge_sha)
    root = create_attestation(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=event,
        workflow_run_id=123,
        workflow_run_attempt=1,
        workflow_ref="opensquilla/opensquilla/.github/workflows/ci.yml@refs/pull/42/merge",
        optimization_mode="enforce",
        **_evidence_metadata(repo),
    )
    full_depth = {
        **root,
        "evidence_kind": "derived",
        "lineage": list(range(1001, 1009)),
        "workflow_run_id": 124,
    }
    root_run = _run(root)
    root_run["created_at"] = "2026-08-20T00:00:00Z"
    full_depth_run = _run(full_depth)
    full_depth_run["created_at"] = "2026-08-20T00:01:00Z"
    archives = {
        "https://api.github.com/root.zip": _archive(root),
        "https://api.github.com/full-depth.zip": _archive(full_depth),
    }
    current_pr = {"number": 42, **event["pull_request"]}

    def fake_json(url: str, _token: str) -> dict[str, Any]:
        if "actions/artifacts" in url:
            return {
                "artifacts": [
                    {
                        "id": 2,
                        "expired": False,
                        "size_in_bytes": len(archives["https://api.github.com/full-depth.zip"]),
                        "created_at": "2026-08-20T00:01:00Z",
                        "archive_download_url": "https://api.github.com/full-depth.zip",
                        "workflow_run": {"id": 124},
                    },
                    {
                        "id": 1,
                        "expired": False,
                        "size_in_bytes": len(archives["https://api.github.com/root.zip"]),
                        "created_at": "2026-08-20T00:00:00Z",
                        "archive_download_url": "https://api.github.com/root.zip",
                        "workflow_run": {"id": 123},
                    },
                ]
            }
        if "/pulls/42" in url:
            return current_pr
        if "actions/workflows/ci.yml/runs" in url:
            return {"workflow_runs": [root_run]}
        if url.endswith("/actions/runs/124"):
            return full_depth_run
        return root_run

    monkeypatch.setitem(verify_queue.__globals__, "_request_json", fake_json)
    monkeypatch.setitem(
        verify_queue.__globals__, "_request_bytes", lambda url, _token: archives[url]
    )
    monkeypatch.setitem(
        verify_queue.__globals__,
        "verify_nightly_health",
        lambda **_kwargs: (True, "latest full nightly is green and fresh"),
    )

    reusable, reason, source_run = verify_queue(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=event,
        token="synthetic-token",
        api_url="https://api.github.com",
        current_run_id=999,
    )

    assert reusable is True
    assert reason == "matching trusted exact-tree CI evidence"
    assert source_run == 123


def test_artifact_listing_retries_visibility_and_paginates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    sleeps: list[int] = []

    def fake_json(url: str, _token: str) -> dict[str, Any]:
        calls.append(url)
        if len(calls) < 3:
            return {"artifacts": []}
        return {"total_count": 1, "artifacts": [{"id": 1}]}

    monkeypatch.setitem(list_attestation_artifacts.__globals__, "_request_json", fake_json)
    monkeypatch.setitem(
        list_attestation_artifacts.__globals__,
        "time",
        type("Clock", (), {"sleep": staticmethod(sleeps.append)}),
    )

    result = list_attestation_artifacts(
        api_url="https://api.github.com",
        repository="opensquilla/opensquilla",
        encoded_name="ci-attestation-tree",
        token="synthetic-token",
    )

    assert result == [{"id": 1}]
    assert sleeps == [10, 20]
    assert "page=1" in calls[-1]


def test_artifact_listing_fails_closed_at_candidate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_json(_url: str, _token: str) -> dict[str, Any]:
        return {"total_count": 301, "artifacts": []}

    monkeypatch.setitem(list_attestation_artifacts.__globals__, "_request_json", fake_json)
    with pytest.raises(AttestationError, match="candidate limit"):
        list_attestation_artifacts(
            api_url="https://api.github.com",
            repository="opensquilla/opensquilla",
            encoded_name="ci-attestation-tree",
            token="synthetic-token",
        )


def test_base_evidence_wait_is_bounded_and_observes_predecessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter((False, False, True))
    sleeps: list[int] = []
    monkeypatch.setitem(
        wait_for_base_successful_ci.__globals__,
        "BASE_CI_VISIBILITY_DELAYS",
        (0, 1, 3),
    )
    monkeypatch.setitem(
        wait_for_base_successful_ci.__globals__,
        "_base_has_successful_ci",
        lambda **_kwargs: next(results),
    )
    monkeypatch.setitem(
        wait_for_base_successful_ci.__globals__,
        "time",
        type("Clock", (), {"sleep": staticmethod(sleeps.append)}),
    )

    assert wait_for_base_successful_ci(
        api_url="https://api.github.com",
        repository="opensquilla/opensquilla",
        token="synthetic-token",
        queue_base_sha="a" * 40,
    )
    assert sleeps == [1, 2]


def test_nightly_health_requires_fresh_authoritative_full_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    run_id = 321
    full_suites = ["python-full", "workflow-lint"]
    full_digests = {
        suite: ("d" if index == 0 else "e") * 64
        for index, suite in enumerate(full_suites)
    }
    full_matrix = [
        {"suite": "python-full", "os": "ubuntu-latest", "shard": "core"},
        {"suite": "workflow-lint", "os": "ubuntu-latest", "shard": "default"},
    ]
    planner_digest = "f" * 64
    health: dict[str, object] = {
        "schema_version": 1,
        "profile": "nightly-health-v1",
        "repository": "opensquilla/opensquilla",
        "workflow_path": ".github/workflows/ci.yml",
        "workflow_ref": (
            "opensquilla/opensquilla/.github/workflows/ci.yml@refs/heads/main"
        ),
        "workflow_run_id": run_id,
        "workflow_run_attempt": 1,
        "head_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "trust_policy_digest": "c" * 64,
        "successful_suites": full_suites,
        "planner_digest": planner_digest,
        "suite_execution_digests": full_digests,
        "platform_matrix": full_matrix,
        "completed_at": now.isoformat(),
    }
    latest = {
        "id": run_id,
        "run_attempt": 1,
        "event": "schedule",
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "head_sha": "a" * 40,
        "updated_at": now.isoformat(),
    }
    archive = _named_archive("ci-nightly-health.json", health)
    monkeypatch.setitem(
        verify_nightly_health.__globals__,
        "_request_json",
        lambda _url, _token: {"workflow_runs": [latest]},
    )
    monkeypatch.setitem(
        verify_nightly_health.__globals__,
        "_list_attestation_artifacts",
        lambda **_kwargs: [
            {
                "workflow_run": {"id": run_id},
                "expired": False,
                "size_in_bytes": len(archive),
                "archive_download_url": "https://api.github.com/nightly.zip",
            }
        ],
    )
    monkeypatch.setitem(
        verify_nightly_health.__globals__,
        "_request_bytes",
        lambda _url, _token: archive,
    )
    monkeypatch.setitem(
        verify_nightly_health.__globals__, "policy_digest", lambda _repo: "c" * 64
    )
    planned_refs: list[str | None] = []

    def plan_full_nightly(
        _repo: Path, _paths: list[str], *, ref: str | None = None
    ) -> dict[str, object]:
        planned_refs.append(ref)
        return {
            "required_suites": full_suites,
            "plan_digest": planner_digest,
            "suite_execution_digests": full_digests,
            "platform_matrix": full_matrix,
        }

    monkeypatch.setitem(
        verify_nightly_health.__globals__,
        "_plan_paths",
        plan_full_nightly,
    )

    healthy, reason = verify_nightly_health(
        repo=tmp_path,
        repository="opensquilla/opensquilla",
        token="synthetic-token",
        api_url="https://api.github.com",
    )
    assert healthy is True
    assert "green and fresh" in reason
    assert planned_refs == ["b" * 40]

    health["successful_suites"] = ["workflow-lint"]
    incomplete_archive = _named_archive("ci-nightly-health.json", health)
    monkeypatch.setitem(
        verify_nightly_health.__globals__,
        "_request_bytes",
        lambda _url, _token: incomplete_archive,
    )
    healthy, reason = verify_nightly_health(
        repo=tmp_path,
        repository="opensquilla/opensquilla",
        token="synthetic-token",
        api_url="https://api.github.com",
    )
    assert healthy is False
    assert "incomplete" in reason
