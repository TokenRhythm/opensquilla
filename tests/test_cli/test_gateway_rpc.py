from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from opensquilla.cli import gateway_lifecycle
from opensquilla.cli.gateway_rpc import (
    default_gateway_token,
    default_gateway_url,
    run_gateway_call,
)


def test_default_gateway_url_uses_implicit_home_config(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_HOST", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_PORT", raising=False)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)

    config = tmp_path / "state" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
host = "127.0.0.1"
port = 18790
""",
        encoding="utf-8",
    )

    assert default_gateway_url() == "ws://127.0.0.1:18790/ws"


def _write_managed_gateway_record(
    home,
    *,
    port: int = 18792,
    config_path: Path | None = None,
) -> None:
    target = home / "state" / "gateway" / "gateway.json"
    target.parent.mkdir(parents=True)
    record: dict[str, Any] = {
        "pid": 4242,
        "host": "127.0.0.1",
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "healthUrl": f"http://127.0.0.1:{port}/health",
        "startedAt": "2026-08-10T00:00:00Z",
        "argv": ["opensquilla", "gateway", "run", "--port", str(port)],
    }
    if config_path is not None:
        record["configPath"] = str(config_path)
    target.write_text(
        json.dumps(record),
        encoding="utf-8",
    )


def test_default_gateway_url_prefers_active_profile_managed_target(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    home = tmp_path / "profile"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    config = home / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('host = "127.0.0.1"\nport = 18791\n', encoding="utf-8")
    _write_managed_gateway_record(home)
    monkeypatch.setattr(
        gateway_lifecycle.GatewayLifecycleManager,
        "_pid_running",
        lambda self, pid: True,
    )
    monkeypatch.setattr(
        gateway_lifecycle.GatewayLifecycleManager,
        "_probe_health",
        lambda self: True,
    )

    assert default_gateway_url() == "ws://127.0.0.1:18792/ws"


def test_default_gateway_url_ignores_stale_profile_managed_target(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    home = tmp_path / "profile"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    config = home / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('host = "127.0.0.1"\nport = 18791\n', encoding="utf-8")
    _write_managed_gateway_record(home)
    monkeypatch.setattr(
        gateway_lifecycle.GatewayLifecycleManager,
        "_pid_running",
        lambda self, pid: False,
    )

    assert default_gateway_url() == "ws://127.0.0.1:18791/ws"


def test_default_gateway_url_explicit_url_wins_over_profile_managed_target(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_URL", "wss://squilla.example.com/ws")
    monkeypatch.setattr(
        gateway_lifecycle,
        "active_managed_gateway_target",
        lambda: (_ for _ in ()).throw(AssertionError("managed target must not be read")),
    )

    assert default_gateway_url() == "wss://squilla.example.com/ws"


def test_default_gateway_url_explicit_config_wins_over_profile_managed_target(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    config = tmp_path / "explicit.toml"
    config.write_text(
        'host = "127.0.0.1"\nport = 18800\n[auth]\nmode = "token"\ntoken = "explicit-token"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config))
    monkeypatch.setattr(
        gateway_lifecycle,
        "active_managed_gateway_target",
        lambda: (_ for _ in ()).throw(AssertionError("managed target must not be read")),
    )

    assert default_gateway_url() == "ws://127.0.0.1:18800/ws"
    assert default_gateway_token() == "explicit-token"


def test_default_gateway_url_keeps_unhealthy_managed_target_authoritative(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    home = tmp_path / "profile"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    config = home / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('host = "127.0.0.1"\nport = 18791\n', encoding="utf-8")
    _write_managed_gateway_record(home)
    monkeypatch.setattr(
        gateway_lifecycle.GatewayLifecycleManager,
        "_pid_running",
        lambda self, pid: True,
    )
    monkeypatch.setattr(
        gateway_lifecycle.GatewayLifecycleManager,
        "_probe_health",
        lambda self: False,
    )

    assert default_gateway_url() == "ws://127.0.0.1:18792/ws"


def test_gateway_call_pairs_managed_target_with_recorded_config_token(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_TOKEN", raising=False)
    home = tmp_path / "profile"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    custom_config = tmp_path / "custom.toml"
    custom_config.write_text(
        'host = "127.0.0.1"\nport = 18791\n[auth]\nmode = "token"\ntoken = "managed-token"\n',
        encoding="utf-8",
    )
    _write_managed_gateway_record(home, config_path=custom_config)
    monkeypatch.setattr(
        gateway_lifecycle.GatewayLifecycleManager,
        "_pid_running",
        lambda self, pid: True,
    )
    monkeypatch.setattr(
        gateway_lifecycle.GatewayLifecycleManager,
        "_probe_health",
        lambda self: True,
    )

    class RecordingGatewayClient:
        connection: tuple[str, str | None] | None = None

        async def connect(self, url: str, *, token: str | None = None) -> None:
            type(self).connection = (url, token)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "opensquilla.cli.gateway_client.GatewayClient",
        RecordingGatewayClient,
    )

    async def action(client: Any) -> str:
        return "ok"

    assert asyncio.run(run_gateway_call(action)) == "ok"
    assert RecordingGatewayClient.connection == (
        "ws://127.0.0.1:18792/ws",
        "managed-token",
    )


def test_default_gateway_token_uses_explicit_config_path(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    config = tmp_path / "custom-opensquilla.toml"
    config.write_text(
        """
[auth]
mode = "token"
token = "from-explicit-config"
""",
        encoding="utf-8",
    )

    assert default_gateway_token(config) == "from-explicit-config"


def test_default_gateway_token_env_override_wins_over_explicit_config(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_TOKEN", "from-env")
    config = tmp_path / "custom-opensquilla.toml"
    config.write_text(
        """
[auth]
mode = "token"
token = "from-explicit-config"
""",
        encoding="utf-8",
    )

    assert default_gateway_token(config) == "from-env"
