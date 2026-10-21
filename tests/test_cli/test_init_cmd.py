import sys
import tomllib
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


def test_init_still_loads_prompts_and_preserves_config_writes(tmp_path, monkeypatch) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text("KEEP_ME=existing\nOPENAI_API_KEY=old\n", encoding="utf-8")
    calls = []

    def prompt(kind, answer):
        def build(message, **options):
            calls.append((kind, message, options))
            return SimpleNamespace(ask=lambda: answer)
        return build

    monkeypatch.setitem(sys.modules, "questionary", SimpleNamespace(
        select=prompt("select", "openai"),
        password=prompt("password", "synthetic-test-key"),
        text=prompt("text", "synthetic-model"),
    ))
    monkeypatch.setattr(init_cmd, "default_opensquilla_home", lambda: home)

    init_cmd.run_init()

    assert [call[0] for call in calls] == ["select", "password", "text"]
    assert calls[0][2]["default"] == "tokenrhythm"
    assert calls[2][2]["default"] == "openai/gpt-4o-mini"
    assert (home / ".env").read_text(encoding="utf-8") == (
        "KEEP_ME=existing\nOPENAI_API_KEY=synthetic-test-key\n"
    )
    config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert config["llm"] == {"provider": "openai", "model": "synthetic-model"}
    assert config["state_dir"] == str(home / "state")


def test_init_cancel_keeps_existing_config(tmp_path, monkeypatch) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    config = home / "config.toml"
    config.write_text('existing = "unchanged"\n', encoding="utf-8")
    monkeypatch.setitem(sys.modules, "questionary", SimpleNamespace(
        select=lambda *args, **kwargs: SimpleNamespace(ask=lambda: None),
    ))
    monkeypatch.setattr(init_cmd, "default_opensquilla_home", lambda: home)

    with pytest.raises(typer.Exit) as stopped:
        init_cmd.run_init()

    assert stopped.value.exit_code == 1
    assert config.read_text(encoding="utf-8") == 'existing = "unchanged"\n'
    assert not (home / ".env").exists()
