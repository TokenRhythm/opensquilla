from __future__ import annotations

import pytest

from tui_real_terminal.scenarios import scenario_by_id

pytestmark = pytest.mark.tui_real_terminal


def test_long_streaming_output(run_real_terminal_scenario) -> None:
    result = run_real_terminal_scenario(scenario_by_id("long_streaming"))

    assert result.status == "pass"
    assert (result.run_dir / "frames").is_dir()
    assert (result.run_dir / "framebuffers").is_dir()
    assert (result.run_dir / "transcript.txt").exists()
    assert (result.run_dir / "scrollback.txt").exists()
    scrollback = (result.run_dir / "scrollback.txt").read_text(encoding="utf-8")
    assert "stream-token-000" in scrollback
    assert "stream-token-079" in scrollback

    frames = result.run_dir / "frames"
    narrow = next(frames.glob("*-after-stream-narrow.txt")).read_text(encoding="utf-8")
    restored = next(frames.glob("*-stream-restored-wide.txt")).read_text(encoding="utf-8")

    # Both captures must be from the active stream, not a post-completion resize
    # that would let a broken incremental renderer pass accidentally.
    assert "stream-token-035" in narrow
    assert "stream-token-079" not in narrow
    assert "stream-token-035" in restored
    assert "stream-token-079" not in restored

    # 72 columns collapses the right context rail; 140 columns restores exactly
    # one copy. The composer must likewise have only one current surface.
    assert "│ AGENT" not in narrow
    assert restored.count("│ AGENT") == 1
    # The composer remains live during streaming and advertises the busy Enter
    # disposition instead of the idle placeholder. It must still exist once.
    assert narrow.count("steer current turn · Tab queues") == 1
    assert restored.count("steer current turn · Tab queues") == 1

    styled = result.run_dir / "framebuffers"
    assert len(tuple(styled.glob("*-after-stream-narrow.ansi"))) == 1
    assert len(tuple(styled.glob("*-after-stream-narrow.json"))) == 1
    assert len(tuple(styled.glob("*-stream-restored-wide.ansi"))) == 1
    assert len(tuple(styled.glob("*-stream-restored-wide.json"))) == 1
