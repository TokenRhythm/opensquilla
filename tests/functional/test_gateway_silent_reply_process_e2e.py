"""Real Gateway/WebSocket regression for silent Goal continuations.

The Gateway runs in a separate process with a deterministic offline provider.
This crosses the public WebSocket RPC boundary and then verifies both public
history and the raw SQLite transcript after shutdown.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from opensquilla.cli.gateway_client import GatewayClient
from opensquilla.gateway.boot import start_gateway_server
from opensquilla.gateway.config import AuthConfig, GatewayConfig, GoalConfig
from opensquilla.gateway.websocket import SubscriptionManager
from opensquilla.provider import ChatConfig, DoneEvent, Message, ModelInfo, TextDeltaEvent

_MODEL = "e2e/silent-reply"
_FIRST_VISIBLE = "VISIBLE_FIRST"
_SILENT_SENTINEL = "NO_REPLY"
_THIRD_VISIBLE = "VISIBLE_THIRD"
_SERVER_MODE_ENV = "OPENSQUILLA_SILENT_REPLY_E2E_SERVER"


def _message_text(message: Message) -> str:
    content = message.content
    return content if isinstance(content, str) else repr(content)


class _ScriptedProvider:
    """Three Goal tasks: visible, silent, then visible for history proof."""

    provider_name = "e2e"

    def __init__(self, event_log: Path) -> None:
        self.calls = 0
        self.model = _MODEL
        self._event_log = event_log

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,  # noqa: ARG002
    ) -> AsyncIterator[Any]:
        self.calls += 1
        call = self.calls
        assistant_history = [
            _message_text(message)
            for message in messages
            if message.role == "assistant"
        ]
        self._event_log.parent.mkdir(parents=True, exist_ok=True)
        with self._event_log.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "call": call,
                        "assistant_history": assistant_history,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        return self._stream(call)

    async def _stream(self, call: int) -> AsyncIterator[Any]:
        replies = {
            1: _FIRST_VISIBLE,
            2: _SILENT_SENTINEL,
            3: _THIRD_VISIBLE,
        }
        reply = replies[call]
        yield TextDeltaEvent(text=reply)
        yield DoneEvent(
            stop_reason="end_turn",
            input_tokens=3,
            output_tokens=1,
            model=self.model,
        )

    async def list_models(self) -> list[ModelInfo]:
        return []


class _ScriptedSelector:
    active_provider_id = "e2e"

    def __init__(self, provider: _ScriptedProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model=provider.model)

    def clone(self) -> _ScriptedSelector:
        # Every clone must share the call counter and evidence log.
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

    def resolve(self) -> _ScriptedProvider:
        return self.provider

    async def list_models(self) -> list[dict[str, Any]]:
        return []


async def _serve_gateway() -> None:
    port = int(os.environ["OPENSQUILLA_SILENT_REPLY_E2E_PORT"])
    state_dir = Path(os.environ["OPENSQUILLA_SILENT_REPLY_E2E_STATE"])
    provider_log = Path(os.environ["OPENSQUILLA_SILENT_REPLY_E2E_PROVIDER_LOG"])
    state_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = state_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    config = GatewayConfig(
        host="127.0.0.1",
        port=port,
        auth=AuthConfig(mode="none"),
        goal=GoalConfig(
            execution_enabled=True,
            max_turns=3,
            runtime_budget_seconds=3600,
        ),
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
    config.llm.provider = "e2e"
    config.llm.model = _MODEL
    config.llm.api_key = ""

    provider = _ScriptedProvider(provider_log)
    await start_gateway_server(
        config=config,
        provider_selector=_ScriptedSelector(provider),
        subscription_manager=SubscriptionManager(),
        run=True,
    )
    # start_gateway_server schedules uvicorn and returns its handle. Keep the
    # owning loop alive until the parent test terminates this process.
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
                    f"Gateway exited before health check (code={process.returncode}):\n"
                    f"{output}"
                )
            try:
                response = await client.get(f"http://127.0.0.1:{port}/health")
                if response.status_code == 200 and response.json().get("ok") is True:
                    return
            except Exception as exc:  # noqa: BLE001 - included in timeout evidence
                last_error = str(exc)
            await asyncio.sleep(0.1)
    output = gateway_log.read_text(encoding="utf-8", errors="replace")
    raise AssertionError(
        f"Gateway did not become healthy: {last_error}\nprocess_output={output}"
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _isolated_gateway_env(
    *,
    tmp_path: Path,
    port: int,
    state_dir: Path,
    provider_log: Path,
) -> dict[str, str]:
    # Start from a minimal platform allowlist instead of trying to enumerate
    # credential spellings. This keeps non-standard secrets such as
    # DATABASE_URL and cloud-specific environment variables out of the child.
    inherited_keys = (
        "PATH",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
    )
    env = {key: os.environ[key] for key in inherited_keys if key in os.environ}
    project_root = Path(__file__).resolve().parents[2]
    home_dir = tmp_path / "home"
    app_data_dir = tmp_path / "appdata"
    local_app_data_dir = tmp_path / "local-appdata"
    xdg_config_dir = tmp_path / "xdg-config"
    xdg_cache_dir = tmp_path / "xdg-cache"
    xdg_data_dir = tmp_path / "xdg-data"
    temp_dir = tmp_path / "tmp"
    opensquilla_home = tmp_path / "opensquilla-home"
    log_dir = tmp_path / "logs"
    for directory in (
        home_dir,
        app_data_dir,
        local_app_data_dir,
        xdg_config_dir,
        xdg_cache_dir,
        xdg_data_dir,
        temp_dir,
        opensquilla_home,
        state_dir,
        log_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            _SERVER_MODE_ENV: "1",
            "OPENSQUILLA_SILENT_REPLY_E2E_PORT": str(port),
            "OPENSQUILLA_SILENT_REPLY_E2E_STATE": str(state_dir),
            "OPENSQUILLA_SILENT_REPLY_E2E_PROVIDER_LOG": str(provider_log),
            "OPENSQUILLA_HOME": str(opensquilla_home),
            "OPENSQUILLA_STATE_DIR": str(state_dir),
            "OPENSQUILLA_LOG_DIR": str(log_dir),
            "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
            "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
            "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY": "true",
            "HOME": str(home_dir),
            "USERPROFILE": str(home_dir),
            "APPDATA": str(app_data_dir),
            "LOCALAPPDATA": str(local_app_data_dir),
            "XDG_CONFIG_HOME": str(xdg_config_dir),
            "XDG_CACHE_HOME": str(xdg_cache_dir),
            "XDG_DATA_HOME": str(xdg_data_dir),
            "TMPDIR": str(temp_dir),
            "TEMP": str(temp_dir),
            "TMP": str(temp_dir),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(project_root / "src"),
        }
    )
    return env


async def _drain_available_frames(
    subscription: Any,
    frames: list[dict[str, Any]],
) -> None:
    while True:
        try:
            frames.append(await asyncio.wait_for(subscription.get(), timeout=0.2))
        except TimeoutError:
            return


@pytest.mark.asyncio
async def test_real_gateway_suppresses_goal_sentinel_everywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep both the health probe and WebSocket upgrade off operator proxies.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/e2e")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-e2e-access-key")
    port = _free_port()
    state_dir = tmp_path / "state"
    provider_log = tmp_path / "provider.jsonl"
    gateway_log = tmp_path / "gateway.log"
    env = _isolated_gateway_env(
        tmp_path=tmp_path,
        port=port,
        state_dir=state_dir,
        provider_log=provider_log,
    )
    assert "DATABASE_URL" not in env
    assert "AWS_ACCESS_KEY_ID" not in env
    gateway_stream = gateway_log.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).resolve())],
        cwd=tmp_path,
        env=env,
        stdout=gateway_stream,
        stderr=subprocess.STDOUT,
    )
    client = GatewayClient()
    subscription = None
    frames: list[dict[str, Any]] = []
    history: dict[str, Any] = {}
    try:
        await _wait_for_health(port, process, gateway_log)
        await client.connect(f"ws://127.0.0.1:{port}/ws")
        session_key = await client.create_session(
            model=_MODEL,
            display_name="Silent Goal process E2E",
        )
        # Goal ownership requires a live session-message subscription. Opening
        # it before goals.set also proves the exact pushed wire events.
        subscription = await client.subscribe_session_events(session_key)
        await client.call(
            "goals.set",
            {
                "sessionKey": session_key,
                "objective": "Exercise automatic Goal continuation.",
                "clientRequestId": str(uuid.uuid4()),
                "clientMessageId": str(uuid.uuid4()),
            },
        )

        deadline = time.monotonic() + 45.0
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                frames.append(await asyncio.wait_for(subscription.get(), timeout=0.1))
            except TimeoutError:
                pass
            status = await client.call("goals.status", {"sessionKey": session_key})
            goal = status.get("goal") if isinstance(status, dict) else None
            if (
                isinstance(goal, dict)
                and goal.get("status") == "paused"
                and goal.get("terminalReason") == "turn_limit"
                and not goal.get("activeTaskId")
            ):
                break
        else:
            raise AssertionError(f"Goal did not reach the deterministic limit: {status!r}")

        goal_snapshot = status["goal"]
        assert goal_snapshot["turnsSettled"] == 3
        # Every scripted turn reports 3 input + 1 output token. The silent
        # middle turn is still a successful, billable AgentTask even though it
        # has no assistant transcript row.
        assert goal_snapshot["usage"]["inputTokens"] == 9
        assert goal_snapshot["usage"]["outputTokens"] == 3
        assert goal_snapshot["usage"]["totalTokens"] == 12

        await _drain_available_frames(subscription, frames)
        history = await client.session_history(
            session_key,
            include_canonical=True,
            include_summaries=False,
        )
    finally:
        if subscription is not None:
            await subscription.close()
        await client.close()
        _stop_process(process)
        gateway_stream.close()

    done_payloads = [
        frame.get("payload") or {}
        for frame in frames
        if frame.get("event") == "session.event.done"
    ]
    assert len(done_payloads) == 3
    visible_done_by_text = {
        payload.get("text_snapshot"): payload
        for payload in done_payloads
        if payload.get("delivery") == "visible"
    }
    assert set(visible_done_by_text) == {_FIRST_VISIBLE, _THIRD_VISIBLE}
    for expected_text, payload in visible_done_by_text.items():
        assert payload["text"] == expected_text
        assert payload["suppression_reason"] is None

        task_id = payload["task_id"]
        text_delta_indexes = [
            index
            for index, frame in enumerate(frames)
            if frame.get("event") == "session.event.text_delta"
            and (frame.get("payload") or {}).get("task_id") == task_id
            and (frame.get("payload") or {}).get("text") == expected_text
        ]
        done_index = next(
            index
            for index, frame in enumerate(frames)
            if frame.get("event") == "session.event.done"
            and (frame.get("payload") or {}).get("task_id") == task_id
        )
        assert len(text_delta_indexes) == 1
        assert text_delta_indexes[0] < done_index

    suppressed = [
        payload
        for payload in done_payloads
        if payload.get("delivery") == "suppressed"
    ]
    assert len(suppressed) == 1
    silent_done = suppressed[0]
    assert silent_done["text"] == ""
    assert silent_done["text_snapshot"] == ""
    assert silent_done["suppression_reason"] == "no_reply"
    assert silent_done["input_mode"] == "system_event"
    assert silent_done["run_kind"] == "goal"
    silent_task_id = silent_done["task_id"]
    assert not any(
        frame.get("event") == "session.event.text_delta"
        and (frame.get("payload") or {}).get("task_id") == silent_task_id
        for frame in frames
    )

    public_messages = history.get("messages")
    assert isinstance(public_messages, list)
    assert _SILENT_SENTINEL not in json.dumps(public_messages, ensure_ascii=False)
    public_assistant_text = [
        message.get("text")
        for message in public_messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    assert public_assistant_text == [_FIRST_VISIBLE, _THIRD_VISIBLE]

    provider_calls = [
        json.loads(line)
        for line in provider_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["call"] for call in provider_calls] == [1, 2, 3]
    third_history = "\n".join(provider_calls[2]["assistant_history"])
    assert _FIRST_VISIBLE in third_history
    assert _SILENT_SENTINEL not in third_history

    db_path = state_dir / "sessions.db"
    with sqlite3.connect(db_path) as connection:
        raw_rows = connection.execute(
            "SELECT role, content, tool_calls FROM transcript_entries ORDER BY id"
        ).fetchall()
    assert _SILENT_SENTINEL not in json.dumps(raw_rows, ensure_ascii=False)
    raw_assistant_text = [content for role, content, _tools in raw_rows if role == "assistant"]
    assert raw_assistant_text == [_FIRST_VISIBLE, _THIRD_VISIBLE]


if __name__ == "__main__" and os.environ.get(_SERVER_MODE_ENV) == "1":
    asyncio.run(_serve_gateway())
