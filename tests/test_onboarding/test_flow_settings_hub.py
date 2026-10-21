"""The multi-level settings entry points: the `onboard configure` section hub
and the re-run fork in `opensquilla onboard`.

Contract under test:
- Bare `configure` on a TTY is a menu LOOP (edit a section, come back, Done
  exits); an explicit section stays a one-shot edit.
- Cancelling inside a section returns to the menu; cancelling at the menu
  itself raises like every other wizard prompt.
- Restart-required guidance survives multi-section sittings.
- A full interactive re-run over a configured install offers
  update / change-sections / start-fresh, and start-fresh backs the config
  file up instead of deleting it.

Everything is offline: prompts are faked at the questionary seam and section
runners are monkeypatched.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any

import pytest

from opensquilla.onboarding import flow
from opensquilla.onboarding.config_store import PersistResult, load_config, persist_config
from opensquilla.onboarding.errors import UserCancelledError
from opensquilla.onboarding.flow import OnboardOptions
from opensquilla.onboarding.mutations import upsert_llm_provider


class _Answer:
    def __init__(self, value: Any) -> None:
        self.value = value

    def ask(self) -> Any:
        return self.value


class _InterruptAnswer:
    """A prompt whose ``ask()`` raises like a raw Ctrl+C (no questionary
    None-conversion)."""

    def ask(self) -> Any:
        raise KeyboardInterrupt


class _BaseQuestionary(types.SimpleNamespace):
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


def _persist_result(target, *, restart_required=False, warnings=()):
    return PersistResult(
        path=target,
        backup_path=None,
        restart_required=restart_required,
        warnings=list(warnings),
    )


def _pick_done(titles: list[str]) -> str:
    return next(t for t in titles if t in ("Done", "Exit (nothing changed)"))


def _seed_configured_install(target) -> None:
    cfg = load_config(target)
    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="dummy/model",
        api_key="sk-dummy-hub-test",
    )
    persist_config(res.config, path=target, restart_required=False, backup=False)


# --- the configure hub -----------------------------------------------------


def test_hub_loops_dispatching_multiple_sections_then_done(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setattr(flow, "_is_tty", lambda: True)
    dispatched: list[str] = []

    def _fake_runner(name, *, restart_required=False):
        def runner(config_path=None):
            assert config_path == target
            dispatched.append(name)
            return _persist_result(target, restart_required=restart_required)

        return runner

    monkeypatch.setattr(
        flow, "run_interactive_search_configure", _fake_runner("search")
    )
    monkeypatch.setattr(
        flow,
        "run_interactive_image_generation_configure",
        _fake_runner("image-generation", restart_required=True),
    )

    menus: list[list[str]] = []

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            assert message == "Section"
            titles = kwargs["choices"]
            menus.append(titles)
            if len(menus) == 1:
                return _Answer(next(t for t in titles if t.startswith("Web search")))
            if len(menus) == 2:
                return _Answer(
                    next(t for t in titles if t.startswith("Image generation"))
                )
            return _Answer(_pick_done(titles))

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = flow.run_interactive_configure(config_path=target)

    assert dispatched == ["search", "image-generation"]
    assert len(menus) == 3, "the menu must reappear after each section"
    assert result is not None
    assert result.restart_required is True, "restart flag must stay sticky"


def test_hub_menu_titles_carry_live_status_words(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setattr(flow, "_is_tty", lambda: True)
    seen: dict[str, list[str]] = {}

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            seen["titles"] = kwargs["choices"]
            return _Answer(_pick_done(kwargs["choices"]))

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = flow.run_interactive_configure(config_path=target)

    assert result is None
    titles = seen["titles"]
    # Fresh install: provider setup is outstanding, search is a deliberate
    # later. The exact words come from the shared SECTION_STATUS_DISPLAY map.
    assert any(t.startswith("Provider — ") for t in titles)
    assert "Provider — Missing" in titles
    assert "Channels — Later" in titles
    assert titles[-1] == "Exit (nothing changed)"
    # Audio has no configure path and must not be offered as a menu entry.
    assert not any(t.lower().startswith("voice audio") for t in titles)
    assert not any(t.lower().startswith("audio") for t in titles)


def test_hub_section_cancel_returns_to_menu_instead_of_aborting(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    monkeypatch.setattr(flow, "_is_tty", lambda: True)
    attempts: list[str] = []

    def _cancelling_runner(config_path=None):
        attempts.append("search")
        raise UserCancelledError("search")

    monkeypatch.setattr(flow, "run_interactive_search_configure", _cancelling_runner)
    menu_calls = {"n": 0}

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            menu_calls["n"] += 1
            titles = kwargs["choices"]
            if menu_calls["n"] == 1:
                return _Answer(next(t for t in titles if t.startswith("Web search")))
            return _Answer(_pick_done(titles))

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = flow.run_interactive_configure(config_path=target)

    assert attempts == ["search"]
    assert menu_calls["n"] == 2, "cancel inside a section must return to the menu"
    assert result is None


def test_hub_menu_cancel_raises_user_cancelled(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setattr(flow, "_is_tty", lambda: True)

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            return _Answer(None)  # questionary returns None on Esc/Ctrl-C

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    with pytest.raises(UserCancelledError):
        flow.run_interactive_configure(config_path=target)


def test_hub_section_keyboard_interrupt_returns_to_menu_instead_of_aborting(
    tmp_path, monkeypatch
):
    """Ctrl+C inside a section is the same operator intent as Esc: leave the
    section unchanged and come back to the menu. Letting the raw
    KeyboardInterrupt escape used to abort the whole hub sitting."""
    target = tmp_path / "c.toml"
    monkeypatch.setattr(flow, "_is_tty", lambda: True)

    def _interrupting_runner(config_path=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        flow, "run_interactive_search_configure", _interrupting_runner
    )
    menu_calls = {"n": 0}

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            menu_calls["n"] += 1
            titles = kwargs["choices"]
            if menu_calls["n"] == 1:
                return _Answer(next(t for t in titles if t.startswith("Web search")))
            return _Answer(_pick_done(titles))

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = flow.run_interactive_configure(config_path=target)

    assert menu_calls["n"] == 2, "Ctrl+C inside a section must return to the menu"
    assert result is None


def test_hub_menu_cancel_after_saved_sections_returns_the_aggregate(
    tmp_path, monkeypatch
):
    """Esc at the menu AFTER a section persisted must exit like "Done": the
    saved changes are on disk and their sticky restart guidance must reach
    the CLI boundary instead of being discarded as "Setup cancelled"."""
    target = tmp_path / "c.toml"
    monkeypatch.setattr(flow, "_is_tty", lambda: True)
    monkeypatch.setattr(
        flow,
        "run_interactive_search_configure",
        lambda config_path=None: _persist_result(
            target, restart_required=True, warnings=["w-search"]
        ),
    )
    menu_calls = {"n": 0}

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            menu_calls["n"] += 1
            titles = kwargs["choices"]
            if menu_calls["n"] == 1:
                return _Answer(next(t for t in titles if t.startswith("Web search")))
            return _Answer(None)  # Esc at the menu — after a save

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = flow.run_interactive_configure(config_path=target)

    assert result is not None, "a saved sitting must not be reported as cancelled"
    assert result.restart_required is True
    assert result.warnings == ["w-search"]


def test_hub_menu_keyboard_interrupt_after_saved_sections_returns_the_aggregate(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    monkeypatch.setattr(flow, "_is_tty", lambda: True)
    monkeypatch.setattr(
        flow,
        "run_interactive_search_configure",
        lambda config_path=None: _persist_result(target, restart_required=True),
    )
    menu_calls = {"n": 0}

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> Any:
            menu_calls["n"] += 1
            titles = kwargs["choices"]
            if menu_calls["n"] == 1:
                return _Answer(next(t for t in titles if t.startswith("Web search")))
            return _InterruptAnswer()  # raw Ctrl+C at the menu — after a save

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = flow.run_interactive_configure(config_path=target)

    assert result is not None
    assert result.restart_required is True


def test_hub_merges_warnings_across_sections(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setattr(flow, "_is_tty", lambda: True)

    monkeypatch.setattr(
        flow,
        "run_interactive_search_configure",
        lambda config_path=None: _persist_result(target, warnings=["w-search"]),
    )
    monkeypatch.setattr(
        flow,
        "run_interactive_image_generation_configure",
        lambda config_path=None: _persist_result(target, warnings=["w-image"]),
    )
    step = {"n": 0}

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            step["n"] += 1
            titles = kwargs["choices"]
            if step["n"] == 1:
                return _Answer(next(t for t in titles if t.startswith("Web search")))
            if step["n"] == 2:
                return _Answer(
                    next(t for t in titles if t.startswith("Image generation"))
                )
            return _Answer(_pick_done(titles))

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = flow.run_interactive_configure(config_path=target)

    assert result is not None
    assert result.warnings == ["w-search", "w-image"]


def test_explicit_section_stays_one_shot(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setattr(flow, "_is_tty", lambda: True)
    dispatched: list[str] = []
    monkeypatch.setattr(
        flow,
        "run_interactive_search_configure",
        lambda config_path=None: (dispatched.append("search"), _persist_result(target))[1],
    )

    class _Questionary(_BaseQuestionary):
        pass  # any menu render would raise via the base class

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = flow.run_interactive_configure("search", config_path=target)

    assert dispatched == ["search"]
    assert result is not None


# --- the onboard re-run fork -------------------------------------------------


def test_rerun_fork_routes_change_sections_to_the_hub(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    _seed_configured_install(target)
    monkeypatch.setattr(flow, "_is_tty", lambda: True)
    monkeypatch.setattr(flow, "_wait_for_setup_start", lambda: None)
    monkeypatch.setattr(flow, "_ensure_config_dir_writable", lambda _path: None)
    hub_calls: list[Any] = []
    sentinel = _persist_result(target, restart_required=True)

    def _fake_hub(questionary, *, config_path=None):
        hub_calls.append(config_path)
        return sentinel

    monkeypatch.setattr(flow, "_run_configure_hub", _fake_hub)

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            assert message.startswith("This install is already configured")
            assert kwargs["default"] == flow._ONBOARD_UPDATE_CHOICE
            return _Answer(flow._ONBOARD_SECTIONS_CHOICE)

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = flow.run_interactive_onboard(OnboardOptions(config_path=target))

    assert hub_calls == [target]
    assert result is sentinel


def test_rerun_fork_start_fresh_backs_up_config_and_restarts_walk(
    tmp_path, monkeypatch
):
    target = tmp_path / "c.toml"
    _seed_configured_install(target)
    monkeypatch.setattr(flow, "_is_tty", lambda: True)
    monkeypatch.setattr(flow, "_wait_for_setup_start", lambda: None)
    monkeypatch.setattr(flow, "_ensure_config_dir_writable", lambda _path: None)
    monkeypatch.setattr(
        flow, "_run_onboard_migration_step", lambda *_a, **_kw: None
    )
    walk_state: dict[str, Any] = {}

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            if message.startswith("This install is already configured"):
                return _Answer(flow._ONBOARD_RESET_CHOICE)
            if message == "LLM provider":
                # The walk restarted from scratch; record that the reset
                # really moved the config aside, then cancel to end the test.
                walk_state["target_during_walk"] = target.exists()
                walk_state["backups_during_walk"] = len(
                    list(tmp_path.glob("c.toml.backup.*"))
                )
                return _Answer(None)
            raise AssertionError(f"unexpected select prompt: {message}")

        def confirm(self, message: str, **kwargs: Any) -> _Answer:
            assert message.startswith("Back up")
            assert kwargs["default"] is False
            return _Answer(True)

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    with pytest.raises(UserCancelledError):
        flow.run_interactive_onboard(OnboardOptions(config_path=target))

    # The fresh walk really did start over a reset config...
    assert walk_state == {"target_during_walk": False, "backups_during_walk": 1}
    # ...but cancelling it must restore the previous config, not leave the
    # install unconfigured with only a dim backup notice (F10).
    assert target.exists()
    assert "sk-dummy-hub-test" in target.read_text()
    assert list(tmp_path.glob("c.toml.backup.*")) == []


def test_restore_reset_backup_keeps_a_newly_persisted_config(tmp_path):
    """When the fresh walk already persisted a new config, a late cancel must
    keep the operator's new answers — the restore only fills a hole, it never
    clobbers a config the walk just wrote."""
    target = tmp_path / "config.toml"
    backup = tmp_path / "config.toml.backup.20260101T000000Z"
    target.write_text("port = 28791\n")  # the fresh walk's new config
    backup.write_text("port = 18791\n")  # the pre-reset original

    flow._restore_reset_backup((target, backup))

    assert target.read_text() == "port = 28791\n"
    assert backup.read_text() == "port = 18791\n"


def test_start_fresh_pins_the_cwd_resolved_config_path(tmp_path, monkeypatch):
    """After the backup renames a cwd-resolved ./opensquilla.toml away, the
    rest of the reset walk must keep operating on THAT file: a dynamic
    re-resolve would fall through to the HOME config — seeding the "fresh"
    walk from a stale file and overwriting one the confirmation never named."""
    proj = tmp_path / "proj"
    proj.mkdir()
    proj_config = proj / "opensquilla.toml"
    _seed_configured_install(proj_config)
    original_project_config = proj_config.read_text()

    home = tmp_path / "home-state"
    home.mkdir()
    stale_home_config = home / "config.toml"
    stale_home_config.write_text(
        '[llm]\nprovider = "openai"\nmodel = "stale-home-model"\n'
        'api_key = "sk-stale-home"\n'
    )
    stale_raw = stale_home_config.read_text()

    monkeypatch.chdir(proj)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setattr(flow, "_is_tty", lambda: True)
    monkeypatch.setattr(flow, "_wait_for_setup_start", lambda: None)
    monkeypatch.setattr(flow, "_ensure_config_dir_writable", lambda _path: None)

    walk_seen: dict[str, Any] = {}

    def _fake_walk(questionary, cfg, status, options, *, config_path):
        walk_seen["config_path"] = config_path
        walk_seen["options_path"] = options.config_path
        walk_seen["seed_model"] = str(cfg.llm.model)
        walk_seen["seed_key"] = str(cfg.llm.api_key)
        raise UserCancelledError(section="provider")

    monkeypatch.setattr(flow, "_run_onboard_walk", _fake_walk)

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            assert message.startswith("This install is already configured")
            return _Answer(flow._ONBOARD_RESET_CHOICE)

        def confirm(self, message: str, **kwargs: Any) -> _Answer:
            assert message.startswith("Back up")
            # The confirmation must name the cwd-resolved project file.
            assert str(proj_config) in message
            return _Answer(True)

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    with pytest.raises(UserCancelledError):
        flow.run_interactive_onboard(OnboardOptions())

    # The walk was pinned to the pre-backup path, seeded from defaults.
    assert walk_seen["config_path"] == proj_config
    assert walk_seen["options_path"] == proj_config
    assert walk_seen["seed_key"] == ""
    assert walk_seen["seed_model"] != "stale-home-model"
    # Cancel restored the project config; the home config was never touched.
    assert proj_config.read_text() == original_project_config
    assert stale_home_config.read_text() == stale_raw
    assert list(proj.glob("opensquilla.toml.backup.*")) == []


def test_onboard_fork_hub_exit_without_changes_does_not_rewrite_config(
    tmp_path, monkeypatch
):
    """`opensquilla onboard` -> "Change specific sections" -> "Exit (nothing
    changed)" is an explicit no-op: it must not persist_config over a
    hand-maintained config.toml (stripping comments, normalizing key order,
    bumping the mtime, forcing mode 0600)."""
    target = tmp_path / "c.toml"
    raw = (
        "# operator-managed: keep my comments\n"
        "[llm]\n"
        'provider = "openrouter"\n'
        'model = "dummy/model"\n'
        'api_key = "sk-dummy-hub-test"\n'
    )
    target.write_text(raw)
    target.chmod(0o644)
    before = target.stat()

    monkeypatch.setattr(flow, "_is_tty", lambda: True)
    monkeypatch.setattr(flow, "_wait_for_setup_start", lambda: None)
    monkeypatch.setattr(flow, "_ensure_config_dir_writable", lambda _path: None)

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            if message.startswith("This install is already configured"):
                return _Answer(flow._ONBOARD_SECTIONS_CHOICE)
            if message == "Section":
                return _Answer("Exit (nothing changed)")
            raise AssertionError(f"unexpected select prompt: {message}")

    monkeypatch.setitem(sys.modules, "questionary", _Questionary())

    result = flow.run_interactive_onboard(OnboardOptions(config_path=target))

    after = target.stat()
    assert target.read_text() == raw, "a no-change exit must not rewrite the file"
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    if os.name != "nt":
        assert (after.st_mode & 0o777) == 0o644
    assert result.path == target
    assert result.backup_path is None
    assert result.restart_required is False


def test_start_fresh_backs_up_symlink_target_without_replacing_link(tmp_path):
    real = tmp_path / "real-config.toml"
    link = tmp_path / "config.toml"
    real.write_text("port = 18791\n")
    try:
        link.symlink_to(real)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symbolic-link privilege is unavailable")
        raise

    flow._backup_and_reset_config(link)

    assert link.is_symlink()
    assert not real.exists()
    backups = list(tmp_path.glob("real-config.toml.backup.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "port = 18791\n"


def test_start_fresh_backup_uses_the_shared_backup_naming_scheme(tmp_path):
    """Reset backups must come from the shared collision-safe helper so the
    state dir carries ONE backup naming scheme: the ``config.toml.backup.*``
    pattern that backup-aware tooling (e.g. the uninstall purge inventory)
    already enumerates. The old hand-rolled ``.bak-<stamp>`` files escaped
    that tooling while still holding secrets."""
    target = tmp_path / "config.toml"
    target.write_text('api_key = "sk-dummy-reset"\n')

    result = flow._backup_and_reset_config(target)

    assert result is not None
    original, backup = result
    assert original == target
    assert not target.exists()
    assert backup.parent == tmp_path
    assert backup.name.startswith("config.toml.backup.")
    assert backup.read_text() == 'api_key = "sk-dummy-reset"\n'
    # No stray files under the retired hand-rolled scheme.
    assert list(tmp_path.glob("config.toml.bak-*")) == []
    # POSIX mode 0600: the backup holds the same secrets as the config it replaces.
    if os.name != "nt":
        assert (backup.stat().st_mode & 0o777) == 0o600


def test_rerun_fork_decline_reset_falls_back_to_update(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    _seed_configured_install(target)
    cfg = load_config(target)
    status = flow.get_onboarding_status(cfg)

    class _Questionary(_BaseQuestionary):
        def select(self, message: str, **kwargs: Any) -> _Answer:
            return _Answer(flow._ONBOARD_RESET_CHOICE)

        def confirm(self, message: str, **kwargs: Any) -> _Answer:
            return _Answer(False)

    action = flow._ask_existing_setup_action(
        _Questionary(), cfg, status, OnboardOptions(config_path=target)
    )

    assert action == "update"
    assert target.exists()


@pytest.mark.parametrize(
    "options",
    [
        OnboardOptions(skip_migration=True),
        OnboardOptions(skip_channels=True),
        OnboardOptions(skip_search=True),
        OnboardOptions(skip_image_generation=True),
        OnboardOptions(minimal=True),
        OnboardOptions(if_needed=True),
        OnboardOptions(provider_id="openrouter"),
        OnboardOptions(api_key="sk-headless"),
        # An explicit --router is headless-shaped: routing it into the fork
        # would let the "sections" path silently drop the requested mode.
        OnboardOptions(router_mode="disabled"),
        OnboardOptions(router_mode="recommended"),
        OnboardOptions(scoped_section=True),
    ],
)
def test_rerun_fork_not_offered_for_scoped_or_headless_runs(
    tmp_path, monkeypatch, options
):
    target = tmp_path / "c.toml"
    _seed_configured_install(target)
    cfg = load_config(target)
    status = flow.get_onboarding_status(cfg)
    options = OnboardOptions(
        **{**options.__dict__, "config_path": target}
    )

    action = flow._ask_existing_setup_action(
        _BaseQuestionary(), cfg, status, options
    )

    assert action is None


def test_rerun_fork_gate_scopes_every_non_default_option_field():
    """The gate compares field-by-field against OnboardOptions() so a future
    field cannot drift out of the check unnoticed: ANY non-default value
    outside the reviewed allowlist must scope the run away from the fork."""
    from dataclasses import fields, replace

    defaults = OnboardOptions()
    assert flow._is_full_interactive_rerun(defaults) is True
    # config_path is the one reviewed exception: an explicit --config targets
    # a different file, not a different walk.
    assert flow._FORK_COMPATIBLE_OPTION_FIELDS == frozenset({"config_path"})
    assert flow._is_full_interactive_rerun(
        replace(defaults, config_path="/tmp/other.toml")
    ) is True

    for fld in fields(OnboardOptions):
        if fld.name in flow._FORK_COMPATIBLE_OPTION_FIELDS:
            continue
        default_value = getattr(defaults, fld.name)
        if default_value is False:
            non_default: Any = True
        elif default_value is None:
            non_default = "non-default-sample"
        else:
            raise AssertionError(
                f"OnboardOptions.{fld.name} has an unhandled default shape "
                f"{default_value!r}; teach this drift test its non-default sample"
            )
        assert not flow._is_full_interactive_rerun(
            replace(defaults, **{fld.name: non_default})
        ), f"non-default {fld.name} must scope the run out of the re-run fork"


def test_rerun_fork_not_offered_for_fresh_or_unfinished_installs(tmp_path):
    target = tmp_path / "c.toml"
    cfg = load_config(target)
    status = flow.get_onboarding_status(cfg)

    action = flow._ask_existing_setup_action(
        _BaseQuestionary(), cfg, status, OnboardOptions(config_path=target)
    )

    assert action is None
