"""Reject unsupported release sources before building or accessing signing secrets."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

TAG_PATTERN = re.compile(
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:rc(?:0|[1-9][0-9]*))?\Z"
)
SIGNING_FILES = (
    "desktop/electron/scripts/build-signed-windows.cjs",
    ".github/scripts/verify-windows-signatures.ps1",
    ".github/signing/windows-signing-policy.json",
)


def source_ref(event: str, ref: str, tag: str, sha: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("The workflow source must be a full commit SHA.")
    if tag and not TAG_PATTERN.fullmatch(tag):
        raise ValueError("Release tag must be a canonical vMAJOR.MINOR.PATCH release tag.")
    if event == "push":
        if not tag or ref != f"refs/tags/{tag}":
            raise ValueError("Release push must reference its v* tag.")
        return sha
    if event != "workflow_dispatch" or not ref.startswith("refs/heads/"):
        raise ValueError("Signing requires a release-tag push or a branch dispatch.")
    if tag:
        if ref != "refs/heads/main":
            raise ValueError("Existing-tag release uploads must be dispatched from main.")
        return f"refs/tags/{tag}"
    return sha


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8").strip()


def resolve_source(ref: str) -> str:
    git("fetch", "--no-tags", "--depth=1", "origin", ref)
    sha = git("rev-parse", "--verify", "FETCH_HEAD^{commit}")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Release source did not resolve to a commit SHA.")
    return sha


def validate_signing_contract(sha: str) -> None:
    for name in SIGNING_FILES:
        try:
            content = git("show", f"{sha}:{name}")
        except subprocess.CalledProcessError as exc:
            raise ValueError(
                f"Release source lacks {name}. Use a new version/tag containing Windows "
                "signing support; historical unsigned tags are not rebuilt by this workflow."
            ) from exc
        if not content:
            raise ValueError(f"Release signing contract is empty: {name}")
        if name.endswith(".json"):
            policy = json.loads(content)
            if (
                not isinstance(policy, dict)
                or type(policy.get("schemaVersion")) is not int
                or policy["schemaVersion"] != 1
                or not isinstance(policy.get("certificateSha1"), str)
                or not re.fullmatch(r"[0-9A-F]{40}", policy["certificateSha1"])
                or not isinstance(policy.get("publisherSubjectContains"), str)
                or not policy["publisherSubjectContains"].strip()
                or not isinstance(policy.get("timestampUrl"), str)
                or not policy["timestampUrl"].strip()
            ):
                raise ValueError("Release source has an unsupported Windows signing policy.")


def validate_release(repository: str, tag: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository.")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{api}/repos/{repository}/releases/tags/{quote(tag, safe='')}", headers=headers
    )
    try:
        with urlopen(request, timeout=30) as response:
            release = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return
        raise ValueError(
            f"Cannot inspect release state (HTTP {exc.code}); refusing to build."
        ) from exc
    if release.get("draft") is not True:
        raise ValueError("Refusing to rebuild an existing non-Draft GitHub Release.")
    expected_preview = bool(re.search(r"rc[0-9]+$", tag))
    if release.get("prerelease") is not expected_preview:
        raise ValueError("Existing Draft has an unexpected prerelease state.")


def main() -> None:
    tag = os.environ.get("RELEASE_TAG", "")
    ref = source_ref(
        os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF"], tag, os.environ["GITHUB_SHA"]
    )
    sha = resolve_source(ref)
    validate_signing_contract(sha)
    if tag:
        validate_release(os.environ["GITHUB_REPOSITORY"], tag)
    workflow_sha = os.environ["GITHUB_WORKFLOW_SHA"]
    print(f"Validated release source: {sha}; workflow: {workflow_sha}; tag: {tag or '(internal)'}")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"source_sha={sha}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(f"Release source: `{sha}`\n\nWorkflow source: `{workflow_sha}`\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Release preflight failed: {error}", file=sys.stderr)
        sys.exit(1)
