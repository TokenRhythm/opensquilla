from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "a" * 40
WORKFLOW = "b" * 40


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location(
        "wait_ci_candidate_contract", ROOT / ".github/scripts/wait_ci_candidate.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("status", [502, 503, 504])
def test_api_retries_only_transient_http_errors_with_pagination(helper, monkeypatch, status):
    calls, sleeps = [], []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(
                1, command, stderr=f"gh: Server Error (HTTP {status})",
            )
        return subprocess.CompletedProcess(command, 0, stdout='[{"jobs": []}]')

    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(helper.time, "sleep", sleeps.append)
    assert helper.api("repos/owner/repo/actions/jobs?per_page=20", pages=True) == [{"jobs": []}]
    assert calls == [
        ["gh", "api", "repos/owner/repo/actions/jobs?per_page=20", "--paginate", "--slurp"],
    ] * 3
    assert sleeps == [1, 2]


@pytest.mark.parametrize("stderr,attempts", [
    ("gh: Server Error (HTTP 502)", 3),
    ("gh: Bad credentials (HTTP 401)", 1),
    ("gh: Forbidden (HTTP 403)", 1),
    ("gh: Not Found (HTTP 404)", 1),
    ("unknown failure", 1),
])
def test_api_preserves_failure_and_bounds_retries(helper, monkeypatch, stderr, attempts):
    calls, sleeps = [], []
    failure = subprocess.CalledProcessError(1, ["gh"], stderr=stderr)

    def run(*args, **kwargs):
        calls.append(args)
        raise failure

    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(helper.time, "sleep", sleeps.append)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        helper.api("repos/owner/repo/actions/runs/123")
    assert caught.value is failure
    assert len(calls) == attempts
    assert sleeps == ([1, 2] if attempts == 3 else [])


def manifest_fixture(destination):
    destination.mkdir()
    installer = destination / "candidate.exe"
    installer.write_bytes(b"candidate bytes")
    manifest = {
        "sourceSha": SOURCE, "workflowSha": WORKFLOW, "auditRunId": "123", "runAttempt": "2",
        "signing": "unsigned regression only", "installerName": installer.name,
        "installerSha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
        "version": json.loads((ROOT / "desktop/electron/package.json").read_text())["version"],
        **dict.fromkeys(("asarSha256", "executableSha256", "gatewaySha256",
                         "dependencyInventorySha256"), "c" * 64),
    }
    (destination / "audit-candidate.json").write_text(json.dumps(manifest), encoding="utf-8")
    return installer, manifest


def test_candidate_identity_accepts_this_attempt(helper, tmp_path):
    installer, manifest = manifest_fixture(tmp_path / "candidate")
    assert helper.verify_candidate(installer.parent, SOURCE, WORKFLOW, "123", "2") == (
        installer, manifest,
    )


@pytest.mark.parametrize("field,value", [
    ("auditRunId", "124"), ("runAttempt", "1"), ("sourceSha", "d" * 40),
    ("workflowSha", "d" * 40), ("installerSha256", "d" * 64),
    ("installerName", "other.exe"), ("version", "0.0.0"),
    ("signing", "signed"), ("asarSha256", ""), ("gatewaySha256", None),
])
def test_candidate_identity_rejects_wrong_run_bytes_or_contract(helper, tmp_path, field, value):
    installer, manifest = manifest_fixture(tmp_path / "candidate")
    manifest[field] = value
    (installer.parent / "audit-candidate.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        helper.verify_candidate(installer.parent, SOURCE, WORKFLOW, "123", "2")


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "timed_out", "neutral"])
def test_failed_producer_cannot_supply_a_candidate(helper, conclusion):
    with pytest.raises(ValueError, match="producer failed"):
        helper.producer_ready([{"jobs": [{"name": "Windows NSIS acceptance / build",
                                          "status": "completed", "conclusion": conclusion}]}])


def test_producer_and_artifact_must_be_unambiguous(helper):
    assert not helper.producer_ready([{"jobs": []}])
    job = {"name": "build", "status": "in_progress"}
    assert not helper.producer_ready([{"jobs": []}, {"jobs": [job]}])
    with pytest.raises(ValueError, match="Ambiguous"):
        helper.producer_ready([{"jobs": [job, job]}])
    artifact = {"id": 1, "name": "candidate", "expired": False}
    assert helper.select_artifact(
        [{"artifacts": []}, {"artifacts": [artifact]}], "candidate",
    ) == artifact
    for matches in ([], [artifact, artifact], [{**artifact, "expired": True}]):
        with pytest.raises(ValueError, match="exactly one"):
            helper.select_artifact([{"artifacts": matches}], "candidate")


@pytest.mark.parametrize("filename", [
    "../outside.exe", "/outside.exe", "C:/outside.exe", r"..\outside.exe",
])
def test_archive_cannot_escape_destination(helper, tmp_path, filename):
    archive = tmp_path / "input.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(filename, b"bytes")
    digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="Unsafe"):
        helper.unpack_candidate(archive, tmp_path / "output", digest)


def test_archive_digest_checked_before_extracting(helper, tmp_path):
    archive = tmp_path / "input.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("candidate.exe", b"bytes")
    with pytest.raises(ValueError, match="digest mismatch"):
        helper.unpack_candidate(archive, tmp_path / "output", "sha256:" + "0" * 64)
    assert not (tmp_path / "output").exists()
    digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    helper.unpack_candidate(archive, tmp_path / "output", digest)
    assert (tmp_path / "output/candidate.exe").read_bytes() == b"bytes"


def configure_wait(helper, monkeypatch):
    for name, value in {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                        "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": SOURCE,
                        "GITHUB_WORKFLOW_SHA": WORKFLOW}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(helper.subprocess, "check_output", lambda *a, **k: SOURCE)
    monkeypatch.setattr(helper.time, "sleep", lambda seconds: None)


def test_wait_uses_current_attempt_and_pins_producer_before_download(helper, monkeypatch, tmp_path):
    configure_wait(helper, monkeypatch)
    endpoints = []
    # PR API head_sha can differ from the merge SHA actually checked out.
    run = {"id": 123, "run_attempt": 2, "head_sha": "e" * 40}
    job = {"id": 456, "name": "Windows NSIS acceptance / build", "status": "in_progress",
           "run_id": 123, "run_attempt": 2}
    responses = [run, [{"jobs": []}, {"jobs": [job]}],
                 {**job, "status": "completed", "conclusion": "success"}, run,
                 [{"artifacts": [{"id": 789, "name": "candidate",
                                  "expired": False, "digest": "hash"}]}]]

    def fake_api(endpoint, **kwargs):
        endpoints.append(endpoint)
        return responses.pop(0)

    monkeypatch.setattr(helper, "api", fake_api)
    downloads = []
    monkeypatch.setattr(helper.subprocess, "run", lambda command, **kw: downloads.append(command))
    monkeypatch.setattr(helper, "unpack_candidate", lambda *a: None)
    monkeypatch.setattr(helper, "verify_candidate",
                        lambda *a: (a[0] / "candidate.exe", {"verified": True}))
    result = helper.wait_for_candidate("candidate", tmp_path / "download", SOURCE)
    assert result[1] == {"verified": True}
    assert endpoints[1].endswith("/attempts/2/jobs?per_page=20")
    assert endpoints[2].endswith("/jobs/456")
    assert downloads == [["gh", "api", "repos/owner/repo/actions/artifacts/789/zip"]]
    assert not responses


def test_failed_build_stops_wait_without_downloading(helper, monkeypatch, tmp_path):
    configure_wait(helper, monkeypatch)
    responses = [{"id": 123, "run_attempt": 2}, [{"jobs": [
        {"id": 456, "name": "build", "status": "completed", "conclusion": "failure",
         "run_id": 123, "run_attempt": 2},
    ]}]]
    monkeypatch.setattr(helper, "api", lambda *a, **k: responses.pop(0))
    with pytest.raises(ValueError, match="producer failed"):
        helper.wait_for_candidate("candidate", tmp_path / "download", SOURCE)
    assert not (tmp_path / "download").exists()


def test_failed_only_rerun_binds_manifest_to_verified_prior_producer(helper, monkeypatch, tmp_path):
    configure_wait(helper, monkeypatch)
    run = {"id": 123, "run_attempt": 2}
    responses = [run, [{"jobs": []}], [{"jobs": []}], [{"jobs": [
        {"id": 456, "name": "build", "status": "completed", "conclusion": "success",
         "run_id": 123, "run_attempt": 1},
    ]}], run, [{"artifacts": [{"id": 789, "name": "candidate",
                               "expired": False, "digest": "hash"}]}]]
    monkeypatch.setattr(helper, "api", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(helper, "unpack_candidate", lambda *a: None)
    identities = []

    def verify(*args):
        identities.append(args[1:])
        return args[0] / "candidate.exe", {}

    monkeypatch.setattr(helper, "verify_candidate", verify)
    helper.wait_for_candidate("candidate", tmp_path / "download", SOURCE)
    assert identities == [(SOURCE, WORKFLOW, "123", "1")]
    assert not responses


def test_wait_has_bounded_deadline(helper, monkeypatch, tmp_path):
    configure_wait(helper, monkeypatch)
    ticks = iter([0, 3601])
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(helper, "api", lambda *a, **k: {"id": 123, "run_attempt": 2})
    with pytest.raises(TimeoutError, match="60 minutes"):
        helper.wait_for_candidate("candidate", tmp_path / "download", SOURCE)
