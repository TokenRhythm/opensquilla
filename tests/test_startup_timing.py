from __future__ import annotations

import builtins
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla import startup_timing as timing

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "desktop/electron/scripts/pyinstaller_runtime_hooks/ensure_ca_trust.py"
ENTRY = ROOT / "desktop/electron/scripts/gateway-entry.py"


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    captured = capsys.readouterr()
    assert captured.out == ""
    return [json.loads(line) for line in captured.err.splitlines()]


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timing, "_ENABLED", True)


def test_disabled_timing_never_reads_clocks_or_writes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(timing, "_ENABLED", False)
    monkeypatch.setattr(timing, "_monotonic_ns", lambda: pytest.fail("clock used"))
    assert timing.startup_phase_start("profile_lock") is None
    timing.startup_phase_end("profile_lock", 1)
    assert _events(capsys) == []


@pytest.mark.parametrize("value", [None, "0", "true", " 1 ", "secret-diagnostic-value"])
def test_only_explicit_process_flag_enables_timing(monkeypatch, capsys, value) -> None:
    if value is None:
        monkeypatch.delenv("OPENSQUILLA_STARTUP_TIMING", raising=False)
    else:
        monkeypatch.setenv("OPENSQUILLA_STARTUP_TIMING", value)
    namespace = runpy.run_path(str(ROOT / "src/opensquilla/startup_timing.py"))
    # Loading dotenv later must not silently turn the diagnostic on.
    monkeypatch.setenv("OPENSQUILLA_STARTUP_TIMING", "1")
    assert namespace["startup_phase_start"]("profile_lock") is None
    assert _events(capsys) == []


def test_fixed_private_safe_schema_and_process_local_duration(enabled, monkeypatch, capsys) -> None:
    clock = iter([2_000_000, 3_250_000])
    monkeypatch.setattr(timing, "_monotonic_ns", lambda: next(clock))
    monkeypatch.setattr(timing, "_wall_ns", lambda: 42_000_000)
    monkeypatch.setenv("OPENSQUILLA_API_KEY", "not-for-diagnostics")
    start = timing.startup_phase_start("profile_lock")
    timing.startup_phase_end("profile_lock", start)
    assert timing.startup_phase_start('secret-path"\n') is None
    timing.startup_phase_end("not-for-diagnostics", start)
    events = _events(capsys)
    assert [item["status"] for item in events] == ["start", "complete"]
    assert [item["duration_us"] for item in events] == [0, 1250]
    assert [item["monotonic_ns"] for item in events] == [2_000_000, 3_250_000]
    assert all(set(item) == {
        "event", "stage", "status", "pid", "at_unix_ms", "monotonic_ns", "duration_us",
    } for item in events)
    assert all(item["event"] == "gateway.startup_early" for item in events)
    assert all(item["stage"] == "profile_lock" for item in events)
    assert all(item["pid"] == timing.os.getpid() for item in events)
    assert all(item["at_unix_ms"] == 42 for item in events)


@pytest.mark.parametrize("clock", ["_monotonic_ns", "_wall_ns"])
def test_clock_failures_do_not_escape(enabled, monkeypatch, clock) -> None:
    def fail():
        raise OSError("private clock failure")

    monkeypatch.setattr(timing, clock, fail)
    start = timing.startup_phase_start("profile_lock")
    timing.startup_phase_end("profile_lock", start)
    timing.startup_phase_end("profile_lock", 1, failed=True)


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_stderr_failures_do_not_escape(enabled, monkeypatch, failure) -> None:
    class BrokenOutput:
        def write(self, _text):
            if failure == "write":
                raise OSError("private output failure")

        def flush(self):
            if failure == "flush":
                raise ValueError("closed output")

    monkeypatch.setattr(timing.sys, "stderr", BrokenOutput())
    start = timing.startup_phase_start("profile_lock")
    assert start is not None
    timing.startup_phase_end("profile_lock", start)


def test_ca_hook_records_original_work_once(enabled, monkeypatch, capsys) -> None:
    monkeypatch.setenv("OPENSQUILLA_STARTUP_TIMING", "1")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    calls = []

    def create_context():
        calls.append("ca")
        return SimpleNamespace(get_ca_certs=lambda **_kwargs: [b"synthetic-ca"])

    import ssl

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    runpy.run_path(str(HOOK))
    assert calls == ["ca"]
    events = _events(capsys)
    assert [(item["stage"], item["status"]) for item in events] == [
        ("diagnostic_setup", "complete"),
        ("frozen_hook_imports", "start"),
        ("frozen_hook_imports", "complete"),
        ("frozen_ca_trust", "start"),
        ("frozen_ca_trust", "complete"),
    ]


def test_ca_failure_remains_packaging_error_and_is_not_logged(enabled, monkeypatch, capsys):
    import ssl

    monkeypatch.setenv("OPENSQUILLA_STARTUP_TIMING", "1")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(ssl, "create_default_context", lambda: SimpleNamespace(
        get_ca_certs=lambda **_kwargs: [],
    ))
    # Both existing CA probes still execute; the missing store stays fatal.
    with pytest.raises(RuntimeError, match="could not initialize its packaged TLS trust store"):
        runpy.run_path(str(HOOK))
    events = _events(capsys)
    assert events[-1]["stage"] == "frozen_ca_trust"
    assert events[-1]["status"] == "failed"


@pytest.mark.parametrize("import_fails", [False, True])
def test_packaged_entry_brackets_real_cli_import_without_running_gateway(
    enabled, monkeypatch, capsys, import_fails,
) -> None:
    sentinel = ImportError("secret import path")
    original_import = builtins.__import__
    calls = []

    def intercept(name, *args, **kwargs):
        if name == "opensquilla.cli.main":
            calls.append("import")
            if import_fails:
                raise sentinel
            return SimpleNamespace(app=lambda: calls.append("app"))
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", intercept)
    monkeypatch.setattr(sys, "argv", ["synthetic-gateway", "gateway", "run"])
    if import_fails:
        with pytest.raises(ImportError) as caught:
            runpy.run_path(str(ENTRY), run_name="__main__")
        assert caught.value is sentinel
        assert calls == ["import"]
    else:
        runpy.run_path(str(ENTRY), run_name="__main__")
        assert calls == ["import", "app"]
    events = _events(capsys)
    assert [(item["stage"], item["status"]) for item in events] == [
        ("cli_import", "start"),
        ("cli_import", "failed" if import_fails else "complete"),
    ]
