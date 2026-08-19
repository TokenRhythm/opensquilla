#!/usr/bin/env python3
"""Create and verify fail-closed CI attestations for merge-queue reuse."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

SCHEMA_VERSION: Final = 1
VALIDATION_PROFILE: Final = "precise-v1"
WORKFLOW_PATH: Final = ".github/workflows/ci.yml"
MAX_ATTESTATION_ARCHIVE_BYTES: Final = 64 * 1024
MAX_ARTIFACT_PAGES: Final = 3
ARTIFACTS_PER_PAGE: Final = 100
ARTIFACT_VISIBILITY_DELAYS: Final = (0, 10, 30)
SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")
POLICY_PREFIXES: Final = (
    ".github/scripts/",
    ".github/workflows/",
)
POLICY_FILES: Final = {
    ".github/CODEOWNERS",
    ".python-version",
    "pyproject.toml",
    "uv.lock",
    "opensquilla-webui/.node-version",
    "opensquilla-webui/package.json",
    "opensquilla-webui/package-lock.json",
    "desktop/electron/package.json",
    "desktop/electron/package-lock.json",
    "src/opensquilla/cli/tui/opentui/package/.bun-version",
    "src/opensquilla/cli/tui/opentui/package/package.json",
    "src/opensquilla/cli/tui/opentui/package/bun.lock",
}


class AttestationError(RuntimeError):
    """A queue attestation could not be trusted."""


class _SafeArtifactRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow artifact redirects without forwarding GitHub API credentials."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None

        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(redirected.full_url)
        if target.scheme.lower() != "https":
            raise AttestationError("artifact download redirect must use HTTPS")
        if (source.scheme.lower(), source.netloc.lower()) != (
            target.scheme.lower(),
            target.netloc.lower(),
        ):
            for header in ("Authorization", "Accept", "X-GitHub-Api-Version"):
                redirected.remove_header(header)
        return redirected


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise AttestationError(f"{label} must be a lowercase 40-character SHA")
    return value


def _policy_paths(repo: Path, ref: str) -> list[str]:
    paths = _git("ls-tree", "-r", "--name-only", ref, cwd=repo).splitlines()
    return sorted(
        path
        for path in paths
        if path in POLICY_FILES or any(path.startswith(prefix) for prefix in POLICY_PREFIXES)
    )


def policy_digest(repo: Path, ref: str = "HEAD") -> str:
    """Hash every workflow, CI helper, and dependency-policy input at *ref*."""

    digest = hashlib.sha256()
    paths = _policy_paths(repo, ref)
    if not paths:
        raise AttestationError(f"CI policy is empty at {ref}")
    for path in paths:
        content = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=repo,
            check=True,
            capture_output=True,
        ).stdout
        encoded_path = path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _read_event(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AttestationError("GitHub event payload must be a JSON object")
    return value


def _write_outputs(path: Path | None, values: Mapping[str, object]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        for name, value in values.items():
            rendered = str(value).replace("\r", " ").replace("\n", " ")
            handle.write(f"{name}={rendered}\n")


def create_attestation(
    *,
    repo: Path,
    repository: str,
    event: Mapping[str, Any],
    workflow_run_id: int,
    workflow_run_attempt: int,
    workflow_ref: str,
    optimization_mode: str,
) -> dict[str, object]:
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise AttestationError("attestations may only be created for pull_request events")

    number = pull_request.get("number")
    if not isinstance(number, int) or number <= 0:
        raise AttestationError("pull request number is missing")
    head = pull_request.get("head")
    base = pull_request.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise AttestationError("pull request head/base metadata is missing")

    head_sha = _require_sha(head.get("sha"), "pull request head SHA")
    _require_sha(base.get("sha"), "pull request base SHA")
    merge_commit = _require_sha(_git("rev-parse", "HEAD", cwd=repo), "tested commit SHA")
    merge_tree = _require_sha(
        _git("rev-parse", "HEAD^{tree}", cwd=repo), "tested tree SHA"
    )
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD", cwd=repo).split()
    if len(parents) != 3:
        raise AttestationError("pull request CI must test a two-parent merge preview")
    tested_base_sha = _require_sha(parents[1], "tested base SHA")
    tested_head_sha = _require_sha(parents[2], "tested head SHA")
    # GitHub's pull_request base SHA can predate the merge preview when main advances. The
    # preview's first parent is the base that CI actually tested; queue validation still requires
    # that exact base, tree, and policy before reusing this attestation.
    if tested_head_sha != head_sha:
        raise AttestationError("tested merge head does not match the pull request event")

    head_repo = head.get("repo")
    head_repository = head_repo.get("full_name") if isinstance(head_repo, dict) else None
    if not isinstance(head_repository, str) or not head_repository:
        raise AttestationError("pull request head repository is missing")

    return {
        "schema_version": SCHEMA_VERSION,
        "validation_profile": VALIDATION_PROFILE,
        "repository": repository,
        "pull_request_number": number,
        "head_repository": head_repository,
        "head_sha": head_sha,
        "base_ref": base.get("ref"),
        "base_sha": tested_base_sha,
        "tested_merge_sha": merge_commit,
        "tested_tree_sha": merge_tree,
        "policy_digest": policy_digest(repo),
        "workflow_path": WORKFLOW_PATH,
        "workflow_ref": workflow_ref,
        "workflow_run_id": workflow_run_id,
        "workflow_run_attempt": workflow_run_attempt,
        "optimization_mode": optimization_mode,
    }


def _request_json(url: str, token: str) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "opensquilla-ci-attestation",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise AttestationError(f"GitHub API returned a non-object for {url}")
    return value


def _request_bytes(url: str, token: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "opensquilla-ci-attestation",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = urllib.request.build_opener(_SafeArtifactRedirectHandler())
    with opener.open(request, timeout=60) as response:
        return response.read()


def _artifact_attestation(archive: bytes) -> Mapping[str, Any]:
    if len(archive) > MAX_ATTESTATION_ARCHIVE_BYTES:
        raise AttestationError("attestation artifact archive is too large")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = bundle.namelist()
        if names != ["ci-attestation.json"]:
            raise AttestationError("attestation artifact must contain only ci-attestation.json")
        info = bundle.getinfo(names[0])
        if info.file_size > MAX_ATTESTATION_ARCHIVE_BYTES:
            raise AttestationError("attestation JSON is too large")
        value = json.loads(bundle.read(info))
    if not isinstance(value, dict):
        raise AttestationError("attestation must be a JSON object")
    return value


def _reason_code(reason: str, *, api_error: bool = False) -> str:
    lowered = reason.lower()
    if api_error:
        return "api_error"
    if "candidate limit" in lowered:
        return "candidate_limit"
    if "no pr ci attestation" in lowered or "expired" in lowered:
        return "artifact_unavailable"
    if "associated with the attested pull request" in lowered:
        return "pr_association_invalid"
    if "workflow run" in lowered:
        return "source_run_invalid"
    if "base_sha" in lowered or "base does not match" in lowered:
        return "base_mismatch"
    if "tree" in lowered:
        return "tree_mismatch"
    if "policy" in lowered:
        return "policy_mismatch"
    if "not in the queue commit" in lowered:
        return "head_not_ancestor"
    if "queue" in lowered or "merge_group" in lowered or "checkout" in lowered:
        return "invalid_context"
    return "artifact_invalid"


def _list_attestation_artifacts(
    *, api_url: str, repository: str, encoded_name: str, token: str
) -> list[Mapping[str, Any]]:
    """List a bounded artifact set, retrying only temporary visibility misses."""

    previous_delay = 0
    for scheduled_delay in ARTIFACT_VISIBILITY_DELAYS:
        if scheduled_delay:
            time.sleep(scheduled_delay - previous_delay)
        previous_delay = scheduled_delay
        candidates: list[Mapping[str, Any]] = []
        try:
            for page in range(1, MAX_ARTIFACT_PAGES + 1):
                listing = _request_json(
                    f"{api_url}/repos/{repository}/actions/artifacts"
                    f"?name={encoded_name}&per_page={ARTIFACTS_PER_PAGE}&page={page}",
                    token,
                )
                total_count = listing.get("total_count")
                if isinstance(total_count, int) and total_count > (
                    MAX_ARTIFACT_PAGES * ARTIFACTS_PER_PAGE
                ):
                    raise AttestationError("artifact candidate limit exceeded")
                page_items = listing.get("artifacts")
                if not isinstance(page_items, list):
                    raise AttestationError("artifact listing is invalid")
                candidates.extend(item for item in page_items if isinstance(item, dict))
                if len(page_items) < ARTIFACTS_PER_PAGE:
                    break
            if len(candidates) > MAX_ARTIFACT_PAGES * ARTIFACTS_PER_PAGE:
                raise AttestationError("artifact candidate limit exceeded")
            if candidates:
                return candidates
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    raise AttestationError("no PR CI attestation exists for the queue tree")


def validate_candidate(
    *,
    attestation: Mapping[str, Any],
    run: Mapping[str, Any],
    repository: str,
    queue_tree_sha: str,
    queue_base_sha: str,
    queue_policy_digest: str,
    pull_request_head_is_ancestor: bool,
) -> None:
    """Validate one downloaded artifact and its authoritative workflow run."""

    expected = {
        "schema_version": SCHEMA_VERSION,
        "validation_profile": VALIDATION_PROFILE,
        "repository": repository,
        "base_ref": "main",
        "base_sha": queue_base_sha,
        "tested_tree_sha": queue_tree_sha,
        "policy_digest": queue_policy_digest,
        "workflow_path": WORKFLOW_PATH,
    }
    for key, expected_value in expected.items():
        if attestation.get(key) != expected_value:
            raise AttestationError(f"attestation {key} does not match the queue")

    run_id = attestation.get("workflow_run_id")
    run_attempt = attestation.get("workflow_run_attempt")
    pr_number = attestation.get("pull_request_number")
    if not isinstance(run_id, int) or run_id <= 0:
        raise AttestationError("attestation workflow_run_id is invalid")
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        raise AttestationError("attestation workflow_run_attempt is invalid")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise AttestationError("attestation pull_request_number is invalid")
    head_sha = _require_sha(attestation.get("head_sha"), "attested head SHA")
    tested_merge_sha = _require_sha(
        attestation.get("tested_merge_sha"), "attested merge SHA"
    )

    run_repository = run.get("repository")
    run_repository_name = (
        run_repository.get("full_name") if isinstance(run_repository, dict) else None
    )
    authoritative = {
        "id": run_id,
        "run_attempt": run_attempt,
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "path": WORKFLOW_PATH,
    }
    for key, expected_value in authoritative.items():
        if run.get(key) != expected_value:
            raise AttestationError(f"workflow run {key} is not authoritative")
    if run.get("head_sha") not in {head_sha, tested_merge_sha}:
        raise AttestationError("workflow run head_sha is not authoritative")
    if run_repository_name != repository:
        raise AttestationError("workflow run belongs to another repository")

    pull_requests = run.get("pull_requests")
    if not isinstance(pull_requests, list):
        raise AttestationError("workflow run pull request association is missing")
    matching_pr = False
    for item in pull_requests:
        if not isinstance(item, dict) or item.get("number") != pr_number:
            continue
        item_head = item.get("head")
        item_base = item.get("base")
        if (
            isinstance(item_head, dict)
            and isinstance(item_base, dict)
            and item_head.get("sha") == head_sha
            and item_base.get("ref") == "main"
        ):
            matching_pr = True
            break
    if not matching_pr:
        raise AttestationError("workflow run is not associated with the attested pull request")
    if not pull_request_head_is_ancestor:
        raise AttestationError("attested pull request head is not in the queue commit")


def verify_queue(
    *,
    repo: Path,
    repository: str,
    event: Mapping[str, Any],
    token: str,
    api_url: str,
    current_run_id: int,
    details: dict[str, object] | None = None,
) -> tuple[bool, str, int | None]:
    details = details if details is not None else {}
    details.update(candidate_count=0, artifact_name="")
    merge_group = event.get("merge_group")
    if not isinstance(merge_group, dict):
        details["reason_code"] = "invalid_context"
        return False, "not a merge_group event", None
    try:
        queue_head_sha = _require_sha(merge_group.get("head_sha"), "queue head SHA")
        queue_base_sha = _require_sha(merge_group.get("base_sha"), "queue base SHA")
        details.update(queue_head_sha=queue_head_sha, queue_base_sha=queue_base_sha)
        checked_out_sha = _require_sha(_git("rev-parse", "HEAD", cwd=repo), "checkout SHA")
        if checked_out_sha != queue_head_sha:
            raise AttestationError("checked out commit is not the merge-group head")
        queue_tree_sha = _require_sha(
            _git("rev-parse", "HEAD^{tree}", cwd=repo), "queue tree SHA"
        )
        details["queue_tree_sha"] = queue_tree_sha
        queue_policy = policy_digest(repo)
        base_policy = policy_digest(repo, queue_base_sha)
        if queue_policy != base_policy:
            raise AttestationError("CI policy changed relative to the queue base")

        name = f"ci-attestation-{queue_tree_sha}"
        details["artifact_name"] = name
        encoded_name = urllib.parse.quote(name, safe="")
        artifacts = _list_attestation_artifacts(
            api_url=api_url,
            repository=repository,
            encoded_name=encoded_name,
            token=token,
        )
        details["candidate_count"] = len(artifacts)

        reasons: list[str] = []
        for artifact in sorted(
            (item for item in artifacts if isinstance(item, dict)),
            key=lambda item: str(item.get("created_at", "")),
            reverse=True,
        ):
            try:
                if artifact.get("expired") is not False:
                    raise AttestationError("artifact is expired")
                artifact_size = artifact.get("size_in_bytes")
                if (
                    not isinstance(artifact_size, int)
                    or artifact_size <= 0
                    or artifact_size > MAX_ATTESTATION_ARCHIVE_BYTES
                ):
                    raise AttestationError("artifact size is invalid")
                workflow_run = artifact.get("workflow_run")
                run_id = workflow_run.get("id") if isinstance(workflow_run, dict) else None
                if not isinstance(run_id, int) or run_id <= 0 or run_id == current_run_id:
                    raise AttestationError("artifact workflow run identity is invalid")
                archive_url = artifact.get("archive_download_url")
                if not isinstance(archive_url, str) or not archive_url.startswith(
                    "https://api.github.com/"
                ):
                    raise AttestationError("artifact download URL is invalid")
                attestation = _artifact_attestation(_request_bytes(archive_url, token))
                if attestation.get("workflow_run_id") != run_id:
                    raise AttestationError("artifact and attestation run IDs differ")
                run = _request_json(
                    f"{api_url}/repos/{repository}/actions/runs/{run_id}", token
                )
                head_sha = attestation.get("head_sha")
                head_is_ancestor = False
                if isinstance(head_sha, str) and SHA_RE.fullmatch(head_sha):
                    head_is_ancestor = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", head_sha, "HEAD"],
                        cwd=repo,
                        check=False,
                    ).returncode == 0
                validate_candidate(
                    attestation=attestation,
                    run=run,
                    repository=repository,
                    queue_tree_sha=queue_tree_sha,
                    queue_base_sha=queue_base_sha,
                    queue_policy_digest=queue_policy,
                    pull_request_head_is_ancestor=head_is_ancestor,
                )
                details["reason_code"] = "reusable_exact"
                return True, "matching trusted PR CI attestation", run_id
            except (AttestationError, OSError, ValueError, zipfile.BadZipFile) as exc:
                reasons.append(str(exc))
        detail = "; ".join(reasons[:3]) or "no usable attestation artifacts"
        raise AttestationError(detail)
    except (AttestationError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        reason = str(exc)
        details["reason_code"] = _reason_code(reason, api_error=isinstance(exc, OSError))
        return False, reason, None


def _create_command(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    event = _read_event(Path(args.event_path))
    value = create_attestation(
        repo=repo,
        repository=args.repository,
        event=event,
        workflow_run_id=args.run_id,
        workflow_run_attempt=args.run_attempt,
        workflow_ref=args.workflow_ref,
        optimization_mode=args.optimization_mode,
    )
    output = Path(args.output)
    output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    _write_outputs(
        Path(args.github_output) if args.github_output else None,
        {
            "tree_sha": value["tested_tree_sha"],
            "policy_digest": value["policy_digest"],
        },
    )
    print(f"Created attestation for tree {value['tested_tree_sha']}")
    return 0


def _verify_queue_command(args: argparse.Namespace) -> int:
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("GH_TOKEN is required", file=sys.stderr)
        return 2
    details: dict[str, object] = {}
    reusable, reason, source_run_id = verify_queue(
        repo=Path(args.repo).resolve(),
        repository=args.repository,
        event=_read_event(Path(args.event_path)),
        token=token,
        api_url=args.api_url.rstrip("/"),
        current_run_id=args.run_id,
        details=details,
    )
    _write_outputs(
        Path(args.github_output) if args.github_output else None,
        {
            "reusable": str(reusable).lower(),
            "reason": reason,
            "reason_code": details.get("reason_code", "artifact_invalid"),
            "source_run_id": source_run_id or "",
            "candidate_count": details.get("candidate_count", 0),
            "artifact_name": details.get("artifact_name", ""),
            "queue_base_sha": details.get("queue_base_sha", ""),
            "queue_head_sha": details.get("queue_head_sha", ""),
            "queue_tree_sha": details.get("queue_tree_sha", ""),
        },
    )
    print(f"reusable={str(reusable).lower()} reason={reason}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--repo", default=".")
    create.add_argument("--repository", required=True)
    create.add_argument("--event-path", required=True)
    create.add_argument("--run-id", type=int, required=True)
    create.add_argument("--run-attempt", type=int, required=True)
    create.add_argument("--workflow-ref", required=True)
    create.add_argument("--optimization-mode", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--github-output")
    create.set_defaults(func=_create_command)

    verify = subparsers.add_parser("verify-queue")
    verify.add_argument("--repo", default=".")
    verify.add_argument("--repository", required=True)
    verify.add_argument("--event-path", required=True)
    verify.add_argument("--run-id", type=int, required=True)
    verify.add_argument("--api-url", default="https://api.github.com")
    verify.add_argument("--github-output")
    verify.set_defaults(func=_verify_queue_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (AttestationError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
