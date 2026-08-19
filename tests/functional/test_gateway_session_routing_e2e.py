"""Real Gateway/WebSocket regression coverage for session model routing."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.cli.gateway_client import GatewayClient, GatewayRPCError
from opensquilla.gateway.boot import start_gateway_server
from opensquilla.gateway.config import AuthConfig, GatewayConfig, LlmProviderConfig
from opensquilla.gateway.model_routing import model_routing_snapshot
from opensquilla.gateway.websocket import SubscriptionManager
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus
from opensquilla.session.storage import SessionStorage
from opensquilla.session.turn_context import turn_context_scope

_SERVER_MODE_ENV = "OPENSQUILLA_SESSION_ROUTING_E2E_SERVER"
_HISTORY_SESSION_KEY = "agent:main:webchat:routing-history-e2e"
_HISTORY_TURN_ID = "routing-history-turn"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _serve_gateway() -> None:
    port = int(os.environ["OPENSQUILLA_SESSION_ROUTING_E2E_PORT"])
    state_dir = Path(os.environ["OPENSQUILLA_SESSION_ROUTING_E2E_STATE"])
    config_path = Path(os.environ["OPENSQUILLA_SESSION_ROUTING_E2E_CONFIG"])
    state_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = state_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    config = GatewayConfig(
        host="127.0.0.1",
        port=port,
        auth=AuthConfig(mode="none"),
        config_path=str(config_path),
        state_dir=str(state_dir),
        workspace_dir=str(workspace_dir),
    )
    config.control_ui.enabled = False
    config.llm = LlmProviderConfig(
        provider="ollama",
        model="routing-e2e-no-model",
        base_url="http://127.0.0.1:9",
    )
    config.naming.enabled = False
    config.compaction.enabled = False
    config.memory.retrieval_mode = "fts_only"
    config.memory.auto_capture_enabled = False
    config.memory.capture_mode = "off"
    config.memory.repair_enabled = False
    config.memory.ttl_sweep_interval_minutes = 0
    config.meta_skill.enabled = False
    config.heartbeat.enabled = False
    config.squilla_router.enabled = False
    config.squilla_router.rollout_phase = "observe"
    config.llm_ensemble.enabled = False

    storage = SessionStorage(str(state_dir / "sessions.db"))
    await storage.connect()
    manager = SessionManager(
        storage,
        inject_time_prefix=False,
        checkpoint_workspace_dir=workspace_dir,
        model_routing_mode_provider=lambda: model_routing_snapshot(config)["mode"],
    )
    await manager.create(
        _HISTORY_SESSION_KEY,
        agent_id="main",
        display_name="Synthetic routing history",
    )
    with turn_context_scope({"turn_id": _HISTORY_TURN_ID}):
        await manager.append_message(
            _HISTORY_SESSION_KEY,
            "user",
            "synthetic routing history request",
        )
    await storage.create_agent_task(
        AgentTaskRecord(
            task_id=_HISTORY_TURN_ID,
            session_key=_HISTORY_SESSION_KEY,
            agent_id="main",
            source_kind="webui",
            queue_mode="followup",
            run_kind="session_turn",
            status=AgentTaskStatus.CANCELLED,
            started_at=100,
            finished_at=200,
            details={
                "turn_id": _HISTORY_TURN_ID,
                "accepted_model_routing": {"effective_mode": "ensemble"},
                "turn_outcome": {
                    "kind": "interrupted",
                    "reason": "cancelled",
                    "cancellation_source": "synthetic_e2e",
                    "retryable": True,
                },
            },
        )
    )

    await start_gateway_server(
        config=config,
        session_manager=manager,
        subscription_manager=SubscriptionManager(),
        run=True,
    )
    await asyncio.Event().wait()


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
            except Exception as exc:  # noqa: BLE001 - included in timeout evidence.
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
    config_path: Path,
) -> dict[str, str]:
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
    directories = {
        "HOME": tmp_path / "home",
        "USERPROFILE": tmp_path / "home",
        "APPDATA": tmp_path / "appdata",
        "LOCALAPPDATA": tmp_path / "local-appdata",
        "XDG_CONFIG_HOME": tmp_path / "xdg-config",
        "XDG_CACHE_HOME": tmp_path / "xdg-cache",
        "XDG_DATA_HOME": tmp_path / "xdg-data",
        "TMPDIR": tmp_path / "tmp",
        "TEMP": tmp_path / "tmp",
        "TMP": tmp_path / "tmp",
        "OPENSQUILLA_HOME": tmp_path / "opensquilla-home",
        "OPENSQUILLA_LOG_DIR": tmp_path / "logs",
    }
    for directory in {*directories.values(), state_dir}:
        directory.mkdir(parents=True, exist_ok=True)
    env.update({key: str(value) for key, value in directories.items()})
    env.update(
        {
            _SERVER_MODE_ENV: "1",
            "OPENSQUILLA_SESSION_ROUTING_E2E_PORT": str(port),
            "OPENSQUILLA_SESSION_ROUTING_E2E_STATE": str(state_dir),
            "OPENSQUILLA_SESSION_ROUTING_E2E_CONFIG": str(config_path),
            "OPENSQUILLA_STATE_DIR": str(state_dir),
            "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
            "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
            "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY": "true",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(project_root / "src"),
        }
    )
    return env


def _routing(snapshot: dict[str, Any]) -> dict[str, Any]:
    nested = snapshot.get("routing")
    return nested if isinstance(nested, dict) else snapshot


@pytest.mark.asyncio
async def test_real_gateway_websocket_session_routing_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    port = _free_port()
    state_dir = tmp_path / "state"
    config_path = tmp_path / "gateway.toml"
    gateway_log = tmp_path / "gateway.log"
    env = _isolated_gateway_env(
        tmp_path=tmp_path,
        port=port,
        state_dir=state_dir,
        config_path=config_path,
    )
    assert "OPENAI_API_KEY" not in env

    gateway_stream = gateway_log.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).resolve())],
        cwd=tmp_path,
        env=env,
        stdout=gateway_stream,
        stderr=subprocess.STDOUT,
    )
    client = GatewayClient()
    try:
        await _wait_for_health(port, process, gateway_log)
        await client.connect(f"ws://127.0.0.1:{port}/ws")

        initial_global = await client.get_model_routing()
        assert initial_global["mode"] == "direct"

        session_a = await client.create_session(display_name="Routing session A")
        session_a_initial = _routing(await client.get_session_routing(session_a))
        assert session_a_initial == {
            "mode": "direct",
            "revision": 0,
            "source": "session",
            "initialized": False,
            "appliesTo": "next_accepted_turn",
        }

        changed_global = await client.set_model_routing("router")
        assert changed_global["mode"] == "router"
        assert changed_global["restart_required"] is False

        session_a_after_global = _routing(await client.get_session_routing(session_a))
        assert session_a_after_global["mode"] == "direct"
        assert session_a_after_global["revision"] == 0

        session_b = await client.create_session(display_name="Routing session B")
        session_b_initial = _routing(await client.get_session_routing(session_b))
        assert session_b_initial["mode"] == "router"
        assert session_b_initial["revision"] == 0

        session_b_direct = _routing(
            await client.set_session_routing(
                session_b,
                "direct",
                expected_revision=0,
            )
        )
        assert session_b_direct["mode"] == "direct"
        assert session_b_direct["revision"] == 1

        lost_ack_retry = _routing(
            await client.set_session_routing(
                session_b,
                "direct",
                expected_revision=0,
            )
        )
        assert lost_ack_retry["mode"] == "direct"
        assert lost_ack_retry["revision"] == 1

        with pytest.raises(GatewayRPCError) as conflict:
            await client.set_session_routing(
                session_b,
                "router",
                expected_revision=0,
            )
        assert conflict.value.code == "SESSION_ROUTING_CHANGED"
        assert conflict.value.retryable is True
        assert _routing(conflict.value.data or {})["revision"] == 1

        final_global = await client.get_model_routing()
        assert final_global["mode"] == "router"
        assert _routing(await client.get_session_routing(session_b))["mode"] == "direct"

        first_turn_session = "agent:main:webchat:routing-first-turn-e2e"
        accepted = await client.call(
            "chat.send",
            {
                "sessionKey": first_turn_session,
                "message": "Synthetic first-turn routing probe.",
                "intent": "new_chat",
                "initialRoutingMode": "direct",
                "clientRequestId": "routing-first-turn-request",
                "clientMessageId": "routing-first-turn-message",
                "queueMode": "followup",
                "_source": {"runMode": "safe"},
            },
        )
        accepted_task_id = str(
            accepted.get("task_id") or accepted.get("taskId") or ""
        ).strip()
        try:
            assert accepted["sessionKey"] == first_turn_session
            assert accepted["acceptedRouting"] == {"mode": "direct"}
            assert _routing(accepted) == {
                "mode": "direct",
                "revision": 0,
                "source": "session",
                "initialized": False,
                "appliesTo": "next_accepted_turn",
            }
            first_turn_routing = _routing(
                await client.get_session_routing(first_turn_session)
            )
            assert first_turn_routing["mode"] == "direct"
            assert first_turn_routing["revision"] == 0
        finally:
            if accepted_task_id:
                await client.call(
                    "chat.abort",
                    {
                        "sessionKey": first_turn_session,
                        "taskId": accepted_task_id,
                        "scope": "task",
                        "source": "functional_test_cleanup",
                    },
                )

        history = await client.session_history(
            _HISTORY_SESSION_KEY,
            include_canonical=True,
            include_summaries=False,
        )
        assert history["turn_outcomes"] == [
            {
                "turn_id": _HISTORY_TURN_ID,
                "task_id": _HISTORY_TURN_ID,
                "status": "cancelled",
                "started_at": 100,
                "finished_at": 200,
                "outcome": {
                    "kind": "interrupted",
                    "reason": "cancelled",
                    "cancellation_source": "synthetic_e2e",
                    "retryable": True,
                },
                "accepted_routing_mode": "ensemble",
            }
        ]
    finally:
        await client.close()
        _stop_process(process)
        gateway_stream.close()


if __name__ == "__main__" and os.environ.get(_SERVER_MODE_ENV) == "1":
    asyncio.run(_serve_gateway())
