"""Exercise actual Gateway assembly around the external MCP boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", ["configured", "disabled", "empty", "default"])
def test_gateway_mcp_boot_dispatch_and_shutdown(mode: str, tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(repo_root / "src"),
            "OPENSQUILLA_HOME": str(tmp_path / "profiles"),
            "OPENSQUILLA_PROFILE": "default",
            "OPENSQUILLA_STATE_DIR": str(tmp_path / "state"),
            "OPENSQUILLA_USER_STATE_DIR": str(tmp_path / "user-state"),
            "OPENSQUILLA_LOG_DIR": str(tmp_path / "logs"),
            "OPENSQUILLA_DESKTOP_FAST_START": "1",
            "OPENSQUILLA_MCP_ENABLED": "false",
            "OPENSQUILLA_MCP_SERVERS": "[]",
        }
    )
    probe = Path(__file__).with_name("fixtures") / "gateway_boot_probe.py"
    completed = subprocess.run(
        [sys.executable, str(probe), mode, str(tmp_path)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result == {
        "mode": mode,
        "child_processes": 1 if mode == "configured" else 0,
        "ok": True,
    }
