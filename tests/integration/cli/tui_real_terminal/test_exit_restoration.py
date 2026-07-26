from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from tui_real_terminal import assertions
from tui_real_terminal.driver import (
    TerminalSize,
    TmuxTerminalSession,
    build_run_id,
    probe_terminal_capabilities,
)
from tui_real_terminal.evidence import EvidenceBundle
from tui_real_terminal.targets import TargetContext, build_tui_target

pytestmark = pytest.mark.tui_real_terminal

_EXIT_MARKER = "TUI_EXITED_TO_SHELL"


def test_exit_restores_primary_screen_and_shell(
    artifact_root: Path,
    tui_backend: str,
    tui_driver: str,
    pytestconfig: pytest.Config,
) -> None:
    """/exit must hand a usable terminal back: primary screen, live shell, echo.

    The fake app runs inside a shell so the tmux pane survives the app's exit —
    the restoration path is only observable while something still owns the pane.
    tmux's own alternate_on flag distinguishes a real alternate-screen exit from
    shell output merely drawn over a stale alternate screen.
    """
    if tui_backend != "opentui":
        pytest.skip("exit-restoration drives the fake opentui app")
    if tui_driver == "pty":
        pytest.skip("exit-restoration requires tmux, not PTY")
    if not probe_terminal_capabilities().tmux_available:
        if pytestconfig.getoption("--tui-require-capabilities"):
            pytest.fail("required real-terminal capability is unavailable: tmux")
        pytest.skip("exit-restoration requires tmux")

    evidence = EvidenceBundle.create(
        artifact_root,
        scenario_id="exit_restoration",
        backend_id=tui_backend,
    )
    target = build_tui_target(
        "opentui",
        TargetContext(
            project_root=Path.cwd(),
            artifact_dir=evidence.run_dir,
            scenario_id="exit_restoration",
            size=TerminalSize(cols=100, rows=30),
        ),
    )
    shell_script = (
        f"{shlex.join(target.command)}; "
        f"printf '\\n{_EXIT_MARKER} status=%s\\n' \"$?\"; "
        "exec /bin/sh -i"
    )
    session = TmuxTerminalSession(
        command=["/bin/sh", "-c", shell_script],
        cwd=Path.cwd(),
        env=target.env,
        run_id=build_run_id("exit_restoration"),
        size=TerminalSize(cols=100, rows=30),
        terminal_log=evidence.run_dir / "terminal.log",
    )
    session.start()
    try:
        ready = session.wait_for_text("OPEN_SQUILLA_TUI_READY", timeout_s=15.0, checkpoint="ready")
        evidence.record_frame(ready)
        assert session.alternate_screen_active(), (
            "the opentui host should run on the alternate screen"
        )

        session.send_text("/exit")
        exited = session.wait_for_text(
            f"{_EXIT_MARKER} status=0", timeout_s=15.0, checkpoint="after-exit"
        )
        evidence.record_frame(exited)
        assertions.assert_no_traceback(exited)
        assert not session.alternate_screen_active(), (
            "exiting the TUI must leave the alternate screen"
        )

        # An interactive round-trip proves the shell prompt is back and typed
        # characters echo again (raw mode off), not merely that output renders.
        session.send_text("printf 'RESTORE-%s\\n' check")
        echoed = session.wait_for_text(
            "RESTORE-check", timeout_s=10.0, checkpoint="after-echo-probe"
        )
        evidence.record_frame(echoed)
        assert "$" in echoed.text
        evidence.write_scrollback(session.capture_scrollback_text("scrollback"))
    finally:
        session.terminate()

    # The app-side log proves the exit went through the dispatch path (the
    # runtime hands /exit to the app, which acknowledges before shutting down).
    app_events = [
        json.loads(line)
        for line in (evidence.run_dir / "opentui-app.log").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(event["event"] == "exit" for event in app_events)
