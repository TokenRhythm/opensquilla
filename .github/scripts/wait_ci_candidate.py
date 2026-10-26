"""Wait for this run's NSIS producer while retaining the installed baseline VM."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath

PRODUCER_NAMES = {"build", "Windows NSIS acceptance / build"}
# Preserve the producer's existing 60-minute budget; callers now spend the
# dependency wait on their VM instead of in GitHub's needs scheduler.
WAIT_SECONDS = 3600
POLL_SECONDS = 10
API_PAGE_SIZE = 20


class MetadataApiError(RuntimeError):
    """A safe summary; the original subprocess failure remains the cause."""


def api_failure(
    error: subprocess.CalledProcessError | subprocess.TimeoutExpired,
) -> tuple[str, bool]:
    if isinstance(error, subprocess.TimeoutExpired):
        return "command-timeout", True
    stderr = error.stderr if isinstance(error.stderr, str) else ""
    # HTTP failures take precedence even if their response mentions a timeout.
    status = re.search(r"\bHTTP (\d{3})\b", stderr)
    if status:
        code = int(status[1])
        return f"http-{code}", code in {502, 503, 504}
    if re.search(r"\bTLS handshake timeout\b", stderr, re.IGNORECASE):
        return "tls-handshake-timeout", True
    if re.search(r"\bconnection reset\b|\bECONNRESET\b|\bforcibly closed by the remote host\b",
                 stderr, re.IGNORECASE):
        return "connection-reset", True
    if re.search(r"\bi/o timeout\b|\bcontext deadline exceeded\b|"
                 r"\bClient\.Timeout exceeded while awaiting headers\b|"
                 r"\bnet/http: timeout awaiting response headers\b", stderr, re.IGNORECASE):
        return "network-timeout", True
    return "unknown-cli-failure", False


def remaining_wait(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("NSIS candidate was not ready within 60 minutes")
    return remaining


def api(endpoint: str, *, pages: bool = False, deadline: float | None = None) -> object:
    command = ["gh", "api", endpoint]
    if pages:
        command += ["--paginate", "--slurp"]
    last_failure = None
    for attempt in range(3):
        timeout = 60
        if deadline is not None:
            try:
                timeout = min(timeout, remaining_wait(deadline))
            except TimeoutError as exhausted:
                if last_failure is not None:
                    reason, _ = api_failure(last_failure)
                    raise TimeoutError(
                        f"{exhausted} (metadata failure: {reason}; attempts={attempt})"
                    ) from last_failure
                raise
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True,
                                    encoding="utf-8", timeout=timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            reason, retryable = api_failure(exc)
            # This helper only reads Actions metadata. Never echo its captured
            # response/body or retry authentication, validation or unknown errors.
            if attempt == 2 or not retryable:
                raise MetadataApiError(
                    f"GitHub Actions metadata request failed: {reason}; "
                    f"attempts={attempt + 1}; endpoint={endpoint}"
                ) from exc
            delay = 2 ** attempt
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= delay:
                    raise TimeoutError(
                        "NSIS candidate was not ready within 60 minutes "
                        f"(metadata failure: {reason}; attempts={attempt + 1})"
                    ) from exc
            last_failure = exc
            time.sleep(delay)
        else:
            return json.loads(result.stdout)
    raise AssertionError("unreachable")


def producer_ready(pages: list[dict]) -> bool:
    producers = [job for page in pages for job in page["jobs"]
                 if job["name"] in PRODUCER_NAMES]
    if len(producers) > 1:
        raise ValueError("Ambiguous NSIS candidate producer")
    if not producers or producers[0]["status"] != "completed":
        return False
    if producers[0]["conclusion"] != "success":
        raise ValueError(f"NSIS producer failed: {producers[0]['conclusion']}")
    return True


def select_artifact(pages: list[dict], name: str) -> dict:
    matches = [artifact for page in pages for artifact in page["artifacts"]
               if artifact["name"] == name and not artifact["expired"]]
    if len(matches) != 1:
        raise ValueError("Successful NSIS producer must publish exactly one candidate artifact")
    return matches[0]


def unpack_candidate(archive: Path, destination: Path, expected_digest: str) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest):
        raise ValueError("Candidate artifact must have a GitHub SHA-256 digest")
    with archive.open("rb") as stream:
        actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected_digest:
        raise ValueError("Candidate artifact archive digest mismatch")
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                    or ":" in entry.filename or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("Unsafe candidate artifact path")
        bundle.extractall(destination)


def verify_candidate(destination: Path, source_sha: str, workflow_sha: str,
                     run_id: str, attempt: str) -> tuple[Path, dict]:
    manifest = json.loads((destination / "audit-candidate.json").read_text(encoding="utf-8-sig"))
    if str(manifest.get("auditRunId")) != run_id or str(manifest.get("runAttempt")) != attempt:
        raise ValueError("Candidate does not belong to this workflow run attempt")
    if manifest.get("signing") != "unsigned regression only":
        raise ValueError("Expected the unsigned NSIS regression candidate")
    for field in ("installerSha256", "asarSha256", "executableSha256", "gatewaySha256",
                  "dependencyInventorySha256"):
        if (not isinstance(manifest.get(field), str)
                or not re.fullmatch(r"[0-9a-f]{64}", manifest[field])):
            raise ValueError(f"Candidate manifest has an invalid {field}")
    installers = list(destination.glob("*.exe"))
    if len(installers) != 1:
        raise ValueError("Expected exactly one candidate installer")
    helper_path = Path(__file__).with_name("windows_candidate_identity.py")
    spec = importlib.util.spec_from_file_location("waiting_candidate_identity", helper_path)
    assert spec is not None and spec.loader is not None
    identity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(identity)
    package_path = Path(__file__).resolve().parents[2] / "desktop/electron/package.json"
    version = json.loads(package_path.read_text(encoding="utf-8"))["version"]
    identity.verify(manifest, installers[0], source_sha,
                    expected_workflow_sha=workflow_sha, expected_version=version)
    return installers[0], manifest


def wait_for_candidate(artifact_name: str, destination: Path, source_sha: str) -> tuple[Path, dict]:
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    workflow_sha = os.environ["GITHUB_WORKFLOW_SHA"]
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not re.fullmatch(r"[1-9][0-9]*", run_id)
            or not re.fullmatch(r"[1-9][0-9]*", attempt)
            or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
            or not re.fullmatch(r"[0-9a-f]{40}", workflow_sha)):
        raise ValueError("Invalid GitHub candidate execution identity")
    if os.environ.get("GITHUB_SHA") != source_sha:
        raise ValueError("Candidate source differs from this workflow")
    checkout_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if checkout_sha != source_sha:
        raise ValueError("Candidate source differs from the acceptance checkout")
    run_path = f"repos/{repository}/actions/runs/{run_id}"
    started = time.monotonic()
    deadline = started + WAIT_SECONDS
    print(json.dumps({"stage": "candidate-wait", "event": "start", "runId": run_id,
                      "runAttempt": attempt}), flush=True)
    run = api(run_path, deadline=deadline)
    # PR API head_sha can identify the branch tip while GITHUB_SHA identifies
    # the tested merge commit. Bind the source through checkout + manifest.
    if str(run["run_attempt"]) != attempt or str(run["id"]) != run_id:
        raise ValueError("Candidate producer workflow identity changed")
    producer_id = None
    producer_attempt = attempt
    while time.monotonic() < deadline:
        if producer_id is None:
            jobs = api(f"{run_path}/attempts/{attempt}/jobs?per_page={API_PAGE_SIZE}",
                       pages=True, deadline=deadline)
            producers = [job for page in jobs for job in page["jobs"]
                         if job["name"] in PRODUCER_NAMES]
            # A failed-jobs rerun can retain the successful producer from a
            # prior attempt. Only reuse that exact completed producer, and bind
            # the downloaded manifest to its attempt as well as source/run.
            if not producers and int(attempt) > 1:
                time.sleep(min(POLL_SECONDS, remaining_wait(deadline)))
                jobs = api(
                    f"{run_path}/attempts/{attempt}/jobs?per_page={API_PAGE_SIZE}", pages=True,
                    deadline=deadline,
                )
                producers = [job for page in jobs for job in page["jobs"]
                             if job["name"] in PRODUCER_NAMES]
                if not producers:
                    jobs = api(
                        f"{run_path}/jobs?filter=latest&per_page={API_PAGE_SIZE}", pages=True,
                        deadline=deadline,
                    )
                    producers = [job for page in jobs for job in page["jobs"]
                                 if job["name"] in PRODUCER_NAMES]
                    if not producers:
                        raise ValueError("NSIS producer missing from rerun; rerun all jobs")
            if len(producers) == 1:
                producer = producers[0]
                producer_attempt = str(producer["run_attempt"])
                if (str(producer["run_id"]) != run_id or not producer_attempt.isdecimal()
                        or not 1 <= int(producer_attempt) <= int(attempt)):
                    raise ValueError("NSIS producer belongs to a different run attempt")
                if producer_attempt != attempt and producer["status"] != "completed":
                    raise ValueError("Cannot reuse an unfinished prior NSIS producer")
                producer_id = producers[0]["id"]
        else:
            job = api(f"repos/{repository}/actions/jobs/{producer_id}", deadline=deadline)
            jobs = [{"jobs": [job]}]
        if producer_ready(jobs):
            break
        time.sleep(min(POLL_SECONDS, remaining_wait(deadline)))
    else:
        raise TimeoutError("NSIS candidate was not ready within 60 minutes")
    run = api(run_path, deadline=deadline)
    if str(run["run_attempt"]) != attempt or str(run["id"]) != run_id:
        raise ValueError("Candidate producer workflow identity changed")
    artifact = select_artifact(
        api(f"{run_path}/artifacts?per_page={API_PAGE_SIZE}", pages=True, deadline=deadline),
        artifact_name,
    )
    destination.mkdir(parents=True, exist_ok=False)
    archive = destination / "candidate-artifact.zip"
    with archive.open("wb") as output:
        subprocess.run(["gh", "api", f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"],
                       check=True, stdout=output, stderr=subprocess.PIPE, timeout=180)
    unpack_candidate(archive, destination, artifact.get("digest", ""))
    candidate = verify_candidate(destination, source_sha, workflow_sha, run_id, producer_attempt)
    print(json.dumps({"stage": "candidate-wait", "event": "finished", "artifactId": artifact["id"],
                      "producerAttempt": producer_attempt,
                      "elapsedSeconds": round(time.monotonic() - started, 3)}), flush=True)
    return candidate
