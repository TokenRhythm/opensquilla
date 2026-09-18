from __future__ import annotations

from pathlib import Path

import pytest
from wcwidth import wcswidth

from tui_real_terminal.assertions import (
    assert_no_completion_menu_overlap,
    assert_no_stale_completion_menu,
)
from tui_real_terminal.driver import TerminalFrame, TerminalSize
from tui_real_terminal.evidence import ScenarioResult
from tui_real_terminal.scenarios import scenario_by_id

pytestmark = pytest.mark.tui_real_terminal


def test_completion_menu_overlap_assertion_accepts_clean_menu() -> None:
    frame = _frame(
        "\n".join(
            (
                "conversation content above the composer",
                " ╭ commands ──────────────────────────────╮",
                " │ › /compact  Compact the conversation   │",
                " │   /resume   Resume an existing session │",
                " ╰────────────────────────────────────────╯",
                " │ send a massage                         │",
            )
        )
    )

    assert_no_completion_menu_overlap(frame)


def test_completion_menu_overlap_assertion_rejects_interleaved_output() -> None:
    frame = _frame(
        "\n".join(
            (
                "conversation content above the composer",
                " ╭ commands ───── fake-response:hello ───╮",
                " │ › /compact  Compact the conversation   │",
                " ╰────────────────────────────────────────╯",
            )
        )
    )

    with pytest.raises(AssertionError, match="completion menu overlap"):
        assert_no_completion_menu_overlap(frame)


@pytest.mark.parametrize("description", ["中文说明", "👩‍💻 编程", "cafe\u0301", "🦐 tools"])
@pytest.mark.parametrize("displaced_edge", [False, True])
def test_completion_menu_checks_unicode_cell_geometry(description, displaced_edge) -> None:
    width = 40
    text = f" › /example  {description}"
    padding = width - wcswidth(text) + int(displaced_edge)
    frame = _frame("\n".join((
        " ╭ commands " + "─" * (width - len(" commands ")) + "╮",
        " │" + text + " " * padding + "│",
        " ╰" + "─" * width + "╯",
    )))

    if displaced_edge:
        with pytest.raises(AssertionError, match="broken vertical border"):
            assert_no_completion_menu_overlap(frame)
    else:
        assert_no_completion_menu_overlap(frame)


def test_stale_completion_menu_assertion_accepts_cleared_frame() -> None:
    frame = _frame(
        "\n".join(
            (
                "conversation content remains visible",
                " │ /co                                   │",
                " ╰──────────────────────────────────────╯",
            )
        ),
        checkpoint="after-close",
    )

    assert_no_stale_completion_menu(frame)


def test_stale_completion_menu_assertion_rejects_leftover_menu() -> None:
    frame = _frame(
        "\n".join(
            (
                "conversation content remains visible",
                " ╭ commands ─────────────────────────────╮",
                " │ › /compact  Compact the conversation   │",
                " ╰────────────────────────────────────────╯",
            )
        ),
        checkpoint="after-close",
    )

    with pytest.raises(AssertionError, match="stale completion menu"):
        assert_no_stale_completion_menu(frame)


def test_slash_completion_menu_renders_without_overlap(run_real_terminal_scenario) -> None:
    result = run_real_terminal_scenario(scenario_by_id("completion_slash_menu_filter"))

    assert result.status == "pass"
    frame = _read_frame(result, "slash-menu-filtered", TerminalSize(cols=100, rows=30))
    assert " commands " in frame.text
    assert "/compact" in frame.text
    assert_no_completion_menu_overlap(frame)


def test_completion_menu_resize_does_not_leave_overlap(
    run_real_terminal_scenario,
) -> None:
    result = run_real_terminal_scenario(scenario_by_id("completion_menu_resize"))

    assert result.status == "pass"
    for checkpoint, size in (
        ("after-narrow-completion-menu", TerminalSize(cols=72, rows=24)),
        ("after-wide-completion-menu", TerminalSize(cols=120, rows=34)),
        ("after-resize-completion-menu", TerminalSize(cols=120, rows=34)),
    ):
        frame = _read_frame(result, checkpoint, size)
        assert " commands " in frame.text
        assert_no_completion_menu_overlap(frame)


def test_file_completion_menu_closes_without_stale_overlay(
    run_real_terminal_scenario,
) -> None:
    result = run_real_terminal_scenario(scenario_by_id("completion_file_menu_escape"))

    assert result.status == "pass"
    file_menu = _read_frame(result, "file-menu-open", TerminalSize(cols=100, rows=30))
    assert " files " in file_menu.text
    assert_no_completion_menu_overlap(file_menu)
    assert_no_stale_completion_menu(
        _read_frame(result, "after-close-file-menu", TerminalSize(cols=100, rows=30))
    )


def _frame(text: str, *, checkpoint: str = "menu") -> TerminalFrame:
    rows = max(1, len(text.splitlines()))
    return TerminalFrame(checkpoint, text, 1, TerminalSize(cols=80, rows=rows))


def _read_frame(
    result: ScenarioResult,
    checkpoint: str,
    size: TerminalSize,
) -> TerminalFrame:
    # ScenarioResult intentionally exposes the artifact directory; frame-level
    # assertions read the checkpoint evidence without broadening run_scenario.
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
    available = ", ".join(
        path.name for path in sorted((run_dir / "frames").glob("*.txt"))
    )
    raise AssertionError(
        f"expected exactly one frame for checkpoint {checkpoint!r}; available: {available}"
    )
