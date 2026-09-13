from __future__ import annotations

from pathlib import Path
from unittest import mock

from opensquilla.cli.init_cmd import _default_model_for_provider, run_init


def test_init_uses_tokenrhythm_0813_model_default() -> None:
    assert _default_model_for_provider("tokenrhythm") == "deepseek-v4-pro-0813"


def test_init_uses_direct_deepseek_model_default() -> None:
    assert _default_model_for_provider("deepseek") == "deepseek-v4-flash"


def test_init_keeps_openrouter_model_default() -> None:
    assert _default_model_for_provider("openrouter") == "deepseek/deepseek-v4-pro"


class _FakeAsk:
    def __init__(self, value: str) -> None:
        self._value = value

    def ask(self) -> str:
        return self._value


def _patch_init_answers(monkeypatch) -> None:
    answers = iter(["openrouter", "sk-test"])

    def fake_select(_prompt: str, choices: list[str], default: str = "") -> object:
        return _FakeAsk(next(answers))

    def fake_password(_prompt: str) -> object:
        return _FakeAsk(next(answers))

    def fake_text(_prompt: str, default: str = "") -> object:
        return _FakeAsk(default)

    monkeypatch.setattr("opensquilla.cli.init_cmd.questionary.select", fake_select)
    monkeypatch.setattr("opensquilla.cli.init_cmd.questionary.password", fake_password)
    monkeypatch.setattr("opensquilla.cli.init_cmd.questionary.text", fake_text)


def test_init_autostart_off_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("OPENSQUILLA_HOME", raising=False)
    monkeypatch.delenv("OPENSQUILLA_PROFILE", raising=False)
    _patch_init_answers(monkeypatch)

    with mock.patch("opensquilla.cli.init_cmd.autostart") as autostart_mock:
        run_init()

    autostart_mock.register_logon_task.assert_not_called()


def test_init_autostart_flag_uses_active_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENSQUILLA_STATE_DIR", raising=False)
    monkeypatch.setenv("OPENSQUILLA_HOME", str(tmp_path / "profiles"))
    monkeypatch.setenv("OPENSQUILLA_PROFILE", "coder")
    _patch_init_answers(monkeypatch)

    with mock.patch("opensquilla.cli.init_cmd.autostart") as autostart_mock:
        autostart_mock.register_logon_task.return_value.summary.return_value = (
            "Windows autostart registered for profile 'coder'"
        )
        run_init(autostart_register=True)

    autostart_mock.register_logon_task.assert_called_once_with(
        profile="coder",
        home=tmp_path / "profiles" / "coder",
        state_dir=None,
    )


def test_init_autostart_state_dir_override_is_registered(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "state-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.delenv("OPENSQUILLA_HOME", raising=False)
    monkeypatch.delenv("OPENSQUILLA_PROFILE", raising=False)
    _patch_init_answers(monkeypatch)

    with mock.patch("opensquilla.cli.init_cmd.autostart") as autostart_mock:
        autostart_mock.register_logon_task.return_value.summary.return_value = (
            "Windows autostart registered for default home"
        )
        run_init(autostart_register=True)

    autostart_mock.register_logon_task.assert_called_once_with(
        profile=None,
        home=home,
        state_dir=home,
    )


def test_init_autostart_state_dir_wins_over_profile_env(
    monkeypatch, tmp_path: Path
) -> None:
    state_home = tmp_path / "state-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(state_home))
    monkeypatch.setenv("OPENSQUILLA_HOME", str(tmp_path / "profiles"))
    monkeypatch.setenv("OPENSQUILLA_PROFILE", "coder")
    _patch_init_answers(monkeypatch)

    with mock.patch("opensquilla.cli.init_cmd.autostart") as autostart_mock:
        autostart_mock.register_logon_task.return_value.summary.return_value = (
            "Windows autostart registered for default home"
        )
        run_init(autostart_register=True)

    autostart_mock.register_logon_task.assert_called_once_with(
        profile=None,
        home=state_home,
        state_dir=state_home,
    )


def test_init_autostart_errors_are_non_fatal(monkeypatch, tmp_path: Path) -> None:
    from opensquilla.cli import autostart

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("OPENSQUILLA_HOME", raising=False)
    monkeypatch.delenv("OPENSQUILLA_PROFILE", raising=False)
    _patch_init_answers(monkeypatch)

    with mock.patch(
        "opensquilla.cli.init_cmd.autostart.register_logon_task",
        side_effect=autostart.AutostartError("simulated failure"),
    ):
        run_init(autostart_register=True)

    assert (tmp_path / "home" / ".env").exists()
    assert (tmp_path / "home" / "config.toml").exists()
