from __future__ import annotations

import io
import json
import runpy
import subprocess
import urllib.request
import zipfile
from email.message import Message
from pathlib import Path
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


def _merge_preview_repo(
    tmp_path: Path, *, advance_base: bool = False
) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "CI Test")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _write(repo, ".github/workflows/ci.yml", "name: CI\n")
    _write(repo, ".github/scripts/classify-ci-changes.sh", "#!/bin/sh\n")
    _write(repo, "pyproject.toml", "[project]\nname='fixture'\nversion='0'\n")
    _write(repo, "src/example.py", "BASE = True\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    _git(repo, "switch", "-c", "feature")
    _write(repo, "src/example.py", "BASE = True\nFEATURE = True\n")
    _git(repo, "add", "src/example.py")
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


def test_create_attestation_pins_merge_parents_tree_and_policy(tmp_path: Path) -> None:
    repo, base_sha, head_sha, merge_sha = _merge_preview_repo(tmp_path)
    attestation = create_attestation(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=_event(base_sha, head_sha, merge_sha),
        workflow_run_id=123,
        workflow_run_attempt=2,
        workflow_ref="opensquilla/opensquilla/.github/workflows/ci.yml@refs/pull/42/merge",
        optimization_mode="shadow",
    )

    assert attestation["base_sha"] == base_sha
    assert attestation["head_sha"] == head_sha
    assert attestation["tested_merge_sha"] == merge_sha
    assert attestation["tested_tree_sha"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert attestation["policy_digest"] == policy_digest(repo)
    assert attestation["validation_profile"] == "precise-v1"


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
            workflow_ref="workflow-ref",
            optimization_mode="enforce",
        )


def test_validate_candidate_rejects_non_green_or_mismatched_runs(tmp_path: Path) -> None:
    repo, base_sha, head_sha, merge_sha = _merge_preview_repo(tmp_path)
    attestation = create_attestation(
        repo=repo,
        repository="opensquilla/opensquilla",
        event=_event(base_sha, head_sha, merge_sha),
        workflow_run_id=123,
        workflow_run_attempt=1,
        workflow_ref="workflow-ref",
        optimization_mode="enforce",
    )
    run = _run(attestation)

    validate_candidate(
        attestation=attestation,
        run=run,
        repository="opensquilla/opensquilla",
        queue_tree_sha=str(attestation["tested_tree_sha"]),
        queue_base_sha=base_sha,
        queue_policy_digest=str(attestation["policy_digest"]),
        pull_request_head_is_ancestor=True,
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
                queue_policy_digest=str(attestation["policy_digest"]),
                pull_request_head_is_ancestor=True,
            )

    wrong_base = dict(run)
    wrong_base["pull_requests"] = [
        {
            "number": 42,
            "head": {"sha": attestation["head_sha"]},
            "base": {"ref": "main", "sha": "0" * 40},
        }
    ]
    with pytest.raises(AttestationError):
        validate_candidate(
            attestation=attestation,
            run=wrong_base,
            repository="opensquilla/opensquilla",
            queue_tree_sha=str(attestation["tested_tree_sha"]),
            queue_base_sha=base_sha,
            queue_policy_digest=str(attestation["policy_digest"]),
            pull_request_head_is_ancestor=True,
        )


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
        workflow_ref="workflow-ref",
        optimization_mode="shadow",
    )
    run = _run(attestation)

    def fake_json(url: str, _token: str) -> dict[str, Any]:
        if "actions/artifacts" in url:
            return {
                "artifacts": [
                    {
                        "expired": False,
                        "size_in_bytes": len(_archive(attestation)),
                        "created_at": "2026-08-13T00:00:00Z",
                        "archive_download_url": "https://api.github.com/artifact.zip",
                        "workflow_run": {"id": 123},
                    }
                ]
            }
        return run

    monkeypatch.setitem(verify_queue.__globals__, "_request_json", fake_json)
    monkeypatch.setitem(
        verify_queue.__globals__, "_request_bytes", lambda _url, _token: _archive(attestation)
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
    assert reason == "matching trusted PR CI attestation"
    assert source_run == 123

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
