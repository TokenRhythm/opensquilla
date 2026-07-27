from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HARNESS_PARENT = Path(__file__).resolve().parents[1]
if str(HARNESS_PARENT) not in sys.path:
    sys.path.insert(0, str(HARNESS_PARENT))

from tui_real_terminal.assertions import (  # noqa: E402
    assert_no_completion_menu_overlap,
    assert_no_inline_prompt_chrome_collision,
    assert_no_raw_ansi_leakage,
    assert_no_stale_completion_menu,
    assert_prompt_ready,
    assert_visible_text,
)
from tui_real_terminal.driver import (  # noqa: E402
    TerminalFrame,
    TerminalSize,
)
from tui_real_terminal.evidence import (  # noqa: E402
    EvidenceBundle,
    ScenarioResult,
)
from tui_real_terminal.scenarios import all_scenarios, scenario_by_id  # noqa: E402
from tui_real_terminal.visual import build_visual_verdict  # noqa: E402


def test_all_abcd_scenarios_are_declared() -> None:
    scenarios = {scenario.scenario_id: scenario for scenario in all_scenarios()}

    assert set(scenarios) == {
        "launch_input_loop",
        "cjk_input_loop",
        "long_streaming",
        "complex_ui_state",
        "architecture_prompt",
        "completion_file_menu_escape",
        "completion_menu_preserves_history",
        "completion_menu_resize",
        "completion_slash_menu_filter",
        "live_opentui_architecture_prompt",
        "terminal_changes",
    }
    assert scenarios["launch_input_loop"].family == "launch_and_input_loop"
    assert scenarios["cjk_input_loop"].family == "launch_and_input_loop"
    assert scenarios["long_streaming"].family == "long_streaming_output"
    assert scenarios["complex_ui_state"].family == "complex_ui_state"
    assert scenarios["complex_ui_state"].requires_tmux is True
    assert scenarios["architecture_prompt"].family == "architecture_prompt"
    assert scenarios["completion_file_menu_escape"].family == "completion_menu"
    assert scenarios["completion_menu_preserves_history"].family == "completion_menu"
    assert scenarios["completion_menu_resize"].family == "completion_menu"
    assert scenarios["completion_slash_menu_filter"].family == "completion_menu"
    assert scenarios["live_opentui_architecture_prompt"].family == "live_prompt"
    assert scenarios["terminal_changes"].family == "terminal_changes"
    assert scenarios["live_opentui_architecture_prompt"].requires_tmux is True
    assert scenarios["live_opentui_architecture_prompt"].requires_prompt_ready is False
    assert (
        scenarios["live_opentui_architecture_prompt"].required_backend_id
        == "live-opentui"
    )


def test_launch_scenario_serializes_to_json(tmp_path: Path) -> None:
    scenario = scenario_by_id("launch_input_loop")
    bundle = EvidenceBundle.create(
        tmp_path,
        scenario_id=scenario.scenario_id,
        backend_id="terminal",
    )

    bundle.write_scenario(scenario.to_json_dict())

    data = json.loads((bundle.run_dir / "scenario.json").read_text())
    assert data["scenario_id"] == "launch_input_loop"
    assert data["family"] == "launch_and_input_loop"
    assert data["initial_size"] == {"cols": 100, "rows": 30}


def test_complex_ui_state_captures_intermediate_frame_before_final_state() -> None:
    scenario = scenario_by_id("complex_ui_state")
    reasoning_steps = [
        step for step in scenario.steps if step.checkpoint == "during-reasoning"
    ]
    intermediate_steps = [
        step for step in scenario.steps if step.checkpoint == "during-intermediate"
    ]

    assert len(reasoning_steps) == 1
    assert reasoning_steps[0].action == "wait_text"
    assert reasoning_steps[0].value == "reasoning-process-streams-live"
    assert len(intermediate_steps) == 1
    assert intermediate_steps[0].action == "wait_text"
    assert intermediate_steps[0].value == "intermediate-before-tool"
    assert reasoning_steps[0].assert_framebuffer is True
    assert intermediate_steps[0].assert_framebuffer is True
    assert {
        step.checkpoint
        for step in scenario.steps
        if step.assert_framebuffer
    } == {
        "during-ensemble",
        "during-reasoning",
        "during-intermediate",
        "during-tool",
        "during-answer",
        "after-usage",
        "after-complex",
    }
    phases = ("ensemble", "reasoning", "intermediate", "tool", "answer", "usage")
    step_ids = [step.step_id for step in scenario.steps]
    for phase in phases:
        wait_id = "wait-usage" if phase == "usage" else f"wait-{phase}"
        assert step_ids.index(wait_id) < step_ids.index(f"ack-{phase}")
        ack = next(step for step in scenario.steps if step.step_id == f"ack-{phase}")
        assert ack.action == "ack_phase"
        assert ack.value == phase


def test_visible_text_assertion_includes_checkpoint() -> None:
    frame = TerminalFrame("after-input", "hello world", 1, TerminalSize())

    with pytest.raises(AssertionError, match="after-input"):
        assert_visible_text(frame, "missing")


def test_prompt_ready_accepts_visible_you_prompt() -> None:
    frame = TerminalFrame("ready", "◢ you  ", 1, TerminalSize())

    assert_prompt_ready(frame)


def test_inline_prompt_chrome_collision_rejects_partial_prompt_redraw() -> None:
    frame = TerminalFrame("after-turn", " │ s### heading\nbody", 1, TerminalSize())

    with pytest.raises(AssertionError, match="inline prompt chrome overlapped"):
        assert_no_inline_prompt_chrome_collision(frame)


def test_inline_prompt_chrome_collision_accepts_placeholder_row() -> None:
    frame = TerminalFrame("ready", " │ send a massage │", 1, TerminalSize())

    assert_no_inline_prompt_chrome_collision(frame)


def test_ansi_leakage_assertion_rejects_raw_escape() -> None:
    frame = TerminalFrame("after-stream", "safe \x1b[2J unsafe", 1, TerminalSize())

    with pytest.raises(AssertionError, match="raw terminal escape"):
        assert_no_raw_ansi_leakage(frame)


def test_completion_menu_overlap_rejects_dirty_border() -> None:
    frame = TerminalFrame(
        "menu",
        " ╭ commands ─ fake-response:hello ─╮\n"
        " │ › /compact  Compact history     │\n"
        " ╰─────────────────────────────────╯",
        1,
        TerminalSize(),
    )

    with pytest.raises(AssertionError, match="completion menu overlap"):
        assert_no_completion_menu_overlap(frame)


def test_stale_completion_menu_rejects_leftover_title_border() -> None:
    frame = TerminalFrame(
        "after-close",
        " ╭ commands ───────────────────────╮\n"
        " │ › /compact  Compact history     │\n"
        " ╰─────────────────────────────────╯",
        1,
        TerminalSize(),
    )

    with pytest.raises(AssertionError, match="stale completion menu"):
        assert_no_stale_completion_menu(frame)


def test_evidence_bundle_writes_required_artifacts(tmp_path: Path) -> None:
    bundle = EvidenceBundle.create(
        tmp_path,
        scenario_id="launch_input_loop",
        backend_id="terminal",
    )
    frame = TerminalFrame("ready", "OPEN_SQUILLA_TUI_READY", 1, TerminalSize())

    bundle.write_scenario({"scenario_id": "launch_input_loop"})
    frame_path = bundle.record_frame(frame)
    bundle.write_visual_verdict(
        {
            "status": "inspect",
            "severity": "inspect-only",
            "affected_region": "terminal",
            "symptom": "screenshot unavailable",
            "suspected_cause": "text-only run",
            "recommended_next_action": "review transcript",
        }
    )
    bundle.write_result(
        ScenarioResult(
            scenario_id="launch_input_loop",
            backend_id="terminal",
            status="pass",
            run_dir=bundle.run_dir,
        )
    )

    assert (bundle.run_dir / "scenario.json").exists()
    assert (bundle.run_dir / "terminal.log").exists()
    assert (bundle.run_dir / "app.log").exists()
    assert (bundle.run_dir / "transcript.txt").exists()
    assert (bundle.run_dir / "scrollback.txt").exists()
    assert frame_path == bundle.run_dir / "frames" / "000-ready.txt"
    assert frame_path.exists()
    assert (bundle.run_dir / "screenshots").is_dir()
    assert (bundle.run_dir / "visual-verdict.json").exists()
    assert (bundle.run_dir / "result.json").exists()


def test_visual_verdict_contract_defaults_to_inspect_without_screenshot() -> None:
    verdict = build_visual_verdict(
        scenario_id="launch_input_loop",
        checkpoint="after-response",
        backend_id="terminal",
        terminal_size={"cols": 100, "rows": 30},
        screenshot_path=None,
        frame_path="frames/003-after-response.txt",
        expected_visible_regions=("prompt", "assistant stream"),
    )

    assert verdict["status"] == "inspect"
    assert verdict["severity"] == "inspect-only"
    assert verdict["affected_region"] == "terminal"
    assert verdict["recommended_next_action"]
    assert verdict["input"]["failure_modes"]
