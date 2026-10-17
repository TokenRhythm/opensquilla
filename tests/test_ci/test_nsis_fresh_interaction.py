from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def nsis():
    path = ROOT / ".github/scripts/verify-nsis-upgrade-regression.py"
    spec = importlib.util.spec_from_file_location("nsis_fresh_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def javascript_fixture():
    return _javascript_fixture()


def _javascript_fixture(*extra):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the cross-language probe contract")
    process = subprocess.run(
        [node, str(ROOT / "desktop/electron/scripts/test-packaged-first-send-evidence.mjs"),
         "--fixture-json", *extra],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    return json.loads(process.stdout)


def _validate(nsis, fixture):
    return nsis.fresh_interaction_result(
        json.dumps(fixture["result"]),
        desktop_source=fixture["desktopSource"].encode(),
        console_source=fixture["consoleSource"].encode(),
    )


def _bind_log_sources(fixture):
    for name, summary in (
        ("desktopSource", fixture["result"]["desktopLog"]),
        ("consoleSource", fixture["result"]["observation"]["journal"]),
    ):
        source = "\n".join(json.dumps(item) for item in summary["records"]) + "\n"
        fixture[name] = source
        summary["bytes"] = len(source.encode())
        summary["sha256"] = hashlib.sha256(source.encode()).hexdigest()


def test_javascript_expected_shutdown_cancellation_passes_python(nsis, javascript_fixture):
    result = _validate(nsis, javascript_fixture)
    assert result["renderer"]["consoleErrors"] > 0
    assert len(result["acceptance"]["consoleMatches"]) == result["renderer"]["consoleErrors"]


def test_delayed_playwright_receipt_does_not_change_verified_native_interval(
    nsis, javascript_fixture,
):
    fixture = copy.deepcopy(javascript_fixture)
    fixture["result"]["renderer"]["consoleErrorDetails"][0]["observedAt"] = "2026-09-17T00:01:00Z"
    _validate(nsis, fixture)


def test_subframe_history_without_console_errors_does_not_need_an_exception(nsis):
    fixture = _javascript_fixture("--no-cancellation")
    fixture["result"]["observation"]["subframePageIds"] = [1]
    _validate(nsis, fixture)


@pytest.mark.parametrize("mutation", [
    "old-report", "unsupported-acceptance", "incomplete-observation", "observation-error",
    "running-error", "other-page", "unknown-message", "missing-console", "duplicate-console",
    "other-frame", "other-window", "missing-journal-console", "wrong-match",
    "invalid-observed-time", "native-before-accepted", "native-after-exit",
    "source-mismatch", "page-error", "cleanup-failed",
    "truncated-desktop", "truncated-journal", "modified-desktop", "modified-journal",
    "incomplete-log", "malformed-log", "rotated-log", "missing-quit", "forced-quit",
    "journal-error", "journal-index", "main-running-error", "truncated-record",
    "noncanonical-source", "missing-commit", "reversed-quit",
    "missing-subframe-observation", "invalid-subframe-observation",
    "duplicate-subframe-observation", "previously-detached-frame", "other-page-had-frame",
    "frame-not-isolated", "missing-frame-isolation",
    "missing-observation-exit", "nonzero-observation-exit", "journal-write-error",
    "missing-write-errors", "invalid-exit-code", "invalid-write-errors",
    "duplicate-observation-exit", "console-after-observation-exit",
    "boolean-page-id", "noninteger-source-line", "boolean-match-index",
])
def test_python_rejects_unverified_report_even_when_ok_is_true(
    nsis, javascript_fixture, mutation,
):
    fixture = copy.deepcopy(javascript_fixture)
    result = fixture["result"]
    renderer, observation = result["renderer"], result["observation"]
    # Keep the report and preserved journal self-consistent for semantic negative
    # cases: these must fail classification, not merely an integrity mismatch.
    observation["mainConsoleRecords"] = [
        record for record in observation["journal"]["records"] if record["event"] == "console"
    ]
    console = renderer["consoleErrorDetails"][0]
    main = observation["mainConsoleRecords"][0]
    if mutation == "old-report":
        result.pop("schemaVersion")
    elif mutation == "unsupported-acceptance":
        result["acceptance"]["version"] = True
    elif mutation == "incomplete-observation":
        observation["completed"] = False
    elif mutation == "observation-error":
        observation["errors"].append("observer disconnected")
    elif mutation == "running-error":
        console["phase"] = "iteration-start"
    elif mutation == "other-page":
        console["pageId"] += 1
    elif mutation == "unknown-message":
        console["message"] = "unknown renderer error"
    elif mutation == "missing-console":
        renderer["consoleErrorDetails"].clear()
    elif mutation == "duplicate-console":
        renderer["consoleErrorDetails"].append(copy.deepcopy(console))
        renderer["consoleErrors"] += 1
    elif mutation == "other-frame":
        main["mainFrame"] = False
    elif mutation == "other-window":
        main["webContentsId"] += 1
    elif mutation == "missing-journal-console":
        observation["mainConsoleRecords"].clear()
    elif mutation == "wrong-match":
        result["acceptance"]["consoleMatches"][0]["desktopRecordIndex"] = 0
    elif mutation == "invalid-observed-time":
        console["observedAt"] = "invalid"
    elif mutation == "native-before-accepted":
        main["at"] = "2026-09-17T00:00:00.050Z"
    elif mutation == "native-after-exit":
        main["at"] = "2026-09-17T00:00:00.300Z"
    elif mutation == "source-mismatch":
        console["source"] = "opensquilla-app://desktop/other.js"
    elif mutation == "page-error":
        renderer["pageErrorDetails"].append({"message": "unhandled rejection"})
    elif mutation == "cleanup-failed":
        result["acceptance"]["cleanupSucceeded"] = False
    elif mutation == "truncated-desktop":
        fixture["desktopSource"] = fixture["desktopSource"].rstrip()
    elif mutation == "truncated-journal":
        fixture["consoleSource"] = fixture["consoleSource"].rstrip()
    elif mutation == "modified-desktop":
        fixture["desktopSource"] += "{}\n"
    elif mutation == "modified-journal":
        fixture["consoleSource"] += "{}\n"
    elif mutation == "incomplete-log":
        result["desktopLog"]["complete"] = False
    elif mutation == "malformed-log":
        result["desktopLog"]["malformedRecords"] = 1
    elif mutation == "rotated-log":
        result["desktopLog"]["rotated"] = True
    elif mutation == "missing-quit":
        result["desktopLog"]["eventCounts"].pop("before_quit")
    elif mutation == "forced-quit":
        for record in result["desktopLog"]["records"]:
            if record["event"] == "quit_gateway_exit":
                record["hardTerminated"] = True
    elif mutation == "journal-error":
        observation["journal"]["records"][0]["event"] = "observation-error"
    elif mutation == "journal-index":
        observation["journal"]["records"][0]["index"] = 4
    elif mutation == "main-running-error":
        main["phase"] = "running"
    elif mutation == "truncated-record":
        result["desktopLog"]["records"][0]["detail_omitted"] = True
    elif mutation == "noncanonical-source":
        console["source"] += "?untrusted=1"
        main["source"] = console["source"]
        for record in result["desktopLog"]["records"]:
            if record["event"] == "renderer_console":
                record["source"] = console["source"]
    elif mutation == "missing-commit":
        for record in result["desktopLog"]["records"]:
            if record["event"] == "desktop_exit_phase":
                record["to"] = "draining"
    elif mutation == "reversed-quit":
        records = result["desktopLog"]["records"]
        requested = next(i for i, r in enumerate(records)
                         if r["event"] == "quit_gateway_shutdown_requested")
        exited = next(i for i, r in enumerate(records) if r["event"] == "quit_gateway_exit")
        records[requested], records[exited] = records[exited], records[requested]
    elif mutation == "missing-subframe-observation":
        observation.pop("subframePageIds")
    elif mutation == "invalid-subframe-observation":
        observation["subframePageIds"] = [True]
    elif mutation == "duplicate-subframe-observation":
        observation["subframePageIds"] = [1, 1]
    elif mutation == "previously-detached-frame":
        # Electron may misattribute an iframe's delayed error to its main frame.
        # Sticky history must still reject it after the iframe has detached.
        observation["subframePageIds"] = [observation["targetPageId"]]
    elif mutation == "other-page-had-frame":
        observation["subframePageIds"] = [observation["targetPageId"] + 1]
    elif mutation == "frame-not-isolated":
        console["frameIsolationProven"] = False
    elif mutation == "missing-frame-isolation":
        console.pop("frameIsolationProven")
    elif mutation == "missing-observation-exit":
        observation["journal"]["records"].pop()
    elif mutation == "nonzero-observation-exit":
        observation["journal"]["records"][-1]["code"] = 1
    elif mutation == "journal-write-error":
        observation["journal"]["records"][-1]["writeErrors"] = 1
    elif mutation == "missing-write-errors":
        observation["journal"]["records"][-1].pop("writeErrors")
    elif mutation == "invalid-exit-code":
        observation["journal"]["records"][-1]["code"] = False
    elif mutation == "invalid-write-errors":
        observation["journal"]["records"][-1]["writeErrors"] = False
    elif mutation == "duplicate-observation-exit":
        records = observation["journal"]["records"]
        records.append({**records[-1], "index": len(records)})
    elif mutation == "console-after-observation-exit":
        records = observation["journal"]["records"]
        records[-1], records[-2] = records[-2], records[-1]
        for index, record in enumerate(records):
            record["index"] = index
    elif mutation == "boolean-page-id":
        console["pageId"] = True
    elif mutation == "noninteger-source-line":
        main["line"] = float(console["line"])
    elif mutation == "boolean-match-index":
        result["acceptance"]["consoleMatches"][0]["consoleIndex"] = False
    if mutation not in {
        "truncated-desktop", "truncated-journal", "modified-desktop", "modified-journal",
    }:
        _bind_log_sources(fixture)
    with pytest.raises(RuntimeError):
        _validate(nsis, fixture)


@pytest.mark.parametrize("failure", [
    "nonzero", "timeout", "missing-profile", "diagnostic-write", "zero-without-evidence",
])
def test_failed_probe_preserves_available_evidence_and_original_failure(
    nsis, tmp_path, monkeypatch, failure,
):
    audit = nsis.Audit.__new__(nsis.Audit)
    audit.user_data = tmp_path / "fresh-user-data"
    audit.install = tmp_path / "installed"
    audit.evidence = tmp_path / "evidence"
    audit.evidence.mkdir()
    audit.args = SimpleNamespace(node=sys.executable)
    audit.report = {"proofs": {}}
    audit.save = lambda: None
    desktop_bytes = b'{"event":"partial","at":"2026-09-17T00:00:00Z"}\n'

    def run(*_args):
        (audit.evidence / "fresh-first-interaction-stdout.log").write_bytes(b"partial stdout")
        (audit.evidence / "fresh-first-interaction-stderr.log").write_bytes(b"original stderr")
        if failure not in {"missing-profile", "zero-without-evidence"}:
            (audit.user_data / "logs").mkdir(parents=True)
            (audit.user_data / "logs/desktop.log").write_bytes(desktop_bytes)
            (audit.user_data / "logs/desktop.log.1").write_bytes(b"earlier evidence")
            (audit.user_data / "first-send-main-console.jsonl").write_bytes(b"partial journal")
        if failure == "timeout":
            raise RuntimeError("original watchdog timeout")
        return {"exitCode": 0 if failure == "zero-without-evidence" else 17}

    if failure == "diagnostic-write":
        def fail_copy(*_args):
            raise OSError("evidence volume unavailable")

        monkeypatch.setattr(nsis.shutil, "copyfile", fail_copy)

        def fail_save():
            raise OSError("result volume unavailable")

        audit.save = fail_save
    audit.run = run
    message = "original watchdog timeout" if failure == "timeout" else "probe failed: 17"
    if failure == "zero-without-evidence":
        message = "diagnostic evidence is incomplete"
    with pytest.raises(RuntimeError, match=message):
        audit.run_fresh_interaction(tmp_path / "child-temp")
    assert (audit.evidence / "fresh-first-interaction-stdout.log").read_bytes() == b"partial stdout"
    stderr = audit.evidence / "fresh-first-interaction-stderr.log"
    assert stderr.read_bytes() == b"original stderr"
    if failure not in {"missing-profile", "diagnostic-write", "zero-without-evidence"}:
        assert (audit.evidence / "fresh-interaction-desktop.log").read_bytes() == desktop_bytes
        rotated = audit.evidence / "fresh-interaction-desktop.log.1"
        journal = audit.evidence / "fresh-interaction-main-console.jsonl"
        assert rotated.read_bytes() == b"earlier evidence"
        assert journal.read_bytes() == b"partial journal"
    else:
        assert audit.report["freshInteractionDiagnostics"]["errors"]
    assert "freshInteraction" not in audit.report


def _candidate_arguments(tmp_path, *, case="baseline"):
    arguments = [
        "--case", case, "--install-path", "default", "--node", sys.executable,
        "--candidate-source-sha", "a" * 40, "--evidence-root", str(tmp_path / "evidence"),
    ]
    if case != "fresh":
        arguments += ["--baseline-installer", str(tmp_path / "baseline.exe"),
                      "--baseline-version", "0.5.3"]
    return arguments


def _direct_candidate_arguments(tmp_path):
    return [
        "--candidate-installer", str(tmp_path / "candidate.exe"),
        "--candidate-asar-sha256", "b" * 64,
        "--candidate-executable-sha256", "c" * 64,
        "--candidate-dependency-inventory-sha256", "d" * 64,
        "--candidate-installer-sha256", "e" * 64,
    ]


def test_direct_candidate_cli_retains_default_and_normalizes_windows_version(nsis, tmp_path):
    arguments = _candidate_arguments(tmp_path) + _direct_candidate_arguments(tmp_path)
    assert nsis.parse_args(arguments).candidate_version == "0.5.4"
    normalized = nsis.parse_args(arguments + ["--candidate-version", "0.6.1.0"])
    assert normalized.candidate_version == "0.6.1"


@pytest.mark.parametrize("version", ["", "0.6.1.1", "0.6.1-beta"])
def test_direct_candidate_cli_rejects_invalid_explicit_version(nsis, tmp_path, version):
    arguments = _candidate_arguments(tmp_path) + _direct_candidate_arguments(tmp_path)
    with pytest.raises(SystemExit):
        nsis.parse_args(arguments + ["--candidate-version", version])


@pytest.mark.parametrize("option,value", [
    ("--candidate-installer", "candidate.exe"),
    ("--candidate-version", "0.5.4"),
    ("--candidate-asar-sha256", "b" * 64),
    ("--candidate-executable-sha256", "c" * 64),
    ("--candidate-dependency-inventory-sha256", "d" * 64),
    ("--candidate-installer-sha256", "e" * 64),
])
def test_deferred_candidate_cli_rejects_mixed_identity(nsis, tmp_path, option, value):
    arguments = _candidate_arguments(tmp_path) + ["--candidate-artifact-name", "candidate"]
    assert nsis.parse_args(arguments).candidate_version is None
    with pytest.raises(SystemExit):
        nsis.parse_args(arguments + [option, value])


@pytest.mark.parametrize("extra", [[], ["--candidate-artifact-name", "  "]])
def test_candidate_cli_requires_exactly_one_complete_mode(nsis, tmp_path, extra):
    with pytest.raises(SystemExit):
        nsis.parse_args(_candidate_arguments(tmp_path) + extra)


def _preparation_audit(nsis, tmp_path, monkeypatch, *, case="baseline"):
    """Exercise the real orchestration without invoking installers or Windows APIs."""
    audit = nsis.Audit.__new__(nsis.Audit)
    audit.args = nsis.parse_args(_candidate_arguments(tmp_path, case=case)
                                 + ["--candidate-artifact-name", "candidate"])
    audit.evidence = tmp_path / "evidence"
    audit.evidence.mkdir()
    audit.root = tmp_path / "task"
    audit.root.mkdir()
    audit.install = audit.root / "installed"
    audit.short_temp = audit.root / "short-temp"
    audit.normal_temp = audit.root / "normal-temp"
    audit.user_data = audit.root / "profile"
    audit.sentinels = {}
    audit.report = {"inputs": {}, "proofs": {}}
    audit.save = lambda: None
    events = []
    monkeypatch.setattr(nsis, "installed_registry", lambda: [])
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    repository = tmp_path / "repository"
    monkeypatch.setattr(nsis, "REPOSITORY", repository)
    dist = repository / "desktop/electron/dist"
    dist.mkdir(parents=True)
    for name in ("desktop-gateway-ownership.js", "gateway-lifecycle.js"):
        (dist / name).write_text("fixture", encoding="utf-8")
    probe = repository / "probe.js"
    probe.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(nsis, "INTERACTION_PROBE", probe)
    monkeypatch.setattr(nsis, "FIRST_SEND_PROBE", probe)
    baseline = tmp_path / "baseline.exe"
    baseline.write_bytes(b"baseline fixture")
    monkeypatch.setitem(nsis.OFFICIAL_BASELINE_SHA256, "0.5.3", nsis.digest(baseline))
    audit.require_no_product_processes = lambda label: events.append(("processes", label))
    audit.seed_retained_profile = lambda: events.append(("seed",))
    audit.state = lambda label: events.append(("state", label)) or {"version": "0.5.3"}
    audit.assert_version = lambda state, version: events.append(("version", version))
    audit.check_profiles = lambda label: events.append(("profiles", label))
    audit.verify_retained_profile = lambda label: events.append(("retained", label))
    audit.check_environment_registry = lambda label: events.append(("registry", label))

    def run(label, *_args):
        events.append(("run", label))
        return {"exitCode": 0}

    audit.run = run
    return audit, events


@pytest.mark.parametrize("case", ["baseline", "readlock", "longpath"])
def test_deferred_candidate_waits_until_old_installation_is_verified(
    nsis, tmp_path, monkeypatch, case,
):
    audit, events = _preparation_audit(nsis, tmp_path, monkeypatch, case=case)

    def resolve():
        events.append(("resolve",))
        raise RuntimeError("candidate handoff reached")

    audit.resolve_candidate = resolve
    with pytest.raises(RuntimeError, match="candidate handoff reached"):
        audit.execute()
    assert events == [
        ("processes", "initial"), ("seed",), ("run", "install-baseline"),
        ("processes", "afterBaseline"), ("state", "baseline-installed"),
        ("version", "0.5.3"), ("profiles", "afterBaseline"),
        ("retained", "afterBaseline"), ("registry", "afterBaseline"), ("resolve",),
    ]
    assert len(audit.sentinels) == 3


def _downloaded_candidate(tmp_path):
    installer = tmp_path / "candidate.exe"
    installer.write_bytes(b"candidate fixture")
    manifest = {
        "sourceSha": "a" * 40, "version": "0.6.1.0",
        "installerSha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
        "asarSha256": "b" * 64, "executableSha256": "c" * 64,
        "dependencyInventorySha256": "d" * 64,
    }
    return installer, manifest


def test_deferred_fresh_candidate_does_not_install_or_seed_baseline(nsis, tmp_path, monkeypatch):
    audit, events = _preparation_audit(nsis, tmp_path, monkeypatch, case="fresh")
    installer, manifest = _downloaded_candidate(tmp_path)

    def wait(artifact_name, destination, source_sha):
        assert (artifact_name, destination, source_sha) == (
            "candidate", audit.evidence / "candidate", "a" * 40,
        )
        events.append(("wait",))
        return installer, manifest

    monkeypatch.setattr(nsis, "wait_for_candidate", wait)
    audit.execute_fresh = lambda candidate: events.append(("fresh", candidate))
    audit.execute()
    assert events == [("processes", "initial"), ("wait",), ("fresh", installer)]
    assert not audit.sentinels
    assert audit.args.candidate_version == audit.report["candidateVersion"] == "0.6.1"
    assert audit.args.candidate_installer == str(installer)
    assert audit.args.candidate_asar_sha256 == manifest["asarSha256"]
    assert audit.args.candidate_executable_sha256 == manifest["executableSha256"]
    assert audit.args.candidate_dependency_inventory_sha256 == manifest["dependencyInventorySha256"]
    assert audit.report["inputs"]["candidateInstallerSha256"] == manifest["installerSha256"]
    assert audit.report["candidateManifest"] == manifest


@pytest.mark.parametrize("field,value,error", [
    ("sourceSha", "f" * 40, "Candidate source mismatch"),
    ("installerSha256", "f" * 64, "Candidate installer differs"),
    ("dependencyInventorySha256", None, "valid SHA-256"),
    ("version", "0.6.1-beta", "Versions must be stable"),
])
def test_deferred_identity_failure_prevents_fault_injection_and_upgrade(
    nsis, tmp_path, monkeypatch, field, value, error,
):
    audit, events = _preparation_audit(nsis, tmp_path, monkeypatch, case="readlock")
    installer, manifest = _downloaded_candidate(tmp_path)
    manifest[field] = value
    monkeypatch.setattr(nsis, "wait_for_candidate", lambda *_args: (installer, manifest))
    with pytest.raises((RuntimeError, ValueError), match=error):
        audit.execute()
    assert [event for event in events if event[0] == "run"] == [
        ("run", "install-baseline"),
    ]
    assert events[-1] == ("registry", "afterBaseline")
    assert audit.args.candidate_installer is None
    assert "fault" not in audit.report


def test_direct_candidate_identity_failure_precedes_baseline_installation(
    nsis, tmp_path, monkeypatch,
):
    audit, events = _preparation_audit(nsis, tmp_path, monkeypatch)
    arguments = _candidate_arguments(tmp_path) + _direct_candidate_arguments(tmp_path)
    audit.args = nsis.parse_args(arguments)
    _downloaded_candidate(tmp_path)
    with pytest.raises(RuntimeError, match="Candidate installer differs"):
        audit.execute()
    assert events == [("processes", "initial")]
    assert not audit.sentinels
