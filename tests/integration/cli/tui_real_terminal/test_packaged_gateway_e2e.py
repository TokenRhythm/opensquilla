"""Packaged companion + real Gateway + real terminal release gate.

Unlike the deterministic renderer scenarios in this directory, this test does
not launch ``fake_opentui_app.py``.  It runs the installed public CLI, the
installed companion executable, and a real local Gateway process end to end.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.cli.gateway_client import GatewayClient
from tui_real_terminal.driver import (
    TerminalFrame,
    TerminalSize,
    TmuxTerminalSession,
    build_run_id,
    probe_terminal_capabilities,
)
from tui_real_terminal.evidence import EvidenceBundle

pytestmark = [
    pytest.mark.tui_real_terminal,
    pytest.mark.tui_gateway_e2e,
]

_EXIT_MARKER = "TUI_GATEWAY_E2E_EXITED"
_TERMINAL_EVENTS = frozenset(
    {
        "session.event.done",
        "session.event.error",
        "task.completed",
        "task.failed",
        "task.cancelled",
    }
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_health(port: int, process: subprocess.Popen[bytes], log: Path) -> None:
    deadline = time.monotonic() + 30
    last_error = ""
    async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f"Gateway exited before health check (code={process.returncode}):\n"
                    f"{log.read_text(encoding='utf-8', errors='replace')}"
                )
            try:
                response = await client.get(f"http://127.0.0.1:{port}/health")
                if response.status_code == 200 and response.json().get("ok") is True:
                    return
            except Exception as exc:  # noqa: BLE001 - retained for timeout evidence
                last_error = str(exc)
            await asyncio.sleep(0.1)
    raise AssertionError(f"Gateway did not become healthy: {last_error}")


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _identity(payload: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    return None


async def _send_web_turn(
    client: GatewayClient,
    session_key: str,
    message: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    wait_terminal: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Send via production ``sessions.send`` with Web surface identity."""

    subscription = await client.subscribe_session_events(session_key)
    client_message_id = uuid.uuid4().hex
    surface_id = f"web:e2e:{uuid.uuid4().hex}"
    accepted = await client.call(
        "sessions.send",
        {
            "key": session_key,
            "message": message,
            "displayText": message,
            "attachments": attachments or [],
            "queueMode": "followup",
            "client_message_id": client_message_id,
            "surface_id": surface_id,
            "_source": {
                "caller_kind": "web",
                "channel_kind": "webchat",
                "channel_id": "web:e2e",
                "source_kind": "webchat",
                "source_name": "gateway-e2e",
                "client_message_id": client_message_id,
                "surface_id": surface_id,
            },
        },
    )
    accepted_payload = accepted if isinstance(accepted, dict) else {}
    subscription.bind_turn(
        turn_id=_identity(accepted_payload, "turn_id", "task_id", "taskId"),
        client_message_id=(
            _identity(accepted_payload, "client_message_id", "clientMessageId") or client_message_id
        ),
    )
    if not wait_terminal:
        await subscription.close()
        return accepted_payload, []

    frames: list[dict[str, Any]] = []
    try:
        while True:
            frame = await asyncio.wait_for(subscription.get(), timeout=45)
            frames.append(frame)
            if str(frame.get("event") or "") in _TERMINAL_EVENTS:
                payload = frame.get("payload")
                if isinstance(payload, dict) and payload.get("session_key") == session_key:
                    return accepted_payload, frames
    finally:
        await subscription.close()


async def _wait_for_queue(
    client: GatewayClient,
    session_key: str,
    *,
    queued_count: int,
    timeout: float = 15,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await client.bootstrap_session(session_key, limit=20)
        queue = last.get("queue") if isinstance(last.get("queue"), dict) else {}
        if int(queue.get("queued_count") or 0) == queued_count:
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"queue did not reach queued_count={queued_count}: {last!r}")


async def _wait_screen(
    session: TmuxTerminalSession,
    predicate,
    *,
    timeout: float,
    checkpoint: str,
) -> TerminalFrame:
    deadline = time.monotonic() + timeout
    last = await asyncio.to_thread(session.capture_text, checkpoint)
    while time.monotonic() < deadline:
        if predicate(last.text):
            return last
        await asyncio.sleep(0.1)
        last = await asyncio.to_thread(session.capture_text, checkpoint)
    raise TimeoutError(f"screen condition timed out at {checkpoint}:\n{last.text}")


@pytest.mark.asyncio
async def test_packaged_tui_real_gateway_shared_session_release_gate(
    artifact_root: Path,
    pytestconfig: pytest.Config,
) -> None:
    """One release-blocking flow across the packaged public product boundary."""

    if os.environ.get("OPENSQUILLA_TUI_PACKAGED_GATE") != "1":
        pytest.skip("release-only: set OPENSQUILLA_TUI_PACKAGED_GATE=1")
    capabilities = probe_terminal_capabilities()
    if not capabilities.tmux_available:
        if pytestconfig.getoption("--tui-require-capabilities"):
            pytest.fail("required real-terminal capability is unavailable: tmux")
        pytest.skip("packaged Gateway E2E requires tmux")

    # macOS system proxy settings are visible to urllib/httpx/websockets even
    # when no proxy variables exist in the runner environment.  The gate owns
    # this loopback Gateway and must never route it through an operator proxy.
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    host_package = importlib.import_module("opensquilla_tui_host")
    core_version = importlib.metadata.version("opensquilla")
    companion_version = importlib.metadata.version("opensquilla-tui-host")
    metadata = host_package.host_metadata()
    assert companion_version == core_version == metadata.product_version
    host = Path(host_package.host_command()[0]).resolve()
    assert host.is_file()
    cli = Path(sys.executable).with_name("opensquilla").resolve()
    assert cli.is_file()

    evidence = EvidenceBundle.create(
        artifact_root,
        scenario_id="packaged_gateway_shared_session",
        backend_id="packaged-opentui-gateway",
    )
    evidence.write_scenario(
        {
            "core_version": core_version,
            "companion_version": companion_version,
            "host": str(host),
            "cli": str(cli),
            "host_metadata": {
                "product_version": metadata.product_version,
                "host_version": metadata.host_version,
                "platform": metadata.platform,
                "arch": metadata.arch,
                "build_id": metadata.build_id,
            },
            "fake_opentui_app": False,
            "capabilities": {
                "tmux": capabilities.tmux_available,
                "real_terminal": True,
                "packaged_host": True,
                "real_gateway": True,
            },
        }
    )

    run_dir = evidence.run_dir.resolve()
    port = _free_port()
    gateway_url = f"ws://127.0.0.1:{port}/ws"
    state_dir = run_dir / "gateway-state"
    provider_log = run_dir / "provider-events.jsonl"
    gateway_log = run_dir / "gateway.log"
    rpc_log = run_dir / "gateway-rpc-events.json"
    gateway_server = Path(__file__).with_name("gateway_e2e_server.py").resolve()
    gateway_env = os.environ.copy()
    gateway_env.update(
        {
            "OPENSQUILLA_TUI_GATEWAY_E2E_PORT": str(port),
            "OPENSQUILLA_TUI_GATEWAY_E2E_STATE": str(state_dir),
            "OPENSQUILLA_TUI_GATEWAY_E2E_EVENT_LOG": str(provider_log),
            "OPENSQUILLA_STATE_DIR": str(state_dir),
            "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
            "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
            "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY": "true",
            "HOME": str(run_dir / "gateway-home"),
        }
    )
    gateway_env.pop("PYTHONPATH", None)
    gateway_env.pop("BUN_INSTALL", None)
    gateway_env.pop("OPENSQUILLA_TUI_DEV_SOURCE_HOST", None)

    gateway_stream = gateway_log.open("wb")
    gateway = subprocess.Popen(
        [sys.executable, "-u", str(gateway_server)],
        cwd=run_dir,
        env=gateway_env,
        stdout=gateway_stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    client = GatewayClient()
    session: TmuxTerminalSession | None = None
    rpc_evidence: dict[str, Any] = {}
    gate_passed = False
    try:
        await _wait_for_health(port, gateway, gateway_log)
        await client.connect(gateway_url)
        session_key = await client.create_session(
            model="e2e/deterministic",
            display_name="Packaged TUI Gateway E2E",
        )
        rpc_evidence["session_key"] = session_key

        note = evidence.run_dir / "seed-note.txt"
        note.write_text("small attachment from packaged Gateway E2E\n", encoding="utf-8")
        file_uuid = await client.upload_file(note, "text/plain", note.name)
        attachment = {"file_uuid": file_uuid, "mime": "text/plain", "name": note.name}
        seed_accepted, seed_frames = await _send_web_turn(
            client,
            session_key,
            "E2E_SEED_ATTACHMENT",
            attachments=[attachment],
        )
        rpc_evidence["seed"] = {"accepted": seed_accepted, "frames": seed_frames}

        tui_env = os.environ.copy()
        tui_home = evidence.run_dir / "fresh-home"
        tui_home.mkdir(parents=True, exist_ok=True)
        tui_env.update(
            {
                "HOME": str(tui_home),
                "OPENSQUILLA_GATEWAY_URL": gateway_url,
                "OPENSQUILLA_STATE_DIR": str(evidence.run_dir / "tui-state"),
                "OPENSQUILLA_LOG_DIR": str(evidence.run_dir / "tui-logs"),
                "OPENSQUILLA_TUI_READY_MARKER": "OPEN_SQUILLA_TUI_READY",
                "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
                "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
                "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY": "true",
                "OPENSQUILLA_TUI_PACKAGED_GATE": "1",
                "TERM": "xterm-256color",
            }
        )
        tui_env.pop("PYTHONPATH", None)
        tui_env.pop("BUN_INSTALL", None)
        tui_env.pop("OPENSQUILLA_TUI_DEV_SOURCE_HOST", None)
        command = [
            str(cli),
            "chat",
            "--ui",
            "tui",
            "--session",
            session_key,
            "--timeout",
            "120",
        ]
        shell_script = (
            f"{shlex.join(command)}; "
            f"printf '\\n{_EXIT_MARKER} status=%s\\n' \"$?\"; "
            "exec /bin/sh -i"
        )
        session = TmuxTerminalSession(
            command=["/bin/sh", "-c", shell_script],
            cwd=Path.cwd(),
            env=tui_env,
            run_id=build_run_id("packaged_gateway_shared_session"),
            size=TerminalSize(cols=120, rows=30),
            terminal_log=evidence.terminal_log_path,
        )
        await asyncio.to_thread(session.start)
        hydrated = await asyncio.to_thread(
            session.wait_for_text,
            "E2E_SEED_REPLY",
            timeout_s=30,
            checkpoint="history-hydrated",
        )
        evidence.record_frame(hydrated)
        assert "E2E_SEED_ATTACHMENT" in hydrated.text
        assert "seed-note.txt" in hydrated.text
        assert await asyncio.to_thread(session.alternate_screen_active)

        web_accepted, web_frames = await _send_web_turn(
            client,
            session_key,
            "E2E_WEB_EXTERNAL",
        )
        rpc_evidence["external_web_turn"] = {
            "accepted": web_accepted,
            "frames": web_frames,
        }
        projected = await asyncio.to_thread(
            session.wait_for_text,
            "E2E_WEB_REPLY",
            timeout_s=30,
            checkpoint="web-turn-projected",
        )
        evidence.record_frame(projected)
        assert "E2E_WEB_EXTERNAL" in projected.text

        requested = await client.call(
            "exec.approval.request",
            {
                "toolName": "exec_command",
                "args": {"command": "touch e2e-approved.txt"},
                "command": "touch e2e-approved.txt",
                "sessionKey": session_key,
            },
        )
        approval_id = str(requested["id"])
        approval_frame = await _wait_screen(
            session,
            lambda screen: "touch e2e-approved.txt" in screen and "approval" in screen,
            timeout=15,
            checkpoint="approval-requested",
        )
        evidence.record_frame(approval_frame)
        resolved = await client.resolve_approval(approval_id, True)
        dismissed = await _wait_screen(
            session,
            lambda screen: "touch e2e-approved.txt" not in screen,
            timeout=15,
            checkpoint="approval-resolved-by-web",
        )
        evidence.record_frame(dismissed)
        opposite_second_resolution = await client.resolve_approval(approval_id, False)
        assert resolved.get("approved") is True
        assert resolved.get("resolved") is True
        assert opposite_second_resolution.get("approved") is True
        assert opposite_second_resolution.get("resolved") is True
        assert opposite_second_resolution.get("resolution") == "approved"
        rpc_evidence["approval"] = {
            "requested": requested,
            "resolved": resolved,
            "opposite_second_resolution": opposite_second_resolution,
        }

        hold_accepted, _ = await _send_web_turn(
            client,
            session_key,
            "E2E_HOLD_QUEUE",
            wait_terminal=False,
        )
        queued_accepted, _ = await _send_web_turn(
            client,
            session_key,
            "E2E_QUEUED_CANCEL",
            wait_terminal=False,
        )
        queued_snapshot = await _wait_for_queue(client, session_key, queued_count=1)
        aborted = await client.abort_session(session_key)
        drained_snapshot = await _wait_for_queue(client, session_key, queued_count=0)
        assert aborted.get("aborted") is True
        assert not bool(drained_snapshot.get("active"))
        rpc_evidence["queue_cancel"] = {
            "hold_accepted": hold_accepted,
            "queued_accepted": queued_accepted,
            "queued_snapshot": queued_snapshot,
            "abort": aborted,
            "drained_snapshot": drained_snapshot,
        }

        await asyncio.to_thread(session.send_text, "/exit")
        exited = await asyncio.to_thread(
            session.wait_for_text,
            f"{_EXIT_MARKER} status=0",
            timeout_s=30,
            checkpoint="after-exit",
        )
        evidence.record_frame(exited)
        assert not await asyncio.to_thread(session.alternate_screen_active)
        await asyncio.to_thread(session.send_text, "printf 'GATEWAY-E2E-RESTORE-%s\\n' ok")
        restored = await asyncio.to_thread(
            session.wait_for_text,
            "GATEWAY-E2E-RESTORE-ok",
            timeout_s=10,
            checkpoint="terminal-restored",
        )
        evidence.record_frame(restored)
        evidence.write_scrollback(
            await asyncio.to_thread(session.capture_scrollback_text, "final-scrollback")
        )
        assert "$" in restored.text
        assert "Traceback (most recent call last)" not in restored.text
        rpc_evidence["terminal_restore"] = {
            "alternate_screen": False,
            "shell_echo": "GATEWAY-E2E-RESTORE-ok",
        }
        gate_passed = True
    finally:
        rpc_log.write_text(
            json.dumps(rpc_evidence, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        (run_dir / "result.json").write_text(
            json.dumps(
                {
                    "scenario_id": "packaged_gateway_shared_session",
                    "status": "pass" if gate_passed else "fail",
                    "artifact_dir": str(run_dir),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if session is not None:
            await asyncio.to_thread(session.terminate)
        await client.close()
        _stop_process(gateway)
        gateway_stream.close()
