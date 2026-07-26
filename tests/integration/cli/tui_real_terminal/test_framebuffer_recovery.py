from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from tui_real_terminal.driver import (
    TerminalSize,
    build_run_id,
    open_real_terminal_session,
    probe_terminal_capabilities,
)
from tui_real_terminal.evidence import EvidenceBundle, ScenarioResult
from tui_real_terminal.framebuffer import (
    assert_opentui_framebuffer,
    opentui_framebuffer_violations,
)
from tui_real_terminal.targets import (
    TUI_READY_TIMEOUT_SECONDS,
    TargetContext,
    build_tui_target,
)

pytestmark = pytest.mark.tui_real_terminal

_SCENARIO_ID = "same_size_framebuffer_recovery"
_EVENTLESS_SCENARIO_ID = "same_size_eventless_framebuffer_recovery"
_MODE_LOSS_SCENARIO_ID = "alternate_screen_mode_loss"
_STREAM_SCENARIO_ID = "same_size_stream_framebuffer_recovery"
_EVENTLESS_STREAM_SCENARIO_ID = "same_size_eventless_stream_framebuffer_recovery"
_SIZE = TerminalSize(cols=140, rows=36)
_EVENTLESS_SIZE = TerminalSize(cols=120, rows=36)


def _pane_state(run_id: str) -> str:
    """Return actual tmux framebuffer geometry, mode, and child identity."""

    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            run_id,
            "#{pane_width}x#{pane_height} alternate=#{alternate_on} pid=#{pane_pid}",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def _pane_input_modes(run_id: str) -> str:
    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            run_id,
            (
                "bracket=#{bracket_paste_flag} "
                "mouse-standard=#{mouse_standard_flag} "
                "mouse-button=#{mouse_button_flag} "
                "mouse-any=#{mouse_any_flag} "
                "mouse-sgr=#{mouse_sgr_flag}"
            ),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def _wait_for_alternate_screen(run_id: str, expected: bool, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    marker = f"alternate={int(expected)}"
    while time.monotonic() < deadline:
        if marker in _pane_state(run_id):
            return
        time.sleep(0.02)
    raise AssertionError(f"pane never reached {marker}: {_pane_state(run_id)}")


def _wait_for_app_event(path: Path, event: str, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    needle = f'"event": "{event}"'
    while time.monotonic() < deadline:
        if path.is_file() and needle in path.read_text(encoding="utf-8"):
            return
        time.sleep(0.02)
    raise AssertionError(f"fake app did not record {event!r}: {path}")


def _send_focus_sequence(run_id: str, *, focused: bool) -> None:
    # CSI I / CSI O are the standard terminal focus-in / focus-out sequences.
    # Hex input bypasses tmux key-name parsing and reaches the child PTY exactly.
    final = "49" if focused else "4f"
    subprocess.run(
        ["tmux", "send-keys", "-H", "-t", run_id, "1b", "5b", final],
        check=True,
    )


def _clear_tmux_framebuffer(run_id: str) -> None:
    # tmux -R resets its pane terminal state and clears the emulated physical
    # screen without sending output through the child process and without a
    # TIOCSWINSZ. This reproduces an embedded terminal remounting a same-size
    # alternate-screen surface while OpenTUI still owns its old back-buffer.
    subprocess.run(["tmux", "send-keys", "-R", "-t", run_id], check=True)


def _normalized_frame(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()).rstrip()


def test_focus_in_restores_same_size_cleared_framebuffer(
    artifact_root: Path,
    pytestconfig: pytest.Config,
) -> None:
    capabilities = probe_terminal_capabilities()
    if not capabilities.tmux_available:
        reason = (
            "same-size framebuffer recovery requires tmux capture-pane; "
            "the PTY fallback records an append-only byte stream and has no "
            "physical screen grid to clear"
        )
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
            "family": "terminal_surface_recovery",
            "initial_size": {"cols": _SIZE.cols, "rows": _SIZE.rows},
            "requires_tmux": True,
        }
    )
    target = build_tui_target(
        "opentui",
        TargetContext(
            project_root=Path.cwd(),
            artifact_dir=evidence.run_dir,
            scenario_id=_SCENARIO_ID,
            size=_SIZE,
        ),
    )
    if not target.available:
        pytest.skip(target.skip_reason or "OpenTUI test target is unavailable")
    # This test proves the focus seam specifically. Keep the eventless fallback
    # disabled so its low-frequency repaint cannot race the deliberately blank
    # intermediate framebuffer asserted below.
    target.env["OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS"] = "0"

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
        initial = session.wait_for_text(
            "OPEN_SQUILLA_TUI_READY",
            timeout_s=TUI_READY_TIMEOUT_SECONDS,
            checkpoint="before-surface-reset",
        )
        evidence.record_frame(initial)
        initial_framebuffer = session.capture_framebuffer("before-surface-reset")
        assert initial_framebuffer is not None
        evidence.record_framebuffer(initial_framebuffer)
        # This recovery fixture intentionally emits only canonical agent
        # context. Runtime projection is covered by the streaming framebuffer
        # scenarios; the rail still gives this test the wide-layout seam that
        # was corrupted in embedded terminals.
        assert_opentui_framebuffer(
            initial_framebuffer,
            required_rail_headings=("AGENT",),
            cursor=session.cursor_position(),
        )
        initial_state = _pane_state(session.run_id)
        assert initial_state.startswith("140x36 alternate=1 "), initial_state

        # A real terminal reports blur before its embedded pane is hidden.
        _send_focus_sequence(session.run_id, focused=False)
        _clear_tmux_framebuffer(session.run_id)

        # Stay past OpenTUI's 100 ms resize debounce. The pane must remain blank
        # until focus-in explicitly requests a full repaint; a continuously
        # redrawing app would make this test unable to prove the recovery seam.
        time.sleep(0.2)
        cleared = session.capture_text("physical-framebuffer-cleared")
        evidence.record_frame(cleared)
        cleared_framebuffer = session.capture_framebuffer("physical-framebuffer-cleared")
        assert cleared_framebuffer is not None
        evidence.record_framebuffer(cleared_framebuffer)
        cleared_state = _pane_state(session.run_id)

        assert cleared_state == initial_state
        assert session.is_alive() is True
        assert not cleared.text.strip(), (
            f"tmux did not expose a genuinely cleared framebuffer; artifacts: {evidence.run_dir}"
        )
        cleared_violations = opentui_framebuffer_violations(cleared_framebuffer)
        assert any("background-mask" in item for item in cleared_violations), (
            "the styled gate did not distinguish a physically cleared screen; "
            f"violations: {cleared_violations}"
        )

        _send_focus_sequence(session.run_id, focused=True)
        restored = session.wait_for_text(
            "Build with your agent. Stay in the flow.",
            timeout_s=3.0,
            checkpoint="after-focus-full-repaint",
        )
        evidence.record_frame(restored)
        restored_framebuffer = session.capture_framebuffer("after-focus-full-repaint")
        assert restored_framebuffer is not None
        evidence.record_framebuffer(restored_framebuffer)
        assert_opentui_framebuffer(
            restored_framebuffer,
            required_rail_headings=("AGENT",),
            cursor=session.cursor_position(),
        )

        assert _pane_state(session.run_id) == initial_state
        # Compare the complete stable frame, not only the one marker used for
        # polling: both the top identity area and the fixed bottom composer must
        # be reconstructed from OpenTUI's retained render tree in one full pass.
        assert _normalized_frame(restored.text) == _normalized_frame(initial.text)
        assert restored_framebuffer.cells == initial_framebuffer.cells
        assert "OpenSquilla · Session" in restored.text
        assert "send a message" in restored.text

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


def test_watchdog_restores_same_size_framebuffer_without_terminal_event(
    artifact_root: Path,
    pytestconfig: pytest.Config,
) -> None:
    """Exercise the explicit eventless-recovery diagnostic fallback.

    The physical grid disappears while the PTY stays alive at the same size.
    Crucially, this test sends no focus, resize, SIGWINCH, key, mouse, or stream
    event after the clear; only an event-independent full repaint can pass.
    """

    capabilities = probe_terminal_capabilities()
    if not capabilities.tmux_available:
        reason = "eventless framebuffer recovery requires tmux framebuffer control"
        if bool(pytestconfig.getoption("--tui-require-capabilities")):
            pytest.fail(f"required real-terminal capability is unavailable: {reason}")
        pytest.skip(reason)

    evidence = EvidenceBundle.create(
        artifact_root,
        scenario_id=_EVENTLESS_SCENARIO_ID,
        backend_id="opentui",
    )
    evidence.write_scenario(
        {
            "scenario_id": _EVENTLESS_SCENARIO_ID,
            "family": "terminal_surface_recovery_without_events",
            "initial_size": {
                "cols": _EVENTLESS_SIZE.cols,
                "rows": _EVENTLESS_SIZE.rows,
            },
            "requires_tmux": True,
        }
    )
    target = build_tui_target(
        "opentui",
        TargetContext(
            project_root=Path.cwd(),
            artifact_dir=evidence.run_dir,
            scenario_id=_EVENTLESS_SCENARIO_ID,
            size=_EVENTLESS_SIZE,
        ),
    )
    if not target.available:
        pytest.skip(target.skip_reason or "OpenTUI test target is unavailable")
    # Periodic full repaint is deliberately not a production default: it can
    # flash a healthy retained surface. Keep the otherwise-undetectable,
    # eventless path as an explicit diagnostic/recovery capability.
    target.env["OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS"] = "400"

    session = open_real_terminal_session(
        command=target.command,
        cwd=Path.cwd(),
        env=target.env,
        run_id=build_run_id(_EVENTLESS_SCENARIO_ID),
        size=target.initial_size,
        artifact_dir=evidence.run_dir,
        driver="tmux",
    )

    session.start()
    try:
        initial = session.wait_for_text(
            "OPEN_SQUILLA_TUI_READY",
            timeout_s=TUI_READY_TIMEOUT_SECONDS,
            checkpoint="eventless-before-surface-reset",
        )
        evidence.record_frame(initial)
        initial_framebuffer = session.capture_framebuffer("eventless-before-surface-reset")
        assert initial_framebuffer is not None
        evidence.record_framebuffer(initial_framebuffer)
        assert_opentui_framebuffer(
            initial_framebuffer,
            required_rail_headings=("AGENT",),
            cursor=session.cursor_position(),
        )
        initial_state = _pane_state(session.run_id)

        _clear_tmux_framebuffer(session.run_id)

        # Do not touch the pane after the clear. Polling reads tmux's physical
        # grid only; it emits no input or terminal lifecycle signal to the Host.
        restored = session.wait_for_text(
            "Build with your agent. Stay in the flow.",
            timeout_s=3.0,
            checkpoint="eventless-watchdog-full-repaint",
        )
        evidence.record_frame(restored)
        restored_framebuffer = session.capture_framebuffer("eventless-watchdog-full-repaint")
        assert restored_framebuffer is not None
        evidence.record_framebuffer(restored_framebuffer)
        assert_opentui_framebuffer(
            restored_framebuffer,
            required_rail_headings=("AGENT",),
            cursor=session.cursor_position(),
        )

        assert _pane_state(session.run_id) == initial_state
        assert session.alternate_screen_active() is True
        assert session.is_alive() is True
        assert _normalized_frame(restored.text) == _normalized_frame(initial.text)
        assert restored_framebuffer.cells == initial_framebuffer.cells
        assert "OpenSquilla · Session" in restored.text
        assert "send a message" in restored.text

        evidence.write_scrollback(session.capture_scrollback_text("scrollback"))
        evidence.write_result(
            ScenarioResult(
                scenario_id=_EVENTLESS_SCENARIO_ID,
                backend_id="opentui",
                status="pass",
                run_dir=evidence.run_dir,
            )
        )
    finally:
        session.terminate()


def test_watchdog_reenters_alternate_screen_without_polluting_scrollback_or_cursor(
    artifact_root: Path,
    pytestconfig: pytest.Config,
) -> None:
    """Recover mode loss, not only a cleared alternate-screen framebuffer."""

    capabilities = probe_terminal_capabilities()
    if not capabilities.tmux_available:
        reason = "alternate-screen mode-loss recovery requires tmux mode introspection"
        if bool(pytestconfig.getoption("--tui-require-capabilities")):
            pytest.fail(f"required real-terminal capability is unavailable: {reason}")
        pytest.skip(reason)

    evidence = EvidenceBundle.create(
        artifact_root,
        scenario_id=_MODE_LOSS_SCENARIO_ID,
        backend_id="opentui",
    )
    evidence.write_scenario(
        {
            "scenario_id": _MODE_LOSS_SCENARIO_ID,
            "family": "terminal_alternate_screen_mode_recovery",
            "initial_size": {
                "cols": _EVENTLESS_SIZE.cols,
                "rows": _EVENTLESS_SIZE.rows,
            },
            "requires_tmux": True,
        }
    )
    target = build_tui_target(
        "opentui",
        TargetContext(
            project_root=Path.cwd(),
            artifact_dir=evidence.run_dir,
            scenario_id=_MODE_LOSS_SCENARIO_ID,
            size=_EVENTLESS_SIZE,
        ),
    )
    if not target.available:
        pytest.skip(target.skip_reason or "OpenTUI test target is unavailable")
    # Leave a deterministic observation window after fault injection, then let
    # at least two independent recovery ticks prove they do not append frames.
    target.env["OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS"] = "2000"

    session = open_real_terminal_session(
        command=target.command,
        cwd=Path.cwd(),
        env=target.env,
        run_id=build_run_id(_MODE_LOSS_SCENARIO_ID),
        size=target.initial_size,
        artifact_dir=evidence.run_dir,
        driver="tmux",
    )

    session.start()
    try:
        initial = session.wait_for_text(
            "OPEN_SQUILLA_TUI_READY",
            timeout_s=TUI_READY_TIMEOUT_SECONDS,
            checkpoint="mode-loss-before-reset",
        )
        evidence.record_frame(initial)
        initial_cursor = session.cursor_position()
        assert initial_cursor is not None
        initial_input_modes = _pane_input_modes(session.run_id)
        assert session.alternate_screen_active() is True

        session.send_text("reset terminal surface")
        _wait_for_app_event(
            evidence.run_dir / "opentui-app.log",
            "surface_mode_reset",
            timeout_s=2.0,
        )
        _wait_for_alternate_screen(session.run_id, False, timeout_s=1.0)
        assert _pane_input_modes(session.run_id) != initial_input_modes

        _wait_for_alternate_screen(session.run_id, True, timeout_s=4.0)
        restored = session.wait_for_text(
            "Build with your agent. Stay in the flow.",
            timeout_s=2.0,
            checkpoint="mode-loss-after-recovery",
        )
        evidence.record_frame(restored)
        restored_framebuffer = session.capture_framebuffer("mode-loss-after-recovery")
        assert restored_framebuffer is not None
        evidence.record_framebuffer(restored_framebuffer)
        assert_opentui_framebuffer(
            restored_framebuffer,
            cursor=session.cursor_position(),
        )

        placeholder_row = next(
            row
            for row in range(restored_framebuffer.rows)
            if "send a message" in restored_framebuffer.row_text(row)
        )
        placeholder_column = restored_framebuffer.row_text(placeholder_row).index("send a message")
        assert session.cursor_position() == initial_cursor
        assert _pane_input_modes(session.run_id) == initial_input_modes
        expected_cursor = (placeholder_column - 1, placeholder_row)
        assert session.cursor_position() == expected_cursor

        # Cross another watchdog interval while the pane is already healthy.
        # Reasserting alternate-screen must remain idempotent: no full frames in
        # the main scrollback and no cursor stranded below the composer.
        time.sleep(2.2)
        scrollback = session.capture_scrollback_text("mode-loss-scrollback")
        evidence.write_scrollback(scrollback)
        assert scrollback.text.count("Build with your agent. Stay in the flow.") == 1
        assert scrollback.text.count("OpenSquilla · Session") == 1
        assert session.cursor_position() == expected_cursor

        evidence.write_result(
            ScenarioResult(
                scenario_id=_MODE_LOSS_SCENARIO_ID,
                backend_id="opentui",
                status="pass",
                run_dir=evidence.run_dir,
            )
        )
    finally:
        session.terminate()


@pytest.mark.parametrize(
    ("recovery_mode", "scenario_id"),
    [
        pytest.param("focus", _STREAM_SCENARIO_ID, id="focus-event"),
        pytest.param("watchdog", _EVENTLESS_STREAM_SCENARIO_ID, id="eventless-watchdog-opt-in"),
    ],
)
def test_restores_same_size_framebuffer_during_live_stream(
    artifact_root: Path,
    pytestconfig: pytest.Config,
    recovery_mode: str,
    scenario_id: str,
) -> None:
    capabilities = probe_terminal_capabilities()
    if not capabilities.tmux_available:
        reason = "same-size streaming recovery requires tmux framebuffer control"
        if bool(pytestconfig.getoption("--tui-require-capabilities")):
            pytest.fail(f"required real-terminal capability is unavailable: {reason}")
        pytest.skip(reason)

    evidence = EvidenceBundle.create(
        artifact_root,
        scenario_id=scenario_id,
        backend_id="opentui",
    )
    evidence.write_scenario(
        {
            "scenario_id": scenario_id,
            "family": "terminal_surface_recovery_during_stream",
            "recovery_mode": recovery_mode,
            "initial_size": {"cols": _SIZE.cols, "rows": _SIZE.rows},
            "requires_tmux": True,
        }
    )
    target = build_tui_target(
        "opentui",
        TargetContext(
            project_root=Path.cwd(),
            artifact_dir=evidence.run_dir,
            scenario_id=scenario_id,
            size=_SIZE,
        ),
    )
    if not target.available:
        pytest.skip(target.skip_reason or "OpenTUI test target is unavailable")
    if recovery_mode == "focus":
        target.env["OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS"] = "0"
    else:
        target.env["OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS"] = "400"
        target.env["OPENSQUILLA_TUI_FAKE_STREAM_DELAY_S"] = "0.04"

    session = open_real_terminal_session(
        command=target.command,
        cwd=Path.cwd(),
        env=target.env,
        run_id=build_run_id(scenario_id),
        size=target.initial_size,
        artifact_dir=evidence.run_dir,
        driver="tmux",
    )

    session.start()
    try:
        session.wait_for_text(
            "OPEN_SQUILLA_TUI_READY",
            timeout_s=TUI_READY_TIMEOUT_SECONDS,
            checkpoint="ready-before-stream",
        )
        session.send_text("stream please")
        session.wait_for_text(
            "stream-token-010",
            timeout_s=10.0,
            checkpoint="stream-before-surface-reset",
        )
        before = session.wait_for_text(
            "RUNTIME",
            timeout_s=3.0,
            checkpoint="stream-context-before-surface-reset",
        )
        evidence.record_frame(before)
        before_framebuffer = session.capture_framebuffer("stream-before-surface-reset")
        evidence.record_framebuffer(before_framebuffer)
        assert_opentui_framebuffer(
            before_framebuffer,
            cursor=session.cursor_position(),
        )
        initial_state = _pane_state(session.run_id)

        # Recreate the reported condition: the embedded surface is discarded
        # while the same reasoning block is still receiving deltas. No resize and
        # no child restart may help the renderer recover.
        if recovery_mode == "focus":
            _send_focus_sequence(session.run_id, focused=False)
            _clear_tmux_framebuffer(session.run_id)
            _send_focus_sequence(session.run_id, focused=True)
        else:
            # No input or lifecycle event follows the clear. The explicitly
            # enabled fallback must commit one coherent full frame while
            # reasoning continues to mutate underneath it.
            _clear_tmux_framebuffer(session.run_id)

        restored = session.wait_for_text(
            "stream-token-035",
            timeout_s=10.0,
            checkpoint=f"stream-after-{recovery_mode}-full-repaint",
        )
        evidence.record_frame(restored)
        restored_framebuffer = session.capture_framebuffer(
            f"stream-after-{recovery_mode}-full-repaint"
        )
        evidence.record_framebuffer(restored_framebuffer)
        assert_opentui_framebuffer(
            restored_framebuffer,
            cursor=session.cursor_position(),
        )

        assert _pane_state(session.run_id) == initial_state
        assert session.alternate_screen_active() is True
        assert session.is_alive() is True
        assert "stream-token-035" in restored.text
        assert "stream-token-079" not in restored.text
        assert restored.text.count("steer current turn · Tab queues") == 1
        assert "send a message" not in restored.text
        assert restored.text.count("│ AGENT") == 1
        assert restored.text.count("│ RUNTIME") == 1

        # Finalized reasoning keeps only a bounded latest-tail preview. Wait on
        # the usage receipt because an arbitrary stream token may still fall
        # outside those eight retained visual rows.
        session.wait_for_text(
            "in 1 / out 2 · fake-terminal",
            timeout_s=10.0,
            checkpoint="stream-complete-after-recovery",
        )
        evidence.write_scrollback(session.capture_scrollback_text("scrollback"))
        evidence.write_result(
            ScenarioResult(
                scenario_id=scenario_id,
                backend_id="opentui",
                status="pass",
                run_dir=evidence.run_dir,
            )
        )
    finally:
        session.terminate()
