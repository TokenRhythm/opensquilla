from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import runpy
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

ENTRY = Path(__file__).resolve().parents[2] / "desktop/electron/scripts/gateway-entry.py"
MIGRATIONS_DIR = ENTRY.parents[3] / "migrations"
# Match Desktop's bounded cold-start contract, including its late-ready window.
# This transport smoke is not a separate 40-second performance gate.
GATEWAY_STARTUP_TIMEOUT_SECONDS = 120


def isolated_environment(tmp_path: Path) -> dict[str, str]:
    profile = tmp_path / "profile"
    profile.mkdir()
    config = profile / "config.toml"
    # These probes exercise Desktop/MCP transport, not semantic memory search.
    # Keep the fresh-profile startup budget focused on the transport under test
    # instead of downloading/loading a local embedding backend on Windows CI.
    config.write_text(
        '[auth]\nmode = "none"\n'
        '[memory]\nretrieval_mode = "fts_only"\n'
        '[memory.embedding]\nprovider = "none"\n',
        encoding="utf-8",
    )
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("OPENSQUILLA_")}
    environment.update({
        "HOME": str(profile), "USERPROFILE": str(profile),
        "OPENSQUILLA_STATE_DIR": str(profile),
        "OPENSQUILLA_GATEWAY_CONFIG_PATH": str(config),
        "OPENSQUILLA_DESKTOP": "1",
        "OPENSQUILLA_INSTALL_METHOD": "desktop",
        "NO_PROXY": "127.0.0.1,localhost,::1",
    })
    return environment


def _gateway_startup_diagnostics(tmp_path: Path) -> str:
    """Inspect only this probe's synthetic profile after startup has failed."""
    diagnostics = []
    for relative in ("gateway.log", "profile/logs/debug.log"):
        try:
            with (tmp_path / relative).open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 16_384))
                tail = stream.read(16_384).decode("utf-8", errors="replace")
        except OSError as exc:
            tail = type(exc).__name__
        diagnostics.append(f"{relative} (last 16 KiB):\n{tail}")

    database = tmp_path / "profile" / "state" / "sessions.db"
    try:
        with closing(sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1,
        )) as connection:
            count = connection.execute("SELECT COUNT(*) FROM _yoyo_migration").fetchone()[0]
            latest = connection.execute(
                "SELECT migration_id FROM _yoyo_migration "
                "ORDER BY applied_at_utc DESC, migration_id DESC LIMIT 8"
            ).fetchall()
        ledger = {"applied_count": count, "latest_ids": [row[0] for row in latest]}
    except sqlite3.Error as exc:
        ledger = {"error_type": type(exc).__name__,
                  "error_code": getattr(exc, "sqlite_errorname", None)}
    diagnostics.append(
        "migration ledger (read-only diagnostic; a live migration may hold the writer lock): "
        f"{json.dumps(ledger)}"
    )
    return "\n\n".join(diagnostics)


def _prepare_migrated_profile(environment: dict[str, str]) -> None:
    """Seed the isolated Gateway profile before timing the MCP bridge startup."""

    from opensquilla.persistence.migrator import apply_pending

    database = Path(environment["OPENSQUILLA_STATE_DIR"]) / "state" / "sessions.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    assert apply_pending(str(database), MIGRATIONS_DIR)


@contextmanager
def _running_gateway(tmp_path: Path, environment: dict[str, str]):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log_path = tmp_path / "gateway.log"
    with log_path.open("w", encoding="utf-8") as log:
        gateway = subprocess.Popen(
            [sys.executable, str(ENTRY), "gateway", "run", "--bind", "127.0.0.1",
             "--port", str(port), "--config", environment["OPENSQUILLA_GATEWAY_CONFIG_PATH"]],
            env=environment, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            yield gateway, port
        finally:
            gateway.terminate()
            try:
                gateway.wait(timeout=10)
            except subprocess.TimeoutExpired:
                gateway.kill()
                gateway.wait(timeout=10)


def _wait_for_gateway_healthz(gateway: subprocess.Popen, port: int, tmp_path: Path) -> None:
    deadline = time.monotonic() + GATEWAY_STARTUP_TIMEOUT_SECONDS
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        assert gateway.poll() is None, _gateway_startup_diagnostics(tmp_path)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with opener.open(
                f"http://127.0.0.1:{port}/healthz", timeout=min(1, remaining),
            ) as response:
                if json.load(response).get("ok") and time.monotonic() < deadline:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    pytest.fail(_gateway_startup_diagnostics(tmp_path))


def test_gateway_probe_matches_desktop_startup_budget() -> None:
    lifecycle = ENTRY.parents[1] / "src" / "gateway-lifecycle.ts"
    match = re.search(
        r"export const DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS = ([\d_]+)",
        lifecycle.read_text(encoding="utf-8"),
    )
    assert match is not None
    assert int(match[1].replace("_", "")) == GATEWAY_STARTUP_TIMEOUT_SECONDS * 1000


@pytest.mark.parametrize("ready_after", [45, GATEWAY_STARTUP_TIMEOUT_SECONDS, None])
def test_gateway_probe_keeps_late_readiness_bounded(tmp_path, monkeypatch, ready_after):
    clock = SimpleNamespace(now=0.0)

    def advance(seconds):
        clock.now += seconds

    def probe(_url, *, timeout):
        assert 0 < timeout <= min(1, GATEWAY_STARTUP_TIMEOUT_SECONDS - clock.now)
        advance(timeout)
        if ready_after is not None and clock.now >= ready_after:
            return StringIO('{"ok": true}')
        raise OSError("synthetic startup still in progress")

    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(time, "sleep", advance)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: SimpleNamespace(open=probe))
    monkeypatch.setattr(
        sys.modules[__name__], "_gateway_startup_diagnostics", lambda _: "synthetic timeout",
    )
    gateway = SimpleNamespace(poll=lambda: None)
    if ready_after is None or ready_after >= GATEWAY_STARTUP_TIMEOUT_SECONDS:
        with pytest.raises(pytest.fail.Exception, match="synthetic timeout"):
            _wait_for_gateway_healthz(gateway, 1, tmp_path)
        assert clock.now == GATEWAY_STARTUP_TIMEOUT_SECONDS
    else:
        _wait_for_gateway_healthz(gateway, 1, tmp_path)
        assert ready_after <= clock.now < ready_after + 2


def test_migration_busy_diagnostic_does_not_mean_migration_failed(tmp_path):
    database = tmp_path / "profile" / "state" / "sessions.db"
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("CREATE TABLE _yoyo_migration (migration_id TEXT, applied_at_utc TEXT)")
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO _yoyo_migration VALUES ('V001__synthetic', '2026-01-01')")
        # The same healthy writer produces SQLITE_BUSY in the separate readonly
        # diagnostic while committing a migration; it does not fail or roll back.
        assert '"error_code": "SQLITE_BUSY"' in _gateway_startup_diagnostics(tmp_path)
        writer.commit()
    assert '"applied_count": 1' in _gateway_startup_diagnostics(tmp_path)


def test_document_probe_extracts_text_and_renders_a_decodable_image(tmp_path: Path) -> None:
    from reportlab.pdfgen.canvas import Canvas

    document = tmp_path / "document.pdf"
    canvas = Canvas(str(document), pagesize=(612, 792))
    canvas.drawString(72, 720, "Packaged document text")
    canvas.save()
    result = subprocess.run(
        [sys.executable, str(ENTRY), "--_desktop-document-probe", str(document)],
        env=isolated_environment(tmp_path), text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "probe": "opensquilla-desktop-document", "pages": 1,
        "text": "Packaged document text", "imageMime": "image/png",
        "imageSize": [1224, 1584],
    }


def test_document_probe_rejects_invalid_pdf(tmp_path: Path) -> None:
    document = tmp_path / "invalid.pdf"
    document.write_bytes(b"This is not a PDF")
    result = subprocess.run(
        [sys.executable, str(ENTRY), "--_desktop-document-probe", str(document)],
        env=isolated_environment(tmp_path), text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "document extraction or image rendering failed" in result.stderr


@pytest.mark.platform_pty
def test_pty_probe_reports_real_tty_when_backend_is_installed(tmp_path: Path) -> None:
    module_name = "winpty" if os.name == "nt" else "ptyprocess"
    if importlib.util.find_spec(module_name) is None:
        pytest.skip(f"{module_name} is not installed in this core test environment")

    result = subprocess.run(
        [sys.executable, str(ENTRY), "--_desktop-pty-probe"],
        env=isolated_environment(tmp_path), text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "probe": "opensquilla-desktop-pty",
        "available": True,
        "ioMode": "pty",
        "returncode": 0,
    }


@pytest.mark.parametrize("failure", ["read", "timeout", "initialization"])
def test_pty_probe_cleans_child_after_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str,
    tmp_path: Path,
) -> None:
    from opensquilla.tools import pty_backend

    handle = object()
    terminated = []
    waited = []
    monkeypatch.setenv("CI_REPORT_DIR", str(tmp_path))

    def spawn(*args, **kwargs):
        if failure == "initialization":
            raise pty_backend.PtyBackendError(
                "probe initialization failed", started=True, handle=handle
            )
        return handle

    async def read(current):
        assert current is handle
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("probe read failed")

    async def terminate(current):
        terminated.append(current)

    async def wait(current):
        waited.append(current)
        return 0

    monkeypatch.setattr(pty_backend, "spawn_pty", spawn)
    monkeypatch.setattr(pty_backend, "read_pty", read)
    monkeypatch.setattr(pty_backend, "terminate_pty", terminate)
    monkeypatch.setattr(pty_backend, "wait_pty", wait)
    namespace = runpy.run_path(str(ENTRY))
    probe = namespace["_run_desktop_pty_probe"]
    monkeypatch.setitem(
        probe.__globals__, "_DESKTOP_PTY_PROBE_TIMEOUT_SECONDS",
        1.0 if failure == "read" else 0.01,
    )

    assert probe() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["available"] is False
    assert result["reason"] == {
        "read": "probe read failed",
        "timeout": "PTY probe timed out",
        "initialization": "probe initialization failed",
    }[failure]
    assert terminated == [handle]
    assert waited == [handle]
    diagnostic = json.loads((tmp_path / "pty-probe-failures.jsonl").read_text())
    assert diagnostic["phase"] in {
        "read": {"read"}, "timeout": {"read", "post-exit-drain"},
        "initialization": {"spawn"},
    }[failure]
    assert diagnostic["cleanupAttempted"] is True
    assert diagnostic["cleanupCompleted"] is True
    assert diagnostic["outputBytes"] == 0
    assert diagnostic["outputChunks"] == 0
    assert diagnostic["firstOutputElapsedMs"] is None
    assert diagnostic["lastOutputElapsedMs"] is None
    assert diagnostic["sawTtyMarker"] is False
    assert diagnostic["sawTtyMissMarker"] is False
    assert diagnostic["sawCursorPositionRequest"] is False
    assert diagnostic["sawDeviceStatusRequest"] is False
    assert diagnostic["errorTypes"][0] == {
        "read": "RuntimeError", "timeout": "TimeoutError", "initialization": "PtyBackendError",
    }[failure]


def test_pty_probe_diagnostic_keeps_only_safe_state_and_preserves_attempts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    from opensquilla.tools import pty_backend

    monkeypatch.setenv("CI_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("SYNTHETIC_SECRET", "do-not-record-this-value")
    handle = object()
    monkeypatch.setattr(pty_backend, "spawn_pty", lambda *args, **kwargs: handle)

    async def terminate(current):
        stopped.set()

    async def wait(current):
        await stopped.wait()
        return 0

    monkeypatch.setattr(pty_backend, "wait_pty", wait)
    monkeypatch.setattr(pty_backend, "terminate_pty", terminate)
    namespace = runpy.run_path(str(ENTRY))
    output_attempts = [
        [
            b"\x1b", b"[6", b"n", b"\x1b[5", b"n",
            b"opensquilla-pty-mi", b"ss",
            b"opensquilla-pty-o", b"k\n", b"private terminal text",
        ],
        [b"[6n [5n ", b"opensquilla-pty-o", b"k\n", b"private terminal text"],
    ]
    for output in output_attempts:
        stopped = asyncio.Event()
        chunks = iter(output)

        async def read(current):
            chunk = next(chunks, None)
            if chunk is None:
                raise RuntimeError("synthetic failure")
            return chunk

        monkeypatch.setattr(pty_backend, "read_pty", read)
        assert namespace["_run_desktop_pty_probe"]() == 1
    assert len(capsys.readouterr().out.splitlines()) == 2
    payload = (tmp_path / "pty-probe-failures.jsonl").read_text()
    attempts = [json.loads(line) for line in payload.splitlines()]
    assert len(attempts) == 2
    assert all(row["sawTtyMarker"] for row in attempts)
    assert [row["sawTtyMissMarker"] for row in attempts] == [True, False]
    assert [row["sawCursorPositionRequest"] for row in attempts] == [True, False]
    assert [row["sawDeviceStatusRequest"] for row in attempts] == [True, False]
    for row, output in zip(attempts, output_attempts, strict=True):
        assert row["outputBytes"] == len(b"".join(output))
        assert row["outputChunks"] == len(output)
        assert 0 <= row["firstOutputElapsedMs"] <= row["lastOutputElapsedMs"]
        assert row["lastOutputElapsedMs"] <= row["elapsedBeforeCleanupMs"]
    assert all(row["phase"] == "read" for row in attempts)
    assert all(row["waitCompletedBeforeCleanup"] is False for row in attempts)
    assert "private terminal text" not in payload
    assert "do-not-record-this-value" not in payload


def test_pty_probe_drains_tail_after_process_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    from opensquilla.tools import pty_backend

    handle = object()
    chunks = iter([b"opensquilla-pty-ok\n", b"late-tail\n", b""])
    monkeypatch.setenv("CI_REPORT_DIR", str(tmp_path))

    monkeypatch.setattr(pty_backend, "spawn_pty", lambda *args, **kwargs: handle)

    async def read(current):
        assert current is handle
        return next(chunks)

    async def wait(current):
        assert current is handle
        return 0

    monkeypatch.setattr(pty_backend, "read_pty", read)
    monkeypatch.setattr(pty_backend, "wait_pty", wait)
    monkeypatch.setattr(pty_backend, "terminate_pty", lambda current: asyncio.sleep(0))
    namespace = runpy.run_path(str(ENTRY))
    probe = namespace["_run_desktop_pty_probe"]

    assert probe() == 0
    assert json.loads(capsys.readouterr().out) == {
        "probe": "opensquilla-desktop-pty",
        "available": True,
        "ioMode": "pty",
        "returncode": 0,
    }
    assert not (tmp_path / "pty-probe-failures.jsonl").exists()


@pytest.mark.parametrize(("output", "returncode", "available"), [
    (b"opensquilla-pty-ok\n", 0, True),
    (b"opensquilla-pty-ok\n", 7, False),
    (b"opensquilla-pty-miss\n", 0, False),
])
def test_pty_probe_accepts_eof_during_post_exit_drain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    output: bytes, returncode: int, available: bool,
) -> None:
    from opensquilla.tools import pty_backend

    handle = object()
    draining = asyncio.Event()
    terminated = []
    reads = 0
    wait_for = asyncio.wait_for

    async def read(current):
        nonlocal reads
        assert current is handle
        reads += 1
        if reads == 1:
            return output
        # Hold EOF until the probe takes the process-exited/reader-pending
        # branch, rather than depending on executor or event-loop scheduling.
        await draining.wait()
        raise EOFError

    async def wait(current):
        assert current is handle
        return returncode

    async def drain(awaitable, timeout):
        draining.set()
        return await wait_for(awaitable, timeout)

    async def terminate(current):
        terminated.append(current)

    monkeypatch.setattr(pty_backend, "spawn_pty", lambda *args, **kwargs: handle)
    monkeypatch.setattr(pty_backend, "read_pty", read)
    monkeypatch.setattr(pty_backend, "wait_pty", wait)
    monkeypatch.setattr(pty_backend, "terminate_pty", terminate)
    monkeypatch.setattr(asyncio, "wait_for", drain)
    namespace = runpy.run_path(str(ENTRY))

    assert namespace["_run_desktop_pty_probe"]() == (0 if available else 1)
    assert draining.is_set()
    assert terminated == [handle]
    assert json.loads(capsys.readouterr().out) == {
        "probe": "opensquilla-desktop-pty",
        "available": available,
        "ioMode": "pty" if b"opensquilla-pty-ok" in output else "pipe",
        "returncode": returncode,
    }


def test_pty_probe_waits_for_process_when_reader_eof_arrives_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from opensquilla.tools import pty_backend

    handle = object()
    chunks = iter([b"opensquilla-pty-ok\n", b""])

    monkeypatch.setattr(pty_backend, "spawn_pty", lambda *args, **kwargs: handle)

    async def read(current):
        assert current is handle
        return next(chunks)

    async def wait(current):
        assert current is handle
        await asyncio.sleep(0.01)
        return 0

    monkeypatch.setattr(pty_backend, "read_pty", read)
    monkeypatch.setattr(pty_backend, "wait_pty", wait)
    monkeypatch.setattr(pty_backend, "terminate_pty", lambda current: asyncio.sleep(0))
    namespace = runpy.run_path(str(ENTRY))
    probe = namespace["_run_desktop_pty_probe"]

    assert probe() == 0
    assert json.loads(capsys.readouterr().out)["returncode"] == 0


@pytest.mark.ci_serial
def test_gateway_fresh_profile_reaches_healthz(tmp_path: Path) -> None:
    """Keep cold CLI startup/migration coverage separate from MCP assertions."""

    environment = isolated_environment(tmp_path)
    with _running_gateway(tmp_path, environment) as (gateway, port):
        _wait_for_gateway_healthz(gateway, port, tmp_path)


@pytest.mark.ci_serial
def test_mcp_probe_uses_real_stdio_server_and_gateway(tmp_path: Path) -> None:
    from mcp_types import LATEST_PROTOCOL_VERSION

    environment = isolated_environment(tmp_path)
    # Cold migration is covered by the dedicated smoke above; bridge timing
    # starts from the same latest-schema profile for source and frozen probes.
    _prepare_migrated_profile(environment)
    with _running_gateway(tmp_path, environment) as (gateway, port):
        _wait_for_gateway_healthz(gateway, port, tmp_path)

        frozen_probe = os.environ.get("OPENSQUILLA_TEST_FROZEN_MCP_PROBE")
        probe_command = [frozen_probe] if frozen_probe else [sys.executable, str(ENTRY)]
        result = subprocess.run(
            [*probe_command, "--_desktop-mcp-probe", f"ws://127.0.0.1:{port}/ws"],
            env=environment, text=True, capture_output=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "probe": "opensquilla-desktop-mcp", "sessions": 0,
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "tools": ["conversations_list", "events_wait", "messages_read",
                      "messages_send", "session_resolve", "transcript_export"],
            "resources": ["opensquilla://sessions"],
        }
