from __future__ import annotations

import json
import shlex
from typing import Any

from typer.testing import CliRunner

from opensquilla.cli import doctor_cmd as _doctor_cmd
from opensquilla.cli.main import app

runner = CliRunner()

# Captured at import time, before the autouse ``_no_local_tui_findings``
# fixture in tests/test_cli/conftest.py stubs the module attribute per test;
# the dedicated TUI-finding tests reinstate this real probe explicitly.
_REAL_LOCAL_TUI_FINDINGS = _doctor_cmd._local_tui_findings


def _config_arg(path: Any) -> str:
    return shlex.quote(str(path))


class _FakeGatewayClient:
    calls: list[tuple[str, Any]] = []
    payload: dict[str, Any] = {
        "status": "action_required",
        "ready": False,
        "summary": "1 action required",
        "counts": {"error": 1, "warn": 0, "info": 0, "ok": 1},
        "findings": [
            {
                "id": "provider.active.not_configured",
                "severity": "error",
                "surface": "provider",
                "title": "Active provider is not configured",
                "detail": "openrouter is active but missing required configuration.",
                "evidence": {"providerId": "openrouter"},
                "fixSteps": [
                    {
                        "label": "Configure provider",
                        "command": (
                            "opensquilla providers configure openrouter "
                            "--api-key YOUR_API_KEY"
                        ),
                    }
                ],
                "restartRequired": True,
            }
        ],
    }

    async def connect(self, url: str) -> None:
        type(self).calls.append(("connect", url))

    async def close(self) -> None:
        type(self).calls.append(("close", None))

    async def call(self, method: str, params: dict | None = None) -> Any:
        type(self).calls.append((method, params or {}))
        return type(self).payload


class _ReadyGatewayClient(_FakeGatewayClient):
    payload = {
        "status": "ready",
        "ready": True,
        "summary": "Ready",
        "counts": {"error": 0, "warn": 0, "info": 0, "ok": 2},
        "findings": [
            {
                "id": "gateway.rpc.ready",
                "severity": "ok",
                "surface": "gateway",
                "title": "Gateway RPC ready",
                "detail": "The gateway accepted and handled doctor.status.",
                "evidence": {},
                "fixSteps": [],
                "restartRequired": False,
            },
            {
                "id": "provider.active.ready",
                "severity": "ok",
                "surface": "provider",
                "title": "Active provider ready",
                "detail": "openrouter is configured and buildable.",
                "evidence": {},
                "fixSteps": [],
                "restartRequired": False,
            },
        ],
    }


class _OfflineGatewayClient(_FakeGatewayClient):
    async def connect(self, url: str) -> None:
        raise SystemExit(
            "Cannot connect to OpenSquilla gateway.\n"
            "Is the gateway running? Start it with: opensquilla gateway run\n"
            "Error: connection refused"
        )


def test_doctor_json_calls_doctor_status(monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "action_required"
    assert ("doctor.status", {"agentId": "main", "deep": True}) in _FakeGatewayClient.calls


def test_doctor_quick_skips_deep_memory_diagnostics(monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    result = runner.invoke(app, ["doctor", "--quick", "--json"])

    assert result.exit_code == 1
    assert ("doctor.status", {"agentId": "main", "deep": False}) in _FakeGatewayClient.calls


def test_doctor_config_targets_gateway_from_config_path(tmp_path, monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    target = tmp_path / "custom.toml"
    target.write_text('host = "0.0.0.0"\nport = 20002\n', encoding="utf-8")
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    result = runner.invoke(app, ["doctor", "--json", "--config", str(target)])

    assert result.exit_code == 1
    assert ("connect", "ws://127.0.0.1:20002/ws") in _FakeGatewayClient.calls
    assert ("doctor.status", {"agentId": "main", "deep": True}) in _FakeGatewayClient.calls


def test_doctor_config_derived_gateway_recovery_preserves_config_target(
    tmp_path,
    monkeypatch,
) -> None:
    target = tmp_path / "custom.toml"
    target.write_text('host = "0.0.0.0"\nport = 20002\n', encoding="utf-8")
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _OfflineGatewayClient)

    result = runner.invoke(app, ["doctor", "--json", "--config", str(target)])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "gateway.unavailable"
    )
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert commands[:2] == [
        f"opensquilla gateway start --config {_config_arg(target)}",
        f"opensquilla gateway status --json --config {_config_arg(target)}",
    ]
    assert all("--bind 127.0.0.1" not in command for command in commands)
    assert all("--port 20002" not in command for command in commands[:2])
    assert finding["evidence"]["gatewayUrl"] == "ws://127.0.0.1:20002/ws"


def test_doctor_targets_existing_cwd_config_without_explicit_config(
    tmp_path,
    monkeypatch,
) -> None:
    _FakeGatewayClient.calls = []
    target = tmp_path / "opensquilla.toml"
    target.write_text('host = "127.0.0.1"\nport = 20009\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert ("connect", "ws://127.0.0.1:20009/ws") in _FakeGatewayClient.calls
    assert payload["configPath"] == str(target)
    provider_finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "provider.active.not_configured"
    )
    commands = [step["command"] for step in provider_finding["fixSteps"] if "command" in step]
    assert commands == [
        "opensquilla providers configure openrouter --api-key YOUR_API_KEY "
        f"--config {_config_arg(target)}"
    ]


def test_doctor_targets_env_config_without_explicit_config(
    tmp_path,
    monkeypatch,
) -> None:
    _FakeGatewayClient.calls = []
    target = tmp_path / "env-opensquilla.toml"
    target.write_text('host = "127.0.0.1"\nport = 20010\n', encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert ("connect", "ws://127.0.0.1:20010/ws") in _FakeGatewayClient.calls
    assert payload["configPath"] == str(target)
    provider_finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "provider.active.not_configured"
    )
    commands = [step["command"] for step in provider_finding["fixSteps"] if "command" in step]
    assert commands == [
        "opensquilla providers configure openrouter --api-key YOUR_API_KEY "
        f"--config {_config_arg(target)}"
    ]


def test_doctor_config_scopes_gateway_recovery_commands(tmp_path, monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    target = tmp_path / "custom.toml"
    target.write_text('host = "127.0.0.1"\nport = 20003\n', encoding="utf-8")
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    result = runner.invoke(app, ["doctor", "--json", "--config", str(target)])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "provider.active.not_configured"
    )
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert commands == [
        "opensquilla providers configure openrouter --api-key YOUR_API_KEY "
        f"--config {_config_arg(target)}"
    ]


def test_doctor_config_scopes_config_set_recovery_commands(tmp_path, monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    target = tmp_path / "custom.toml"
    target.write_text('host = "127.0.0.1"\nport = 20003\n', encoding="utf-8")
    original_payload = _FakeGatewayClient.payload
    _FakeGatewayClient.payload = {
        "status": "ready",
        "ready": True,
        "summary": "Ready, 1 optional setup item",
        "counts": {"error": 0, "warn": 0, "info": 1, "ok": 1},
        "impactCounts": {"blocks_ready": 0, "degrades": 0, "optional": 1, "none": 1},
        "configPath": str(target),
        "findings": [
            {
                "id": "logs.gateway_file_log.disabled",
                "severity": "info",
                "readinessImpact": "optional",
                "surface": "logs",
                "title": "Gateway file logging is disabled",
                "detail": "Persistent gateway file logging is optional.",
                "fixSteps": [
                    {
                        "label": "Enable gateway file logging",
                        "command": "opensquilla config set log_file_enabled true",
                    },
                    {
                        "label": "Restart gateway",
                        "command": "opensquilla gateway restart",
                    },
                ],
                "restartRequired": True,
            }
        ],
    }
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    try:
        result = runner.invoke(app, ["doctor", "--json", "--config", str(target)])
    finally:
        _FakeGatewayClient.payload = original_payload

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "logs.gateway_file_log.disabled"
    )
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert commands == [
        f"opensquilla config set log_file_enabled true --config {_config_arg(target)}",
        f"opensquilla gateway restart --config {_config_arg(target)}",
    ]


def test_doctor_bad_config_reports_config_recovery(tmp_path, monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    target = tmp_path / "broken.toml"
    target.write_text("[llm\n", encoding="utf-8")
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    result = runner.invoke(app, ["doctor", "--json", "--config", str(target)])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    ids = [finding["id"] for finding in payload["findings"]]
    assert "config.local.unreadable" in ids
    assert _FakeGatewayClient.calls == []


def test_doctor_bad_gateway_config_env_reports_config_recovery(
    tmp_path, monkeypatch
) -> None:
    _FakeGatewayClient.calls = []
    target = tmp_path / "broken-env.toml"
    target.write_text("[llm\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    config_finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "config.local.unreadable"
    )
    assert config_finding["evidence"]["configPath"] == str(target)
    assert _FakeGatewayClient.calls == []


def test_doctor_explicit_gateway_context_ignores_env_config_path(
    tmp_path, monkeypatch
) -> None:
    _ReadyGatewayClient.calls = []
    target = tmp_path / "env.toml"
    target.write_text('host = "127.0.0.1"\nport = 20004\n', encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _ReadyGatewayClient)

    result = runner.invoke(
        app,
        ["doctor", "--json", "--gateway", "http://squilla.example.com:9443"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["gatewayUrl"] == "ws://squilla.example.com:9443/ws"
    assert "configPath" not in payload


def test_doctor_remote_gateway_ignores_explicit_local_config_context(
    tmp_path,
    monkeypatch,
) -> None:
    _FakeGatewayClient.calls = []
    target = tmp_path / "local-only.toml"
    target.write_text('host = "127.0.0.1"\nport = 20008\n', encoding="utf-8")
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    result = runner.invoke(
        app,
        [
            "doctor",
            "--json",
            "--gateway",
            "https://squilla.example.com",
            "--config",
            str(target),
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["gatewayUrl"] == "wss://squilla.example.com/ws"
    assert "configPath" not in payload
    assert "requestedConfigPath" not in payload
    finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "provider.active.not_configured"
    )
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert commands == ["opensquilla providers configure openrouter --api-key YOUR_API_KEY"]


def test_doctor_ready_json_exits_zero(monkeypatch) -> None:
    _ReadyGatewayClient.calls = []
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _ReadyGatewayClient)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["ready"] is True


def test_doctor_ready_table_summarizes_without_dumping_ok_findings(monkeypatch) -> None:
    _ReadyGatewayClient.calls = []
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _ReadyGatewayClient)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "No action needed" in result.stdout
    assert "Gateway RPC ready" not in result.stdout
    assert "Active provider ready" not in result.stdout
    assert "Reference" not in result.stdout


def test_doctor_table_shows_gateway_config_and_agent_context(tmp_path, monkeypatch) -> None:
    _ReadyGatewayClient.calls = []
    target = tmp_path / "custom.toml"
    target.write_text('host = "127.0.0.1"\nport = 20003\n', encoding="utf-8")
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _ReadyGatewayClient)

    result = runner.invoke(
        app,
        ["doctor", "--agent", "worker", "--config", str(target)],
    )

    assert result.exit_code == 0
    # The context line soft-wraps when the pytest tmp path is long, so compare
    # against a wrap-insensitive projection of the output.
    unwrapped = " ".join(result.stdout.split())
    assert "Gateway: ws://127.0.0.1:20003/ws" in unwrapped
    assert f"Config: {target}" in unwrapped
    assert "Agent: worker" in unwrapped


def test_doctor_config_reports_running_gateway_config_mismatch(
    tmp_path,
    monkeypatch,
) -> None:
    _ReadyGatewayClient.calls = []
    requested = tmp_path / "requested.toml"
    running = tmp_path / "running.toml"
    requested.write_text('host = "127.0.0.1"\nport = 20005\n', encoding="utf-8")
    original_payload = _ReadyGatewayClient.payload
    _ReadyGatewayClient.payload = {
        **original_payload,
        "configPath": str(running),
    }
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _ReadyGatewayClient)

    try:
        result = runner.invoke(app, ["doctor", "--json", "--config", str(requested)])
    finally:
        _ReadyGatewayClient.payload = original_payload

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ready"] is False
    assert payload["status"] == "action_required"
    assert payload["requestedConfigPath"] == str(requested)
    assert payload["configPath"] == str(running)
    finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "gateway.config.mismatch"
    )
    assert finding["readinessImpact"] == "blocks_ready"
    assert finding["evidence"]["requestedConfigPath"] == str(requested)
    assert finding["evidence"]["runningConfigPath"] == str(running)
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert f"opensquilla gateway restart --config {_config_arg(requested)}" in commands
    assert (
        f"opensquilla gateway status --json --config {_config_arg(requested)}"
        in commands
    )


def test_doctor_config_mismatch_prioritizes_requested_gateway_recovery(
    tmp_path,
    monkeypatch,
) -> None:
    _FakeGatewayClient.calls = []
    requested = tmp_path / "requested.toml"
    running = tmp_path / "running.toml"
    requested.write_text('host = "127.0.0.1"\nport = 20006\n', encoding="utf-8")
    running.write_text('host = "127.0.0.1"\nport = 20007\n', encoding="utf-8")
    original_payload = _FakeGatewayClient.payload
    _FakeGatewayClient.payload = {
        **original_payload,
        "configPath": str(running),
    }
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    try:
        result = runner.invoke(app, ["doctor", "--json", "--config", str(requested)])
    finally:
        _FakeGatewayClient.payload = original_payload

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert [finding["id"] for finding in payload["findings"][:2]] == [
        "gateway.config.mismatch",
        "provider.active.not_configured",
    ]


def test_doctor_config_mismatch_keeps_running_findings_scoped_to_running_config(
    tmp_path,
    monkeypatch,
) -> None:
    _FakeGatewayClient.calls = []
    requested = tmp_path / "requested.toml"
    running = tmp_path / "running.toml"
    requested.write_text('host = "127.0.0.1"\nport = 20006\n', encoding="utf-8")
    running.write_text('host = "127.0.0.1"\nport = 20007\n', encoding="utf-8")
    original_payload = _FakeGatewayClient.payload
    _FakeGatewayClient.payload = {
        **original_payload,
        "configPath": str(running),
    }
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    try:
        result = runner.invoke(app, ["doctor", "--json", "--config", str(requested)])
    finally:
        _FakeGatewayClient.payload = original_payload

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    provider_finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "provider.active.not_configured"
    )
    provider_commands = [
        step["command"] for step in provider_finding["fixSteps"] if "command" in step
    ]
    assert provider_commands == [
        "opensquilla providers configure openrouter --api-key YOUR_API_KEY "
        f"--config {_config_arg(running)}"
    ]
    mismatch_finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "gateway.config.mismatch"
    )
    mismatch_commands = [
        step["command"] for step in mismatch_finding["fixSteps"] if "command" in step
    ]
    assert f"opensquilla gateway restart --config {_config_arg(requested)}" in mismatch_commands


def test_doctor_table_prints_fix_steps(monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "Active provider is not configured" in result.stdout
    assert "opensquilla providers configure openrouter" in result.stdout


def test_doctor_degraded_table_exits_zero(monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    original_payload = _FakeGatewayClient.payload
    _FakeGatewayClient.payload = {
        "status": "degraded",
        "ready": True,
        "summary": "Ready, 1 degraded check",
        "counts": {"error": 0, "warn": 1, "info": 0, "ok": 1},
        "findings": [
            {
                "id": "search.provider.not_configured",
                "severity": "warn",
                "surface": "search",
                "title": "Search provider is not configured",
                "detail": "brave is selected for web search but is missing required configuration.",
                "fixSteps": [
                    {
                        "label": "Configure search",
                        "command": (
                            "opensquilla configure search "
                            "--search-provider brave --api-key YOUR_API_KEY"
                        ),
                    }
                ],
                "restartRequired": True,
            }
        ],
    }
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    try:
        result = runner.invoke(app, ["doctor"])
    finally:
        _FakeGatewayClient.payload = original_payload

    assert result.exit_code == 0
    assert "Ready, 1 degraded check" in result.stdout
    assert "Search provider is not configured" in result.stdout
    assert "Recovery requires restart." in result.stdout
    assert "Restart required." not in result.stdout


def test_doctor_table_explains_non_blocking_error_impact(monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    original_payload = _FakeGatewayClient.payload
    _FakeGatewayClient.payload = {
        "status": "degraded",
        "ready": True,
        "summary": "Ready, 1 degraded check",
        "counts": {"error": 1, "warn": 0, "info": 0, "ok": 1},
        "impactCounts": {"blocks_ready": 0, "degrades": 1, "optional": 0, "none": 1},
        "findings": [
            {
                "id": "channel.feishu.dead",
                "severity": "error",
                "readinessImpact": "degrades",
                "surface": "channels",
                "title": "Channel feishu is dead",
                "detail": "The configured channel is not able to receive or send messages.",
                "fixSteps": [
                    {
                        "label": "Restart channel",
                        "command": "opensquilla channels restart feishu --yes",
                    }
                ],
                "restartRequired": False,
            }
        ],
    }
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    try:
        result = runner.invoke(app, ["doctor"])
    finally:
        _FakeGatewayClient.payload = original_payload

    assert result.exit_code == 0
    assert "Channel feishu is dead" in result.stdout
    assert "degrades" in result.stdout


def test_doctor_table_uses_readiness_impact_to_select_action_findings(monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    original_payload = _FakeGatewayClient.payload
    _FakeGatewayClient.payload = {
        "status": "action_required",
        "ready": False,
        "summary": "1 action required",
        "counts": {"error": 0, "warn": 0, "info": 2, "ok": 0},
        "impactCounts": {"blocks_ready": 1, "degrades": 0, "optional": 1, "none": 0},
        "findings": [
            {
                "id": "image_generation.disabled",
                "severity": "info",
                "readinessImpact": "optional",
                "surface": "image_generation",
                "title": "Image generation is disabled",
                "detail": "Image generation is optional and is currently disabled.",
                "fixSteps": [
                    {
                        "label": "Enable image generation",
                        "command": (
                            "opensquilla configure image-generation "
                            "--image-provider openai --api-key YOUR_API_KEY"
                        ),
                    }
                ],
            },
            {
                "id": "provider.active.not_configured",
                "severity": "info",
                "readinessImpact": "blocks_ready",
                "surface": "provider",
                "title": "Active provider is not configured",
                "detail": "openrouter is active but missing required configuration.",
                "fixSteps": [
                    {
                        "label": "Configure provider",
                        "command": (
                            "opensquilla providers configure openrouter --api-key YOUR_API_KEY"
                        ),
                    }
                ],
                "restartRequired": True,
            },
        ],
    }
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    try:
        result = runner.invoke(app, ["doctor"])
    finally:
        _FakeGatewayClient.payload = original_payload

    assert result.exit_code == 1
    assert "Active provider is not configured" in result.stdout
    assert "Image generation is disabled" not in result.stdout
    assert "blocks readiness" in result.stdout
    assert "Recovery" in result.stdout
    assert "Optional setup" not in result.stdout


def test_doctor_table_uses_readiness_impact_to_label_optional_steps(monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    original_payload = _FakeGatewayClient.payload
    _FakeGatewayClient.payload = {
        "status": "ready",
        "ready": True,
        "summary": "Ready, 1 optional setup item",
        "counts": {"error": 0, "warn": 1, "info": 0, "ok": 1},
        "impactCounts": {"blocks_ready": 0, "degrades": 0, "optional": 1, "none": 1},
        "findings": [
            {
                "id": "sandbox.posture.soft",
                "severity": "warn",
                "readinessImpact": "optional",
                "surface": "sandbox",
                "title": "Sandbox posture can be hardened",
                "detail": "Sandbox hardening is optional for this local setup.",
                "fixSteps": [
                    {
                        "label": "Inspect sandbox",
                        "command": "opensquilla sandbox status --json",
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    try:
        result = runner.invoke(app, ["doctor"])
    finally:
        _FakeGatewayClient.payload = original_payload

    assert result.exit_code == 0
    assert "Sandbox posture can be hardened" in result.stdout
    assert "Optional setup" in result.stdout
    assert "Recovery" not in result.stdout


def test_doctor_table_hides_non_actionable_findings_when_attention_is_needed(monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    original_payload = _FakeGatewayClient.payload
    _FakeGatewayClient.payload = {
        **original_payload,
        "findings": [
            *original_payload["findings"],
            {
                "id": "image_generation.disabled",
                "severity": "info",
                "surface": "image_generation",
                "title": "Image generation is disabled",
                "detail": "Image generation is optional and is currently disabled.",
                "evidence": {},
                "fixSteps": [
                    {
                        "label": "Enable image generation",
                        "command": (
                            "opensquilla configure image-generation "
                            "--image-provider openai --api-key YOUR_API_KEY"
                        ),
                    }
                ],
                "restartRequired": False,
            },
            {
                "id": "gateway.rpc.ready",
                "severity": "ok",
                "surface": "gateway",
                "title": "Gateway RPC ready",
                "detail": "The gateway accepted and handled doctor.status.",
                "evidence": {},
                "fixSteps": [],
                "restartRequired": False,
            },
        ],
    }
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    try:
        result = runner.invoke(app, ["doctor"])
    finally:
        _FakeGatewayClient.payload = original_payload

    assert result.exit_code == 1
    assert "Active provider is not configured" in result.stdout
    assert "Image generation is disabled" not in result.stdout
    assert "Gateway RPC ready" not in result.stdout


def test_doctor_table_labels_info_only_steps_as_optional(monkeypatch) -> None:
    _FakeGatewayClient.calls = []
    original_payload = _FakeGatewayClient.payload
    _FakeGatewayClient.payload = {
        "status": "ready",
        "ready": True,
        "summary": "Ready, 1 optional setup item",
        "counts": {"error": 0, "warn": 0, "info": 1, "ok": 1},
        "findings": [
            {
                "id": "image_generation.disabled",
                "severity": "info",
                "surface": "image_generation",
                "title": "Image generation is disabled",
                "detail": "Image generation is optional and is currently disabled.",
                "evidence": {},
                "fixSteps": [
                    {
                        "label": "Enable image generation",
                        "command": (
                            "opensquilla configure image-generation "
                            "--image-provider openai --api-key YOUR_API_KEY"
                        ),
                    }
                ],
                "restartRequired": False,
            },
            {
                "id": "gateway.rpc.ready",
                "severity": "ok",
                "surface": "gateway",
                "title": "Gateway RPC ready",
                "detail": "The gateway accepted and handled doctor.status.",
                "evidence": {},
                "fixSteps": [],
                "restartRequired": False,
            },
        ],
    }
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _FakeGatewayClient)

    try:
        result = runner.invoke(app, ["doctor"])
    finally:
        _FakeGatewayClient.payload = original_payload

    assert result.exit_code == 0
    assert "Optional setup" in result.stdout
    assert "Recovery" not in result.stdout
    assert "Image generation is disabled" in result.stdout
    assert "Gateway RPC ready" not in result.stdout


def test_doctor_reports_gateway_unavailable_as_json(monkeypatch) -> None:
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _OfflineGatewayClient)
    monkeypatch.setattr("opensquilla.cli.doctor_cmd._local_config_findings", lambda: [])

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "unavailable"
    assert payload["impactCounts"] == {
        "blocks_ready": 1,
        "degrades": 0,
        "optional": 0,
        "none": 0,
    }
    finding = payload["findings"][0]
    assert finding["id"] == "gateway.unavailable"
    assert finding["readinessImpact"] == "blocks_ready"
    assert "opensquilla gateway start" in finding["fixSteps"][0]["command"]
    assert "opensquilla gateway run" not in finding["detail"]
    assert "recovery steps" in finding["detail"]
    assert "opensquilla gateway run" in finding["evidence"]["error"]


def test_doctor_gateway_unavailable_uses_requested_gateway_url(monkeypatch) -> None:
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _OfflineGatewayClient)
    monkeypatch.setattr("opensquilla.cli.doctor_cmd._local_config_findings", lambda: [])

    result = runner.invoke(
        app,
        ["doctor", "--json", "--gateway", "http://127.0.0.1:19999"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    finding = payload["findings"][0]
    assert finding["evidence"]["gatewayUrl"] == "ws://127.0.0.1:19999/ws"
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert commands[:2] == [
        "opensquilla gateway start --bind 127.0.0.1 --port 19999",
        "opensquilla gateway status --bind 127.0.0.1 --port 19999 --json",
    ]


def test_doctor_gateway_unavailable_uses_requested_config_path(
    tmp_path,
    monkeypatch,
) -> None:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.onboarding.config_store import persist_config

    target = tmp_path / "custom-config.toml"
    persist_config(
        GatewayConfig(
            llm={
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-flash",
                "api_key": "",
                "api_key_env": "CUSTOM_LLM_KEY",
            }
        ),
        path=target,
    )
    monkeypatch.delenv("CUSTOM_LLM_KEY", raising=False)
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _OfflineGatewayClient)

    result = runner.invoke(
        app,
        [
            "doctor",
            "--json",
            "--gateway",
            "http://127.0.0.1:19999",
            "--config",
            str(target),
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    gateway_finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "gateway.unavailable"
    )
    gateway_commands = [
        step["command"] for step in gateway_finding["fixSteps"] if "command" in step
    ]
    assert gateway_commands[:2] == [
        "opensquilla gateway start --bind 127.0.0.1 --port 19999 "
        f"--config {_config_arg(target)}",
        (
            "opensquilla gateway status --bind 127.0.0.1 --port 19999 "
            f"--json --config {_config_arg(target)}"
        ),
    ]
    assert f"opensquilla diagnostics status --config {_config_arg(target)}" in gateway_commands
    finding = next(
        finding
        for finding in payload["findings"]
        if finding["id"] == "config.local.needs_onboarding"
    )
    assert finding["evidence"]["configPath"] == str(target)
    assert "CUSTOM_LLM_KEY" in finding["detail"]
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert commands[-2:] == [
        f"opensquilla onboard status --json --config {_config_arg(target)}",
        f"opensquilla onboard --if-needed --config {_config_arg(target)}",
    ]


def test_doctor_gateway_unavailable_prioritizes_unreadable_config(
    tmp_path,
    monkeypatch,
) -> None:
    target = tmp_path / "broken.toml"
    target.write_text("[llm\n", encoding="utf-8")
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _OfflineGatewayClient)

    result = runner.invoke(
        app,
        [
            "doctor",
            "--json",
            "--gateway",
            "http://127.0.0.1:19999",
            "--config",
            str(target),
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    ids = [finding["id"] for finding in payload["findings"]]
    assert ids[:2] == ["config.local.unreadable", "gateway.unavailable"]
    first_commands = [
        step["command"] for step in payload["findings"][0]["fixSteps"] if "command" in step
    ]
    assert first_commands == [
        f"opensquilla onboard status --json --config {_config_arg(target)}",
        f"opensquilla onboard --if-needed --config {_config_arg(target)}",
    ]


def test_doctor_gateway_unavailable_does_not_start_remote_gateway(monkeypatch) -> None:
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _OfflineGatewayClient)
    monkeypatch.setattr("opensquilla.cli.doctor_cmd._local_config_findings", lambda: [])

    result = runner.invoke(
        app,
        ["doctor", "--json", "--gateway", "https://squilla.example.com"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    finding = payload["findings"][0]
    assert finding["evidence"]["gatewayUrl"] == "wss://squilla.example.com/ws"
    commands = [step["command"] for step in finding["fixSteps"] if "command" in step]
    assert all("gateway start" not in command for command in commands)
    assert commands[0] == (
        "opensquilla gateway status --gateway wss://squilla.example.com/ws --json"
    )
    details = [step.get("detail", "") for step in finding["fixSteps"]]
    assert any("remote" in detail.lower() for detail in details)


def test_doctor_offline_prioritizes_local_config_before_gateway_restart(monkeypatch) -> None:
    from opensquilla.health.model import HealthFinding

    monkeypatch.setattr(
        _doctor_cmd,
        "_local_config_findings",
        lambda: [
            HealthFinding(
                id="config.local.needs_onboarding",
                severity="error",
                readiness_impact="blocks_ready",
                surface="config",
                title="Local configuration needs onboarding",
                detail="Local config has incomplete sections.",
            )
        ],
    )

    report = _doctor_cmd._offline_report(
        ConnectionError("connection refused"),
        gateway_url="ws://127.0.0.1:19999/ws",
    )

    assert report["status"] == "unavailable"
    assert report["ready"] is False
    assert [finding["id"] for finding in report["findings"]] == [
        "config.local.needs_onboarding",
        "gateway.unavailable",
    ]
    assert report["impactCounts"]["blocks_ready"] == 2


def test_doctor_offline_does_not_mix_local_config_into_remote_gateway(monkeypatch) -> None:
    from opensquilla.health.model import HealthFinding

    monkeypatch.setattr(
        _doctor_cmd,
        "_local_config_findings",
        lambda: [
            HealthFinding(
                id="config.local.needs_onboarding",
                severity="error",
                surface="config",
                title="Local configuration needs onboarding",
                detail="Local config has incomplete sections.",
            )
        ],
    )

    report = _doctor_cmd._offline_report(
        ConnectionError("connection refused"),
        gateway_url="wss://squilla.example.com/ws",
    )

    assert [finding["id"] for finding in report["findings"]] == ["gateway.unavailable"]


def test_local_config_findings_explain_missing_llm_env(monkeypatch) -> None:
    from opensquilla.gateway.config import GatewayConfig

    monkeypatch.delenv("CUSTOM_LLM_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
            "api_key": "",
            "api_key_env": "CUSTOM_LLM_KEY",
        }
    )

    findings = _doctor_cmd._local_onboarding_findings(cfg)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.id == "config.local.needs_onboarding"
    assert finding.severity == "error"
    assert finding.readiness_impact == "blocks_ready"
    assert finding.evidence["sections"]["llm"] == "degraded"
    assert "CUSTOM_LLM_KEY" in finding.detail
    assert any(
        step.detail and "CUSTOM_LLM_KEY" in step.detail
        for step in finding.fix_steps
    )
    assert [step.command for step in finding.fix_steps if step.command][-2:] == [
        "opensquilla onboard status --json",
        "opensquilla onboard --if-needed",
    ]


def test_local_config_findings_explain_optional_env_references(monkeypatch) -> None:
    from opensquilla.gateway.config import GatewayConfig

    monkeypatch.delenv("CUSTOM_SEARCH_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_IMAGE_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
            "api_key": "test-key",
        },
        search_provider="brave",
        search_api_key="",
        search_api_key_env="CUSTOM_SEARCH_KEY",
        image_generation={
            "enabled": True,
            "primary": "openai/dall-e-3",
            "providers": {
                "openai": {
                    "api_key": "",
                    "api_key_env": "CUSTOM_IMAGE_KEY",
                }
            },
        },
    )

    findings = _doctor_cmd._local_onboarding_findings(cfg)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "warn"
    assert finding.readiness_impact == "degrades"
    assert finding.evidence["sections"]["search"] == "degraded"
    assert finding.evidence["sections"]["image_generation"] == "degraded"
    assert "CUSTOM_SEARCH_KEY" in finding.detail
    assert "CUSTOM_IMAGE_KEY" in finding.detail
    detail_steps = " ".join(step.detail or "" for step in finding.fix_steps)
    assert "CUSTOM_SEARCH_KEY" in detail_steps
    assert "CUSTOM_IMAGE_KEY" in detail_steps


def test_refresh_report_readiness_labels_operator_action_items() -> None:
    # The CLI recomputes the summary locally after rewriting fix commands; a
    # warn-severity optional finding must keep the "awaiting operator action"
    # label the gateway report uses, not decay to "optional setup item".
    from opensquilla.cli.doctor_cmd import _refresh_report_readiness

    report: dict[str, Any] = {
        "findings": [
            {
                "id": "channel.telegram-main.pending_pairings",
                "severity": "warn",
                "readinessImpact": "optional",
                "surface": "channels",
                "title": "Channel telegram-main has pairing requests awaiting approval",
                "detail": "1 sender is waiting on operator approval.",
            },
            {
                "id": "logs.gateway_file_log.disabled",
                "severity": "info",
                "readinessImpact": "optional",
                "surface": "logs",
                "title": "Gateway file logging is disabled",
                "detail": "Persistent gateway file logging is optional.",
            },
        ]
    }

    _refresh_report_readiness(report)

    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["summary"] == "Ready, 1 item awaiting operator action, 1 optional setup item"


def test_doctor_appends_local_tui_finding_when_opentui_unavailable(monkeypatch) -> None:
    from opensquilla.cli.tui.renderers.selection import (
        ChatUiFallback,
        ChatUiSelection,
        RendererBackendUnavailableReason,
    )

    class _PlainBackend:
        backend_id = "native"

    monkeypatch.setattr(_doctor_cmd, "_local_tui_findings", _REAL_LOCAL_TUI_FINDINGS)
    monkeypatch.setattr(
        "opensquilla.cli.tui.renderers.selection.select_chat_ui_backend",
        lambda mode=None: ChatUiSelection(
            requested_mode="auto",
            backend=_PlainBackend(),
            fallback=ChatUiFallback(
                code=RendererBackendUnavailableReason.MISSING,
                detail="OpenTUI companion is not installed.",
            ),
        ),
    )
    monkeypatch.setattr(
        "opensquilla.cli.tui.source_checkout.resolve_tui_source_checkout_hint",
        lambda: None,
    )
    _ReadyGatewayClient.calls = []
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _ReadyGatewayClient)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    tui = [f for f in report["findings"] if f["id"] == "tui.opentui_unavailable"]
    assert len(tui) == 1
    assert tui[0]["severity"] == "info"
    assert tui[0]["readinessImpact"] == "optional"
    assert tui[0]["evidence"]["reasonCode"] == "missing"
    assert tui[0]["evidence"]["selectedBackend"] == "native"
    # An optional UI note never affects readiness.
    assert report["ready"] is True

    # The human rendering surfaces the same finding.
    rendered = runner.invoke(app, ["doctor"])
    assert "Full-screen terminal UI not active" in rendered.stdout


def test_doctor_reports_nothing_when_opentui_is_active(monkeypatch) -> None:
    from opensquilla.cli.tui.renderers.selection import ChatUiSelection

    class _OpenTuiBackend:
        backend_id = "opentui"

    monkeypatch.setattr(_doctor_cmd, "_local_tui_findings", _REAL_LOCAL_TUI_FINDINGS)
    monkeypatch.setattr(
        "opensquilla.cli.tui.renderers.selection.select_chat_ui_backend",
        lambda mode=None: ChatUiSelection(
            requested_mode="auto",
            backend=_OpenTuiBackend(),
            fallback=None,
        ),
    )
    _ReadyGatewayClient.calls = []
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _ReadyGatewayClient)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "No action needed" in result.stdout


def test_local_tui_findings_reports_probe_crash_as_warn(monkeypatch) -> None:
    def _boom(mode=None):
        raise RuntimeError("selection exploded")

    monkeypatch.setattr("opensquilla.cli.tui.renderers.selection.select_chat_ui_backend", _boom)

    findings = _REAL_LOCAL_TUI_FINDINGS()

    assert [f.id for f in findings] == ["tui.selection_failed"]
    assert findings[0].severity == "warn"
    assert findings[0].readiness_impact == "optional"
    assert "selection exploded" in findings[0].detail


def test_local_tui_findings_offers_source_checkout_fix_steps(monkeypatch) -> None:
    from opensquilla.cli.tui.renderers.selection import (
        ChatUiFallback,
        ChatUiSelection,
        RendererBackendUnavailableReason,
    )
    from opensquilla.cli.tui.source_checkout import TuiSourceCheckoutHint

    class _PlainBackend:
        backend_id = "native"

    monkeypatch.setattr(
        "opensquilla.cli.tui.renderers.selection.select_chat_ui_backend",
        lambda mode=None: ChatUiSelection(
            requested_mode="auto",
            backend=_PlainBackend(),
            fallback=ChatUiFallback(
                code=RendererBackendUnavailableReason.MISSING,
                detail="dependency @opentui/core is not installed\x1b[0m",
            ),
        ),
    )
    monkeypatch.setattr(
        "opensquilla.cli.tui.source_checkout.resolve_tui_source_checkout_hint",
        lambda: TuiSourceCheckoutHint(
            install_command="bun install --cwd /repo/package",
            launch_command="OPENSQUILLA_TUI_DEV_SOURCE_HOST=1 opensquilla chat --ui tui",
        ),
    )

    findings = _REAL_LOCAL_TUI_FINDINGS()

    assert [f.id for f in findings] == ["tui.opentui_unavailable"]
    commands = [step.command for step in findings[0].fix_steps]
    assert commands == [
        "bun install --cwd /repo/package",
        "OPENSQUILLA_TUI_DEV_SOURCE_HOST=1 opensquilla chat --ui tui",
    ]
    # Host-derived detail is sanitized before it reaches a report.
    assert "\x1b" not in findings[0].detail


def test_doctor_merge_keeps_counts_and_summary_consistent(monkeypatch) -> None:
    from opensquilla.cli.tui.renderers.selection import (
        ChatUiFallback,
        ChatUiSelection,
        RendererBackendUnavailableReason,
    )

    class _PlainBackend:
        backend_id = "native"

    monkeypatch.setattr(_doctor_cmd, "_local_tui_findings", _REAL_LOCAL_TUI_FINDINGS)
    monkeypatch.setattr(
        "opensquilla.cli.tui.renderers.selection.select_chat_ui_backend",
        lambda mode=None: ChatUiSelection(
            requested_mode="auto",
            backend=_PlainBackend(),
            fallback=ChatUiFallback(
                code=RendererBackendUnavailableReason.MISSING,
                detail="OpenTUI companion is not installed.",
            ),
        ),
    )
    monkeypatch.setattr(
        "opensquilla.cli.tui.source_checkout.resolve_tui_source_checkout_hint",
        lambda: None,
    )
    _ReadyGatewayClient.calls = []
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _ReadyGatewayClient)

    result = runner.invoke(app, ["doctor", "--json"])

    report = json.loads(result.stdout)
    # Counts, impact counts, and the summary all reflect the merged findings.
    assert report["counts"]["info"] == 1
    assert report["impactCounts"]["optional"] == 1
    assert report["summary"] == "Ready, 1 optional setup item"
    assert report["ready"] is True
    severities = [f["severity"] for f in report["findings"]]
    assert severities.count("info") == 1


def test_doctor_merge_preserves_offline_unavailable_sentinel(monkeypatch) -> None:
    from opensquilla.cli.tui.renderers.selection import (
        ChatUiFallback,
        ChatUiSelection,
        RendererBackendUnavailableReason,
    )

    class _PlainBackend:
        backend_id = "native"

    monkeypatch.setattr(_doctor_cmd, "_local_tui_findings", _REAL_LOCAL_TUI_FINDINGS)
    monkeypatch.setattr(
        "opensquilla.cli.tui.renderers.selection.select_chat_ui_backend",
        lambda mode=None: ChatUiSelection(
            requested_mode="auto",
            backend=_PlainBackend(),
            fallback=ChatUiFallback(
                code=RendererBackendUnavailableReason.MISSING,
                detail="OpenTUI companion is not installed.",
            ),
        ),
    )
    monkeypatch.setattr(
        "opensquilla.cli.tui.source_checkout.resolve_tui_source_checkout_hint",
        lambda: None,
    )
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _OfflineGatewayClient)

    result = runner.invoke(app, ["doctor", "--json"])

    report = json.loads(result.stdout)
    assert report["status"] == "unavailable"
    assert report["ready"] is False
    assert any(f["id"] == "tui.opentui_unavailable" for f in report["findings"])
