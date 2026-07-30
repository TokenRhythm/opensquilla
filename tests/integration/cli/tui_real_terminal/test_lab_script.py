from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path
from types import ModuleType

import pytest

REMOVED_TEXT_BACKEND = "text" + "ual"
REMOVED_BACKEND_IDS = ("terminal", REMOVED_TEXT_BACKEND, f"live-{REMOVED_TEXT_BACKEND}")


def _load_lab_script() -> ModuleType:
    script_path = Path(__file__).resolve().parents[4] / "scripts" / "tui_real_terminal_lab.py"
    spec = importlib.util.spec_from_file_location("tui_real_terminal_lab", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_opentui_lab_requires_explicit_live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    lab = _load_lab_script()
    monkeypatch.delenv("OPENSQUILLA_TUI_LIVE_REAL", raising=False)

    with pytest.raises(SystemExit, match="OPENSQUILLA_TUI_LIVE_REAL=1"):
        lab._assert_live_backend_enabled("live-opentui")

    monkeypatch.setenv("OPENSQUILLA_TUI_LIVE_REAL", "1")
    lab._assert_live_backend_enabled("live-opentui")
    lab._assert_live_backend_enabled("opentui")


def test_lab_script_accepts_opentui_backend_for_manual_render_runs() -> None:
    module = _load_lab_script()

    args: Namespace = module._parser().parse_args(  # noqa: SLF001
        ["--scenario", "architecture_prompt", "--backend", "opentui"]
    )

    assert args.backend == "opentui"


@pytest.mark.parametrize(
    ("requested_driver", "scenario_requires_tmux", "expected"),
    [("auto", False, "auto"), ("pty", False, "pty"), ("auto", True, "tmux")],
)
def test_lab_script_selects_driver_for_scenario_requirements(
    requested_driver: str,
    scenario_requires_tmux: bool,
    expected: str,
) -> None:
    module = _load_lab_script()

    selected = module._select_driver(  # noqa: SLF001
        requested_driver,
        scenario_requires_tmux=scenario_requires_tmux,
    )

    assert selected == expected


@pytest.mark.parametrize("backend", REMOVED_BACKEND_IDS)
def test_lab_script_rejects_removed_backend_choices(backend: str) -> None:
    module = _load_lab_script()

    with pytest.raises(SystemExit):
        module._parser().parse_args(["--scenario", "architecture_prompt", "--backend", backend])
