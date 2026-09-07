"""Verify release source selection and fail-closed gates without remote API calls."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError, URLError

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/release_signing_preflight.py"
SHA = "a" * 40
POLICY = {
    "schemaVersion": 1,
    "certificateSha1": "A" * 40,
    "publisherSubjectContains": "Test Publisher",
    "timestampUrl": "http://timestamp.example.invalid",
}


@pytest.fixture
def preflight(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_signing_preflight", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def no_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Release preflight tests must not contact a real API")

    monkeypatch.setattr(module, "urlopen", no_network)
    monkeypatch.setenv("GH_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("GITHUB_API_URL", "https://github.example.invalid/api/v3")
    return module


@pytest.mark.parametrize(
    ("event", "ref", "tag", "expected"),
    [
        ("push", "refs/tags/v0.5.5", "v0.5.5", SHA),
        ("push", "refs/tags/v0.5.5rc1", "v0.5.5rc1", SHA),
        ("workflow_dispatch", "refs/heads/main", "", SHA),
        ("workflow_dispatch", "refs/heads/feat/digicert-windows-signing", "", SHA),
        ("workflow_dispatch", "refs/heads/main", "v0.5.5", "refs/tags/v0.5.5"),
    ],
)
def test_source_context_selects_immutable_source(
    preflight: ModuleType, event: str, ref: str, tag: str, expected: str
) -> None:
    assert preflight.source_ref(event, ref, tag, SHA) == expected


@pytest.mark.parametrize(
    ("event", "ref", "tag", "sha"),
    [
        ("push", "refs/heads/main", "", SHA),
        ("push", "refs/tags/v0.5.5", "v0.5.6", SHA),
        ("workflow_dispatch", "refs/tags/v0.5.5", "", SHA),
        ("workflow_dispatch", "refs/heads/feature", "v0.5.5", SHA),
        ("pull_request", "refs/pull/1480/merge", "", SHA),
        ("workflow_dispatch", "refs/heads/main", "../v0.5.5", SHA),
        ("workflow_dispatch", "refs/heads/main", "v00.5.5", SHA),
        ("workflow_dispatch", "refs/heads/main", "v1.2.3-rc1", SHA),
        ("workflow_dispatch", "refs/heads/main", "v1.2.3a1", SHA),
        ("workflow_dispatch", "refs/heads/main", "v1.2.3alpha1", SHA),
        ("workflow_dispatch", "refs/heads/main", "v1.2.3beta1", SHA),
        ("workflow_dispatch", "refs/heads/main", "v0.5.5-rc01", SHA),
        ("workflow_dispatch", "refs/heads/main", "v0.5.5\n", SHA),
        ("workflow_dispatch", "refs/heads/main", "", "main"),
        ("workflow_dispatch", "refs/heads/main", "", SHA[:8]),
        ("workflow_dispatch", "refs/heads/main", "", "g" * 40),
    ],
)
def test_invalid_source_context_fails_before_fetch(
    preflight: ModuleType, event: str, ref: str, tag: str, sha: str
) -> None:
    with pytest.raises(ValueError):
        preflight.source_ref(event, ref, tag, sha)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, encoding="utf-8", stderr=subprocess.PIPE
    ).strip()


def _commit(repo: Path) -> str:
    _git(repo, "add", ".")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "Test signing contract")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def local_source(tmp_path: Path, preflight: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "Release Test")
    for name in preflight.SIGNING_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(POLICY) if name.endswith(".json") else "# Test signing script\n"
        path.write_text(content, encoding="utf-8")
    _commit(source)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _git(consumer, "init", "-q")
    _git(consumer, "remote", "add", "origin", str(source))
    monkeypatch.chdir(consumer)
    return source


@pytest.mark.parametrize("annotated", [False, True])
def test_local_git_resolves_tag_to_commit_and_validates_contract(
    preflight: ModuleType, local_source: Path, annotated: bool
) -> None:
    expected = _git(local_source, "rev-parse", "HEAD")
    args = ("-a", "v0.5.5", "-m", "Test tag") if annotated else ("v0.5.5",)
    _git(local_source, "-c", "tag.gpgsign=false", "tag", *args)
    assert preflight.resolve_source("refs/tags/v0.5.5") == expected
    preflight.validate_signing_contract(expected)


def test_local_git_resolves_exact_workflow_commit(
    preflight: ModuleType, local_source: Path
) -> None:
    expected = _git(local_source, "rev-parse", "HEAD")
    assert preflight.resolve_source(expected) == expected


def test_missing_remote_tag_fails_without_substituting_main(
    preflight: ModuleType, local_source: Path
) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        preflight.resolve_source("refs/tags/v0.0.0")


@pytest.mark.parametrize(
    "missing",
    [
        "desktop/electron/scripts/build-signed-windows.cjs",
        ".github/scripts/verify-windows-signatures.ps1",
        ".github/signing/windows-signing-policy.json",
    ],
)
def test_historical_source_missing_signing_contract_is_rejected(
    preflight: ModuleType, local_source: Path, missing: str
) -> None:
    (local_source / missing).unlink()
    sha = preflight.resolve_source(_commit(local_source))
    with pytest.raises(ValueError, match="historical unsigned tags are not rebuilt"):
        preflight.validate_signing_contract(sha)


@pytest.mark.parametrize(
    "policy",
    [
        [],
        None,
        {**POLICY, "schemaVersion": True},
        {**POLICY, "schemaVersion": 2},
        {**POLICY, "certificateSha1": "a" * 40},
        {**POLICY, "certificateSha1": "A" * 39},
        {**POLICY, "publisherSubjectContains": ""},
        {**POLICY, "publisherSubjectContains": ["Test Publisher"]},
        {**POLICY, "timestampUrl": ""},
        {**POLICY, "timestampUrl": 42},
    ],
)
def test_invalid_policy_fails_before_signing(
    preflight: ModuleType, local_source: Path, policy: object
) -> None:
    (local_source / ".github/signing/windows-signing-policy.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    sha = preflight.resolve_source(_commit(local_source))
    with pytest.raises(ValueError, match="unsupported Windows signing policy"):
        preflight.validate_signing_contract(sha)


def test_empty_signing_script_is_rejected(preflight: ModuleType, local_source: Path) -> None:
    (local_source / "desktop/electron/scripts/build-signed-windows.cjs").write_text(
        "", encoding="utf-8"
    )
    sha = preflight.resolve_source(_commit(local_source))
    with pytest.raises(ValueError, match="signing contract is empty"):
        preflight.validate_signing_contract(sha)


@pytest.mark.parametrize("status", [404, 401, 403, 500])
def test_only_api_not_found_allows_new_release(
    preflight: ModuleType, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def response(request: object, *, timeout: int) -> None:
        assert request.full_url == (
            "https://github.example.invalid/api/v3/repos/TokenRhythm/opensquilla/releases/tags/v0.5.5"
        )
        assert request.get_header("Authorization") == "Bearer synthetic-test-token"
        assert timeout > 0
        raise HTTPError(request.full_url, status, "Synthetic API failure", {}, None)

    monkeypatch.setattr(preflight, "urlopen", response)
    if status == 404:
        preflight.validate_release("TokenRhythm/opensquilla", "v0.5.5")
    else:
        with pytest.raises(ValueError, match=f"HTTP {status}.*refusing to build"):
            preflight.validate_release("TokenRhythm/opensquilla", "v0.5.5")


@pytest.mark.parametrize("tag", ["v0.5.5", "v0.5.5rc1", "v0.5.5rc0"])
def test_existing_draft_requires_matching_preview_state(
    preflight: ModuleType, monkeypatch: pytest.MonkeyPatch, tag: str
) -> None:
    expected = tag != "v0.5.5"
    monkeypatch.setattr(
        preflight,
        "urlopen",
        lambda *_args, **_kwargs: io.StringIO(json.dumps({"draft": True, "prerelease": expected})),
    )
    preflight.validate_release("TokenRhythm/opensquilla", tag)
    monkeypatch.setattr(
        preflight,
        "urlopen",
        lambda *_args, **_kwargs: io.StringIO(
            json.dumps({"draft": True, "prerelease": not expected})
        ),
    )
    with pytest.raises(ValueError, match="unexpected prerelease state"):
        preflight.validate_release("TokenRhythm/opensquilla", tag)


@pytest.mark.parametrize("draft", [False, None, 1])
def test_existing_release_without_explicit_draft_true_is_rejected(
    preflight: ModuleType, monkeypatch: pytest.MonkeyPatch, draft: object
) -> None:
    monkeypatch.setattr(
        preflight,
        "urlopen",
        lambda *_args, **_kwargs: io.StringIO(json.dumps({"draft": draft, "prerelease": False})),
    )
    with pytest.raises(ValueError, match="non-Draft"):
        preflight.validate_release("TokenRhythm/opensquilla", "v0.5.5")


def test_transport_failure_does_not_mean_release_absent(
    preflight: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failure(*_args: object, **_kwargs: object) -> None:
        raise URLError("Synthetic connection failure")

    monkeypatch.setattr(preflight, "urlopen", failure)
    with pytest.raises(URLError):
        preflight.validate_release("TokenRhythm/opensquilla", "v0.5.5")


@pytest.mark.parametrize("tag", ["", "v0.5.5"])
def test_main_exports_only_validated_sha_and_records_workflow_provenance(
    preflight: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str
) -> None:
    events = []
    output = tmp_path / "outputs"
    summary = tmp_path / "summary"
    for name, value in {
        "RELEASE_TAG": tag,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SHA,
        "GITHUB_WORKFLOW_SHA": "b" * 40,
        "GITHUB_REPOSITORY": "TokenRhythm/opensquilla",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }.items():
        monkeypatch.setenv(name, value)

    def resolve(ref: str) -> str:
        events.append(("resolve", ref))
        return "c" * 40

    monkeypatch.setattr(preflight, "resolve_source", resolve)
    monkeypatch.setattr(
        preflight, "validate_signing_contract", lambda sha: events.append(("contract", sha))
    )
    monkeypatch.setattr(
        preflight, "validate_release", lambda repo, tag: events.append(("release", repo, tag))
    )
    preflight.main()
    assert events[:2] == [("resolve", f"refs/tags/{tag}" if tag else SHA), ("contract", "c" * 40)]
    assert events[2:] == ([("release", "TokenRhythm/opensquilla", tag)] if tag else [])
    assert output.read_text(encoding="utf-8") == f"source_sha={'c' * 40}\n"
    assert "c" * 40 in summary.read_text(encoding="utf-8")
    assert "b" * 40 in summary.read_text(encoding="utf-8")


def test_workflow_checkouts_use_preflight_sha_through_declared_job_outputs() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/wheelhouse-release.yml").read_text())["jobs"]
    expression = re.compile(r"\$\{\{ needs\.([\w-]+)\.outputs\.source_sha \}\}")

    def assert_provenance(job_name: str, visited: set[str]) -> None:
        assert job_name not in visited, "Source SHA output chain contains a cycle"
        job = jobs[job_name]
        if job_name == "release-preflight":
            assert job["outputs"]["source_sha"] == "${{ steps.source.outputs.source_sha }}"
            return
        match = expression.fullmatch(job["outputs"]["source_sha"])
        assert match, f"{job_name} does not forward its validated source SHA"
        upstream = match[1]
        needs = job["needs"]
        assert upstream in ([needs] if isinstance(needs, str) else needs)
        assert_provenance(upstream, visited | {job_name})

    checkouts = 0
    for job_name, job in jobs.items():
        for step in job["steps"]:
            if not step.get("uses", "").startswith("actions/checkout@"):
                continue
            ref = step["with"]["ref"]
            assert step["with"]["persist-credentials"] is False
            if job_name == "release-preflight":
                assert ref == "${{ github.workflow_sha }}"
                continue
            match = expression.fullmatch(ref)
            assert match, f"{job_name} checks out a mutable or unvalidated source"
            needs = job["needs"]
            assert match[1] in ([needs] if isinstance(needs, str) else needs)
            assert_provenance(match[1], {job_name})
            checkouts += 1
    assert checkouts >= 8
    assert "environment" not in jobs["release-preflight"]


@pytest.mark.parametrize("failed_gate", ["validate_signing_contract", "validate_release"])
def test_failed_preflight_never_exports_source_for_build_jobs(
    preflight: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_gate: str
) -> None:
    output = tmp_path / "outputs"
    summary = tmp_path / "summary"
    for name, value in {
        "RELEASE_TAG": "v0.5.5",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SHA,
        "GITHUB_WORKFLOW_SHA": "b" * 40,
        "GITHUB_REPOSITORY": "TokenRhythm/opensquilla",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(preflight, "resolve_source", lambda _ref: "c" * 40)
    monkeypatch.setattr(preflight, "validate_signing_contract", lambda _sha: None)
    monkeypatch.setattr(preflight, "validate_release", lambda _repo, _tag: None)

    def reject(*_args: object) -> None:
        raise ValueError("Synthetic preflight rejection")

    monkeypatch.setattr(preflight, failed_gate, reject)
    with pytest.raises(ValueError, match="Synthetic preflight rejection"):
        preflight.main()
    assert not output.exists()
    assert not summary.exists()


def test_empty_tag_runs_independent_windows_artifact_audit_matrix() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/wheelhouse-release.yml").read_text())["jobs"]
    audit = jobs["audit-internal-windows-artifact"]
    assert (
        audit["if"]
        == "${{ github.event_name == 'workflow_dispatch' && github.event.inputs.tag == '' }}"
    )
    assert "build-desktop-windows" in audit["needs"]
    assert "publish-release" not in audit["needs"]
    assert audit["runs-on"].startswith("windows-")
    assert audit["strategy"]["fail-fast"] is False
    assert audit["strategy"]["matrix"] == {
        "baseline-version": ["0.5.3", "0.5.4"],
        "install-mode": ["default", "custom"],
    }
    assert "environment" not in audit
    download = next(
        step
        for step in audit["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    )
    assert download["with"]["name"] == "opensquilla-electron-windows"
    verify = next(
        step
        for step in audit["steps"]
        if "verify-release-windows-upgrade.ps1" in step.get("run", "")
    )
    assert verify["env"]["BASELINE_VERSION"] == "${{ matrix.baseline-version }}"
    assert verify["env"]["INSTALL_MODE"] == "${{ matrix.install-mode }}"
    assert "verify-windows-signatures.ps1 -InstallerPath" in verify["run"]
    assert "-BaselineVersion $env:BASELINE_VERSION" in verify["run"]
    assert "-InstallMode $env:INSTALL_MODE" in verify["run"]
    assert "secrets." not in json.dumps(audit)


def test_internal_diagnostics_preserve_signed_bytes_without_feeding_publication() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/wheelhouse-release.yml").read_text())["jobs"]
    steps = jobs["build-desktop-windows"]["steps"]
    by_name = {step.get("name"): step for step in steps}
    diagnostic = by_name["Retain internal signed candidate for diagnosis"]
    assert diagnostic["if"] == jobs["audit-internal-windows-artifact"]["if"]
    assert (
        steps.index(by_name["Verify Windows Authenticode signatures and timestamps"])
        < steps.index(diagnostic)
        < steps.index(by_name["Gate packaged first-send renderer"])
    )
    assert diagnostic["with"]["path"].splitlines() == [
        "dist/desktop-electron/*.exe",
        "dist/desktop-electron/*.blockmap",
        "dist/desktop-electron/latest.yml",
    ]
    assert by_name["Gate packaged first-send renderer"]["timeout-minutes"] == 15
    failure_log = by_name["Retain Windows first-send failure log"]
    assert failure_log["if"] == "${{ failure() }}"
    assert failure_log["with"]["path"].endswith("/logs/desktop.log")
    assert (
        "p1-5-first-send-${{ github.run_id }}-${{ github.run_attempt }}"
        in failure_log["with"]["path"]
    )
    assert steps.index(by_name["Remove DigiCert client authentication material"]) < steps.index(
        failure_log
    )
    for job in jobs.values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/download-artifact@"):
                assert "windows-signed-candidate-diagnostics" not in json.dumps(step)


def test_reused_windows_audits_require_signatures_without_signing_credentials() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/desktop-fault-injection.yml").read_text())
    jobs = workflow["jobs"]
    assert workflow["permissions"] == {"actions": "read", "contents": "read"}
    for name in ("macos-fault-injection", "macos-wedge-probe"):
        assert "inputs.source_run_id != ''" in jobs[name]["if"]
    audit = jobs["windows-release-upgrade-audit"]
    assert "inputs.run_windows_release_audit" in audit["if"]
    assert "inputs.run_windows_upgrade_matrix" in audit["if"]
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["workflow_dispatch"]["inputs"]["run_windows_upgrade_matrix"]["default"] is True
    assert "inputs.windows_source_run_id != ''" in audit["if"]
    assert "github.actor == 'Open-Squilla'" in audit["if"]
    assert audit["strategy"]["matrix"] == {
        "baseline-version": ["0.5.3", "0.5.4"],
        "install-mode": ["default", "custom"],
    }
    assert audit["strategy"]["fail-fast"] is False
    for name in ("windows-fault-injection", "windows-release-upgrade-audit"):
        job = jobs[name]
        assert "environment" not in job
        encoded = json.dumps(job)
        assert "secrets." not in encoded and "SM_" not in encoded
        assert "build-signed-windows" not in encoded and "build:gateway" not in encoded
        download = next(
            step for step in job["steps"] if "download-artifact@" in step.get("uses", "")
        )
        assert download["with"]["run-id"] == "${{ inputs.windows_source_run_id }}"
        assert download["with"]["repository"] == "${{ github.repository }}"
        assert download["with"]["name"] == "${{ inputs.windows_artifact_name }}"
        assert "verify-windows-signatures.ps1" in encoded
    first_send = jobs["windows-fault-injection"]
    gate = next(
        step for step in first_send["steps"] if step["name"] == "Verify signed installed first-send"
    )
    assert gate["if"] == "inputs.run_windows_release_audit"
    assert "test-packaged-first-send-renderer.mjs" in gate["run"]
    assert gate["timeout-minutes"] == 15
    assert "--owned-electron-launcher" not in gate["run"]
    diagnostic = next(
        step for step in first_send["steps"]
        if step["name"] == "Compare quit with independently owned debug connections"
    )
    assert "failure()" in diagnostic["if"]
    assert "steps.first_send.outcome == 'failure'" in diagnostic["if"]
    assert "--owned-electron-launcher" in diagnostic["run"]
    assert "--iterations 1" in diagnostic["run"]
    assert first_send["steps"].index(diagnostic) > first_send["steps"].index(gate)
    native_control = next(
        step for step in first_send["steps"]
        if step["name"] == "Validate native stack collector against an owned process"
    )
    assert native_control["if"] == "inputs.run_windows_release_audit"
    assert "test-windows-native-stack-diagnostics.mjs" in native_control["run"]
    assert "continue-on-error" not in native_control
    assert first_send["steps"].index(native_control) < first_send["steps"].index(gate)
