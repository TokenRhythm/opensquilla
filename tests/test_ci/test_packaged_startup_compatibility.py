from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github/scripts/verify-packaged-startup-compatibility.py"
)
_SPEC = importlib.util.spec_from_file_location("packaged_startup_compatibility", _SCRIPT)
assert _SPEC and _SPEC.loader
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


@pytest.mark.parametrize(("source", "value"), probe.CASES)
def test_probe_isolates_every_input_from_inherited_settings(
    source: str, value: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_RECOVERY_OFFLINE", "1")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", "real-profile-must-not-be-used")
    monkeypatch.setenv(probe.AUTO_SETUP_ENV, "inherited-must-not-win")
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-secret-must-not-leak")
    monkeypatch.setenv("PYTHONPATH", "unrelated-source-must-not-be-loaded")
    case = probe.prepare_case(tmp_path, Path(sys.executable), source, value)
    env = case["env"]

    assert "OPENSQUILLA_RECOVERY_OFFLINE" not in env
    assert "OPENAI_API_KEY" not in env
    assert "PYTHONPATH" not in env
    for key in ("HOME", "USERPROFILE", "OPENSQUILLA_STATE_DIR"):
        assert env[key] == str(case["profile"])
    assert env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] == str(case["config"])
    assert env.get(probe.AUTO_SETUP_ENV) == (value if source == "process-env" else None)
    env_file = case["profile"] / ".env"
    assert env_file.exists() is (source == "profile-dotenv")
    if env_file.exists():
        assert env_file.read_text(encoding="utf-8") == f"{probe.AUTO_SETUP_ENV}={value}\n"
    config = tomllib.loads(case["config"].read_text(encoding="utf-8"))
    assert config["sandbox"].get("auto_setup") == (
        value == "true" if source == "toml" else None
    )


def test_probe_refuses_existing_profile_without_modifying_it(tmp_path: Path) -> None:
    workdir = tmp_path / "existing-profile"
    workdir.mkdir()
    config = workdir / "config.toml"
    config.write_text("existing profile contents", encoding="utf-8")
    output = tmp_path / "result.json"

    with pytest.raises(FileExistsError, match="existing paths are refused"):
        probe.run(Path(sys.executable), workdir, output)

    assert config.read_text(encoding="utf-8") == "existing profile contents"
    assert list(workdir.iterdir()) == [config]
    assert not output.exists()


def test_probe_refuses_to_overwrite_existing_evidence(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    output.write_text("existing evidence", encoding="utf-8")
    workdir = tmp_path / "new-profile"

    with pytest.raises(FileExistsError, match="existing paths are refused"):
        probe.run(Path(sys.executable), workdir, output)

    assert output.read_text(encoding="utf-8") == "existing evidence"
    assert not workdir.exists()


@pytest.fixture
def fake_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Use a real child and loopback HTTP to test the controller's exit contract."""
    script = tmp_path / "fake-gateway.py"
    script.write_text(textwrap.dedent("""\
        import os
        import sys
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, HTTPServer

        mode = os.environ["FAKE_GATEWAY_MODE"]
        if mode == "early-exit":
            raise SystemExit(17)
        port = int(sys.argv[sys.argv.index("--port") + 1])
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
            def do_POST(self):
                self.send_response(202)
                self.end_headers()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
        server = HTTPServer(("127.0.0.1", port), Handler)
        server.serve_forever(poll_interval=0.05)
        server.server_close()
        if mode == "hang-on-exit":
            time.sleep(60)
        raise SystemExit(17 if mode == "bad-exit" else 0)
        """), encoding="utf-8")
    real_popen = subprocess.Popen
    children = []

    def launch(argv, **kwargs):
        child = real_popen([sys.executable, str(script), *argv[1:]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    yield children
    for child in children:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.parametrize("mode", ["healthy", "early-exit", "bad-exit", "hang-on-exit"])
def test_probe_requires_ready_and_graceful_exit_without_force(
    mode: str, fake_gateway, tmp_path: Path
) -> None:
    case = probe.prepare_case(tmp_path, Path(sys.executable), "clean", None)
    case["env"]["FAKE_GATEWAY_MODE"] = mode

    result = probe.boot_gateway(Path(sys.executable), case, 1, timeout=3)

    assert result["ok"] is (mode == "healthy")
    assert result["forced_cleanup"] is (mode == "hang-on-exit")
    assert len(fake_gateway) == 1
    assert fake_gateway[0].poll() is not None
    if mode == "healthy":
        assert result["readiness"] == dict.fromkeys(probe.READINESS_PATHS, 200)
        assert result["shutdown_status"] == 202
        assert result["exit_code"] == 0
    elif mode in {"early-exit", "bad-exit"}:
        assert result["exit_code"] == 17
        assert "error" in result
