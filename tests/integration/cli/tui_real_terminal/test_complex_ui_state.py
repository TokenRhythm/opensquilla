from __future__ import annotations

from pathlib import Path

import pytest

from tui_real_terminal.assertions import assert_visible_text
from tui_real_terminal.driver import TerminalFrame, TerminalSize
from tui_real_terminal.evidence import ScenarioResult
from tui_real_terminal.scenarios import scenario_by_id

pytestmark = pytest.mark.tui_real_terminal


def test_complex_ui_state(run_real_terminal_scenario) -> None:
    scenario = scenario_by_id("complex_ui_state")
    result = run_real_terminal_scenario(scenario)

    assert result.status == "pass"
    assert (result.run_dir / "frames").is_dir()
    assert (result.run_dir / "transcript.txt").exists()
    intermediate_frame = _read_frame(
        result,
        "during-intermediate",
        scenario.initial_size,
    )
    reasoning_frame = _read_frame(
        result,
        "during-reasoning",
        scenario.initial_size,
    )
    ensemble_frame = _read_frame(
        result,
        "during-ensemble",
        scenario.initial_size,
    )
    final_frame = _read_frame(
        result,
        "after-complex",
        scenario.initial_size,
    )

    assert result.backend_id == "opentui"
    assert "Ensemble · 0/2 complete" in ensemble_frame.text
    assert "candidate-fast" not in ensemble_frame.text
    compact_status_lines = [
        line for line in ensemble_frame.text.splitlines() if "queue idle" in line
    ]
    assert compact_status_lines
    assert "router standard 99%" in compact_status_lines[-1]
    # While provider reasoning is active, the exact safe delta is visible in
    # the transcript under the live Thinking header; the pre-token waiting copy
    # has already been replaced in place.
    assert_visible_text(reasoning_frame, "reasoning-process-streams-live")
    assert "Thinking" in reasoning_frame.text
    assert "Waiting for model output" not in reasoning_frame.text
    # Intermediate narration is visible as purple thinking text, separate from
    # reasoning and from the final answer card.
    assert_visible_text(intermediate_frame, "intermediate-before-tool")
    intermediate_lines = [
        line for line in intermediate_frame.text.splitlines() if "intermediate-before-tool" in line
    ]
    assert intermediate_lines
    # The card rail is part of the captured terminal frame; the visible
    # intermediate marker itself follows it and uses the Host's canonical
    # thinking-accent glyph.
    assert intermediate_lines[0].lstrip().startswith("│  ✻ ")
    assert "second-intermediate-line" in intermediate_frame.text
    # Once the model moves on to narration, completed reasoning keeps its latest
    # eight visual rows. The opening rows fold out, real provider content stays
    # visible, and the complete payload remains discoverable via Ctrl+O.
    assert "reasoning-opening-context" not in intermediate_frame.text
    assert "reasoning-process-streams-live" in intermediate_frame.text
    assert "Thinking" not in intermediate_frame.text
    assert "Thought for" in intermediate_frame.text
    assert "4 earlier · Ctrl+O details" in intermediate_frame.text
    assert_visible_text(final_frame, "think 436")
    assert "Ensemble · 2/2 complete" in final_frame.text
    assert "in 36,060 / out 1,047 / think 436" in final_frame.text


def _read_frame(
    result: ScenarioResult,
    checkpoint: str,
    size: TerminalSize,
) -> TerminalFrame:
    frame_path = _frame_path(result.run_dir, checkpoint)
    return TerminalFrame(
        checkpoint,
        frame_path.read_text(encoding="utf-8"),
        0,
        size,
    )


def _frame_path(run_dir: Path, checkpoint: str) -> Path:
    matches = sorted((run_dir / "frames").glob(f"*-{checkpoint}.txt"))
    if len(matches) == 1:
        return matches[0]
    available = ", ".join(path.name for path in sorted((run_dir / "frames").glob("*.txt")))
    raise AssertionError(
        f"expected exactly one frame for checkpoint {checkpoint!r}; available: {available}"
    )
