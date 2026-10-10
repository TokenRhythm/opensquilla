"""Desktop Gateway boot must not import the unrelated interactive init UI."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_gateway_cli_bootstrap_does_not_require_interactive_prompts(tmp_path) -> None:
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("OPENSQUILLA_") or key in {"PYTHONHOME", "PYTHONPATH"}:
            env.pop(key)
    env.update(
        PYTHONPATH=str(root / "src"), PYTHONDONTWRITEBYTECODE="1",
        HOME=str(tmp_path), USERPROFILE=str(tmp_path),
        OPENSQUILLA_STATE_DIR=str(tmp_path / "state"),
        OPENSQUILLA_USER_STATE_DIR=str(tmp_path / "user-state"),
    )
    code = """
import importlib.abc
import sys

class BlockPromptStack(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'questionary', 'prompt_toolkit'}:
            raise AssertionError('Gateway bootstrap loaded interactive UI: ' + fullname)

sys.meta_path.insert(0, BlockPromptStack())
from opensquilla.cli.main import app
from opensquilla.cli.gateway_cmd import run_gateway
from typer.testing import CliRunner
result = CliRunner().invoke(app, ['gateway', 'run', '--help'])
assert result.exit_code == 0, result.output
assert '--port' in result.stdout and '--config' in result.stdout
assert not any(name.split('.')[0] in {'questionary', 'prompt_toolkit'} for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
