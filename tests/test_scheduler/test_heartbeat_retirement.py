from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import tomli_w

from opensquilla.gateway.config import GatewayConfig, HeartbeatConfig
from opensquilla.scheduler.heartbeat_loop import DEFAULT_HEARTBEAT_PROMPT, HeartbeatLoop
from opensquilla.scheduler.heartbeat_service import HeartbeatRunResult


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "# No checks\n",
        "---\nenabled: true\ninterval_ms: 1\nprompt: INJECTED\n---\ncheck everything",
        "---\nenabled: false\nactive_hours: [23, 0]\n---\n",
        "---\nmalformed: [",
    ],
)
async def test_retired_md_neither_enables_nor_suppresses_execution(
    tmp_path,
    monkeypatch,
    enabled,
    body,
):
    retired = tmp_path / "HEARTBEAT.md"
    if body is not None:
        retired.write_text(body)
    cfg = GatewayConfig(
        workspace_dir=str(tmp_path),
        heartbeat={"enabled": enabled, "prompt": None, "configPath": str(retired)},
    )
    service = SimpleNamespace(
        run_once=AsyncMock(
            return_value=HeartbeatRunResult(status="ok", session_key="agent:main:main"),
        )
    )
    loop = HeartbeatLoop(config=cfg, heartbeat_service=service)

    def forbidden(*_args, **_kwargs):
        pytest.fail("heartbeat must not inspect or read the retired path")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", forbidden)
        patch.setattr(Path, "is_file", forbidden)
        await loop._tick()
        result = await loop.run_once_now(reason="cron", agent_id="main", session_key="s")
    assert service.run_once.await_count == (2 if enabled else 0)
    if enabled:
        for call in service.run_once.call_args_list:
            assert call.kwargs["prompt"] == DEFAULT_HEARTBEAT_PROMPT
            assert call.kwargs["heartbeat_ack_max_chars"] == 500
    else:
        assert result.reason == "disabled"


async def test_nudge_and_live_config_still_work_without_md(tmp_path):
    cfg = GatewayConfig(
        workspace_dir=str(tmp_path),
        heartbeat={"enabled": True, "interval_ms": 3_600_000},
    )
    ran = asyncio.Event()

    async def run_once(**_kwargs):
        ran.set()

    loop = HeartbeatLoop(config=cfg, heartbeat_service=SimpleNamespace(run_once=run_once))
    await loop.start()
    try:
        await asyncio.sleep(0)  # Let the loop enter its long interval wait.
        loop.request_now(reason="cron", agent_id="main", session_key="agent:main:main")
        await asyncio.wait_for(ran.wait(), timeout=1)
        cfg.heartbeat.enabled = False
        ran.clear()
        await loop._tick()
        assert not ran.is_set()
    finally:
        await loop.stop()


@pytest.mark.parametrize("heartbeat", [{}, {"enabled": True}, {"configPath": ""}])
async def test_retirement_notice_once_per_start(tmp_path, monkeypatch, heartbeat):
    import opensquilla.scheduler.heartbeat_loop as module

    log = Mock()
    monkeypatch.setattr(module, "log", log)
    loop = HeartbeatLoop(
        config=GatewayConfig(workspace_dir=str(tmp_path), heartbeat=heartbeat),
        heartbeat_service=SimpleNamespace(run_once=AsyncMock()),
    )
    await loop.start()
    await loop.start()
    await loop.stop()
    assert log.warning.call_count == (1 if heartbeat else 0)
    if heartbeat:
        assert "only heartbeat configuration applies" in log.warning.call_args.kwargs["detail"]


@pytest.mark.parametrize("key", ["config_path", "configPath"])
def test_retired_path_round_trips_and_schema_marks_it_ignored(tmp_path, key):
    config = GatewayConfig(heartbeat={key: "legacy-heartbeat.md"})
    payload = config.to_toml_dict()
    assert payload["heartbeat"]["config_path"] == "legacy-heartbeat.md"
    file = tmp_path / "config.toml"
    file.write_text(tomli_w.dumps(payload))
    restored = GatewayConfig.load(str(file))
    assert restored.heartbeat.config_path == "legacy-heartbeat.md"
    field = HeartbeatConfig.model_json_schema()["properties"]["config_path"]
    assert field["deprecated"] is True
    assert "ignored" in field["description"]
    assert "config_path" not in GatewayConfig().to_toml_dict()["heartbeat"]


@pytest.mark.parametrize(
    "env",
    ["OPENSQUILLA_HEARTBEAT_CONFIG_PATH", "OPENSQUILLA_GATEWAY_HEARTBEAT__CONFIG_PATH"],
)
def test_retired_path_environment_input_remains_accepted(monkeypatch, env):
    monkeypatch.setenv(env, "legacy-md")
    assert GatewayConfig().heartbeat.config_path == "legacy-md"
