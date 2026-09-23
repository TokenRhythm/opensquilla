from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import runpy
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import pytest

ENTRY = Path(__file__).resolve().parents[2] / "desktop/electron/scripts/gateway-entry.py"


def isolated_environment(tmp_path: Path) -> dict[str, str]:
    profile = tmp_path / "profile"
    profile.mkdir()
    config = profile / "config.toml"
    config.write_text('[auth]\nmode = "none"\n', encoding="utf-8")
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
    diagnostics.append(f"migration ledger: {json.dumps(ledger)}")
    return "\n\n".join(diagnostics)


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
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    from opensquilla.tools import pty_backend

    handle = object()
    terminated = []
    waited = []

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


def test_pty_probe_drains_tail_after_process_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from opensquilla.tools import pty_backend

    handle = object()
    chunks = iter([b"opensquilla-pty-ok\n", b"late-tail\n", b""])

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


# Fresh-profile migrations and a real stdio server share the runner's process
# and disk budget. Keep the startup deadline independent of parallel test load.
@pytest.mark.ci_serial
def test_mcp_probe_uses_real_stdio_server_and_gateway(tmp_path: Path) -> None:
    from mcp_types import LATEST_PROTOCOL_VERSION

    environment = isolated_environment(tmp_path)
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
            deadline = time.monotonic() + 40
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while time.monotonic() < deadline:
                assert gateway.poll() is None, _gateway_startup_diagnostics(tmp_path)
                try:
                    with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                        if json.load(response).get("ok"):
                            break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(0.1)
            else:
                pytest.fail(_gateway_startup_diagnostics(tmp_path))

            frozen_probe = os.environ.get("OPENSQUILLA_TEST_FROZEN_MCP_PROBE")
            probe_command = [frozen_probe] if frozen_probe else [sys.executable, str(ENTRY)]
            result = subprocess.run(
                [*probe_command, "--_desktop-mcp-probe",
                 f"ws://127.0.0.1:{port}/ws"],
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
        finally:
            gateway.terminate()
            try:
                gateway.wait(timeout=10)
            except subprocess.TimeoutExpired:
                gateway.kill()
                gateway.wait(timeout=10)
