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


@pytest.mark.parametrize("stderr,attempts,reason", [
    ("gh: Server Error (HTTP 502)", 3, "http-502"),
    ("gh: Bad credentials (HTTP 401); TLS handshake timeout", 1, "http-401"),
    ("gh: Forbidden (HTTP 403); connection reset", 1, "http-403"),
    ("gh: Not Found (HTTP 404); i/o timeout", 1, "http-404"),
    ("gh: rate limit (HTTP 429)", 1, "http-429"),
    ("gh: Bad Request (HTTP 400)", 1, "http-400"),
    ("gh: Validation Failed (HTTP 422)", 1, "http-422"),
    ("net/http: TLS handshake timeout", 3, "tls-handshake-timeout"),
    ("read tcp: connection reset by peer", 3, "connection-reset"),
    ("wsarecv: An existing connection was forcibly closed by the remote host.",
     3, "connection-reset"),
    ("dial tcp: i/o timeout", 3, "network-timeout"),
    ("net/http: request canceled (Client.Timeout exceeded while awaiting headers)",
     3, "network-timeout"),
    ("x509: certificate signed by unknown authority", 1, "certificate-error"),
    ("unknown failure", 3, "unknown-cli-failure"),
    ("", 3, "unknown-cli-failure"),
])
def test_api_preserves_failure_and_bounds_retries(helper, monkeypatch, stderr, attempts, reason):
    calls, sleeps = [], []
    failure = subprocess.CalledProcessError(1, ["gh"], stderr=stderr)

    def run(*args, **kwargs):
        calls.append(args)
        raise failure

    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(helper.time, "sleep", sleeps.append)
    with pytest.raises(helper.MetadataApiError, match=reason) as caught:
        helper.api("repos/owner/repo/actions/runs/123")
    assert caught.value.__cause__ is failure
    assert f"attempts={attempts}" in str(caught.value)
    assert len(calls) == attempts
    assert sleeps == ([1, 2] if attempts == 3 else [])


def test_api_retries_explicit_command_timeout_without_exposing_captured_content(
    helper, monkeypatch,
):
    calls, sleeps = [], []
    failure = subprocess.TimeoutExpired(
        ["gh", "api", "repos/owner/repo/actions/jobs/1"], 60,
        output="sensitive-response-body", stderr="Authorization: Bearer sensitive-token",
    )

    def run(*args, **kwargs):
        calls.append(kwargs)
        raise failure

    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(helper.time, "sleep", sleeps.append)
    with pytest.raises(helper.MetadataApiError, match="command-timeout") as caught:
        helper.api("repos/owner/repo/actions/jobs/1")
    assert caught.value.__cause__ is failure
    assert "sensitive" not in str(caught.value)
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_api_unknown_failure_summary_does_not_echo_stderr(helper, monkeypatch):
    failure = subprocess.CalledProcessError(1, ["gh"], stderr="sensitive-token sensitive-body")

    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)
    with pytest.raises(helper.MetadataApiError, match="unknown-cli-failure") as caught:
        helper.api("repos/owner/repo/actions/jobs/1")
    assert caught.value.__cause__ is failure
    assert "attempts=3" in str(caught.value)
    assert "sensitive" not in str(caught.value)


def test_api_transport_failure_can_recover_on_the_second_read(helper, monkeypatch):
    calls, sleeps = [], []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(
                1, command, stderr="net/http: TLS handshake timeout",
            )
        return subprocess.CompletedProcess(command, 0, stdout='{"status":"completed"}')

    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(helper.time, "sleep", sleeps.append)
    assert helper.api("repos/owner/repo/actions/jobs/1") == {"status": "completed"}
    assert len(calls) == 2
    assert sleeps == [1]


def test_api_does_not_retry_malformed_json(helper, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="not JSON")

    monkeypatch.setattr(helper.subprocess, "run", run)
    with pytest.raises(json.JSONDecodeError):
        helper.api("repos/owner/repo/actions/jobs/1")
    assert len(calls) == 1


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(1)])
def test_api_does_not_intercept_cancellation(helper, monkeypatch, error):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        raise error

    monkeypatch.setattr(helper.subprocess, "run", run)
    with pytest.raises(type(error)) as caught:
        helper.api("repos/owner/repo/actions/jobs/1")
    assert caught.value is error
    assert len(calls) == 1


def test_api_expired_wait_budget_never_starts_another_request(helper, monkeypatch):
    monkeypatch.setattr(helper.time, "monotonic", lambda: 10)
    calls = []
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(TimeoutError, match="60 minutes"):
        helper.api("repos/owner/repo/actions/jobs/1", deadline=10)
    assert not calls


def test_api_does_not_sleep_past_the_shared_wait_budget(helper, monkeypatch):
    clock, calls, sleeps = [0.0], [], []
    failure = subprocess.CalledProcessError(1, ["gh"], stderr="connection reset by peer")

    def run(*args, **kwargs):
        calls.append(kwargs["timeout"])
        clock[0] = 0.5
        raise failure

    monkeypatch.setattr(helper.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(helper.time, "sleep", sleeps.append)
    with pytest.raises(TimeoutError, match="60 minutes") as caught:
        helper.api("repos/owner/repo/actions/jobs/1", deadline=1)
    assert caught.value.__cause__ is failure
    assert "connection-reset" in str(caught.value)
    assert calls == [1]
    assert not sleeps


def test_api_retry_timeout_is_clamped_to_remaining_wait_budget(helper, monkeypatch):
    clock, calls, sleeps = [0.0], [], []

    def run(command, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            clock[0] += 2
            raise subprocess.CalledProcessError(1, command, stderr="connection reset by peer")
        return subprocess.CompletedProcess(command, 0, stdout="{}")

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(helper.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(helper.time, "sleep", sleep)
    assert helper.api("repos/owner/repo/actions/jobs/1", deadline=5) == {}
    assert calls == [5, 2]
    assert sleeps == [1]


def test_api_command_timeout_exhausts_wait_budget_without_an_extra_attempt(helper, monkeypatch):
    clock, calls, sleeps = [0.0], [], []
    failure = subprocess.TimeoutExpired(["gh"], 1)

    def run(*args, **kwargs):
        calls.append(kwargs["timeout"])
        clock[0] = 1
        raise failure

    monkeypatch.setattr(helper.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(helper.time, "sleep", sleeps.append)
    with pytest.raises(TimeoutError, match="60 minutes") as caught:
        helper.api("repos/owner/repo/actions/jobs/1", deadline=1)
    assert caught.value.__cause__ is failure
    assert "command-timeout" in str(caught.value)
    assert calls == [1]
    assert not sleeps


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
    endpoints, deadlines = [], []
    monkeypatch.setattr(helper.time, "monotonic", lambda: 0)
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
        deadlines.append(kwargs["deadline"])
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
    assert deadlines == [helper.WAIT_SECONDS] * 5
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


def test_wait_poll_sleep_stays_inside_original_budget(helper, monkeypatch, tmp_path):
    configure_wait(helper, monkeypatch)
    clock, sleeps, calls = [0.0], [], []

    def api(endpoint, **kwargs):
        calls.append(endpoint)
        if len(calls) == 1:
            clock[0] = helper.WAIT_SECONDS - 1
            return {"id": 123, "run_attempt": 2}
        return [{"jobs": [{"id": 456, "name": "build", "status": "in_progress",
                            "run_id": 123, "run_attempt": 2}]}]

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(helper.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(helper.time, "sleep", sleep)
    monkeypatch.setattr(helper, "api", api)
    with pytest.raises(TimeoutError, match="60 minutes"):
        helper.wait_for_candidate("candidate", tmp_path / "download", SOURCE)
    assert sleeps == [1]
    assert len(calls) == 2
    assert clock[0] == helper.WAIT_SECONDS
    assert not (tmp_path / "download").exists()


@pytest.mark.parametrize("returncode", [2, 4])
def test_api_cli_cancellation_and_authentication_are_not_retried(helper, monkeypatch, returncode):
    calls, sleeps = [], []
    failure = subprocess.CalledProcessError(returncode, ["gh"], stderr="TLS handshake timeout")

    def run(*args, **kwargs):
        calls.append(args)
        raise failure

    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(helper.time, "sleep", sleeps.append)
    with pytest.raises(helper.MetadataApiError, match="attempts=1") as caught:
        helper.api("repos/owner/repo/actions/jobs/1")
    assert caught.value.__cause__ is failure
    assert len(calls) == 1
    assert not sleeps


def test_api_unclassified_exit_one_can_recover_without_disclosing_cli_output(helper, monkeypatch):
    calls, sleeps = [], []
    failure = subprocess.CalledProcessError(
        1, ["gh"], stderr="opaque-sensitive-cli-error", output="sensitive-body",
    )

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise failure
        return subprocess.CompletedProcess(command, 0, stdout='{"status":"completed"}')

    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(helper.time, "sleep", sleeps.append)
    assert helper.api("repos/owner/repo/actions/jobs/1") == {"status": "completed"}
    assert len(calls) == 2
    assert sleeps == [1]
