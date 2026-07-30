from __future__ import annotations

import os

import pytest

from tui_real_terminal.driver import probe_terminal_capabilities
from tui_real_terminal.scenarios import scenario_by_id

pytestmark = [
    pytest.mark.tui_real_terminal,
    pytest.mark.llm,
    pytest.mark.llm_gateway,
]


def test_bare_chat_default_runs_live_opentui_architecture_prompt_in_tmux(
    run_real_terminal_scenario,
    tui_backend: str,
    tui_driver: str,
) -> None:
    if os.environ.get("OPENSQUILLA_TUI_LIVE_REAL") != "1":
        pytest.skip("set OPENSQUILLA_TUI_LIVE_REAL=1 to run the real CLI/OpenTUI tmux smoke")
    if tui_backend != "live-opentui":
        pytest.skip("run with --tui-backend=live-opentui")
    if tui_driver == "pty":
        pytest.skip("live OpenTUI real CLI mode requires tmux, not PTY")
    if not probe_terminal_capabilities().tmux_available:
        pytest.skip("tmux is unavailable")

    result = run_real_terminal_scenario(scenario_by_id("live_opentui_architecture_prompt"))

    assert result.status == "pass"
    assert (result.run_dir / "terminal.log").exists()
    scrollback_path = result.run_dir / "scrollback.txt"
    assert scrollback_path.exists()
    # The scenario's wait step matched either a completed turn's usage receipt
    # or the runtime's own timeout notice; the final scrollback must still show
    # that state rather than an empty or crashed pane. Keep the receipt marker
    # specific: the welcome chrome contains many generic middle-dot separators.
    scrollback = scrollback_path.read_text(encoding="utf-8")
    assert scrollback.strip()
    assert "Traceback (most recent call last)" not in scrollback
    assert (
        "╰ in " in scrollback
        or "The task timed out before it could finish." in scrollback
    )
