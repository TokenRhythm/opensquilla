"""Real Gateway/WebSocket Stop regression for task-owned process trees.

The Gateway runs in an independent process with a deterministic offline
provider. The provider invokes the real ``background_process`` tool, waits
until its command leader exits while a descendant remains, then blocks until
the public task-scoped Stop RPC cancels the turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import select
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from opensquilla.cli.gateway_client import GatewayClient
from opensquilla.gateway.boot import start_gateway_server
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.websocket import SubscriptionManager
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    Message,
    ModelInfo,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

_MODEL = "e2e/stop-process-tree"
_SERVER_MODE_ENV = "OPENSQUILLA_STOP_PROCESS_E2E_SERVER"


def _shell_command(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    import shlex

    return " ".join(shlex.quote(part) for part in argv)


class _StopProvider:
    provider_name = "e2e"

    def __init__(self, evidence_dir: Path) -> None:
        self.calls = 0
        self.model = _MODEL
        self.evidence_dir = evidence_dir

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,  # noqa: ARG002
    ) -> AsyncIterator[Any]:
        self.calls += 1
        return self._stream(self.calls, messages)

    @staticmethod
    def _tool_result_contents(messages: list[Message]) -> list[str]:
        contents: list[str] = []
        for message in messages:
            if not isinstance(message.content, list):
                continue
            for block in message.content:
                if getattr(block, "type", None) != "tool_result":
                    continue
                content = getattr(block, "content", "")
                if isinstance(content, str):
                    contents.append(content)
        return contents

    async def _stream(self, call: int, messages: list[Message]) -> AsyncIterator[Any]:
        if call == 1:
            child_pid = self.evidence_dir / "child.pid"
            survived = self.evidence_dir / "descendant-survived"
            child_script = self.evidence_dir / "owned-child.py"
            parent_script = self.evidence_dir / "exited-parent.py"
            child_script.write_text(
                "import os\n"
                "import pathlib\n"
                "import time\n"
                f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(30)\n"
                f"pathlib.Path({str(survived)!r}).write_text('survived')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            parent_script.write_text(
                "import subprocess\n"
                "import sys\n"
                f"subprocess.Popen([sys.executable, {str(child_script)!r}])\n",
                encoding="utf-8",
            )
            yield ToolUseStartEvent(
                tool_use_id="stop-background-1",
                tool_name="background_process",
            )
            yield ToolUseEndEvent(
                tool_use_id="stop-background-1",
                tool_name="background_process",
                arguments={
                    "command": _shell_command([sys.executable, str(parent_script)]),
                    "timeout": 30,
                    "sandbox_permissions": "use_default",
                },
            )
            yield DoneEvent(
                stop_reason="tool_use",
                input_tokens=3,
                output_tokens=1,
                model=self.model,
            )
            return
        if call == 2:
            session_id = ""
            for content in reversed(self._tool_result_contents(messages)):
                match = re.search(r"(?:^|\n)session_id=([A-Za-z0-9-]+)", content)
                if match is not None:
                    session_id = match.group(1)
                    break
            if not session_id:
                raise AssertionError("background_process result did not expose session_id")
            yield ToolUseStartEvent(
                tool_use_id="wait-background-leader-1",
                tool_name="process",
            )
            yield ToolUseEndEvent(
                tool_use_id="wait-background-leader-1",
                tool_name="process",
                arguments={
                    "action": "wait",
                    "session_id": session_id,
                    "timeout": 5,
                },
            )
            yield DoneEvent(
                stop_reason="tool_use",
                input_tokens=3,
                output_tokens=1,
                model=self.model,
            )
            return
        if call == 3:
            wait_result = None
            for content in reversed(self._tool_result_contents(messages)):
                try:
                    candidate = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(candidate, dict) and candidate.get("action") == "wait":
                    wait_result = candidate
                    break
            if wait_result is None or wait_result.get("exited") is not True:
                raise AssertionError("process wait did not confirm direct leader exit")
            (self.evidence_dir / "leader-terminal").write_text("confirmed")
            (self.evidence_dir / "provider-blocked").write_text("ready")
            while True:
                await asyncio.sleep(1)
        yield TextDeltaEvent(text="NEXT_TASK_OK")
        yield DoneEvent(
            stop_reason="end_turn",
            input_tokens=3,
            output_tokens=1,
            model=self.model,
        )

    async def list_models(self) -> list[ModelInfo]:
        return []


class _StopSelector:
    active_provider_id = "e2e"

    def __init__(self, provider: _StopProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model=provider.model)

    def clone(self) -> _StopSelector:
        return self

    def override_model(self, model: str) -> None:
        self.provider.model = model
        self.current_config = SimpleNamespace(model=model)

    def override_model_with_fallback_chain(
        self,
        model: str,
        fallback_chain: list[object],  # noqa: ARG002
    ) -> None:
        self.override_model(model)

    def resolve(self) -> _StopProvider:
        return self.provider

    async def list_models(self) -> list[dict[str, Any]]:
        return []


async def _serve_gateway() -> None:
    port = int(os.environ["OPENSQUILLA_STOP_PROCESS_E2E_PORT"])
    state_dir = Path(os.environ["OPENSQUILLA_STOP_PROCESS_E2E_STATE"])
    evidence_dir = Path(os.environ["OPENSQUILLA_STOP_PROCESS_E2E_EVIDENCE"])
    state_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = state_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    config = GatewayConfig(
        host="127.0.0.1",
        port=port,
        auth=AuthConfig(mode="none"),
        agent_max_provider_retries=0,
    )
    config.state_dir = str(state_dir)
    config.workspace_dir = str(workspace_dir)
    config.attachments.media_root = str(state_dir / "media")
    config.control_ui.enabled = False
    config.squilla_router.enabled = False
    config.naming.enabled = False
    config.compaction.enabled = False
    config.memory.retrieval_mode = "fts_only"
    config.memory.auto_capture_enabled = False
    config.memory.capture_mode = "off"
    config.memory.repair_enabled = False
    config.memory.ttl_sweep_interval_minutes = 0
    config.meta_skill.enabled = False
    config.heartbeat.enabled = False
    config.task_runtime.max_concurrency = 1
    config.task_runtime.max_pending_per_session = 4
    config.subagents.subagent_reserved_slots = 0
    config.sandbox.sandbox = False
    config.sandbox.security_grading = False
    config.sandbox.run_mode = "full"
    config.llm.provider = "e2e"
    config.llm.model = _MODEL
    config.llm.api_key = ""

    provider = _StopProvider(evidence_dir)
    await start_gateway_server(
        config=config,
        provider_selector=_StopSelector(provider),
        subscription_manager=SubscriptionManager(),
        run=True,
    )
    await asyncio.Event().wait()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_health(
    port: int,
    process: subprocess.Popen[bytes],
    gateway_log: Path,
) -> None:
    deadline = time.monotonic() + 45.0
    last_error = ""
    async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = gateway_log.read_text(encoding="utf-8", errors="replace")
                raise AssertionError(
                    f"Gateway exited before health check (code={process.returncode}):\n{output}"
                )
            try:
                response = await client.get(f"http://127.0.0.1:{port}/health")
                if response.status_code == 200 and response.json().get("ok") is True:
                    return
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            await asyncio.sleep(0.1)
    output = gateway_log.read_text(encoding="utf-8", errors="replace")
    raise AssertionError(f"Gateway health timeout: {last_error}\n{output}")


async def _wait_for_file(path: Path, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for process evidence: {path}")


def _open_process_liveness(pid: int) -> tuple[str, int]:
    """Retain a non-reusable identity for the descendant observed before Stop."""

    if os.name == "nt":
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            raise OSError(int(getattr(ctypes, "get_last_error")()))
        return "windows", int(handle)

    pidfd_open = getattr(os, "pidfd_open", None)
    if callable(pidfd_open):
        return "pidfd", int(pidfd_open(pid))
    return "pid", pid


async def _wait_process_exit(identity: tuple[str, int], *, timeout: float) -> None:
    kind, value = identity
    if kind == "windows":
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        result = await asyncio.to_thread(
            kernel32.WaitForSingleObject,
            wintypes.HANDLE(value),
            max(0, int(timeout * 1000)),
        )
        assert result == 0, "task-owned Windows descendant remained alive after Stop"
        return
    if kind == "pidfd":
        readable, _, _ = await asyncio.to_thread(select.select, [value], [], [], timeout)
        assert readable, "task-owned Linux descendant remained alive after Stop"
        return

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            os.kill(value, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("task-owned POSIX descendant remained alive after Stop")


def _close_process_liveness(identity: tuple[str, int]) -> None:
    kind, value = identity
    if kind == "windows":
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(value))
    elif kind == "pidfd":
        with contextlib.suppress(OSError):
            os.close(value)


def _isolated_env(tmp_path: Path, port: int, state: Path, evidence: Path) -> dict[str, str]:
    inherited = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
    env = {key: os.environ[key] for key in inherited if key in os.environ}
    roots = {
        "HOME": tmp_path / "home",
        "USERPROFILE": tmp_path / "home",
        "APPDATA": tmp_path / "appdata",
        "LOCALAPPDATA": tmp_path / "local-appdata",
        "TMPDIR": tmp_path / "tmp",
        "TEMP": tmp_path / "tmp",
        "TMP": tmp_path / "tmp",
    }
    for path in {*roots.values(), state, evidence, tmp_path / "logs"}:
        path.mkdir(parents=True, exist_ok=True)
    env.update({key: str(path) for key, path in roots.items()})
    env.update(
        {
            _SERVER_MODE_ENV: "1",
            "OPENSQUILLA_STOP_PROCESS_E2E_PORT": str(port),
            "OPENSQUILLA_STOP_PROCESS_E2E_STATE": str(state),
            "OPENSQUILLA_STOP_PROCESS_E2E_EVIDENCE": str(evidence),
            "OPENSQUILLA_STATE_DIR": str(state),
            "OPENSQUILLA_LOG_DIR": str(tmp_path / "logs"),
            "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
            "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
            "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY": "true",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        }
    )
    return env


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


@pytest.mark.ci_serial
@pytest.mark.asyncio
async def test_stop_kills_leaderless_descendant_and_gateway_accepts_next_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    port = _free_port()
    state = tmp_path / "state"
    evidence = tmp_path / "evidence"
    gateway_log = tmp_path / "gateway.log"
    stream = gateway_log.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).resolve())],
        cwd=tmp_path,
        env=_isolated_env(tmp_path, port, state, evidence),
        stdout=stream,
        stderr=subprocess.STDOUT,
    )
    client = GatewayClient()
    first_frames: list[dict[str, Any]] = []
    descendant_identity: tuple[str, int] | None = None
    try:
        await _wait_for_health(port, process, gateway_log)
        await client.connect(f"ws://127.0.0.1:{port}/ws")
        session_key = await client.create_session(model=_MODEL, display_name="Stop process E2E")

        async def consume_first() -> None:
            async for frame in client.send_message(session_key, "start owned process"):
                first_frames.append(frame)

        first_turn = asyncio.create_task(consume_first())
        await _wait_for_file(evidence / "child.pid")
        descendant_identity = _open_process_liveness(
            int((evidence / "child.pid").read_text(encoding="utf-8"))
        )
        await _wait_for_file(evidence / "leader-terminal")
        await _wait_for_file(evidence / "provider-blocked")
        task_id = client._active_turn_ids[session_key]
        stopped = await client.call(
            "chat.abort",
            {
                "sessionKey": session_key,
                "taskId": task_id,
                "scope": "task",
                "source": "webui_stop",
            },
        )
        assert stopped["aborted"] is True
        await asyncio.wait_for(first_turn, timeout=10.0)
        await _wait_process_exit(descendant_identity, timeout=10.0)
        assert not (evidence / "descendant-survived").exists()

        await _wait_for_health(port, process, gateway_log)
        next_frames = [
            frame
            async for frame in client.send_message(session_key, "run after Stop")
        ]
        assert any("NEXT_TASK_OK" in str(frame) for frame in next_frames)
        await _wait_for_health(port, process, gateway_log)
        assert process.poll() is None
    finally:
        if descendant_identity is not None:
            _close_process_liveness(descendant_identity)
        await client.close()
        _stop_process(process)
        stream.close()


if __name__ == "__main__" and os.environ.get(_SERVER_MODE_ENV) == "1":
    asyncio.run(_serve_gateway())
