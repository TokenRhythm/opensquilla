from __future__ import annotations

from pathlib import Path

import pytest

from tui_real_terminal.driver import (
    TerminalSize,
    build_run_id,
    open_real_terminal_session,
    probe_terminal_capabilities,
)
from tui_real_terminal.evidence import EvidenceBundle
from tui_real_terminal.framebuffer import assert_opentui_framebuffer
from tui_real_terminal.targets import (
    TUI_READY_TIMEOUT_SECONDS,
    TargetContext,
    build_tui_target,
)

pytestmark = pytest.mark.tui_real_terminal

_SIZE = TerminalSize(cols=140, rows=36)


def test_sgr_wheel_holds_streaming_view_and_end_restores_follow(
    artifact_root: Path,
    pytestconfig: pytest.Config,
) -> None:
    """Exercise the exact SGR wheel path used by Terminal/iTerm/VS Code."""

    capabilities = probe_terminal_capabilities()
    if not capabilities.tmux_available:
        reason = "SGR wheel framebuffer gate requires tmux"
        if bool(pytestconfig.getoption("--tui-require-capabilities")):
            pytest.fail(f"required real-terminal capability is unavailable: {reason}")
        pytest.skip(reason)

    evidence = EvidenceBundle.create(
        artifact_root,
        scenario_id="sgr_mouse_scroll_streaming",
        backend_id="opentui",
    )
    evidence.write_scenario(
        {
            "scenario_id": "sgr_mouse_scroll_streaming",
            "family": "terminal_mouse_scroll",
            "initial_size": {"cols": _SIZE.cols, "rows": _SIZE.rows},
            "requires_tmux": True,
        }
    )
    target = build_tui_target(
        "opentui",
        TargetContext(
            project_root=Path.cwd(),
            artifact_dir=evidence.run_dir,
            # Reuse the deterministic 80-token fake provider while recording
            # this independent wheel-specific evidence bundle.
            scenario_id="long_streaming",
            size=_SIZE,
        ),
    )
    if not target.available:
        pytest.skip(target.skip_reason or "OpenTUI test target is unavailable")
    target.env["OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS"] = "0"
    # Keep a deterministic live window after token 055 even when the complete
    # real-terminal suite is contending for CPU. This gate must exercise wheel
    # handling during an active stream, not accidentally after finalization.
    target.env["OPENSQUILLA_TUI_FAKE_STREAM_DELAY_S"] = "0.08"

    session = open_real_terminal_session(
        command=target.command,
        cwd=Path.cwd(),
        env=target.env,
        run_id=build_run_id("sgr_mouse_scroll_streaming"),
        size=target.initial_size,
        artifact_dir=evidence.run_dir,
        driver="tmux",
    )
    session.start()
    try:
        session.wait_for_text(
            "OPEN_SQUILLA_TUI_READY",
            timeout_s=TUI_READY_TIMEOUT_SECONDS,
            checkpoint="mouse-scroll-ready",
        )
        session.send_text("stream please")
        session.wait_for_text(
            # The framebuffer is already growing here, while OpenTUI can still
            # expose the previous Yoga scrollHeight for one event turn. This
            # deliberately gates the upward *intent* across that pending layout
            # instead of waiting until the native scrollbar has settled.
            "stream-token-055",
            timeout_s=10.0,
            checkpoint="before-wheel",
        )

        session.mouse_scroll("up", ticks=2, x=10, y=12)
        held = session.wait_for_text(
            "↓ new output · End to follow",
            timeout_s=4.0,
            checkpoint="wheel-held-during-stream",
        )
        evidence.record_frame(held)
        held_framebuffer = session.capture_framebuffer("wheel-held-during-stream")
        assert held_framebuffer is not None
        evidence.record_framebuffer(held_framebuffer)
        assert_opentui_framebuffer(
            held_framebuffer,
            cursor=session.cursor_position(),
        )
        assert held.text.count("OpenSquilla · Session") == 1
        assert held.text.count("steer current turn · Tab queues") == 1

        session.send_key("End")
        followed = session.wait_for_text(
            "stream-token-079",
            timeout_s=6.0,
            checkpoint="end-restored-follow",
        )
        evidence.record_frame(followed)
        assert "↓ new output · End to follow" not in followed.text
        followed_framebuffer = session.capture_framebuffer("end-restored-follow")
        assert followed_framebuffer is not None
        evidence.record_framebuffer(followed_framebuffer)
        assert_opentui_framebuffer(
            followed_framebuffer,
            cursor=session.cursor_position(),
        )
        evidence.write_scrollback(session.capture_scrollback_text("scrollback"))
    finally:
        session.terminate()
