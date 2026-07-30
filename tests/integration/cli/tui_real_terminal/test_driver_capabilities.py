from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HARNESS_PARENT = Path(__file__).resolve().parents[1]
if str(HARNESS_PARENT) not in sys.path:
    sys.path.insert(0, str(HARNESS_PARENT))

from tui_real_terminal import driver  # noqa: E402
from tui_real_terminal.driver import (  # noqa: E402
    PtyTerminalSession,
    TerminalFrame,
    TerminalSize,
    TmuxTerminalSession,
    build_run_id,
    open_real_terminal_session,
    probe_terminal_capabilities,
)


class _FakePtyModule:
    """Stand-in for the Unix-only :mod:`pty` module.

    Probe logic keys off ``driver.pty`` exposing ``openpty``; on Windows
    ``import pty`` yields ``None``. Patching this fake in lets the capability
    tests exercise the probe's PTY branch on any platform instead of skipping.
    """

    @staticmethod
    def openpty() -> tuple[int, int]:  # pragma: no cover - never invoked in probe
        raise NotImplementedError


def _force_pty_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "pty", _FakePtyModule)


def test_terminal_size_validates_positive_dimensions() -> None:
    size = TerminalSize(cols=100, rows=30)

    assert size.cols == 100
    assert size.rows == 30

    with pytest.raises(ValueError, match="terminal size must be positive"):
        TerminalSize(cols=0, rows=30)

    with pytest.raises(ValueError, match="terminal size must be positive"):
        TerminalSize(cols=100, rows=-1)


def test_terminal_frame_records_checkpoint_text_time_and_size() -> None:
    size = TerminalSize(cols=80, rows=24)

    frame = TerminalFrame(
        checkpoint="ready",
        text="OPEN_SQUILLA_TUI_READY",
        captured_at_ms=123,
        size=size,
    )

    assert frame.checkpoint == "ready"
    assert frame.text == "OPEN_SQUILLA_TUI_READY"
    assert frame.captured_at_ms == 123
    assert frame.size is size


def test_build_run_id_is_tmux_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "_run_id_suffix", lambda: "123456789-777-0")

    run_id = build_run_id(" Launch input/loop!? ")

    assert run_id == "opensquilla-tui-launch-input-loop-123456789-777-0"
    assert all(ch.isalnum() or ch in "-_" for ch in run_id)


def test_build_run_id_uses_scenario_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "_run_id_suffix", lambda: "42-777-0")

    assert build_run_id(" !!! ") == "opensquilla-tui-scenario-42-777-0"


def test_capability_probe_prefers_tmux_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_which(name: str) -> str | None:
        return "/usr/bin/tmux" if name == "tmux" else None

    monkeypatch.setattr(driver.shutil, "which", fake_which)
    monkeypatch.setattr(driver.sys, "platform", "linux")
    _force_pty_module(monkeypatch)

    capabilities = probe_terminal_capabilities()

    assert capabilities.tmux_available is True
    assert capabilities.pty_available is True
    assert capabilities.screenshot_available is False
    assert capabilities.resize_available is True
    assert capabilities.preferred_driver == "tmux"
    assert capabilities.skip_reason is None


def test_capability_probe_falls_back_to_pty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver.shutil, "which", lambda name: None)
    monkeypatch.setattr(driver.sys, "platform", "linux")
    _force_pty_module(monkeypatch)

    capabilities = probe_terminal_capabilities()

    assert capabilities.tmux_available is False
    assert capabilities.pty_available is True
    assert capabilities.preferred_driver == "pty"
    assert capabilities.skip_reason is None


def test_capability_probe_reports_none_when_no_terminal_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver.shutil, "which", lambda name: None)
    monkeypatch.setattr(driver.sys, "platform", "win32")

    capabilities = probe_terminal_capabilities()

    assert capabilities.tmux_available is False
    assert capabilities.pty_available is False
    assert capabilities.preferred_driver == "none"
    assert capabilities.resize_available is False
    assert capabilities.skip_reason is not None
    assert "WSL2" in capabilities.skip_reason


def test_factory_selects_available_driver_and_reports_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver.shutil, "which", lambda name: None)
    monkeypatch.setattr(driver.sys, "platform", "linux")
    _force_pty_module(monkeypatch)

    session = open_real_terminal_session(
        command=[sys.executable, "-c", "print('ready')"],
        cwd=tmp_path,
        env={},
        run_id="opensquilla-tui-test-1",
        size=TerminalSize(),
        artifact_dir=tmp_path,
        driver="auto",
    )

    assert isinstance(session, PtyTerminalSession)
    assert session.kind == "pty"

    with pytest.raises(RuntimeError, match="requested terminal driver 'tmux' is unavailable"):
        open_real_terminal_session(
            command=[sys.executable, "-c", "print('ready')"],
            cwd=tmp_path,
            env={},
            run_id="opensquilla-tui-test-2",
            size=TerminalSize(),
            artifact_dir=tmp_path,
            driver="tmux",
        )


def test_factory_reports_when_no_driver_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver.shutil, "which", lambda name: None)
    monkeypatch.setattr(driver.sys, "platform", "win32")

    with pytest.raises(RuntimeError, match="WSL2"):
        open_real_terminal_session(
            command=[sys.executable, "-c", "print('ready')"],
            cwd=tmp_path,
            env={},
            run_id="opensquilla-tui-test-3",
            size=TerminalSize(),
            artifact_dir=tmp_path,
        )


def test_tmux_session_uses_owned_session_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(
        args: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        if args[:2] == ["tmux", "capture-pane"]:
            return subprocess.CompletedProcess(args, 0, stdout="ready screen")
        if args[:2] == ["tmux", "display-message"]:
            return subprocess.CompletedProcess(args, 0, stdout="7,28\n")
        if args[:2] == ["tmux", "has-session"]:
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    session = TmuxTerminalSession(
        command=["python", "-c", "print('ready')"],
        cwd=tmp_path,
        env={"TERM": "xterm-256color"},
        run_id="opensquilla-tui-owned-1",
        size=TerminalSize(cols=100, rows=30),
        terminal_log=tmp_path / "terminal.log",
    )

    session.start()
    session.send_text("hello")
    session.send_key("C-c")
    session.paste("line 1\nline 2")
    session.mouse_scroll("up", ticks=2, x=7, y=9)
    session.resize(TerminalSize(cols=120, rows=40))
    frame = session.wait_for_text("ready", timeout_s=0.1, checkpoint="ready")
    cursor = session.cursor_position()
    alive = session.is_alive()
    session.terminate()

    assert session.kind == "tmux"
    assert calls[0][0][:5] == ["tmux", "new-session", "-d", "-s", "opensquilla-tui-owned-1"]
    assert ["tmux", "send-keys", "-t", "opensquilla-tui-owned-1", "-l", "hello"] in [
        call for call, _ in calls
    ]
    assert ["tmux", "send-keys", "-t", "opensquilla-tui-owned-1", "Enter"] in [
        call for call, _ in calls
    ]
    assert ["tmux", "send-keys", "-t", "opensquilla-tui-owned-1", "C-c"] in [
        call for call, _ in calls
    ]
    paste_call = next(call for call in calls if call[0][:3] == ["tmux", "load-buffer", "-b"])
    assert paste_call[1]["input"] == "line 1\nline 2"
    wheel = [
        call
        for call, _ in calls
        if call[:5] == ["tmux", "send-keys", "-H", "-t", "opensquilla-tui-owned-1"]
    ]
    assert len(wheel) == 2
    assert wheel[0][5:] == [f"{byte:02x}" for byte in b"\x1b[<64;7;9M"]
    assert ["tmux", "resize-window", "-t", "opensquilla-tui-owned-1", "-x", "120", "-y", "40"] in [
        call for call, _ in calls
    ]
    assert frame.text == "ready screen"
    assert frame.size == TerminalSize(cols=120, rows=40)
    assert cursor == (7, 28)
    assert alive is True
    assert calls[-1][0] == ["tmux", "kill-session", "-t", "opensquilla-tui-owned-1"]


@pytest.mark.skipif(sys.platform == "win32", reason="PTY fallback is only available on Unix")
def test_pty_session_drives_text_process_and_cleans_up(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-u",
        "-c",
        (
            "import sys\n"
            "print('READY', flush=True)\n"
            "for line in sys.stdin:\n"
            "    print('ECHO:' + line.strip(), flush=True)\n"
        ),
    ]
    session = PtyTerminalSession(
        command=command,
        cwd=tmp_path,
        env=os.environ.copy(),
        run_id="opensquilla-tui-pty-1",
        size=TerminalSize(cols=80, rows=24),
        terminal_log=tmp_path / "terminal.log",
    )

    try:
        session.start()
        ready = session.wait_for_text("READY", timeout_s=2, checkpoint="ready")
        session.send_text("hello")
        echoed = session.wait_for_text("ECHO:hello", timeout_s=2, checkpoint="echo")
        session.paste("draft")
        session.send_key("Enter")
        pasted = session.wait_for_text("ECHO:draft", timeout_s=2, checkpoint="paste")
        session.resize(TerminalSize(cols=90, rows=20))

        assert session.kind == "pty"
        assert "READY" in ready.text
        assert "ECHO:hello" in echoed.text
        assert "ECHO:draft" in pasted.text
        assert session.size == TerminalSize(cols=90, rows=20)
        assert session.cursor_position() is None
        assert session.is_alive() is True
    finally:
        session.terminate()

    assert session.is_alive() is False


def test_tmux_alternate_screen_probe_reads_pane_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(["1\n", "0\n"])

    def fake_run(
        args: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        assert args == [
            "tmux",
            "display-message",
            "-p",
            "-t",
            "opensquilla-tui-owned-3",
            "#{alternate_on}",
        ]
        return subprocess.CompletedProcess(args, 0, stdout=next(responses))

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    session = TmuxTerminalSession(
        command=["python", "-c", "print('ready')"],
        cwd=tmp_path,
        env={},
        run_id="opensquilla-tui-owned-3",
        size=TerminalSize(),
        terminal_log=tmp_path / "terminal.log",
    )

    assert session.alternate_screen_active() is True
    assert session.alternate_screen_active() is False


def test_tmux_styled_capture_reads_current_screen_without_joining_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "\x1b[48;2;18;18;18m    \n    \n"

    def fake_run(
        args: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        assert args == [
            "tmux",
            "capture-pane",
            "-t",
            "opensquilla-tui-styled",
            "-e",
            "-N",
            "-p",
        ]
        # In particular, `-a` (saved normal screen) and `-J` (joined wrapped
        # rows) must never enter the exact-cell framebuffer command.
        assert "-a" not in args
        assert "-J" not in args
        return subprocess.CompletedProcess(args, 0, stdout=raw)

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    session = TmuxTerminalSession(
        command=["python", "-c", "print('ready')"],
        cwd=tmp_path,
        env={},
        run_id="opensquilla-tui-styled",
        size=TerminalSize(cols=4, rows=2),
        terminal_log=tmp_path / "terminal.log",
    )

    framebuffer = session.capture_framebuffer("styled")

    assert framebuffer.cols == 4
    assert framebuffer.rows == 2
    assert framebuffer.text == "    \n    "


def test_wait_for_text_times_out_with_last_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        args: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["tmux", "capture-pane"]:
            return subprocess.CompletedProcess(args, 0, stdout="not yet")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    session = TmuxTerminalSession(
        command=["python", "-c", "print('ready')"],
        cwd=tmp_path,
        env={},
        run_id="opensquilla-tui-owned-2",
        size=TerminalSize(),
        terminal_log=tmp_path / "terminal.log",
    )

    with pytest.raises(TimeoutError, match="timed out waiting for 'missing'.*not yet"):
        session.wait_for_text("missing", timeout_s=0, checkpoint="missing")
