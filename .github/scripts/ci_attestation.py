#!/usr/bin/env python3
"""Create and verify fail-closed CI attestations for merge-queue reuse."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

SCHEMA_VERSION: Final = 2
VALIDATION_PROFILE: Final = "suite-evidence-v2"
WORKFLOW_PATH: Final = ".github/workflows/ci.yml"
MAX_ATTESTATION_ARCHIVE_BYTES: Final = 64 * 1024
MAX_ARTIFACT_PAGES: Final = 3
ARTIFACTS_PER_PAGE: Final = 100
ARTIFACT_VISIBILITY_DELAYS: Final = (0, 10, 30)
BASE_CI_VISIBILITY_DELAYS: Final = (0, 15, 45, 90, 150)
EVIDENCE_TTL_SECONDS: Final = 72 * 60 * 60
NIGHTLY_MAX_AGE_SECONDS: Final = 30 * 60 * 60
MAX_LINEAGE_DEPTH: Final = 8
SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
PR_QUEUE_REF_RE: Final = re.compile(r"(?:^|/)pr-(?P<number>[1-9][0-9]*)-")
COMPOSITION_BASELINE_SUITES: Final = frozenset({"readme-locale", "workflow-lint"})
COMPOSITION_COMBINED_SMOKE_TRUST_ROOT: Final = frozenset()
TRUST_POLICY_PATHS: Final = (
    ".github/CODEOWNERS",
    ".github/ci/suites.v1.json",
    ".github/scripts",
    ".github/workflows",
)


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


def _changed_paths(repo: Path, before: str, after: str) -> list[str]:
    """Return both sides of renames so the planner can route them safely."""

    return _git(
        "diff", "--no-renames", "--name-only", before, after, cwd=repo
    ).splitlines()


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise AttestationError(f"{label} must be a lowercase 40-character SHA")
    return value


def _policy_entries(repo: Path, ref: str) -> bytes:
    return subprocess.run(
        ["git", "ls-tree", "-r", "-z", ref, "--", *TRUST_POLICY_PATHS],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout


def policy_digest(repo: Path, ref: str = "HEAD") -> str:
    """Hash the small, stable trust root that is allowed to suppress CI suites."""

    digest = hashlib.sha256()
    entries = _policy_entries(repo, ref)
    if not entries:
        raise AttestationError(f"CI policy is empty at {ref}")
    digest.update(entries)
    return digest.hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise AttestationError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttestationError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise AttestationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _evidence_age_seconds(attestation: Mapping[str, Any]) -> float:
    issued = _parse_time(attestation.get("root_issued_at"), "root evidence timestamp")
    return (datetime.now(UTC) - issued).total_seconds()


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


def _canonical_suite_json(suites: Sequence[str] = ()) -> str:
    """Return a stable JSON array for suite-valued action outputs."""

    return json.dumps(sorted(set(suites)), separators=(",", ":"))


def create_attestation(
    *,
    repo: Path,
    repository: str,
    event: Mapping[str, Any],
    workflow_run_id: int,
    workflow_run_attempt: int,
    workflow_ref: str,
    optimization_mode: str,
    successful_suites: Sequence[str] = (),
    planner_digest: str = "",
    suite_execution_digests: Mapping[str, str] | None = None,
    platform_matrix: Sequence[Mapping[str, str]] = (),
    plan_basis: str = "change_set",
    source_attestation: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Create root PR/full evidence or a derived merge-queue evidence record."""

    pull_request = event.get("pull_request")
    merge_group = event.get("merge_group")
    tested_commit = _require_sha(_git("rev-parse", "HEAD", cwd=repo), "tested commit SHA")
    tested_tree = _require_sha(_git("rev-parse", "HEAD^{tree}", cwd=repo), "tested tree SHA")
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD", cwd=repo).split()

    source_event: str
    number: int | None = None
    head_repository: str | None = None
    head_sha: str | None = None
    head_ref: str | None = None
    base_ref = "main"
    if isinstance(pull_request, dict):
        source_event = "pull_request"
        number_value = pull_request.get("number")
        if not isinstance(number_value, int) or number_value <= 0:
            raise AttestationError("pull request number is missing")
        number = number_value
        head = pull_request.get("head")
        base = pull_request.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise AttestationError("pull request head/base metadata is missing")
        head_sha = _require_sha(head.get("sha"), "pull request head SHA")
        head_ref_value = head.get("ref")
        if not isinstance(head_ref_value, str) or not head_ref_value:
            raise AttestationError("pull request head ref is missing")
        head_ref = head_ref_value
        _require_sha(base.get("sha"), "pull request base SHA")
        if len(parents) != 3:
            raise AttestationError("pull request CI must test a two-parent merge preview")
        tested_base_sha = _require_sha(parents[1], "tested base SHA")
        tested_head_sha = _require_sha(parents[2], "tested head SHA")
        if tested_head_sha != head_sha:
            raise AttestationError("tested merge head does not match the pull request event")
        head_repo = head.get("repo")
        head_repository_value = (
            head_repo.get("full_name") if isinstance(head_repo, dict) else None
        )
        if not isinstance(head_repository_value, str) or not head_repository_value:
            raise AttestationError("pull request head repository is missing")
        head_repository = head_repository_value
        base_ref_value = base.get("ref")
        if isinstance(base_ref_value, str) and base_ref_value:
            base_ref = base_ref_value
    elif isinstance(merge_group, dict):
        source_event = "merge_group"
        event_head = _require_sha(merge_group.get("head_sha"), "merge-group head SHA")
        if tested_commit != event_head:
            raise AttestationError("tested commit does not match the merge-group head")
        tested_base_sha = _require_sha(merge_group.get("base_sha"), "merge-group base SHA")
        if (
            len(parents) != 2
            or _require_sha(parents[1], "merge-group parent SHA") != tested_base_sha
        ):
            raise AttestationError("merge-group CI must test the expected single-parent commit")
        head_ref = merge_group.get("head_ref")
        if isinstance(head_ref, str):
            match = PR_QUEUE_REF_RE.search(head_ref)
            if match:
                number = int(match.group("number"))
    else:
        raise AttestationError("attestations require a pull_request or merge_group event")

    issued_at = _now_iso()
    root_issued_at = issued_at
    lineage: list[int] = []
    evidence_kind = "root"
    if source_attestation is not None:
        root_issued_at = str(source_attestation.get("root_issued_at", ""))
        _parse_time(root_issued_at, "source root evidence timestamp")
        existing_lineage = source_attestation.get("lineage", [])
        if not isinstance(existing_lineage, list) or any(
            not isinstance(item, int) or item <= 0 for item in existing_lineage
        ):
            raise AttestationError("source evidence lineage is invalid")
        lineage = [*existing_lineage, int(source_attestation["workflow_run_id"])]
        if len(lineage) > MAX_LINEAGE_DEPTH or len(lineage) != len(set(lineage)):
            raise AttestationError("source evidence lineage is invalid")
        evidence_kind = "derived"

    if plan_basis not in {"change_set", "full_fallback"}:
        raise AttestationError("evidence plan basis is invalid")
    if plan_basis == "full_fallback" and (
        source_event != "merge_group" or evidence_kind != "root"
    ):
        raise AttestationError("full-fallback evidence must be a root merge-group run")

    suites = sorted({item for item in successful_suites if item}) or ["ci-result"]
    execution_digests = dict(sorted((suite_execution_digests or {}).items()))
    canonical_platform_matrix = sorted(
        (dict(cell) for cell in platform_matrix),
        key=lambda cell: (cell.get("suite", ""), cell.get("os", ""), cell.get("shard", "")),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_profile": VALIDATION_PROFILE,
        "repository": repository,
        "source_event": source_event,
        "evidence_kind": evidence_kind,
        "plan_basis": plan_basis,
        "issued_at": issued_at,
        "root_issued_at": root_issued_at,
        "lineage": lineage,
        "pull_request_number": number,
        "head_repository": head_repository,
        "head_sha": head_sha,
        "head_ref": head_ref,
        "base_ref": base_ref,
        "base_sha": tested_base_sha,
        "tested_merge_sha": tested_commit,
        "tested_tree_sha": tested_tree,
        "trust_policy_digest": policy_digest(repo),
        "successful_suites": suites,
        "planner_digest": planner_digest,
        "suite_execution_digests": execution_digests,
        "platform_matrix": canonical_platform_matrix,
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


def _artifact_json(archive: bytes, *, expected_name: str) -> Mapping[str, Any]:
    if len(archive) > MAX_ATTESTATION_ARCHIVE_BYTES:
        raise AttestationError("attestation artifact archive is too large")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = bundle.namelist()
        if names != [expected_name]:
            raise AttestationError(f"attestation artifact must contain only {expected_name}")
        info = bundle.getinfo(names[0])
        if info.file_size > MAX_ATTESTATION_ARCHIVE_BYTES:
            raise AttestationError("attestation JSON is too large")
        value = json.loads(bundle.read(info))
    if not isinstance(value, dict):
        raise AttestationError("attestation must be a JSON object")
    return value


def _artifact_attestation(archive: bytes) -> Mapping[str, Any]:
    return _artifact_json(archive, expected_name="ci-attestation.json")


def _reason_code(reason: str, *, api_error: bool = False) -> str:
    lowered = reason.lower()
    if api_error:
        return "api_error"
    if "candidate limit" in lowered:
        return "candidate_limit"
    if "no ci evidence" in lowered or "expired" in lowered or "too old" in lowered:
        return "artifact_unavailable"
    if "associated with the attested pull request" in lowered:
        return "pr_association_invalid"
    if "workflow run" in lowered:
        return "source_run_invalid"
    if "nightly" in lowered:
        return "nightly_unhealthy"
    if "base_sha" in lowered or "base does not match" in lowered or "base evidence" in lowered:
        return "base_mismatch"
    if "tree" in lowered:
        return "tree_mismatch"
    if "policy" in lowered:
        return "policy_mismatch"
    if "lineage" in lowered:
        return "lineage_invalid"
    if "coverage" in lowered or "suite" in lowered or "planner" in lowered:
        return "coverage_mismatch"
    if "queue" in lowered or "merge_group" in lowered or "checkout" in lowered:
        return "invalid_context"
    return "artifact_invalid"


def _list_attestation_artifacts(
    *,
    api_url: str,
    repository: str,
    encoded_name: str,
    token: str,
    visibility_delays: Sequence[int] = ARTIFACT_VISIBILITY_DELAYS,
) -> list[Mapping[str, Any]]:
    """List a bounded artifact set, retrying only temporary visibility misses."""

    previous_delay = 0
    for scheduled_delay in visibility_delays:
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
    raise AttestationError("no CI evidence exists for the requested identity")


def _queue_pr_number(merge_group: Mapping[str, Any]) -> int:
    head_ref = merge_group.get("head_ref")
    if not isinstance(head_ref, str):
        raise AttestationError("merge queue head ref is missing")
    match = PR_QUEUE_REF_RE.search(head_ref)
    if match is None:
        raise AttestationError("merge queue head ref does not identify one pull request")
    return int(match.group("number"))


def _pull_request_identity(value: Mapping[str, Any]) -> tuple[str, str, str]:
    head = value.get("head")
    base = value.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise AttestationError("pull request identity is incomplete")
    head_sha = _require_sha(head.get("sha"), "current pull request head SHA")
    head_repo = head.get("repo")
    head_repository = head_repo.get("full_name") if isinstance(head_repo, dict) else None
    if not isinstance(head_repository, str) or not head_repository:
        raise AttestationError("current pull request head repository is missing")
    base_ref = base.get("ref")
    if not isinstance(base_ref, str) or not base_ref:
        raise AttestationError("current pull request base ref is missing")
    return head_sha, head_repository, base_ref


def _ensure_pr_head(repo: Path, *, pull_request_number: int, head_sha: str) -> None:
    if subprocess.run(
        ["git", "cat-file", "-e", f"{head_sha}^{{commit}}"],
        cwd=repo,
        check=False,
        capture_output=True,
    ).returncode == 0:
        return
    subprocess.run(
        [
            "git",
            "fetch",
            "--no-tags",
            "--depth=1",
            "origin",
            f"+refs/pull/{pull_request_number}/head:refs/ci/pr-{pull_request_number}",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    fetched = _require_sha(
        _git("rev-parse", f"refs/ci/pr-{pull_request_number}", cwd=repo),
        "fetched pull request head SHA",
    )
    if fetched != head_sha:
        raise AttestationError("fetched pull request head changed during verification")


def _reconstructed_queue_tree(repo: Path, *, queue_base_sha: str, head_sha: str) -> str:
    return _require_sha(
        _git("merge-tree", "--write-tree", queue_base_sha, head_sha, cwd=repo).splitlines()[0],
        "reconstructed queue tree SHA",
    )


def _validate_lineage(attestation: Mapping[str, Any]) -> None:
    evidence_kind = attestation.get("evidence_kind")
    if evidence_kind not in {"root", "derived"}:
        raise AttestationError("evidence kind is invalid")
    lineage = attestation.get("lineage")
    if not isinstance(lineage, list) or any(
        not isinstance(item, int) or item <= 0 for item in lineage
    ):
        raise AttestationError("evidence lineage is invalid")
    if len(lineage) > MAX_LINEAGE_DEPTH or len(lineage) != len(set(lineage)):
        raise AttestationError("evidence lineage is invalid")
    if evidence_kind == "root" and lineage:
        raise AttestationError("root evidence must not have lineage")


def _validated_suites(attestation: Mapping[str, Any]) -> set[str]:
    value = attestation.get("successful_suites")
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise AttestationError("evidence suite coverage is invalid")
    if len(value) != len(set(value)) or value != sorted(value):
        raise AttestationError("evidence suite coverage must be canonical")
    return set(value)


def _validated_execution_digests(
    attestation: Mapping[str, Any], suites: set[str]
) -> Mapping[str, str]:
    planner_digest = attestation.get("planner_digest")
    value = attestation.get("suite_execution_digests")
    if not isinstance(planner_digest, str) or DIGEST_RE.fullmatch(planner_digest) is None:
        raise AttestationError("evidence planner digest is invalid")
    if not isinstance(value, dict) or set(value) != suites:
        raise AttestationError("evidence suite execution digest coverage is invalid")
    if any(
        not isinstance(key, str)
        or not isinstance(digest, str)
        or DIGEST_RE.fullmatch(digest) is None
        for key, digest in value.items()
    ):
        raise AttestationError("evidence suite execution digest is invalid")
    if list(value) != sorted(value):
        raise AttestationError("evidence suite execution digests must be canonical")
    return value


def _validated_platform_matrix(
    attestation: Mapping[str, Any], suites: set[str]
) -> list[dict[str, str]]:
    value = attestation.get("platform_matrix")
    if not isinstance(value, list) or not value:
        raise AttestationError("evidence platform matrix is invalid")
    canonical: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_cell in value:
        if not isinstance(raw_cell, dict) or set(raw_cell) != {"suite", "os", "shard"}:
            raise AttestationError("evidence platform matrix cell is invalid")
        if any(not isinstance(raw_cell[key], str) or not raw_cell[key] for key in raw_cell):
            raise AttestationError("evidence platform matrix cell is invalid")
        cell = {
            "suite": raw_cell["suite"],
            "os": raw_cell["os"],
            "shard": raw_cell["shard"],
        }
        if cell["suite"] not in suites:
            raise AttestationError("evidence platform matrix references an unknown suite")
        key = (cell["suite"], cell["os"], cell["shard"])
        if key in seen:
            raise AttestationError("evidence platform matrix contains duplicate cells")
        seen.add(key)
        canonical.append(cell)
    expected = sorted(
        canonical,
        key=lambda cell: (cell["suite"], cell["os"], cell["shard"]),
    )
    if value != expected:
        raise AttestationError("evidence platform matrix must be canonical")
    covered = {cell["suite"] for cell in canonical}
    if covered != suites:
        raise AttestationError("evidence platform matrix suite coverage is incomplete")
    return canonical


def _current_suite_execution_digests(
    repo: Path, suites: set[str]
) -> Mapping[str, str]:
    if not suites:
        return {}
    planner_path = repo / ".github" / "scripts" / "plan_ci.py"
    config_path = repo / ".github" / "ci" / "suites.v1.json"
    if not planner_path.is_file() or not config_path.is_file():
        raise AttestationError("CI suite planner contract is missing")
    try:
        namespace = runpy.run_path(planner_path.as_posix(), run_name="ci_digest_planner")
        config = namespace["load_config"](config_path)
        value = namespace["suite_execution_digests"](
            suites, repo=repo, config=config
        )
    except (KeyError, OSError, ValueError) as exc:
        raise AttestationError("CI suite execution digest calculation failed") from exc
    if not isinstance(value, dict):
        raise AttestationError("CI suite execution digest calculation returned invalid output")
    return value


def _latest_pr_run_id(
    *,
    api_url: str,
    repository: str,
    token: str,
    pull_request_number: int,
    head_sha: str,
    head_ref: str,
) -> int:
    if not head_ref:
        raise AttestationError("attested pull request head ref is missing")
    encoded_branch = urllib.parse.quote(head_ref, safe="")
    listing = _request_json(
        f"{api_url}/repos/{repository}/actions/workflows/ci.yml/runs"
        f"?event=pull_request&branch={encoded_branch}&per_page=100",
        token,
    )
    runs = listing.get("workflow_runs")
    if not isinstance(runs, list):
        raise AttestationError("workflow run listing is invalid")
    matching: list[Mapping[str, Any]] = []
    for candidate in runs:
        if not isinstance(candidate, dict):
            continue
        pull_requests = candidate.get("pull_requests")
        if not isinstance(pull_requests, list):
            continue
        if any(
            isinstance(item, dict)
            and item.get("number") == pull_request_number
            and isinstance(item.get("head"), dict)
            and item["head"].get("sha") == head_sha
            for item in pull_requests
        ):
            matching.append(candidate)
    if not matching:
        raise AttestationError("no authoritative successful workflow run exists for the PR head")
    matching.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    if matching[0].get("conclusion") != "success":
        raise AttestationError("latest authoritative workflow run for the PR head is not green")
    run_id = matching[0].get("id")
    if not isinstance(run_id, int) or run_id <= 0:
        raise AttestationError("latest workflow run identity is invalid")
    return run_id


def _base_has_successful_ci(
    *, api_url: str, repository: str, token: str, queue_base_sha: str
) -> bool:
    listing = _request_json(
        f"{api_url}/repos/{repository}/actions/runs"
        f"?head_sha={queue_base_sha}&event=merge_group&status=success&per_page=20",
        token,
    )
    runs = listing.get("workflow_runs")
    if not isinstance(runs, list):
        raise AttestationError("base workflow run listing is invalid")
    return any(
        isinstance(run, dict)
        and run.get("path") == WORKFLOW_PATH
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("head_sha") == queue_base_sha
        for run in runs
    )


def _wait_for_base_successful_ci(
    *, api_url: str, repository: str, token: str, queue_base_sha: str
) -> bool:
    previous_delay = 0
    for scheduled_delay in BASE_CI_VISIBILITY_DELAYS:
        if scheduled_delay:
            time.sleep(scheduled_delay - previous_delay)
        previous_delay = scheduled_delay
        if _base_has_successful_ci(
            api_url=api_url,
            repository=repository,
            token=token,
            queue_base_sha=queue_base_sha,
        ):
            return True
    return False


def _plan_paths(
    repo: Path, paths: Sequence[str], *, ref: str | None = None
) -> Mapping[str, Any]:
    planner = repo / ".github" / "scripts" / "plan_ci.py"
    if not planner.is_file():
        raise AttestationError("CI suite planner is missing")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="opensquilla-ci-paths-", delete=False
        ) as handle:
            for path in sorted(set(paths)):
                handle.write(f"{path}\n")
            temporary_path = Path(handle.name)
        command = [
            sys.executable,
            str(planner),
            str(temporary_path),
            "--repo",
            str(repo),
        ]
        if ref is not None:
            command.extend(("--ref", ref))
        completed = subprocess.run(
            command,
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(completed.stdout)
    except (json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise AttestationError("CI suite planner failed") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    if not isinstance(value, dict):
        raise AttestationError("CI suite planner returned invalid output")
    return value


def _validate_canonical_source_plan(
    *, repo: Path, attestation: Mapping[str, Any], suites: set[str]
) -> None:
    tested_tree = _require_sha(attestation.get("tested_tree_sha"), "tested tree SHA")
    tested_base = _require_sha(attestation.get("base_sha"), "tested base SHA")
    source_event = attestation.get("source_event")
    plan_basis = attestation.get("plan_basis")
    evidence_kind = attestation.get("evidence_kind")
    if plan_basis == "full_fallback":
        if source_event != "merge_group" or evidence_kind != "root":
            raise AttestationError(
                "full-fallback evidence must be a root merge-group run"
            )
        changed_paths = [".ci/run-all"]
    elif plan_basis != "change_set":
        raise AttestationError("evidence plan basis is invalid")
    elif source_event == "pull_request":
        head_sha = _require_sha(attestation.get("head_sha"), "attested head SHA")
        reconstructed = _reconstructed_queue_tree(
            repo, queue_base_sha=tested_base, head_sha=head_sha
        )
        if reconstructed != tested_tree:
            raise AttestationError("attested PR base/head do not reconstruct tested tree")
        changed_paths = _changed_paths(repo, tested_base, head_sha)
    elif source_event == "merge_group":
        changed_paths = _changed_paths(repo, tested_base, tested_tree)
    else:
        raise AttestationError("attestation source event is invalid")
    plan = _plan_paths(repo, changed_paths, ref=tested_tree)
    if plan.get("required_suites") != sorted(suites):
        raise AttestationError("evidence suite coverage does not match canonical source plan")
    if plan.get("plan_digest") != attestation.get("planner_digest"):
        raise AttestationError("evidence planner digest does not match canonical source plan")
    if plan.get("suite_execution_digests") != attestation.get(
        "suite_execution_digests"
    ):
        raise AttestationError(
            "evidence execution digests do not match canonical source plan"
        )
    if plan.get("platform_matrix") != attestation.get("platform_matrix"):
        raise AttestationError(
            "evidence platform matrix does not match canonical source plan"
        )


def _composition_is_safe(
    *, repo: Path, attestation: Mapping[str, Any], queue_base_sha: str
) -> tuple[bool, str, tuple[str, ...]]:
    tested_base_sha = _require_sha(attestation.get("base_sha"), "tested base SHA")
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", tested_base_sha, queue_base_sha],
        cwd=repo,
        check=False,
        capture_output=True,
    ).returncode != 0:
        return False, "attested base is not an ancestor of the queue base", ()
    changed = _changed_paths(repo, tested_base_sha, queue_base_sha)
    if not changed:
        return False, "advanced-base composition has no verifiable base delta", ()
    plan = _plan_paths(repo, changed)
    if plan.get("full_fallback") is True:
        return False, "base delta requires full fallback", ()
    required = plan.get("required_suites")
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        return False, "base delta planner coverage is invalid", ()
    source_suites = _validated_suites(attestation)
    if source_suites == {"ci-result"} or not attestation.get("planner_digest"):
        return False, "source evidence lacks suite planner coverage", ()
    source_risk_suites = source_suites - COMPOSITION_BASELINE_SUITES
    base_risk_suites = set(required) - COMPOSITION_BASELINE_SUITES
    overlap = source_risk_suites.intersection(base_risk_suites)
    unsupported_overlap = overlap - COMPOSITION_COMBINED_SMOKE_TRUST_ROOT
    if unsupported_overlap:
        return (
            False,
            "base delta overlaps unsupported source suites: "
            + ",".join(sorted(unsupported_overlap)),
            (),
        )
    combined_smoke_suites = tuple(sorted(overlap))
    attested_digests = _validated_execution_digests(attestation, source_suites)
    current_digests = _current_suite_execution_digests(repo, source_suites)
    changed_execution = sorted(
        suite
        for suite in source_suites - set(combined_smoke_suites)
        if current_digests.get(suite) != attested_digests.get(suite)
    )
    if changed_execution:
        return (
            False,
            "base delta changed source suite execution inputs: "
            + ",".join(changed_execution),
            (),
        )
    if combined_smoke_suites:
        return (
            True,
            "PR and base delta overlap only trusted combined-smoke suites: "
            + ",".join(combined_smoke_suites),
            combined_smoke_suites,
        )
    return True, "PR and base delta suite coverage are disjoint", ()


def validate_candidate(
    *,
    attestation: Mapping[str, Any],
    run: Mapping[str, Any],
    repository: str,
    queue_tree_sha: str,
    queue_base_sha: str,
    queue_policy_digest: str,
    match_kind: str = "exact",
    current_pull_request: Mapping[str, Any] | None = None,
    reconstructed_queue_tree: str | None = None,
    repo: Path | None = None,
) -> None:
    """Validate one evidence artifact and its authoritative workflow run."""

    expected = {
        "schema_version": SCHEMA_VERSION,
        "validation_profile": VALIDATION_PROFILE,
        "repository": repository,
        "base_ref": "main",
        "trust_policy_digest": queue_policy_digest,
        "workflow_path": WORKFLOW_PATH,
    }
    for key, expected_value in expected.items():
        if attestation.get(key) != expected_value:
            raise AttestationError(f"attestation {key} does not match the queue")
    workflow_ref = attestation.get("workflow_ref")
    expected_workflow_prefix = f"{repository}/{WORKFLOW_PATH}@"
    if not isinstance(workflow_ref, str) or not workflow_ref.startswith(
        expected_workflow_prefix
    ):
        raise AttestationError("attestation workflow ref is not authoritative")

    if _evidence_age_seconds(attestation) < -300:
        raise AttestationError("root evidence timestamp is in the future")
    if _evidence_age_seconds(attestation) > EVIDENCE_TTL_SECONDS:
        raise AttestationError("root evidence is too old")
    _validate_lineage(attestation)
    lineage = attestation.get("lineage")
    if not isinstance(lineage, list):
        raise AttestationError("evidence lineage is invalid")
    if len(lineage) >= MAX_LINEAGE_DEPTH:
        raise AttestationError("evidence lineage cannot be extended")
    suites = _validated_suites(attestation)
    execution_digests = _validated_execution_digests(attestation, suites)
    _validated_platform_matrix(attestation, suites)
    if repo is not None:
        _validate_canonical_source_plan(
            repo=repo, attestation=attestation, suites=suites
        )
        if match_kind == "exact":
            current_digests = _current_suite_execution_digests(repo, suites)
            if current_digests != execution_digests:
                raise AttestationError(
                    "evidence suite execution digests do not match the queue"
                )

    tested_tree_sha = _require_sha(attestation.get("tested_tree_sha"), "tested tree SHA")
    tested_base_sha = _require_sha(attestation.get("base_sha"), "tested base SHA")
    if match_kind == "exact":
        if tested_tree_sha != queue_tree_sha:
            raise AttestationError("attestation tested tree does not match the queue")
    elif match_kind == "composed":
        if reconstructed_queue_tree != queue_tree_sha:
            raise AttestationError("pull request head and queue base do not reconstruct queue tree")
        if tested_base_sha == queue_base_sha:
            raise AttestationError("composed evidence requires an advanced queue base")
    else:
        raise AttestationError("evidence match kind is invalid")

    run_id = attestation.get("workflow_run_id")
    run_attempt = attestation.get("workflow_run_attempt")
    if not isinstance(run_id, int) or run_id <= 0:
        raise AttestationError("attestation workflow_run_id is invalid")
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        raise AttestationError("attestation workflow_run_attempt is invalid")
    tested_merge_sha = _require_sha(
        attestation.get("tested_merge_sha"), "attested merge SHA"
    )

    source_event = attestation.get("source_event")
    if source_event not in {"pull_request", "merge_group"}:
        raise AttestationError("attestation source event is invalid")

    run_repository = run.get("repository")
    run_repository_name = (
        run_repository.get("full_name") if isinstance(run_repository, dict) else None
    )
    authoritative = {
        "id": run_id,
        "run_attempt": run_attempt,
        "event": source_event,
        "status": "completed",
        "conclusion": "success",
        "path": WORKFLOW_PATH,
    }
    for key, expected_value in authoritative.items():
        if run.get(key) != expected_value:
            raise AttestationError(f"workflow run {key} is not authoritative")
    if run.get("head_sha") != tested_merge_sha and source_event == "merge_group":
        raise AttestationError("workflow run head_sha is not authoritative")
    if run_repository_name != repository:
        raise AttestationError("workflow run belongs to another repository")

    if source_event == "pull_request":
        pr_number = attestation.get("pull_request_number")
        if not isinstance(pr_number, int) or pr_number <= 0:
            raise AttestationError("attestation pull_request_number is invalid")
        head_sha = _require_sha(attestation.get("head_sha"), "attested head SHA")
        if run.get("head_sha") not in {head_sha, tested_merge_sha}:
            raise AttestationError("workflow run head_sha is not authoritative")
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
            raise AttestationError(
                "workflow run is not associated with the attested pull request"
            )
        if current_pull_request is None:
            raise AttestationError("current pull request identity is missing")
        current_head, current_repository, current_base_ref = _pull_request_identity(
            current_pull_request
        )
        if current_pull_request.get("number") != pr_number:
            raise AttestationError("current pull request number changed")
        if current_head != head_sha:
            raise AttestationError("current pull request head changed")
        if current_repository != attestation.get("head_repository"):
            raise AttestationError("current pull request repository changed")
        if current_base_ref != "main":
            raise AttestationError("current pull request target changed")


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
    details.update(
        candidate_count=0,
        artifact_name="",
        combined_smoke_suites=_canonical_suite_json(),
    )
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

        nightly_details: dict[str, object] = {}
        nightly_healthy, nightly_reason = verify_nightly_health(
            repo=repo,
            repository=repository,
            token=token,
            api_url=api_url,
            details=nightly_details,
        )
        details.update(
            nightly_healthy=str(nightly_healthy).lower(),
            nightly_reason=nightly_reason,
            nightly_run_id=nightly_details.get("source_run_id", ""),
            nightly_age_seconds=nightly_details.get("age_seconds", ""),
        )
        if not nightly_healthy:
            raise AttestationError(f"nightly health check failed: {nightly_reason}")

        pull_request_number = _queue_pr_number(merge_group)
        current_pull_request = _request_json(
            f"{api_url}/repos/{repository}/pulls/{pull_request_number}", token
        )
        if current_pull_request.get("number") != pull_request_number:
            raise AttestationError("current pull request number does not match queue ref")
        current_head_sha, _head_repository, current_base_ref = _pull_request_identity(
            current_pull_request
        )
        if current_base_ref != "main":
            raise AttestationError("queued pull request no longer targets main")
        _ensure_pr_head(
            repo,
            pull_request_number=pull_request_number,
            head_sha=current_head_sha,
        )
        reconstructed_tree = _reconstructed_queue_tree(
            repo, queue_base_sha=queue_base_sha, head_sha=current_head_sha
        )
        if reconstructed_tree != queue_tree_sha:
            raise AttestationError(
                "current pull request head and queue base do not reconstruct queue tree"
            )

        artifact_names = [
            f"ci-evidence-v2-pr-{pull_request_number}-{current_head_sha}",
            f"ci-evidence-v2-tree-{queue_tree_sha}",
        ]
        artifacts: list[Mapping[str, Any]] = []
        listing_reasons: list[str] = []
        seen_artifact_ids: set[int] = set()
        for visibility_delays in ((0,), ARTIFACT_VISIBILITY_DELAYS):
            for name in artifact_names:
                try:
                    candidates = _list_attestation_artifacts(
                        api_url=api_url,
                        repository=repository,
                        encoded_name=urllib.parse.quote(name, safe=""),
                        token=token,
                        visibility_delays=visibility_delays,
                    )
                except AttestationError as exc:
                    listing_reasons.append(str(exc))
                    continue
                for candidate in candidates:
                    artifact_id = candidate.get("id")
                    if isinstance(artifact_id, int):
                        if artifact_id in seen_artifact_ids:
                            continue
                        seen_artifact_ids.add(artifact_id)
                    artifacts.append(candidate)
            if artifacts:
                break
        if not artifacts:
            raise AttestationError("; ".join(listing_reasons) or "no CI evidence exists")
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
                    f"{api_url}/"
                ):
                    raise AttestationError("artifact download URL is invalid")
                attestation = _artifact_attestation(_request_bytes(archive_url, token))
                if attestation.get("workflow_run_id") != run_id:
                    raise AttestationError("artifact and attestation run IDs differ")
                run = _request_json(
                    f"{api_url}/repos/{repository}/actions/runs/{run_id}", token
                )
                tested_tree = _require_sha(
                    attestation.get("tested_tree_sha"), "attested tree SHA"
                )
                match_kind = "exact" if tested_tree == queue_tree_sha else "composed"
                validate_candidate(
                    attestation=attestation,
                    run=run,
                    repository=repository,
                    queue_tree_sha=queue_tree_sha,
                    queue_base_sha=queue_base_sha,
                    queue_policy_digest=queue_policy,
                    match_kind=match_kind,
                    current_pull_request=current_pull_request,
                    reconstructed_queue_tree=reconstructed_tree,
                    repo=repo,
                )
                if attestation.get("source_event") == "pull_request":
                    attested_number = attestation.get("pull_request_number")
                    attested_head = _require_sha(
                        attestation.get("head_sha"), "attested head SHA"
                    )
                    if attested_number != pull_request_number:
                        raise AttestationError("evidence belongs to another pull request")
                    latest_run_id = _latest_pr_run_id(
                        api_url=api_url,
                        repository=repository,
                        token=token,
                        pull_request_number=pull_request_number,
                        head_sha=attested_head,
                        head_ref=str(attestation.get("head_ref", "")),
                    )
                    if latest_run_id != run_id:
                        raise AttestationError(
                            "evidence workflow run is not the latest authoritative PR run"
                        )
                combined_smoke_suites: tuple[str, ...] = ()
                if match_kind == "composed":
                    if attestation.get("source_event") != "pull_request":
                        raise AttestationError(
                            "only pull request evidence may be composed across a base advance"
                        )
                    if not _wait_for_base_successful_ci(
                        api_url=api_url,
                        repository=repository,
                        token=token,
                        queue_base_sha=queue_base_sha,
                    ):
                        raise AttestationError("queue base evidence is not green")
                    safe, composition_reason, combined_smoke_suites = _composition_is_safe(
                        repo=repo,
                        attestation=attestation,
                        queue_base_sha=queue_base_sha,
                    )
                    if not safe:
                        raise AttestationError(composition_reason)
                    details["reason_code"] = "reusable_base_advance"
                    reason = f"composed trusted PR/base evidence: {composition_reason}"
                else:
                    details["reason_code"] = "reusable_exact"
                    reason = "matching trusted exact-tree CI evidence"
                details.update(
                    artifact_name=(
                        f"ci-evidence-v2-tree-{queue_tree_sha}"
                        if match_kind == "exact"
                        else f"ci-evidence-v2-pr-{pull_request_number}-{current_head_sha}"
                    ),
                    source_root_issued_at=attestation.get("root_issued_at", ""),
                    source_lineage=json.dumps(
                        attestation.get("lineage", []), separators=(",", ":")
                    ),
                    source_successful_suites=json.dumps(
                        attestation.get("successful_suites", []), separators=(",", ":")
                    ),
                    source_planner_digest=attestation.get("planner_digest", ""),
                    source_suite_execution_digests=json.dumps(
                        attestation.get("suite_execution_digests", {}),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    combined_smoke_suites=_canonical_suite_json(
                        combined_smoke_suites
                    ),
                )
                return True, reason, run_id
            except (AttestationError, OSError, ValueError, zipfile.BadZipFile) as exc:
                reasons.append(str(exc))
        detail = "; ".join(reasons[:3]) or "no usable attestation artifacts"
        raise AttestationError(detail)
    except (AttestationError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        reason = str(exc)
        details["reason_code"] = _reason_code(reason, api_error=isinstance(exc, OSError))
        return False, reason, None


def create_nightly_health(
    *,
    repo: Path,
    repository: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    workflow_ref: str,
) -> dict[str, object]:
    plan = _plan_paths(repo, [".ci/run-all"])
    suites = plan.get("required_suites")
    execution_digests = plan.get("suite_execution_digests")
    platform_matrix = plan.get("platform_matrix")
    planner_digest = plan.get("plan_digest")
    if (
        not isinstance(suites, list)
        or not suites
        or not isinstance(execution_digests, dict)
        or not isinstance(platform_matrix, list)
        or not isinstance(planner_digest, str)
    ):
        raise AttestationError("nightly suite coverage is missing")
    return {
        "schema_version": 1,
        "profile": "nightly-health-v1",
        "repository": repository,
        "workflow_path": WORKFLOW_PATH,
        "workflow_ref": workflow_ref,
        "workflow_run_id": workflow_run_id,
        "workflow_run_attempt": workflow_run_attempt,
        "head_sha": _require_sha(_git("rev-parse", "HEAD", cwd=repo), "nightly head SHA"),
        "tree_sha": _require_sha(
            _git("rev-parse", "HEAD^{tree}", cwd=repo), "nightly tree SHA"
        ),
        "trust_policy_digest": policy_digest(repo),
        "successful_suites": sorted(set(suites)),
        "planner_digest": planner_digest,
        "suite_execution_digests": execution_digests,
        "platform_matrix": platform_matrix,
        "completed_at": _now_iso(),
    }


def verify_nightly_health(
    *,
    repo: Path,
    repository: str,
    token: str,
    api_url: str,
    details: dict[str, object] | None = None,
) -> tuple[bool, str]:
    details = details if details is not None else {}
    details.update(reason_code="nightly_unhealthy", source_run_id="", age_seconds="")
    try:
        listing = _request_json(
            f"{api_url}/repos/{repository}/actions/workflows/ci.yml/runs"
            "?event=schedule&per_page=1",
            token,
        )
        runs = listing.get("workflow_runs")
        if not isinstance(runs, list) or not runs or not isinstance(runs[0], dict):
            raise AttestationError("latest nightly workflow run is missing")
        latest = runs[0]
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            raise AttestationError("latest nightly workflow run is not green")
        if latest.get("event") != "schedule" or latest.get("path") != WORKFLOW_PATH:
            raise AttestationError("latest nightly workflow run is not authoritative")
        run_id = latest.get("id")
        run_attempt = latest.get("run_attempt")
        if not isinstance(run_id, int) or run_id <= 0:
            raise AttestationError("latest nightly workflow run identity is invalid")
        if not isinstance(run_attempt, int) or run_attempt <= 0:
            raise AttestationError("latest nightly workflow run attempt is invalid")
        completed_at = _parse_time(latest.get("updated_at"), "latest nightly completion time")
        age = (datetime.now(UTC) - completed_at).total_seconds()
        details.update(source_run_id=run_id, age_seconds=max(0, int(age)))
        if age < -300 or age > NIGHTLY_MAX_AGE_SECONDS:
            raise AttestationError("latest nightly workflow run is stale")

        artifacts = _list_attestation_artifacts(
            api_url=api_url,
            repository=repository,
            encoded_name=urllib.parse.quote("ci-nightly-health-v1", safe=""),
            token=token,
        )
        artifact = next(
            (
                item
                for item in artifacts
                if isinstance(item.get("workflow_run"), dict)
                and item["workflow_run"].get("id") == run_id
                and item.get("expired") is False
            ),
            None,
        )
        if artifact is None:
            raise AttestationError("latest nightly health artifact is missing")
        artifact_size = artifact.get("size_in_bytes")
        if (
            not isinstance(artifact_size, int)
            or artifact_size <= 0
            or artifact_size > MAX_ATTESTATION_ARCHIVE_BYTES
        ):
            raise AttestationError("nightly health artifact size is invalid")
        archive_url = artifact.get("archive_download_url")
        if not isinstance(archive_url, str) or not archive_url.startswith(f"{api_url}/"):
            raise AttestationError("nightly health artifact download URL is invalid")
        health = _artifact_json(
            _request_bytes(archive_url, token), expected_name="ci-nightly-health.json"
        )
        expected = {
            "schema_version": 1,
            "profile": "nightly-health-v1",
            "repository": repository,
            "workflow_path": WORKFLOW_PATH,
            "workflow_run_id": run_id,
            "workflow_run_attempt": run_attempt,
            "head_sha": latest.get("head_sha"),
            "trust_policy_digest": policy_digest(repo),
        }
        for key, expected_value in expected.items():
            if health.get(key) != expected_value:
                raise AttestationError(f"nightly health {key} does not match")
        workflow_ref = health.get("workflow_ref")
        if not isinstance(workflow_ref, str) or not workflow_ref.startswith(
            f"{repository}/{WORKFLOW_PATH}@"
        ):
            raise AttestationError("nightly health workflow ref is not authoritative")
        nightly_tree_sha = _require_sha(
            health.get("tree_sha"), "nightly health tree SHA"
        )
        suites = health.get("successful_suites")
        if (
            not isinstance(suites, list)
            or not suites
            or any(not isinstance(item, str) or not item for item in suites)
            or suites != sorted(set(suites))
        ):
            raise AttestationError("nightly health suite coverage is missing")
        # Validate product/suite execution digests against the tree the nightly
        # actually tested. Comparing them with the current PR/queue tree would
        # make every ordinary product change invalidate otherwise healthy
        # nightly evidence; trust_policy_digest separately pins the verifier
        # and planner contract to the current tree.
        expected_plan = _plan_paths(
            repo,
            [".ci/run-all"],
            ref=nightly_tree_sha,
        )
        expected_suites = expected_plan.get("required_suites")
        if suites != expected_suites:
            raise AttestationError("nightly health suite coverage is incomplete")
        if health.get("planner_digest") != expected_plan.get("plan_digest"):
            raise AttestationError("nightly health planner digest does not match")
        if health.get("suite_execution_digests") != expected_plan.get(
            "suite_execution_digests"
        ):
            raise AttestationError("nightly health execution digests do not match")
        if health.get("platform_matrix") != expected_plan.get("platform_matrix"):
            raise AttestationError("nightly health platform matrix does not match")
        artifact_completed_at = _parse_time(
            health.get("completed_at"), "nightly health completion time"
        )
        artifact_age = (datetime.now(UTC) - artifact_completed_at).total_seconds()
        if artifact_age < -300 or artifact_age > NIGHTLY_MAX_AGE_SECONDS:
            raise AttestationError("nightly health artifact is stale")
        details["reason_code"] = "healthy"
        return True, "latest full nightly is green and fresh"
    except (AttestationError, OSError, ValueError, zipfile.BadZipFile) as exc:
        reason = str(exc)
        details["reason_code"] = _reason_code(reason, api_error=isinstance(exc, OSError))
        return False, reason


def _create_command(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    event = _read_event(Path(args.event_path))
    source_attestation: dict[str, Any] | None = None
    if args.source_run_id:
        source_attestation = {
            "workflow_run_id": args.source_run_id,
            "root_issued_at": args.source_root_issued_at,
            "lineage": json.loads(args.source_lineage or "[]"),
        }

    if args.successful_suites:
        successful_suites = json.loads(args.successful_suites)
        suite_execution_digests = json.loads(args.suite_execution_digests or "{}")
        platform_matrix = json.loads(args.platform_matrix or "[]")
        planner_digest = args.planner_digest
        plan_basis = args.plan_basis
    else:
        pull_request = event.get("pull_request")
        merge_group = event.get("merge_group")
        if isinstance(pull_request, dict):
            head = pull_request.get("head")
            head_sha = _require_sha(
                head.get("sha") if isinstance(head, dict) else None,
                "pull request head SHA",
            )
            parents = _git("rev-list", "--parents", "-n", "1", "HEAD", cwd=repo).split()
            if len(parents) != 3:
                raise AttestationError("pull request CI must test a two-parent merge preview")
            changed_paths = _changed_paths(repo, parents[1], head_sha)
            plan_basis = "change_set"
        elif isinstance(merge_group, dict):
            base_sha = _require_sha(merge_group.get("base_sha"), "merge-group base SHA")
            if source_attestation is None:
                changed_paths = [".ci/run-all"]
                plan_basis = "full_fallback"
            else:
                changed_paths = _changed_paths(repo, base_sha, "HEAD")
                plan_basis = "change_set"
        else:
            raise AttestationError("cannot plan suites outside PR or merge-group CI")
        plan = _plan_paths(repo, changed_paths)
        suites_value = plan.get("required_suites")
        digests_value = plan.get("suite_execution_digests")
        platform_value = plan.get("platform_matrix")
        digest_value = plan.get("plan_digest")
        if (
            not isinstance(suites_value, list)
            or not isinstance(digests_value, dict)
            or not isinstance(platform_value, list)
        ):
            raise AttestationError("CI suite planner returned incomplete evidence metadata")
        if not isinstance(digest_value, str) or not digest_value:
            raise AttestationError("CI suite planner digest is missing")
        successful_suites = suites_value
        suite_execution_digests = digests_value
        platform_matrix = platform_value
        planner_digest = digest_value

    value = create_attestation(
        repo=repo,
        repository=args.repository,
        event=event,
        workflow_run_id=args.run_id,
        workflow_run_attempt=args.run_attempt,
        workflow_ref=args.workflow_ref,
        optimization_mode=args.optimization_mode,
        successful_suites=successful_suites,
        planner_digest=planner_digest,
        suite_execution_digests=suite_execution_digests,
        platform_matrix=platform_matrix,
        plan_basis=plan_basis,
        source_attestation=source_attestation,
    )
    output = Path(args.output)
    output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    _write_outputs(
        Path(args.github_output) if args.github_output else None,
        {
            "tree_sha": value["tested_tree_sha"],
            "trust_policy_digest": value["trust_policy_digest"],
            "pull_request_number": value.get("pull_request_number") or "",
            "head_sha": value.get("head_sha") or "",
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
            "source_root_issued_at": details.get("source_root_issued_at", ""),
            "source_lineage": details.get("source_lineage", "[]"),
            "source_successful_suites": details.get("source_successful_suites", "[]"),
            "source_planner_digest": details.get("source_planner_digest", ""),
            "source_suite_execution_digests": details.get(
                "source_suite_execution_digests", "{}"
            ),
            "combined_smoke_suites": details.get("combined_smoke_suites", "[]"),
            "nightly_healthy": details.get("nightly_healthy", "false"),
            "nightly_reason": details.get("nightly_reason", ""),
            "nightly_run_id": details.get("nightly_run_id", ""),
            "nightly_age_seconds": details.get("nightly_age_seconds", ""),
        },
    )
    print(f"reusable={str(reusable).lower()} reason={reason}")
    return 0


def _create_nightly_command(args: argparse.Namespace) -> int:
    value = create_nightly_health(
        repo=Path(args.repo).resolve(),
        repository=args.repository,
        workflow_run_id=args.run_id,
        workflow_run_attempt=args.run_attempt,
        workflow_ref=args.workflow_ref,
    )
    Path(args.output).write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Created nightly health for run {args.run_id}")
    return 0


def _verify_nightly_command(args: argparse.Namespace) -> int:
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("GH_TOKEN is required", file=sys.stderr)
        return 2
    details: dict[str, object] = {}
    healthy, reason = verify_nightly_health(
        repo=Path(args.repo).resolve(),
        repository=args.repository,
        token=token,
        api_url=args.api_url.rstrip("/"),
        details=details,
    )
    _write_outputs(
        Path(args.github_output) if args.github_output else None,
        {
            "healthy": str(healthy).lower(),
            "reason": reason,
            "reason_code": details.get("reason_code", "nightly_unhealthy"),
            "source_run_id": details.get("source_run_id", ""),
            "age_seconds": details.get("age_seconds", ""),
        },
    )
    print(f"healthy={str(healthy).lower()} reason={reason}")
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
    create.add_argument("--successful-suites")
    create.add_argument("--planner-digest", default="")
    create.add_argument("--suite-execution-digests")
    create.add_argument("--platform-matrix")
    create.add_argument(
        "--plan-basis", choices=("change_set", "full_fallback"), default="change_set"
    )
    create.add_argument("--source-run-id", type=int)
    create.add_argument("--source-root-issued-at", default="")
    create.add_argument("--source-lineage", default="[]")
    create.set_defaults(func=_create_command)

    verify = subparsers.add_parser("verify-queue")
    verify.add_argument("--repo", default=".")
    verify.add_argument("--repository", required=True)
    verify.add_argument("--event-path", required=True)
    verify.add_argument("--run-id", type=int, required=True)
    verify.add_argument("--api-url", default="https://api.github.com")
    verify.add_argument("--github-output")
    verify.set_defaults(func=_verify_queue_command)

    create_nightly = subparsers.add_parser("create-nightly-health")
    create_nightly.add_argument("--repo", default=".")
    create_nightly.add_argument("--repository", required=True)
    create_nightly.add_argument("--run-id", type=int, required=True)
    create_nightly.add_argument("--run-attempt", type=int, required=True)
    create_nightly.add_argument("--workflow-ref", required=True)
    create_nightly.add_argument("--output", required=True)
    create_nightly.set_defaults(func=_create_nightly_command)

    verify_nightly = subparsers.add_parser("verify-nightly-health")
    verify_nightly.add_argument("--repo", default=".")
    verify_nightly.add_argument("--repository", required=True)
    verify_nightly.add_argument("--api-url", default="https://api.github.com")
    verify_nightly.add_argument("--github-output")
    verify_nightly.set_defaults(func=_verify_nightly_command)
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
