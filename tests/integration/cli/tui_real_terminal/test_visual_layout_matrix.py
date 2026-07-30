from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tui_real_terminal.driver import TerminalSize
from tui_real_terminal.scenarios import scenario_by_id

pytestmark = pytest.mark.tui_real_terminal

_CHECKPOINTS = (
    "during-ensemble",
    "during-reasoning",
    "during-intermediate",
    "during-tool",
    "during-answer",
    "after-usage",
    "after-complex",
)

_PHASE_CONTRACTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "during-ensemble": (
        "Ensemble · 0/2 complete",
        (
            "reasoning-process-streams-live",
            "intermediate-before-tool",
            "fake_tool",
            "complex-state-complete",
            "think 436",
        ),
    ),
    "during-reasoning": (
        "reasoning-process-streams-live",
        ("intermediate-before-tool", "fake_tool", "complex-state-complete", "think 436"),
    ),
    "during-intermediate": (
        "intermediate-before-tool",
        ("fake_tool", "complex-state-complete", "think 436"),
    ),
    "during-tool": ("fake_tool", ("complex-state-complete", "think 436")),
    "during-answer": ("complex-state-complete", ("think 436",)),
    "after-usage": ("think 436", ()),
}


@pytest.mark.parametrize(
    "size",
    (
        pytest.param(TerminalSize(cols=80, rows=24), id="80x24"),
        pytest.param(TerminalSize(cols=120, rows=30), id="120x30"),
        pytest.param(TerminalSize(cols=160, rows=40), id="160x40"),
    ),
)
def test_complex_turn_visual_layout_matrix(
    run_real_terminal_scenario,
    size: TerminalSize,
) -> None:
    """Gate the same live turn hierarchy at the three supported viewports.

    ``run_scenario`` performs the exact styled-framebuffer and hardware-cursor
    assertion at every checkpoint below.  This matrix additionally proves that
    every requested semantic stage produced evidence at the requested physical
    size and that the wide context rail crosses its breakpoint exactly once.
    """

    base = scenario_by_id("complex_ui_state")
    scenario = replace(
        base,
        initial_size=size,
        steps=(
            replace(base.steps[0], assert_framebuffer=True),
            *base.steps[1:],
        ),
        # At 80x24 older completed blocks may be correctly outside the visible
        # viewport; the per-stage checkpoints above carry those assertions.
        expected_text=("complex-state-complete", "think 436"),
    )
    result = run_real_terminal_scenario(scenario)

    assert result.status == "pass"
    phase_texts: dict[str, str] = {}
    for checkpoint in _CHECKPOINTS:
        matches = sorted(
            (result.run_dir / "framebuffers").glob(f"*-{checkpoint}.json")
        )
        assert len(matches) == 1, (checkpoint, matches)
        payload = json.loads(matches[0].read_text(encoding="utf-8"))
        assert payload["size"] == {"cols": size.cols, "rows": size.rows}
        phase_texts[checkpoint] = "\n".join(row["text"] for row in payload["rows"])

    # A wait-for-text-only harness can accidentally capture the same final
    # framebuffer for every semantic phase. The fake provider now waits for an
    # explicit ack after each capture; these required/future-marker contracts
    # prove that the evidence really represents that transient state.
    for checkpoint, (required, forbidden) in _PHASE_CONTRACTS.items():
        text = phase_texts[checkpoint]
        assert required in text, (checkpoint, required)
        for future_marker in forbidden:
            assert future_marker not in text, (checkpoint, future_marker)
    semantic_frames = {phase_texts[checkpoint] for checkpoint in _PHASE_CONTRACTS}
    assert len(semantic_frames) == len(_PHASE_CONTRACTS)

    final_path = next(
        (result.run_dir / "framebuffers").glob("*-after-complex.json")
    )
    final = json.loads(final_path.read_text(encoding="utf-8"))
    final_text = "\n".join(row["text"] for row in final["rows"])
    if size.cols >= 132:
        assert final_text.count("│ AGENT") == 1
        assert final_text.count("│ RUNTIME") == 1
    else:
        assert "│ AGENT" not in final_text
        assert "│ RUNTIME" not in final_text
