"""Source Host + public CLI + deterministic real Gateway startup contract."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from opensquilla.cli.gateway_client import GatewayClient
from tui_real_terminal.driver import (
    TerminalSize,
    TmuxTerminalSession,
    build_run_id,
    probe_terminal_capabilities,
)
from tui_real_terminal.evidence import EvidenceBundle
from tui_real_terminal.framebuffer import assert_opentui_framebuffer
from tui_real_terminal.test_packaged_gateway_e2e import (
    _free_port,
    _stop_process,
    _wait_for_health,
)

pytestmark = [
    pytest.mark.tui_real_terminal,
    pytest.mark.tui_gateway_e2e,
]


@pytest.mark.asyncio
async def test_source_tui_real_gateway_empty_bootstrap_first_screen(
    artifact_root: Path,
    pytestconfig: pytest.Config,
) -> None:
    """Exercise the exact public startup path behind ``opensquilla chat``.

    Unlike the ordinary renderer fixture, this launches the deterministic real
    Gateway and lets ``run_gateway_chat`` create and normalize the empty
    sessions.bootstrap snapshot before opening the source OpenTUI Host.
    """

    capabilities = probe_terminal_capabilities()
    if not capabilities.tmux_available:
        if pytestconfig.getoption("--tui-require-capabilities"):
            pytest.fail("required real-terminal capability is unavailable: tmux")
        pytest.skip("source Gateway bootstrap startup requires tmux")

    project_root = Path.cwd().resolve()
    evidence = EvidenceBundle.create(
        artifact_root,
        scenario_id="source_gateway_empty_bootstrap_startup",
        backend_id="source-opentui-gateway",
    )
    run_dir = evidence.run_dir.resolve()
    port = _free_port()
    gateway_url = f"ws://127.0.0.1:{port}/ws"
    source_path = str(project_root / "src")
    gateway_log = run_dir / "gateway.log"
    gateway_env = os.environ.copy()
    gateway_env.update(
        {
            "PYTHONPATH": source_path,
            "OPENSQUILLA_TUI_GATEWAY_E2E_PORT": str(port),
            "OPENSQUILLA_TUI_GATEWAY_E2E_STATE": str(run_dir / "gateway-state"),
            "OPENSQUILLA_TUI_GATEWAY_E2E_EVENT_LOG": str(run_dir / "provider.jsonl"),
            "OPENSQUILLA_STATE_DIR": str(run_dir / "gateway-state"),
            "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
            "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
            "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY": "true",
            "HOME": str(run_dir / "gateway-home"),
        }
    )
    gateway_stream = gateway_log.open("wb")
    gateway = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(Path(__file__).with_name("gateway_e2e_server.py")),
        ],
        cwd=run_dir,
        env=gateway_env,
        stdout=gateway_stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    client = GatewayClient()
    session: TmuxTerminalSession | None = None
    try:
        await _wait_for_health(port, gateway, gateway_log)
        await client.connect(gateway_url)
        session_key = await client.create_session(
            model="e2e/deterministic",
            display_name="Source Gateway startup",
        )

        tui_env = os.environ.copy()
        tui_env.update(
            {
                "PYTHONPATH": source_path,
                "HOME": str(run_dir / "tui-home"),
                "OPENSQUILLA_GATEWAY_URL": gateway_url,
                "OPENSQUILLA_STATE_DIR": str(run_dir / "tui-state"),
                "OPENSQUILLA_LOG_DIR": str(run_dir / "tui-logs"),
                "OPENSQUILLA_TUI_DEV_SOURCE_HOST": "1",
                "OPENSQUILLA_TUI_READY_MARKER": "OPEN_SQUILLA_TUI_READY",
                "OPENSQUILLA_TUI_THEME": "opensquilla-dark",
                "OPENSQUILLA_TUI_COLOR": "truecolor",
                # Exercise the event-independent recovery seam through the
                # public CLI -> Python bridge -> source Host environment.
                "OPENSQUILLA_TUI_REPAINT_WATCHDOG_MS": "400",
                "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
                "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
                "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY": "true",
                "TERM": "xterm-256color",
                "COLORTERM": "truecolor",
            }
        )
        session = TmuxTerminalSession(
            command=[
                sys.executable,
                "-u",
                "-m",
                "opensquilla.cli.main",
                "chat",
                "--ui",
                "tui",
                "--session",
                session_key,
                "--timeout",
                "30",
            ],
            cwd=project_root,
            env=tui_env,
            run_id=build_run_id("source_gateway_empty_bootstrap_startup"),
            size=TerminalSize(cols=140, rows=36),
            terminal_log=evidence.terminal_log_path,
        )
        await asyncio.to_thread(session.start)
        ready = await asyncio.to_thread(
            session.wait_for_text,
            "OPEN_SQUILLA_TUI_READY",
            timeout_s=20,
            checkpoint="real-gateway-ready",
        )
        evidence.record_frame(ready)
        await asyncio.sleep(0.3)
        settled = await asyncio.to_thread(session.capture_text, "real-gateway-first-screen")
        evidence.record_frame(settled)
        framebuffer = await asyncio.to_thread(
            session.capture_framebuffer,
            "real-gateway-first-screen",
        )
        evidence.record_framebuffer(framebuffer)

        assert "OpenSquilla" in settled.text
        assert "Build with your agent. Stay in the flow." in settled.text
        assert "Source Gateway startup" in settled.text
        assert settled.text.count("send a message") == 1
        assert await asyncio.to_thread(session.alternate_screen_active)
        # Runtime rows are covered by the rail-focused gate.  This startup test
        # requires one coherent rail boundary and identity heading while
        # keeping its root contract focused on a non-blank first screen.
        assert_opentui_framebuffer(framebuffer, required_rail_headings=("AGENT",))

        # Codex's side terminal can discard this same-size physical surface
        # without sending focus or resize to the child. Reset only tmux's grid,
        # then prove the source Host reconstructs the exact stable frame without
        # any terminal event, input, provider delta, or Gateway message.
        subprocess.run(
            ["tmux", "send-keys", "-R", "-t", session.run_id],
            check=True,
        )
        restored = await asyncio.to_thread(
            session.wait_for_text,
            "Build with your agent. Stay in the flow.",
            timeout_s=3.0,
            checkpoint="real-gateway-eventless-repaint",
        )
        evidence.record_frame(restored)
        restored_framebuffer = await asyncio.to_thread(
            session.capture_framebuffer,
            "real-gateway-eventless-repaint",
        )
        evidence.record_framebuffer(restored_framebuffer)
        assert_opentui_framebuffer(restored_framebuffer, required_rail_headings=("AGENT",))
        assert restored_framebuffer.cells == framebuffer.cells
        assert await asyncio.to_thread(session.alternate_screen_active)
        evidence.write_scrollback(
            await asyncio.to_thread(session.capture_scrollback_text, "final-scrollback")
        )
    finally:
        if session is not None:
            await asyncio.to_thread(session.terminate)
        await client.close()
        _stop_process(gateway)
        gateway_stream.close()
