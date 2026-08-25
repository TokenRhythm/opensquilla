"""Interactive [llm_ensemble] onboarding runner (offline, monkeypatched prompts)."""

from __future__ import annotations

import sys
import tomllib
import types
from typing import Any


class _Answer:
    def __init__(self, value: Any) -> None:
        self.value = value

    def ask(self) -> Any:
        return self.value


class _BaseQuestionary(types.SimpleNamespace):
    """Complete fake: ``flow._styled`` eagerly wraps every prompt method."""

    def confirm(self, message: str, **_kwargs: Any) -> _Answer:
        raise AssertionError(f"unexpected confirm prompt: {message}")

    def select(self, message: str, **_kwargs: Any) -> _Answer:
        raise AssertionError(f"unexpected select prompt: {message}")

    def text(self, message: str, **_kwargs: Any) -> _Answer:
        raise AssertionError(f"unexpected text prompt: {message}")

    def password(self, message: str, **_kwargs: Any) -> _Answer:
        raise AssertionError(f"unexpected password prompt: {message}")

    def checkbox(self, message: str, **_kwargs: Any) -> _Answer:
        raise AssertionError(f"unexpected checkbox prompt: {message}")


def test_interactive_ensemble_configure_persists(tmp_path, monkeypatch):
    from opensquilla.onboarding import flow

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setattr(flow, "_is_tty", lambda: True)

    calls: list[str] = []

    class _Questionary(_BaseQuestionary):
        def confirm(self, message: str, **kwargs: Any) -> _Answer:
            calls.append(message)
            if message == "Enable the LLM ensemble?":
                # Model router ships as the default strategy; ensemble is opt-in.
                assert kwargs.get("default") is False
                return _Answer(True)
            raise AssertionError(f"unexpected confirm prompt: {message}")

        def select(self, message: str, **kwargs: Any) -> _Answer:
            calls.append(message)
            if message == "Ensemble selection mode":
                assert kwargs.get("choices") == [
                    "static_openrouter_b5",
                    "static_tokenrhythm_b5",
                    "custom_b5",
                    "router_dynamic",
                ]
                assert kwargs.get("default") == "static_openrouter_b5"
                return _Answer("router_dynamic")
            raise AssertionError(f"unexpected select prompt: {message}")

        def text(self, message: str, **kwargs: Any) -> _Answer:
            calls.append(message)
            if message.startswith("Ensemble model options"):
                return _Answer("prov/model-a, prov/model-b")
            if message == "Minimum successful proposers":
                assert kwargs.get("default") == "1"
                return _Answer("2")
            raise AssertionError(f"unexpected text prompt: {message}")

        def password(self, message: str, **_kwargs: Any) -> _Answer:
            raise AssertionError(f"unexpected password prompt: {message}")

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    flow.run_interactive_ensemble_configure()

    assert calls[0] == "Enable the LLM ensemble?"
    data = tomllib.loads(target.read_text())
    ensemble = data["llm_ensemble"]
    assert ensemble["enabled"] is True
    assert ensemble["selection_mode"] == "router_dynamic"
    assert ensemble["model_options"] == ["prov/model-a", "prov/model-b"]
    assert ensemble["min_successful_proposers"] == 2
    # The sole supported policy is the schema default, so a fresh interactive
    # save does not need to persist a redundant key.
    assert "all_failed_policy" not in ensemble
    assert "Policy when all proposers fail" not in calls


def test_interactive_ensemble_blank_model_options_keep_current(
    tmp_path, monkeypatch
):
    from opensquilla.onboarding import flow

    target = tmp_path / "c.toml"
    target.write_text(
        "[llm_ensemble]\n"
        "enabled = true\n"
        'selection_mode = "router_dynamic"\n'
        'model_options = ["stored/model-a", "stored/model-b"]\n'
        "min_successful_proposers = 3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setattr(flow, "_is_tty", lambda: True)

    class _Questionary(_BaseQuestionary):
        def confirm(self, message: str, **kwargs: Any) -> _Answer:
            if message == "Enable the LLM ensemble?":
                return _Answer(True)
            raise AssertionError(f"unexpected confirm prompt: {message}")

        def select(self, message: str, **kwargs: Any) -> _Answer:
            # Accepting the stored defaults must be a no-op edit.
            if message == "Ensemble selection mode":
                assert kwargs.get("default")
                return _Answer(kwargs["default"])
            raise AssertionError(f"unexpected select prompt: {message}")

        def text(self, message: str, **kwargs: Any) -> _Answer:
            if message.startswith("Ensemble model options"):
                return _Answer("")
            if message == "Minimum successful proposers":
                assert kwargs.get("default") == "3"
                return _Answer(kwargs["default"])
            raise AssertionError(f"unexpected text prompt: {message}")

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    flow.run_interactive_ensemble_configure()

    data = tomllib.loads(target.read_text())
    ensemble = data["llm_ensemble"]
    assert ensemble["model_options"] == ["stored/model-a", "stored/model-b"]
    assert ensemble["selection_mode"] == "router_dynamic"
    assert ensemble["min_successful_proposers"] == 3


def test_interactive_ensemble_disable_is_a_single_answer(tmp_path, monkeypatch):
    from opensquilla.onboarding import flow

    target = tmp_path / "c.toml"
    target.write_text(
        "[llm_ensemble]\n"
        'model_options = ["stored/model-a"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setattr(flow, "_is_tty", lambda: True)

    class _Questionary(_BaseQuestionary):
        def confirm(self, message: str, **_kwargs: Any) -> _Answer:
            if message == "Enable the LLM ensemble?":
                return _Answer(False)
            raise AssertionError(f"unexpected confirm prompt: {message}")

        def select(self, message: str, **_kwargs: Any) -> _Answer:
            raise AssertionError(f"unexpected select prompt: {message}")

        def text(self, message: str, **_kwargs: Any) -> _Answer:
            raise AssertionError(f"unexpected text prompt: {message}")

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    flow.run_interactive_ensemble_configure()

    # Sparse persistence may omit default-equal keys (the ensemble ships
    # disabled), so pin the reloaded semantic state instead of raw TOML.
    from opensquilla.onboarding.config_store import load_config

    saved = load_config(target)
    assert saved.llm_ensemble.enabled is False
    # Tuning values stay untouched for a later re-enable.
    assert list(saved.llm_ensemble.model_options) == ["stored/model-a"]


def test_interactive_ensemble_configure_without_tty_prints_hint(
    tmp_path, monkeypatch, capsys
):
    from opensquilla.onboarding import flow

    target = tmp_path / "c.toml"
    monkeypatch.setattr(flow, "_is_tty", lambda: False)

    result = flow.run_interactive_ensemble_configure(config_path=target)

    assert result.warnings == ["tty_required"]
    assert not target.exists()
    out = capsys.readouterr().out
    assert "Headless ensemble:" in out
    assert "opensquilla onboard configure ensemble --enabled" in out


def test_ensemble_saved_message_directs_to_gateway_reload(tmp_path, monkeypatch):
    """The CLI runner edits the config file on disk, and file edits are only
    read at boot — the save message must direct the operator to a gateway
    reload/restart instead of claiming the change applies to the next turn
    (that is only true for the RPC hot-apply path)."""

    from opensquilla.onboarding import flow

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setattr(flow, "_is_tty", lambda: True)

    messages: list[str] = []

    class _Console:
        def print(self, message: Any = "", *_a: Any, **_kw: Any) -> None:
            messages.append(str(message))

    monkeypatch.setattr(flow, "console", _Console())

    class _Questionary(_BaseQuestionary):
        def confirm(self, message: str, **_kwargs: Any) -> _Answer:
            if message == "Enable the LLM ensemble?":
                return _Answer(False)
            raise AssertionError(f"unexpected confirm prompt: {message}")

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    flow.run_interactive_ensemble_configure()

    joined = "\n".join(messages)
    assert "no restart needed" not in joined
    assert "applies to the next turn" not in joined
    assert "opensquilla gateway reload" in joined


def test_interactive_configure_offers_and_dispatches_ensemble(
    tmp_path, monkeypatch
):
    from opensquilla.onboarding import flow
    from opensquilla.onboarding.config_store import PersistResult

    target = tmp_path / "custom.toml"
    seen: dict[str, Any] = {}
    monkeypatch.setattr(flow, "_is_tty", lambda: True)

    def fake_runner(config_path=None):
        seen["config_path"] = config_path
        return PersistResult(
            path=target,
            backup_path=None,
            restart_required=False,
            warnings=[],
        )

    monkeypatch.setattr(flow, "run_interactive_ensemble_configure", fake_runner)

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            assert message == "Section"
            titles = kwargs["choices"]
            ensemble_title = next(
                (t for t in titles if t.startswith("Ensemble")), None
            )
            if ensemble_title is not None and "config_path" not in seen:
                return _Answer(ensemble_title)
            done = next(t for t in titles if t in ("Done", "Exit (nothing changed)"))
            return _Answer(done)

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = flow.run_interactive_configure(config_path=target)

    assert result is not None
    assert seen["config_path"] == target


class _ValidatedAnswer:
    """Fake prompt honouring ``validate=`` like questionary: rejected input
    is re-asked, so garbage can only leak through when no validator is set."""

    def __init__(self, values: list[Any], validate: Any) -> None:
        self._values = list(values)
        self._validate = validate

    def ask(self) -> Any:
        while self._values:
            candidate = self._values.pop(0)
            if self._validate is None or self._validate(candidate) is True:
                return candidate
        raise AssertionError("prompt inputs exhausted without an accepted value")


def test_interactive_ensemble_min_proposers_garbage_reprompts(tmp_path, monkeypatch):
    """Typing a non-number used to blow up in the mutation layer with a raw
    ``ValueError`` after every other answer had already been given."""

    from opensquilla.onboarding import flow

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setattr(flow, "_is_tty", lambda: True)

    class _Questionary(_BaseQuestionary):
        def confirm(self, message: str, **_kwargs: Any) -> _Answer:
            if message == "Enable the LLM ensemble?":
                return _Answer(True)
            raise AssertionError(f"unexpected confirm prompt: {message}")

        def select(self, message: str, **kwargs: Any) -> _Answer:
            if message == "Ensemble selection mode":
                return _Answer(kwargs["default"])
            raise AssertionError(f"unexpected select prompt: {message}")

        def text(self, message: str, **kwargs: Any):
            if message.startswith("Ensemble model options"):
                return _Answer("")
            if message == "Minimum successful proposers":
                return _ValidatedAnswer(["many", "0", "2"], kwargs.get("validate"))
            raise AssertionError(f"unexpected text prompt: {message}")

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    flow.run_interactive_ensemble_configure()

    data = tomllib.loads(target.read_text())
    assert data["llm_ensemble"]["min_successful_proposers"] == 2
