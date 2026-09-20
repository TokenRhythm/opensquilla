"""Verify release source selection and fail-closed gates without remote API calls."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
from email.message import Message
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
def protocol_preflight(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    script = ROOT / ".github/scripts/release_protocol_preflight.py"
    spec = importlib.util.spec_from_file_location("release_protocol_preflight", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def no_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Protocol preflight tests must not contact a real collector")

    monkeypatch.setattr(module, "build_opener", no_network)
    # Keep the retry guard local: subprocess also uses the shared time module.
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=no_network))
    return module


def _health(scope: str, fingerprint: str) -> dict[str, object]:
    return {
        "ok": True,
        "scope": scope,
        "schema_version": 1,
        "protocol_fingerprint": fingerprint,
    }


def test_protocol_approved_predecessor_diff_is_only_optional_device_field(
    protocol_preflight: ModuleType,
) -> None:
    raw = (ROOT / protocol_preflight.MANIFEST_PATH).read_bytes()
    current = protocol_preflight.manifest_fingerprint(raw)
    manifest = json.loads(raw)
    assert manifest.pop("device_identity") == {
        "field": "device_id",
        "format": "sha256-lowercase-hex",
        "optional": True,
        "scope": "application-events",
        "deduplication_unit": "device",
    }
    assert manifest["manifest_version"] == 2
    manifest["manifest_version"] = 1
    previous = protocol_preflight.manifest_fingerprint(json.dumps(manifest).encode())
    assert protocol_preflight.COMPATIBLE_PAIRS == {(current, previous)}
    for scope in ("growth", "reliability"):
        protocol_preflight.validate_health(_health(scope, current), scope, current)
        protocol_preflight.validate_health(_health(scope, current), scope, previous)
        with pytest.raises(ValueError, match="does not support"):
            protocol_preflight.validate_health(_health(scope, previous), scope, current)


def test_protocol_source_reads_fixed_commit_not_mutated_checkout_or_tag(
    protocol_preflight: ModuleType, local_source: Path,
) -> None:
    raw = (ROOT / protocol_preflight.MANIFEST_PATH).read_bytes()
    path = local_source / protocol_preflight.MANIFEST_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    sha = _commit(local_source)
    _git(local_source, "tag", "v0.5.5")
    path.write_text("{}")
    newer = _commit(local_source)
    _git(local_source, "tag", "-f", "v0.5.5", newer)
    assert protocol_preflight.source_fingerprint(local_source, sha) == (
        protocol_preflight.manifest_fingerprint(raw)
    )
    with pytest.raises(ValueError, match="Unsupported release protocol"):
        protocol_preflight.source_fingerprint(local_source, newer)
    with pytest.raises(ValueError, match="full commit SHA"):
        protocol_preflight.source_fingerprint(local_source, "v0.5.5")


def test_protocol_legacy_tag_without_clients_does_not_need_collector(
    protocol_preflight: ModuleType, local_source: Path,
) -> None:
    sha = _git(local_source, "rev-parse", "HEAD")
    assert protocol_preflight.source_fingerprint(local_source, sha) is None


@pytest.mark.parametrize("client_path", [
    "src/opensquilla/telemetry", "desktop/electron/src/telemetry",
])
def test_protocol_existing_client_cannot_bypass_gate_with_missing_manifest(
    protocol_preflight: ModuleType, local_source: Path, client_path: str,
) -> None:
    path = local_source / client_path / "client.txt"
    path.parent.mkdir(parents=True)
    path.write_text("synthetic client")
    sha = _commit(local_source)
    with pytest.raises(ValueError, match="manifest is missing"):
        protocol_preflight.source_fingerprint(local_source, sha)


@pytest.mark.parametrize("replacement", [
    {"ok": False}, {"ok": 1}, {"scope": "reliability"},
    {"schema_version": True}, {"schema_version": 2},
    {"protocol_fingerprint": "A" * 64}, {"protocol_fingerprint": "b" * 64},
    {"extra": "unexpected"},
])
def test_protocol_health_rejects_wrong_service_version_or_identity(
    protocol_preflight: ModuleType, replacement: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        protocol_preflight.validate_health({**_health("growth", "a" * 64), **replacement},
                                          "growth", "a" * 64)


@pytest.mark.parametrize("raw", [
    b"[]", b"null", b"{broken", b'{"manifest_version":1,"manifest_version":2}',
    b'{"manifest_version":true,"batch_version":1,"events":[{}]}',
    b'{"manifest_version":2,"batch_version":1,"events":[NaN]}', b"\xff",
])
def test_protocol_manifest_rejects_malformed_input(
    protocol_preflight: ModuleType, raw: bytes,
) -> None:
    with pytest.raises(ValueError):
        protocol_preflight.manifest_fingerprint(raw)


@pytest.mark.parametrize(("status", "content_type", "final_url", "raw", "valid"), [
    (200, "application/json; charset=utf-8", None, b'{"ok":true}', True),
    (202, "application/json", None, b'{"ok":true}', False),
    (200, "text/html", None, b'{"ok":true}', False),
    (200, "application/json", "https://other.example.invalid/", b'{"ok":true}', False),
    (200, "application/json", None, b'{"ok":false,"ok":true}', False),
    (200, "application/json", None, b"x" * 4097, False),
])
def test_protocol_https_response_is_bounded_and_not_redirected(
    protocol_preflight: ModuleType, monkeypatch: pytest.MonkeyPatch,
    status: int, content_type: str, final_url: str | None, raw: bytes, valid: bool,
) -> None:
    url = protocol_preflight.HEALTH_URLS["growth"]
    response = io.BytesIO(raw)
    response.status = status  # type: ignore[attr-defined]
    response.headers = Message()  # type: ignore[attr-defined]
    response.headers["Content-Type"] = content_type  # type: ignore[attr-defined]
    response.geturl = lambda: final_url or url  # type: ignore[attr-defined]

    class Opener:
        def open(self, request: object, timeout: int) -> io.BytesIO:
            assert request.full_url == url  # type: ignore[attr-defined]
            assert request.get_method() == "GET"  # type: ignore[attr-defined]
            assert request.get_header("Cache-control") == "no-cache"  # type: ignore[attr-defined]
            assert timeout == 10
            return response

    def opener(handler: object) -> Opener:
        with pytest.raises(ValueError, match="redirects are forbidden"):
            handler.redirect_request(None, None, 302, None, None, url)  # type: ignore[attr-defined]
        return Opener()

    monkeypatch.setattr(protocol_preflight, "build_opener", opener)
    if valid:
        assert protocol_preflight.read_health(url) == {"ok": True}
    else:
        with pytest.raises(ValueError):
            protocol_preflight.read_health(url)
    assert response.closed


@pytest.mark.parametrize("error", [
    URLError("synthetic network failure"), TimeoutError(),
    HTTPError("https://example.invalid", 503, "unavailable", {}, None),
    HTTPError("https://example.invalid", 429, "rate limited", {}, None),
])
def test_protocol_transient_failure_retries_then_checks_both_scopes(
    protocol_preflight: ModuleType, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    calls: list[str] = []
    delays: list[int] = []

    def read(url: str) -> object:
        calls.append(url)
        if len(calls) <= 2:
            raise error
        scope = "growth" if "/growth/" in url else "reliability"
        return _health(scope, "a" * 64)

    monkeypatch.setattr(protocol_preflight, "read_health", read)
    monkeypatch.setattr(protocol_preflight.time, "sleep", delays.append)
    protocol_preflight.check_collectors("a" * 64)
    assert calls == [protocol_preflight.HEALTH_URLS["reliability"]] * 3 + [
        protocol_preflight.HEALTH_URLS["growth"]
    ]
    assert delays == [1, 2]


@pytest.mark.parametrize(("status", "attempts"), [(301, 1), (401, 1), (404, 1), (503, 3)])
def test_protocol_http_failure_never_allows_publication(
    protocol_preflight: ModuleType, monkeypatch: pytest.MonkeyPatch, status: int, attempts: int,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(protocol_preflight.time, "sleep", lambda _delay: None)

    def read(url: str) -> object:
        calls.append(url)
        raise HTTPError(url, status, "synthetic error", {}, None)

    monkeypatch.setattr(protocol_preflight, "read_health", read)
    with pytest.raises(ValueError):
        protocol_preflight.check_collectors("a" * 64)
    assert len(calls) == attempts


def test_protocol_second_scope_mismatch_blocks_after_first_scope_succeeds(
    protocol_preflight: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def read(url: str) -> object:
        if url == protocol_preflight.HEALTH_URLS["reliability"]:
            return _health("reliability", "a" * 64)
        return _health("growth", "b" * 64)

    monkeypatch.setattr(protocol_preflight, "read_health", read)
    with pytest.raises(ValueError, match="growth: collector protocol"):
        protocol_preflight.check_collectors("a" * 64)


def test_protocol_release_workflow_gates_fixed_source_before_build_and_publication() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/wheelhouse-release.yml").read_text())["jobs"]
    condition = "${{ github.event_name == 'push' || github.event.inputs.tag != '' }}"
    preflight = jobs["release-preflight"]["steps"]
    gate = next(step for step in preflight if step.get("name") ==
                "Check production collector compatibility")
    assert gate["env"]["RELEASE_SOURCE_SHA"] == "${{ steps.source.outputs.source_sha }}"
    assert gate["run"] == (
        'python .github/scripts/release_protocol_preflight.py --source-sha "$RELEASE_SOURCE_SHA"'
    )
    assert preflight.index(gate) > next(i for i, step in enumerate(preflight)
                                        if step.get("id") == "source")
    publication = jobs["publish-release"]["steps"]
    index = next(i for i, step in enumerate(publication)
                 if step.get("name") == "Upload to GitHub Release")
    final_gate = publication[index - 1]
    assert final_gate["env"]["RELEASE_SOURCE_SHA"] == (
        "${{ needs.build-release-assets.outputs.source_sha }}"
    )
    assert final_gate["run"] == (
        'python .release-validation/.github/scripts/release_protocol_preflight.py '
        '--source-sha "$RELEASE_SOURCE_SHA"'
    )
    tooling = publication[index - 2]
    assert tooling["uses"] == "actions/checkout@v4"
    assert tooling["with"]["ref"] == "${{ github.workflow_sha }}"
    assert tooling["with"]["path"] == ".release-validation"
    for step in (gate, final_gate):
        assert step["if"] == condition
        assert step["timeout-minutes"] == 3
        assert not step.get("continue-on-error", False)
        assert "secrets." not in json.dumps(step)


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
    monkeypatch.delenv("INTERNAL_WINDOWS_ONLY", raising=False)
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


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/integration/native-audit"])
def test_windows_only_internal_dispatch_keeps_immutable_source(
    preflight: ModuleType, ref: str
) -> None:
    assert (
        preflight.source_ref("workflow_dispatch", ref, "", SHA, internal_windows_only=True) == SHA
    )


@pytest.mark.parametrize(
    ("event", "ref", "tag"),
    [
        ("push", "refs/tags/v0.5.5", "v0.5.5"),
        ("push", "refs/heads/main", ""),
        ("workflow_dispatch", "refs/heads/main", "v0.5.5"),
        ("workflow_dispatch", "refs/heads/integration/native-audit", "v0.5.5"),
        ("workflow_dispatch", "refs/tags/v0.5.5", ""),
        ("pull_request", "refs/pull/1584/merge", ""),
    ],
)
def test_windows_only_invalid_dispatch_fails_before_fetch_or_release_query(
    preflight: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    ref: str,
    tag: str,
) -> None:
    output = tmp_path / "outputs"
    for name, value in {
        "INTERNAL_WINDOWS_ONLY": "true",
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REF": ref,
        "RELEASE_TAG": tag,
        "GITHUB_SHA": SHA,
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(name, value)

    def unexpected_side_effect(*_args: object) -> None:
        pytest.fail("Invalid Windows-only invocation must fail before Git/API access")

    monkeypatch.setattr(preflight, "resolve_source", unexpected_side_effect)
    monkeypatch.setattr(preflight, "validate_release", unexpected_side_effect)
    with pytest.raises(ValueError, match="branch dispatch with an empty release tag"):
        preflight.main()
    assert not output.exists()


@pytest.mark.parametrize("value", ["", "1", "0", "TRUE", "true\n"])
def test_windows_only_malformed_boolean_is_not_silently_ignored(
    preflight: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("INTERNAL_WINDOWS_ONLY", value)
    with pytest.raises(ValueError, match="INTERNAL_WINDOWS_ONLY must be true or false"):
        preflight.main()


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


@pytest.mark.parametrize(
    ("tag", "windows_only"), [("", None), ("", "false"), ("", "true"), ("v0.5.5", "false")]
)
def test_main_exports_only_validated_sha_and_records_workflow_provenance(
    preflight: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tag: str,
    windows_only: str | None,
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
    if windows_only is not None:
        monkeypatch.setenv("INTERNAL_WINDOWS_ONLY", windows_only)

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
    scope = "internal Windows only" if windows_only == "true" else "all release platforms"
    assert f"Build scope: {scope}" in summary.read_text(encoding="utf-8")


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
        if "uses" in job:
            # A reusable verifier consumes the bound candidate SHA rather than
            # exposing checkout steps in the caller. Keep its provenance chain
            # subject to the same declared-needs and preflight validation.
            assert job_name == "internal-windows-candidate-probes"
            assert job["uses"] == "./.github/workflows/windows-candidate-probes.yml"
            assert "steps" not in job
            match = expression.fullmatch(job["with"]["source_sha"])
            assert match, f"{job_name} consumes a mutable or unvalidated source"
            needs = job["needs"]
            assert match[1] in ([needs] if isinstance(needs, str) else needs)
            assert_provenance(match[1], {job_name})
            assert job["with"]["require_signature"] is True
            continue
        for step in job["steps"]:
            if not step.get("uses", "").startswith("actions/checkout@"):
                continue
            ref = step["with"]["ref"]
            assert step["with"]["persist-credentials"] is False
            if job_name == "release-preflight" or (
                job_name == "publish-release"
                and step.get("name") == "Checkout publication validation tooling"
            ):
                assert ref == "${{ github.workflow_sha }}"
                if job_name == "publish-release":
                    assert step["with"]["path"] == ".release-validation"
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
        == "${{ always() && github.event_name == 'workflow_dispatch' "
        "&& github.event.inputs.tag == '' "
        "&& needs.build-desktop-windows.outputs.candidate_artifact_id != '' }}"
    )
    assert jobs["build-desktop-windows"]["outputs"]["candidate_artifact_id"] == (
        "${{ steps.internal-candidate.outputs.artifact-id }}"
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
    assert download["with"]["name"] == "windows-signed-candidate-diagnostics"
    verify = next(
        step
        for step in audit["steps"]
        if "verify-release-windows-upgrade.ps1" in step.get("run", "")
    )
    assert verify["env"]["BASELINE_VERSION"] == "${{ matrix.baseline-version }}"
    assert verify["env"]["INSTALL_MODE"] == "${{ matrix.install-mode }}"
    assert verify["env"]["CANDIDATE_SOURCE_SHA"] == (
        "${{ needs.build-control-ui.outputs.source_sha }}"
    )
    assert "windows_candidate_identity.py" in verify["run"]
    assert (
        "--manifest release-audit/audit-candidate.json --source-sha $env:CANDIDATE_SOURCE_SHA"
    ) in verify["run"]
    assert (
        "if ($LASTEXITCODE -ne 0) { throw 'Signed candidate provenance mismatch.' }"
    ) in verify["run"]
    assert verify["run"].index("windows_candidate_identity.py") < verify["run"].index(
        "verify-windows-signatures.ps1"
    )
    assert "verify-windows-signatures.ps1 -InstallerPath" in verify["run"]
    assert "-BaselineVersion $env:BASELINE_VERSION" in verify["run"]
    assert "-InstallMode $env:INSTALL_MODE" in verify["run"]
    assert "secrets." not in json.dumps(audit)


def test_windows_only_input_skips_unrelated_jobs_and_keeps_signed_windows_audits() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/wheelhouse-release.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    option = triggers["workflow_dispatch"]["inputs"]["internal_windows_only"]
    assert option["type"] == "boolean"
    assert option["default"] is False
    assert option["required"] is False
    jobs = workflow["jobs"]
    preflight_step = next(
        step for step in jobs["release-preflight"]["steps"] if step.get("id") == "source"
    )
    assert preflight_step["env"]["INTERNAL_WINDOWS_ONLY"] == (
        "${{ inputs.internal_windows_only || false }}"
    )
    assert preflight_step["run"] == "python .github/scripts/release_signing_preflight.py"
    skipped = {"build-release-assets", "build-desktop-macos", "publish-release"}
    for name in skipped:
        assert jobs[name]["if"] == "${{ inputs.internal_windows_only != true }}"
    assert {
        name for name, job in jobs.items() if "internal_windows_only" in job.get("if", "")
    } == skipped
    for name in ("release-preflight", "build-control-ui", "build-desktop-windows"):
        assert "if" not in jobs[name]
    assert jobs["build-control-ui"]["needs"] == "release-preflight"
    windows = jobs["build-desktop-windows"]
    assert windows["needs"] == "build-control-ui"
    assert windows["environment"] == {"name": "windows-code-signing"}
    steps = {step.get("name"): step for step in windows["steps"]}
    assert "node scripts/build-signed-windows.cjs" in steps["Build signed Windows installer"]["run"]
    assert steps["Verify Windows Authenticode signatures and timestamps"]["run"] == (
        ".github/scripts/verify-windows-signatures.ps1"
    )
    assert "if" not in steps["Gate packaged first-send renderer"]
    assert jobs["audit-internal-windows-artifact"]["needs"] == [
        "build-control-ui",
        "build-desktop-windows",
    ]
    assert workflow["permissions"] == {"contents": "read"}
    for name in (
        "prestage-draft-updater-assets",
        "audit-downloaded-macos-release",
        "audit-downloaded-windows-release",
    ):
        assert jobs[name]["if"] == (
            "${{ github.event_name == 'push' || github.event.inputs.tag != '' }}"
        )


def test_internal_diagnostics_preserve_signed_bytes_without_feeding_publication() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/wheelhouse-release.yml").read_text())["jobs"]
    steps = jobs["build-desktop-windows"]["steps"]
    by_name = {step.get("name"): step for step in steps}
    diagnostic = by_name["Retain internal signed candidate for diagnosis"]
    # Upload after signature verification succeeds; downstream audits must
    # still run after a later build-job gate fails, but only with this artifact.
    assert diagnostic["if"] == (
        "${{ github.event_name == 'workflow_dispatch' && github.event.inputs.tag == '' }}"
    )
    assert diagnostic["id"] == "internal-candidate"
    assert diagnostic["with"]["name"] == "windows-signed-candidate-diagnostics"
    assert (
        steps.index(by_name["Verify Windows Authenticode signatures and timestamps"])
        < steps.index(diagnostic)
        < steps.index(by_name["Gate packaged first-send renderer"])
    )
    assert diagnostic["with"]["path"].splitlines() == [
        "dist/desktop-electron/audit-candidate.json",
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
    for name, job in jobs.items():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/download-artifact@"):
                if name == "audit-internal-windows-artifact":
                    assert step["with"]["name"] == "windows-signed-candidate-diagnostics"
                else:
                    assert "windows-signed-candidate-diagnostics" not in json.dumps(step)
    reusable = jobs["internal-windows-candidate-probes"]
    assert reusable["with"]["artifact_name"] == diagnostic["with"]["name"]
    assert reusable["with"]["require_signature"] is True
    assert reusable["if"] == jobs["audit-internal-windows-artifact"]["if"]


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
    assert "--iterations" not in gate["run"], "Formal audit must retain the default 20 rounds"
    assert "--quit-diagnostics-file" not in gate["run"]
    assert "--extended-quit-diagnostics" not in json.dumps(first_send)
    assert "--owned-electron-launcher" not in json.dumps(first_send)
    assert "test-windows-native-stack-diagnostics.mjs" not in json.dumps(first_send)
    assert (
        sum(
            "test-packaged-first-send-renderer.mjs" in step.get("run", "")
            for step in first_send["steps"]
        )
        == 1
    )


def _run_signing_material_step(
    name: str,
    tmp_path: Path,
    environment: dict[str, str],
    *,
    lock_certificate: bool = False,
) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is required to execute signing material cleanup")
    jobs = yaml.safe_load((ROOT / ".github/workflows/wheelhouse-release.yml").read_text())["jobs"]
    step = next(step for step in jobs["build-desktop-windows"]["steps"] if step["name"] == name)
    if name == "Remove DigiCert client authentication material":
        assert step["if"] == "${{ always() }}"
    source = step["run"]
    if lock_certificate:
        source = (
            "$locked = [IO.File]::Open((Join-Path $env:RUNNER_TEMP "
            "'digicert-client-auth.p12'), 'Open', 'ReadWrite', 'None')\ntry {\n"
            + source
            + "\n} finally { $locked.Dispose() }\n"
        )
    script = tmp_path / "signing-material-step.ps1"
    started_marker = "opensquilla-signing-material-step-started"
    script.write_text(
        f"[Console]::Error.WriteLine('{started_marker}')\n" + source,
        encoding="utf-8",
    )
    # No signing credentials or real user profile are inherited by these scripts.
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    }
    env.update(
        {
            key: str(tmp_path)
            for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")
        }
    )
    env.update(environment)
    # These offline tests need neither startup telemetry nor update checks.
    env.update(
        POWERSHELL_TELEMETRY_OPTOUT="1",
        POWERSHELL_UPDATECHECK="Off",
    )
    try:
        return subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-File", str(script)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        phase = "workflow script" if started_marker in stderr else "PowerShell startup"
        raise AssertionError(f"Signing material test timed out during {phase}: {name}") from exc


@pytest.mark.parametrize(
    ("stderr", "phase"),
    [
        (b"", "PowerShell startup"),
        (b"opensquilla-signing-material-step-started\n", "workflow script"),
    ],
)
def test_signing_material_timeout_is_offline_and_diagnoses_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: bytes, phase: str,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "synthetic-pwsh")
    monkeypatch.setenv("SM_API_KEY", "synthetic-parent-secret")
    monkeypatch.setenv("POWERSHELL_TELEMETRY_OPTOUT", "0")
    monkeypatch.setenv("POWERSHELL_UPDATECHECK", "Default")
    calls = []

    def timeout(command: list[str], **kwargs: object) -> None:
        calls.append(command)
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert "SM_API_KEY" not in env
        assert env["POWERSHELL_TELEMETRY_OPTOUT"] == "1"
        assert env["POWERSHELL_UPDATECHECK"] == "Off"
        assert kwargs["timeout"] == 15
        assert Path(command[-1]).read_text().startswith("[Console]::Error.WriteLine(")
        raise subprocess.TimeoutExpired(command, timeout=15, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AssertionError, match=f"timed out during {phase}"):
        _run_signing_material_step("Remove DigiCert client authentication material", tmp_path, {})
    assert len(calls) == 1


@pytest.mark.ci_serial
def test_signing_cleanup_removes_certificate_after_environment_export_fails(tmp_path: Path) -> None:
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    certificate = runner_temp / "digicert-client-auth.p12"
    msi = runner_temp / "Keylockertools-windows-x64.msi"
    msi.write_bytes(b"synthetic MSI; never executed")
    sentinel = tmp_path / "external-sentinel.p12"
    sentinel.write_bytes(b"outside signing cleanup")
    dummy = b"synthetic client certificate bytes; no signing capability"
    export_file = tmp_path / "missing-parent" / "github-env"
    configured = _run_signing_material_step(
        "Configure DigiCert KeyLocker credentials",
        tmp_path,
        {
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_ENV": str(export_file),
            "SM_HOST_SECRET": "https://signing.example.invalid",
            "SM_API_KEY_SECRET": "synthetic-api-key-no-access",
            "SM_CLIENT_CERT_FILE_B64_SECRET": base64.b64encode(dummy).decode("ascii"),
            "SM_CLIENT_CERT_PASSWORD_SECRET": "synthetic-password-no-access",
        },
    )
    assert configured.returncode != 0
    assert certificate.read_bytes() == dummy
    assert not export_file.exists()
    cleaned = _run_signing_material_step(
        "Remove DigiCert client authentication material",
        tmp_path,
        {"RUNNER_TEMP": str(runner_temp)},
    )
    assert cleaned.returncode == 0, cleaned.stderr
    assert not certificate.exists()
    assert not msi.exists()
    assert sentinel.read_bytes() == b"outside signing cleanup"


@pytest.mark.ci_serial
def test_signing_cleanup_missing_certificate_does_not_follow_exported_path(tmp_path: Path) -> None:
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    msi = runner_temp / "Keylockertools-windows-x64.msi"
    msi.write_bytes(b"synthetic MSI; never executed")
    sentinel = tmp_path / "external-sentinel.p12"
    sentinel.write_bytes(b"outside signing cleanup")
    environment = {"RUNNER_TEMP": str(runner_temp), "SM_CLIENT_CERT_FILE": str(sentinel)}
    for _ in range(2):
        cleaned = _run_signing_material_step(
            "Remove DigiCert client authentication material", tmp_path, environment
        )
        assert cleaned.returncode == 0, cleaned.stderr
        assert sentinel.read_bytes() == b"outside signing cleanup"
        assert not msi.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive file handles prevent deletion")
@pytest.mark.ci_serial
def test_signing_cleanup_delete_failure_fails_the_step(tmp_path: Path) -> None:
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    certificate = runner_temp / "digicert-client-auth.p12"
    certificate.write_bytes(b"synthetic client certificate bytes")
    cleaned = _run_signing_material_step(
        "Remove DigiCert client authentication material",
        tmp_path,
        {"RUNNER_TEMP": str(runner_temp), "SM_CLIENT_CERT_FILE": str(certificate)},
        lock_certificate=True,
    )
    assert cleaned.returncode != 0
    assert certificate.read_bytes() == b"synthetic client certificate bytes"
    certificate.unlink()  # The helper must have released its owned exclusive handle.
