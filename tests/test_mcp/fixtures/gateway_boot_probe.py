"""Run one isolated Gateway MCP boot/dispatch/shutdown probe."""

from __future__ import annotations

import asyncio
import importlib.abc
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


async def run_probe(mode: str, state_root: Path) -> None:
    attempted_sdk_imports: list[str] = []

    class RejectSDKImport(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "mcp" or fullname.startswith("mcp."):
                attempted_sdk_imports.append(fullname)
                raise ImportError("MCP SDK must stay lazy when no server is configured")
            return None

    if mode != "configured":
        sys.meta_path.insert(0, RejectSDKImport())

    from starlette.applications import Starlette

    from opensquilla.gateway.boot import GatewayServer, build_services
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.mcp.discovery import active_clients_snapshot
    from opensquilla.tool_boundary import ToolCall
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import ToolRegistry
    from opensquilla.tools.types import CallerKind, ToolContext

    server_entry = {
        "name": "gateway-probe",
        "description": "Synthetic SDK server",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(Path(__file__).with_name("fastmcp_server.py"))],
        "tool_timeout_seconds": 5.0,
    }
    config_values = {
        "state_dir": str(state_root / "state"),
        "workspace_dir": str(state_root / "workspace"),
        "config_path": str(state_root / "config.toml"),
        "control_ui": {"enabled": False},
        "channels": {"channels": []},
    }
    if mode != "default":
        config_values["mcp"] = {
            "enabled": mode != "disabled",
            "servers": [] if mode == "empty" else [server_entry],
            "connect_timeout_seconds": 10.0,
        }
    config = GatewayConfig(**config_values)
    registry = ToolRegistry()
    processes: list[asyncio.subprocess.Process] = []
    original_spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    # Sandbox setup and memory indexing are separate integrations. Keep the
    # Gateway service assembly, MCP discovery, registry and dispatch real.
    with (
        patch("opensquilla.env.load_env"),
        patch(
            "opensquilla.sandbox.integration.configure_runtime",
            return_value=SimpleNamespace(effective=SimpleNamespace(as_dict=lambda: {})),
        ),
        patch("opensquilla.memory.manager.build_memory_managers", AsyncMock(return_value={})),
        patch("asyncio.create_subprocess_exec", side_effect=observe_spawn),
    ):
        services = await build_services(
            config=config,
            tool_registry=registry,
            seed_agent_workspaces=False,
            defer_sandbox_startup=True,
        )
        gateway = GatewayServer(app=Starlette(), config=config, _services=services)
        try:
            if mode == "configured":
                name = "mcp__gateway-probe__ping"
                assert registry.get(name) is not None
                assert registry.mcp_namespaces() == {"mcp__gateway-probe": "Synthetic SDK server"}
                assert len(active_clients_snapshot()) == 1
                assert len(processes) == 1
                assert processes[0].returncode is None

                owner = ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)
                definition = next(
                    tool for tool in registry.to_tool_definitions(owner) if tool.name == name
                )
                assert definition.input_schema.properties["text"]["type"] == "string"
                assert definition.input_schema.required == ["text"]
                restricted = ToolContext(
                    is_owner=True, caller_kind=CallerKind.AGENT, denied_tools={name}
                )
                assert name not in {tool.name for tool in registry.to_tool_definitions(restricted)}

                call = ToolCall(
                    tool_use_id="gateway-probe-call",
                    tool_name=name,
                    arguments={"text": "gateway-sdk-round-trip"},
                )
                denied = await build_tool_handler(registry, restricted)(call)
                assert denied.is_error is True
                result = await build_tool_handler(registry, owner)(call)
                assert result.is_error is False
                assert result.content == "gateway-sdk-round-trip"
            else:
                assert active_clients_snapshot() == ()
                assert processes == []
                assert registry.mcp_namespaces() == {}
        finally:
            await gateway.close()

    assert active_clients_snapshot() == ()
    assert registry.mcp_namespaces() == {}
    assert registry.get("mcp__gateway-probe__ping") is None
    assert all(process.returncode is not None for process in processes)
    if mode != "configured":
        assert attempted_sdk_imports == []
        assert not any(name == "mcp" or name.startswith("mcp.") for name in sys.modules)
    print(json.dumps({"mode": mode, "child_processes": len(processes), "ok": True}))


if __name__ == "__main__":
    asyncio.run(run_probe(sys.argv[1], Path(sys.argv[2])))
