from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_INVALID_API_KEY = "sk-test-invalid-issue-1201"
_REQUEST_START_TIMEOUT_SECONDS = 60.0
_POST_RESPONSE_EXIT_TIMEOUT_SECONDS = 5.0


def _isolated_agent_env(tmp_path: Path, base_url: str) -> dict[str, str]:
    inherited_keys = (
        "LANG",
        "LC_ALL",
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TMPDIR",
        "TEMP",
        "TMP",
    )
    env = {key: os.environ[key] for key in inherited_keys if key in os.environ}
    if os.name == "nt":
        windows_home = tmp_path / "windows-home"
        windows_home.mkdir()
        env["USERPROFILE"] = str(windows_home)
    project_root = Path(__file__).resolve().parents[2]
    env.update(
        {
            "OPENSQUILLA_HOME": str(tmp_path / "home"),
            "OPENSQUILLA_STATE_DIR": str(tmp_path / "state"),
            "OPENSQUILLA_USER_STATE_DIR": str(tmp_path / "user-state"),
            "OPENSQUILLA_TEST_PROFILE_LOCK_ROOT": "1",
            "OPENSQUILLA_LOG_DIR": str(tmp_path / "logs"),
            "OPENSQUILLA_LOG_FILE_ENABLED": "false",
            "OPENSQUILLA_LLM_PROVIDER": "openai",
            "OPENSQUILLA_LLM_MODEL": "gpt-4.1-mini",
            "OPENSQUILLA_LLM_API_KEY": _INVALID_API_KEY,
            "OPENSQUILLA_LLM_BASE_URL": base_url,
            "OPENSQUILLA_SQUILLA_ROUTER_ENABLED": "false",
            "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
            "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
            "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY": "true",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(project_root / "src"),
        }
    )
    return env


def _stop_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.terminate()
        try:
            return process.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.communicate(timeout=5.0)


def test_invalid_api_key_exits_promptly_with_nonzero_status(tmp_path: Path) -> None:
    response_sent = threading.Event()
    request_count = 0
    request_count_lock = threading.Lock()

    class InvalidKeyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            nonlocal request_count
            content_length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(content_length)
            with request_count_lock:
                request_count += 1

            body = json.dumps(
                {
                    "error": {
                        "message": "Incorrect API key provided.",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                }
            ).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            response_sent.set()

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), InvalidKeyHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    env = _isolated_agent_env(tmp_path, f"http://{host}:{port}/v1")
    assert env["OPENSQUILLA_TEST_PROFILE_LOCK_ROOT"] == "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from opensquilla.cli.main import app; app()",
            "agent",
            "-m",
            "Reply with INVALID_KEY_TEST",
            "--json",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        if not response_sent.wait(timeout=_REQUEST_START_TIMEOUT_SECONDS):
            stdout, stderr = _stop_process(process)
            pytest.fail(
                "agent CLI did not reach the local provider within the startup limit\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            stdout, stderr = process.communicate(timeout=_POST_RESPONSE_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            stdout, stderr = _stop_process(process)
            pytest.fail(
                "agent CLI remained alive after the provider returned HTTP 401\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
    finally:
        if process.poll() is None:
            _stop_process(process)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)

    payload = json.loads(stdout)
    assert request_count == 1
    assert process.returncode == 1
    assert payload["status"] == "error"
    assert payload["errors"] == [
        {
            "message": "The model provider rejected the configured credentials.",
            "code": "401",
        }
    ]
    assert _INVALID_API_KEY not in stdout
    assert _INVALID_API_KEY not in stderr
