from __future__ import annotations

import pytest

from tui_real_terminal.driver import TerminalSize
from tui_real_terminal.scenarios import ScenarioStep, TuiScenario

pytestmark = pytest.mark.tui_real_terminal


def test_empty_gateway_bootstrap_keeps_complete_first_screen(
    run_real_terminal_scenario,
) -> None:
    """An empty canonical history must still mount the branded first screen.

    This differs deliberately from the ordinary fake-provider launch: the
    Gateway path always sends ``history.replace`` before ``context.update``.
    """

    scenario = TuiScenario(
        scenario_id="gateway_empty_bootstrap_startup",
        family="launch_and_input_loop",
        initial_size=TerminalSize(cols=140, rows=36),
        steps=(
            ScenarioStep(
                "wait-ready",
                "wait_text",
                "OPEN_SQUILLA_TUI_READY",
                "gateway-bootstrap-ready",
                timeout_s=10.0,
            ),
            ScenarioStep(
                "settle-first-screen",
                "capture",
                "",
                "gateway-bootstrap-first-screen",
                timeout_s=0.3,
            ),
        ),
        expected_text=(
            "OpenSquilla",
            "Build with your agent. Stay in the flow.",
            "Fresh Gateway session",
            "AGENT",
            "send a message",
        ),
        requires_tmux=True,
    )

    result = run_real_terminal_scenario(scenario)

    assert result.status == "pass"
    frames = result.run_dir / "frames"
    frame = next(frames.glob("*-gateway-bootstrap-first-screen.txt")).read_text(encoding="utf-8")
    assert frame.count("OpenSquilla") == 1
    assert frame.count("send a message") == 1
    assert (result.run_dir / "opentui-app.log").is_file()


def test_resumed_gateway_bootstrap_hydrates_history_before_first_input(
    run_real_terminal_scenario,
) -> None:
    """The same startup order must replay a non-empty canonical snapshot."""

    scenario = TuiScenario(
        scenario_id="gateway_resumed_bootstrap_startup",
        family="launch_and_input_loop",
        initial_size=TerminalSize(cols=120, rows=30),
        steps=(
            ScenarioStep(
                "wait-history",
                "wait_text",
                "BOOTSTRAP_ASSISTANT_HISTORY",
                "gateway-history-hydrated",
                timeout_s=10.0,
            ),
            ScenarioStep(
                "settle-history",
                "capture",
                "",
                "gateway-history-first-screen",
                timeout_s=0.3,
            ),
        ),
        expected_text=(
            "BOOTSTRAP_USER_HISTORY",
            "BOOTSTRAP_ASSISTANT_HISTORY",
            "Resumed Gateway session",
            "send a message",
        ),
        requires_tmux=True,
    )

    result = run_real_terminal_scenario(scenario)

    assert result.status == "pass"
    frame = next((result.run_dir / "frames").glob("*-gateway-history-first-screen.txt")).read_text(
        encoding="utf-8"
    )
    assert frame.count("BOOTSTRAP_USER_HISTORY") == 1
    assert frame.count("BOOTSTRAP_ASSISTANT_HISTORY") == 1
    # A resumed transcript gets the compact header brand, not the large
    # empty-session logo/tagline embedded into conversation history.
    assert "Build with your agent. Stay in the flow." not in frame
