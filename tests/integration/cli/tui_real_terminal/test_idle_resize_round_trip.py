from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from tui_real_terminal.driver import (
    RealTerminalSession,
    TerminalSize,
    build_run_id,
    open_real_terminal_session,
    probe_terminal_capabilities,
)
from tui_real_terminal.evidence import EvidenceBundle, ScenarioResult
from tui_real_terminal.framebuffer import (
    FOOTER_BACKGROUND,
    StyledFramebuffer,
    assert_opentui_framebuffer,
)
from tui_real_terminal.targets import (
    TUI_READY_TIMEOUT_SECONDS,
    TargetContext,
    build_tui_target,
)

pytestmark = pytest.mark.tui_real_terminal

_SCENARIO_ID = "idle_resize_round_trip"
_COLLAPSED_STREAM_SCENARIO_ID = "collapsed_stream_resize_round_trip"
_WELCOME_REMOUNT_SCENARIO_ID = "welcome_resize_remount"
_COLLAPSED = TerminalSize(cols=18, rows=24)
_COMPACT = TerminalSize(cols=30, rows=24)
_NARROW = TerminalSize(cols=72, rows=24)
_LARGE = TerminalSize(cols=160, rows=40)
_TRANSCRIPT_MARKER = "fake-response:idle resize transcript"
_STREAM_TRANSCRIPT_MARKER = "complex-state-complete"


def _reset_tmux_surface(run_id: str) -> None:
    # tmux -R clears the emulated physical grid without writing a recovery
    # event to the child. Issuing it inside the resize debounce window models
    # Codex discarding and remounting the side-terminal surface.
    subprocess.run(["tmux", "send-keys", "-R", "-t", run_id], check=True)


def _assert_idle_frame(
    frame: StyledFramebuffer,
    *,
    expected_size: TerminalSize,
    cursor: tuple[int, int],
    transcript_marker: str = _TRANSCRIPT_MARKER,
    required_rail_headings: tuple[str, ...] = ("AGENT",),
) -> None:
    assert (frame.cols, frame.rows) == (expected_size.cols, expected_size.rows)
    # This minimal idle fixture projects canonical agent context but no live
    # runtime/routing event; the geometry contract still requires the rail and
    # every fixed cell without inventing a RUNTIME heading.
    assert_opentui_framebuffer(
        frame,
        required_rail_headings=required_rail_headings,
        cursor=cursor,
    )
    assert transcript_marker in frame.text

    geometry = frame.geometry
    rail_width = geometry.rail_width
    content_width = geometry.content_width
    footer_top = geometry.footer_top
    top_row = geometry.composer_top
    bottom_row = geometry.composer_bottom

    top_border = "".join(
        frame.cells[top_row][column].glyph for column in range(1, content_width - 1)
    )
    bottom_border = "".join(
        frame.cells[bottom_row][column].glyph for column in range(1, content_width - 1)
    )
    assert top_border == "╭" + "─" * (content_width - 4) + "╮"
    assert bottom_border == "╰" + "─" * (content_width - 4) + "╯"

    # The one-cell gutter to the right of the composer is where stale cells
    # from the previous geometry showed up after a wide-to-narrow transition.
    # It must be a fully repainted blank footer column at every footer row.
    right_gutter = content_width - 1
    for row in range(footer_top, frame.rows):
        cell = frame.cells[row][right_gutter]
        assert cell.glyph == " ", (row, right_gutter, cell)
        assert cell.background == FOOTER_BACKGROUND, (row, right_gutter, cell)

    if rail_width:
        for row in range(footer_top):
            text = frame.row_text(row)
            offset = text.find(transcript_marker)
            if offset >= 0:
                assert offset + len(transcript_marker) <= content_width, (
                    row,
                    offset,
                    content_width,
                )


def _wait_for_idle_frame(
    *,
    session: RealTerminalSession,
    evidence: EvidenceBundle,
    checkpoint: str,
    expected_size: TerminalSize,
    transcript_marker: str = _TRANSCRIPT_MARKER,
    required_rail_headings: tuple[str, ...] = ("AGENT",),
    timeout_s: float = 1.5,
) -> StyledFramebuffer:
    deadline = time.monotonic() + timeout_s
    last_frame: StyledFramebuffer | None = None
    last_error: AssertionError | None = None
    while time.monotonic() < deadline:
        last_frame = session.capture_framebuffer(checkpoint)
        assert last_frame is not None
        cursor = session.cursor_position()
        assert cursor is not None
        try:
            _assert_idle_frame(
                last_frame,
                expected_size=expected_size,
                cursor=cursor,
                transcript_marker=transcript_marker,
                required_rail_headings=required_rail_headings,
            )
        except AssertionError as exc:
            last_error = exc
            time.sleep(0.05)
            continue
        evidence.record_framebuffer(last_frame)
        evidence.record_frame(session.capture_text(checkpoint))
        return last_frame

    assert last_frame is not None
    evidence.record_framebuffer(last_frame)
    evidence.record_frame(session.capture_text(checkpoint))
    raise AssertionError(
        f"{checkpoint} never reached a clean idle framebuffer; "
        f"last failure: {last_error}; artifacts: {evidence.run_dir}"
    )


def _resize_rapidly(
    session: RealTerminalSession,
    sizes: tuple[TerminalSize, ...],
) -> None:
    for size in sizes:
        session.resize(size)
        # Keep all SIGWINCH events inside OpenTUI's debounce window while still
        # giving tmux time to apply each real PTY geometry.
        time.sleep(0.015)


def _assert_collapsed_frame(
    frame: StyledFramebuffer,
    *,
    cursor: tuple[int, int],
) -> None:
    assert (frame.cols, frame.rows) == (_COLLAPSED.cols, _COLLAPSED.rows)

    # At 18 columns the placeholder and status label intentionally elide, so
    # use the exact cell geometry rather than the normal copy-bearing gate.
    geometry = frame.geometry
    content_width = geometry.content_width
    footer_top = geometry.footer_top
    top_row = geometry.composer_top
    bottom_row = geometry.composer_bottom
    top_border = "".join(
        frame.cells[top_row][column].glyph for column in range(1, content_width - 1)
    )
    bottom_border = "".join(
        frame.cells[bottom_row][column].glyph for column in range(1, content_width - 1)
    )
    assert top_border == "╭" + "─" * (content_width - 4) + "╮"
    assert bottom_border == "╰" + "─" * (content_width - 4) + "╯"

    right_gutter = content_width - 1
    for row in range(footer_top, frame.rows):
        cell = frame.cells[row][right_gutter]
        assert cell.glyph == " ", (row, right_gutter, cell)
        assert cell.background == FOOTER_BACKGROUND, (row, right_gutter, cell)

    cursor_x, cursor_y = cursor
    assert 1 < cursor_x < content_width - 2, cursor
    assert top_row < cursor_y < bottom_row, cursor
    # The semantic marker wraps at its hyphen in 18 columns. Both physical
    # fragments must remain visible; joining them would hide real cell loss.
    assert "complex-state-" in frame.text
    assert "complete tool-" in frame.text
    assert all(fragment not in frame.text for fragment in ("GENT", "AFETY", "OUTING"))


def _wait_for_collapsed_frame(
    *,
    session: RealTerminalSession,
    evidence: EvidenceBundle,
    checkpoint: str,
    timeout_s: float = 1.5,
) -> StyledFramebuffer:
    deadline = time.monotonic() + timeout_s
    last_frame: StyledFramebuffer | None = None
    last_error: AssertionError | None = None
    while time.monotonic() < deadline:
        last_frame = session.capture_framebuffer(checkpoint)
        assert last_frame is not None
        cursor = session.cursor_position()
        assert cursor is not None
        try:
            _assert_collapsed_frame(
                last_frame,
                cursor=cursor,
            )
        except AssertionError as exc:
            last_error = exc
            time.sleep(0.05)
            continue
        evidence.record_framebuffer(last_frame)
        evidence.record_frame(session.capture_text(checkpoint))
        return last_frame

    assert last_frame is not None
    evidence.record_framebuffer(last_frame)
    evidence.record_frame(session.capture_text(checkpoint))
    raise AssertionError(
        f"{checkpoint} never reached a clean collapsed framebuffer; "
        f"last failure: {last_error}; artifacts: {evidence.run_dir}"
    )


def _assert_welcome_frame(
    frame: StyledFramebuffer,
    *,
    cursor: tuple[int, int],
) -> None:
    assert (frame.cols, frame.rows) == (_LARGE.cols, _LARGE.rows)
    assert_opentui_framebuffer(
        frame,
        required_rail_headings=("AGENT",),
        cursor=cursor,
    )
    assert "Build with your agent. Stay in the flow." in frame.text

    block_logo_cells = sum(
        cell.glyph in "█▀▄╔╗╚╝║═"
        for row in frame.cells[: frame.geometry.footer_top]
        for cell in row
    )
    assert block_logo_cells >= 30

    geometry = frame.geometry
    content_width = geometry.content_width
    top_row = geometry.composer_top
    bottom_row = geometry.composer_bottom
    top_border = "".join(
        frame.cells[top_row][column].glyph for column in range(1, content_width - 1)
    )
    bottom_border = "".join(
        frame.cells[bottom_row][column].glyph for column in range(1, content_width - 1)
    )
    assert top_border == "╭" + "─" * (content_width - 4) + "╮"
    assert bottom_border == "╰" + "─" * (content_width - 4) + "╯"


def _wait_for_welcome_frame(
    *,
    session: RealTerminalSession,
    evidence: EvidenceBundle,
    checkpoint: str,
    timeout_s: float = 2.0,
) -> StyledFramebuffer:
    deadline = time.monotonic() + timeout_s
    last_frame: StyledFramebuffer | None = None
    last_error: AssertionError | None = None
    while time.monotonic() < deadline:
        last_frame = session.capture_framebuffer(checkpoint)
        assert last_frame is not None
        cursor = session.cursor_position()
        assert cursor is not None
        try:
            _assert_welcome_frame(
                last_frame,
                cursor=cursor,
            )
        except AssertionError as exc:
            last_error = exc
            time.sleep(0.05)
            continue
        evidence.record_framebuffer(last_frame)
        evidence.record_frame(session.capture_text(checkpoint))
        return last_frame

    assert last_frame is not None
    evidence.record_framebuffer(last_frame)
    evidence.record_frame(session.capture_text(checkpoint))
    raise AssertionError(
        f"{checkpoint} never recovered the welcome framebuffer; "
        f"last failure: {last_error}; artifacts: {evidence.run_dir}"
    )


def test_idle_resize_narrow_large_narrow_repaints_complete_framebuffer(
    artifact_root: Path,
    pytestconfig: pytest.Config,
) -> None:
    capabilities = probe_terminal_capabilities()
    if not capabilities.tmux_available:
        reason = "idle resize framebuffer regression requires tmux cell capture"
        if bool(pytestconfig.getoption("--tui-require-capabilities")):
            pytest.fail(f"required real-terminal capability is unavailable: {reason}")
        pytest.skip(reason)

    evidence = EvidenceBundle.create(
        artifact_root,
        scenario_id=_SCENARIO_ID,
        backend_id="opentui",
    )
    evidence.write_scenario(
        {
            "scenario_id": _SCENARIO_ID,
            "family": "idle_terminal_resize_round_trip",
            "initial_size": {"cols": _NARROW.cols, "rows": _NARROW.rows},
            "resize_sequence": [
                {"cols": _LARGE.cols, "rows": _LARGE.rows},
                {"cols": _NARROW.cols, "rows": _NARROW.rows},
            ],
            "requires_tmux": True,
        }
    )
    target = build_tui_target(
        "opentui",
        TargetContext(
            project_root=Path.cwd(),
            artifact_dir=evidence.run_dir,
            scenario_id=_SCENARIO_ID,
            size=_NARROW,
        ),
    )
    if not target.available:
        pytest.skip(target.skip_reason or "OpenTUI test target is unavailable")
    target.env.update(
        {
            "OPENSQUILLA_TUI_THEME": "opensquilla-dark",
            "OPENSQUILLA_TUI_COLOR": "truecolor",
            # Prove the resize path itself; a periodic full repaint must not
            # hide stale geometry left by the resize handler.
            "OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS": "0",
        }
    )

    session = open_real_terminal_session(
        command=target.command,
        cwd=Path.cwd(),
        env=target.env,
        run_id=build_run_id(_SCENARIO_ID),
        size=target.initial_size,
        artifact_dir=evidence.run_dir,
        driver="tmux",
    )

    session.start()
    try:
        session.wait_for_text(
            "OPEN_SQUILLA_TUI_READY",
            timeout_s=TUI_READY_TIMEOUT_SECONDS,
            checkpoint="idle-resize-ready",
        )
        session.send_text("idle resize transcript")
        completed = session.wait_for_text(
            "in 1 / out 2 · fake-terminal",
            timeout_s=8.0,
            checkpoint="idle-resize-turn-complete",
        )
        evidence.record_frame(completed)

        _wait_for_idle_frame(
            session=session,
            evidence=evidence,
            checkpoint="idle-narrow-before-resize",
            expected_size=_NARROW,
        )

        session.resize(_LARGE)
        _wait_for_idle_frame(
            session=session,
            evidence=evidence,
            checkpoint="idle-after-resize-large",
            expected_size=_LARGE,
        )

        session.resize(_NARROW)
        _wait_for_idle_frame(
            session=session,
            evidence=evidence,
            checkpoint="idle-after-resize-narrow",
            expected_size=_NARROW,
        )

        assert session.alternate_screen_active() is True
        assert session.is_alive() is True
        evidence.write_scrollback(session.capture_scrollback_text("scrollback"))
        evidence.write_result(
            ScenarioResult(
                scenario_id=_SCENARIO_ID,
                backend_id="opentui",
                status="pass",
                run_dir=evidence.run_dir,
            )
        )
    finally:
        session.terminate()


def test_collapsed_stream_resize_round_trip_recovers_without_stale_geometry(
    artifact_root: Path,
    pytestconfig: pytest.Config,
) -> None:
    capabilities = probe_terminal_capabilities()
    if not capabilities.tmux_available:
        reason = "collapsed streaming resize regression requires tmux cell capture"
        if bool(pytestconfig.getoption("--tui-require-capabilities")):
            pytest.fail(f"required real-terminal capability is unavailable: {reason}")
        pytest.skip(reason)

    evidence = EvidenceBundle.create(
        artifact_root,
        scenario_id=_COLLAPSED_STREAM_SCENARIO_ID,
        backend_id="opentui",
    )
    evidence.write_scenario(
        {
            "scenario_id": _COLLAPSED_STREAM_SCENARIO_ID,
            "family": "collapsed_streaming_resize_round_trip",
            "initial_size": {"cols": _COLLAPSED.cols, "rows": _COLLAPSED.rows},
            "resize_sequence": [
                {"cols": _COMPACT.cols, "rows": _COMPACT.rows},
                {"cols": _NARROW.cols, "rows": _NARROW.rows},
                {"cols": _LARGE.cols, "rows": _LARGE.rows},
                {"cols": _NARROW.cols, "rows": _NARROW.rows},
                {"cols": _COMPACT.cols, "rows": _COMPACT.rows},
                {"cols": _COLLAPSED.cols, "rows": _COLLAPSED.rows},
            ],
            "requires_tmux": True,
        }
    )
    target = build_tui_target(
        "opentui",
        TargetContext(
            project_root=Path.cwd(),
            artifact_dir=evidence.run_dir,
            # Reuse the deterministic context/ensemble/tool/answer stream. It
            # carries long mutable blocks across the rail breakpoint at 132.
            scenario_id="complex_ui_state",
            size=_COLLAPSED,
        ),
    )
    if not target.available:
        pytest.skip(target.skip_reason or "OpenTUI test target is unavailable")
    target.env.update(
        {
            "OPENSQUILLA_TUI_THEME": "opensquilla-dark",
            "OPENSQUILLA_TUI_COLOR": "truecolor",
            "OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS": "0",
        }
    )

    session = open_real_terminal_session(
        command=target.command,
        cwd=Path.cwd(),
        env=target.env,
        run_id=build_run_id(_COLLAPSED_STREAM_SCENARIO_ID),
        size=target.initial_size,
        artifact_dir=evidence.run_dir,
        driver="tmux",
    )

    session.start()
    try:
        session.wait_for_text(
            # The 27-cell readiness marker necessarily wraps in an 18-column
            # pane; its stable first physical row still proves host readiness.
            "OPEN_SQUILLA_TUI_R",
            timeout_s=TUI_READY_TIMEOUT_SECONDS,
            checkpoint="collapsed-stream-ready",
        )
        session.send_text("complex state please")
        during_stream = session.wait_for_text(
            # Copy after the label necessarily wraps at 18 columns, but the
            # stable label keeps the resize anchored inside the live turn.
            "Ensemble",
            timeout_s=8.0,
            checkpoint="collapsed-stream-started",
        )
        evidence.record_frame(during_stream)

        _resize_rapidly(session, (_COMPACT, _NARROW, _LARGE))
        completed = session.wait_for_text(
            _STREAM_TRANSCRIPT_MARKER,
            timeout_s=8.0,
            checkpoint="collapsed-stream-complete-wide",
        )
        evidence.record_frame(completed)
        session.wait_for_text(
            "think 436",
            timeout_s=8.0,
            checkpoint="collapsed-stream-usage-wide",
        )
        _wait_for_idle_frame(
            session=session,
            evidence=evidence,
            checkpoint="collapsed-stream-settled-wide",
            expected_size=_LARGE,
            transcript_marker=_STREAM_TRANSCRIPT_MARKER,
            required_rail_headings=("AGENT", "RUNTIME"),
        )

        _resize_rapidly(session, (_NARROW, _COMPACT, _COLLAPSED))
        _wait_for_collapsed_frame(
            session=session,
            evidence=evidence,
            checkpoint="collapsed-stream-settled-back",
        )

        assert session.alternate_screen_active() is True
        assert session.is_alive() is True
        evidence.write_scrollback(session.capture_scrollback_text("scrollback"))
        evidence.write_result(
            ScenarioResult(
                scenario_id=_COLLAPSED_STREAM_SCENARIO_ID,
                backend_id="opentui",
                status="pass",
                run_dir=evidence.run_dir,
            )
        )
    finally:
        session.terminate()


def test_empty_welcome_resize_remount_recovers_without_duplicate_frames(
    artifact_root: Path,
    pytestconfig: pytest.Config,
) -> None:
    capabilities = probe_terminal_capabilities()
    if not capabilities.tmux_available:
        reason = "welcome remount regression requires tmux surface reset support"
        if bool(pytestconfig.getoption("--tui-require-capabilities")):
            pytest.fail(f"required real-terminal capability is unavailable: {reason}")
        pytest.skip(reason)

    evidence = EvidenceBundle.create(
        artifact_root,
        scenario_id=_WELCOME_REMOUNT_SCENARIO_ID,
        backend_id="opentui",
    )
    evidence.write_scenario(
        {
            "scenario_id": _WELCOME_REMOUNT_SCENARIO_ID,
            "family": "empty_welcome_resize_surface_remount",
            "initial_size": {"cols": _COLLAPSED.cols, "rows": _COLLAPSED.rows},
            "final_size": {"cols": _LARGE.cols, "rows": _LARGE.rows},
            "fault": "tmux_surface_reset_inside_resize_debounce",
            "requires_tmux": True,
        }
    )
    target = build_tui_target(
        "opentui",
        TargetContext(
            project_root=Path.cwd(),
            artifact_dir=evidence.run_dir,
            scenario_id=_WELCOME_REMOUNT_SCENARIO_ID,
            size=_COLLAPSED,
        ),
    )
    if not target.available:
        pytest.skip(target.skip_reason or "OpenTUI test target is unavailable")
    target.env.update(
        {
            "OPENSQUILLA_TUI_THEME": "opensquilla-dark",
            "OPENSQUILLA_TUI_COLOR": "truecolor",
            # Only the resize/final recovery path may repair the physical grid.
            "OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS": "0",
        }
    )

    session = open_real_terminal_session(
        command=target.command,
        cwd=Path.cwd(),
        env=target.env,
        run_id=build_run_id(_WELCOME_REMOUNT_SCENARIO_ID),
        size=target.initial_size,
        artifact_dir=evidence.run_dir,
        driver="tmux",
    )

    session.start()
    try:
        initial = session.wait_for_text(
            "OPEN_SQUILLA_TUI_R",
            timeout_s=TUI_READY_TIMEOUT_SECONDS,
            checkpoint="welcome-remount-ready-collapsed",
        )
        evidence.record_frame(initial)

        # resize-window sends SIGWINCH; OpenTUI coalesces layout work briefly.
        # Reset tmux's physical grid immediately, before that final resize pass.
        session.resize(_LARGE)
        _reset_tmux_surface(session.run_id)
        cleared = session.capture_text("welcome-remount-physical-grid-cleared")
        evidence.record_frame(cleared)
        # Depending on tmux scheduling this is either blank or the first
        # incomplete resize pass. Preserve its exact cells as fault evidence;
        # only the subsequent settled frame is required to be canonical.
        fault_framebuffer = session.capture_framebuffer(
            "welcome-remount-physical-grid-cleared"
        )
        assert fault_framebuffer is not None
        evidence.record_framebuffer(fault_framebuffer)

        restored = _wait_for_welcome_frame(
            session=session,
            evidence=evidence,
            checkpoint="welcome-remount-restored-large",
        )
        assert session.alternate_screen_active() is True
        assert session.is_alive() is True

        # Cross the resize settle window while completely idle. A recovery
        # implemented as terminal writes instead of one framebuffer repaint
        # would append repeated welcome frames to tmux's captured history.
        time.sleep(0.4)
        settled = session.capture_framebuffer("welcome-remount-settled-large")
        assert settled is not None
        evidence.record_framebuffer(settled)
        cursor = session.cursor_position()
        assert cursor is not None
        _assert_welcome_frame(settled, cursor=cursor)
        assert settled.cells == restored.cells

        scrollback = session.capture_scrollback_text("welcome-remount-scrollback")
        evidence.write_scrollback(scrollback)
        assert scrollback.text.count("Build with your agent. Stay in the flow.") == 1
        assert scrollback.text.count("OpenSquilla · Session") == 1

        evidence.write_result(
            ScenarioResult(
                scenario_id=_WELCOME_REMOUNT_SCENARIO_ID,
                backend_id="opentui",
                status="pass",
                run_dir=evidence.run_dir,
            )
        )
    finally:
        session.terminate()
