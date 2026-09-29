from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from opensquilla.cli import init_cmd
from opensquilla.cli.init_cmd import _default_model_for_provider


def test_init_uses_tokenrhythm_0813_model_default() -> None:
    assert _default_model_for_provider("tokenrhythm") == "deepseek-v4-pro-0813"


def test_init_uses_direct_deepseek_model_default() -> None:
    assert _default_model_for_provider("deepseek") == "deepseek-flash"


def test_init_keeps_openrouter_model_default() -> None:
    assert _default_model_for_provider("openrouter") == "deepseek/deepseek-v4-pro"


def test_interactive_init_writes_selected_provider_and_preserves_other_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text("KEEP_ME=unchanged\nDEEPSEEK_API_KEY=old\n", encoding="utf-8")
    monkeypatch.setattr(init_cmd, "default_opensquilla_home", lambda: home)
    calls = []

    def prompt(kind, answer):
        def build(message, **kwargs):
            calls.append((kind, message, kwargs))
            return SimpleNamespace(ask=lambda: answer)
        return build

    monkeypatch.setitem(sys.modules, "questionary", SimpleNamespace(
        select=prompt("select", "deepseek"),
        password=prompt("password", "synthetic-key"),
        text=prompt("text", "synthetic-model"),
    ))

    init_cmd.init_command()

    assert [call[0] for call in calls] == ["select", "password", "text"]
    assert calls[-1][2]["default"] == "deepseek-flash"
    assert (home / ".env").read_text(encoding="utf-8") == (
        "KEEP_ME=unchanged\nDEEPSEEK_API_KEY=synthetic-key\n"
    )
    config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert config == {
        "llm": {"provider": "deepseek", "model": "synthetic-model"},
        "state_dir": str(home / "state"),
    }
    assert (home / "state").is_dir()


@pytest.mark.parametrize("cancel_at", ["provider", "key", "model"])
def test_interactive_init_cancellation_preserves_existing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_at: str,
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    env_text = "KEEP_ME=unchanged\n"
    config_text = '[llm]\nprovider="existing"\n'
    (home / ".env").write_text(env_text, encoding="utf-8")
    (home / "config.toml").write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(init_cmd, "default_opensquilla_home", lambda: home)
    monkeypatch.setitem(sys.modules, "questionary", SimpleNamespace(
        select=lambda *_a, **_k: SimpleNamespace(
            ask=lambda: None if cancel_at == "provider" else "deepseek"),
        password=lambda *_a, **_k: SimpleNamespace(
            ask=lambda: None if cancel_at == "key" else "synthetic-key"),
        text=lambda *_a, **_k: SimpleNamespace(
            ask=lambda: None if cancel_at == "model" else "synthetic-model"),
    ))

    with pytest.raises(typer.Exit) as cancelled:
        init_cmd.init_command()
    assert cancelled.value.exit_code == 1
    assert (home / ".env").read_text(encoding="utf-8") == env_text
    assert (home / "config.toml").read_text(encoding="utf-8") == config_text


def test_cli_import_and_help_do_not_load_interactive_prompt_stack(tmp_path: Path) -> None:
    # Use a clean interpreter: other tests legitimately import questionary.
    # A forbidden import fails the test even if a fallback catches ImportError.
    source_root = Path(__file__).resolve().parents[2] / "src"
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATHEXT"}
    }
    environment.update({
        "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
        "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "TEMP": str(tmp_path), "TMP": str(tmp_path),
        "OPENSQUILLA_STATE_DIR": str(tmp_path / "profile"),
        "PYTHONPATH": str(source_root), "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    code = """
import importlib.abc
import sys

class RejectInteractivePrompts(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'questionary', 'prompt_toolkit'}:
            raise AssertionError(f'unneeded interactive import: {fullname}')

sys.meta_path.insert(0, RejectInteractivePrompts())
from opensquilla.cli.main import app
from typer.testing import CliRunner
for args in (['--help'], ['init', '--help'], ['gateway', '--help'],
             ['gateway', 'run', '--help'], ['onboard', '--help']):
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, (args, result.exception, result.output)
    assert 'Usage:' in result.output
assert not any(name.split('.')[0] in {'questionary', 'prompt_toolkit'} for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
