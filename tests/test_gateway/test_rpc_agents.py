from __future__ import annotations

import pytest

from opensquilla.agents.registry import AgentRegistry
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher


class _FailingModelSelector:
    async def list_models_detailed(self):
        raise RuntimeError("provider unavailable")


def _ctx(config: GatewayConfig, registry: AgentRegistry) -> RpcContext:
    return RpcContext(conn_id="test", config=config, agent_registry=registry)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "accepted_params"),
    [
        (
            {"agentId": "ops", "sessionKey": "agent:ops:main", "timeoutMs": 100},
            {"agentId": "ops", "sessionKey": "agent:ops:main", "timeoutMs": 100},
        ),
        (
            {"agent_id": "ops", "session_key": "agent:ops:main", "timeout_ms": 100},
            {"agentId": "ops", "sessionKey": "agent:ops:main", "timeoutMs": 100},
        ),
    ],
)
async def test_agent_wait_reports_runtime_bridge_unavailable_with_compat_params(
    params: dict[str, object],
    accepted_params: dict[str, object],
) -> None:
    result = await get_dispatcher().dispatch(
        "r1",
        "agent.wait",
        params,
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert result.error is not None
    assert result.error.code == "agent.unavailable"
    assert result.error.details["reason"] == "runtime_bridge_unavailable"
    assert result.error.details["acceptedParams"] == accepted_params
    assert result.error.details["supportedParams"] == [
        "agentId",
        "agent_id",
        "sessionKey",
        "session_key",
        "timeoutMs",
        "timeout_ms",
    ]
    assert "agents.list" in result.error.details["availableRpcMethods"]


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"agentId": 123}, {"sessionKey": "  "}])
async def test_agent_wait_rejects_non_string_identifiers(params: dict[str, object]) -> None:
    result = await get_dispatcher().dispatch(
        "r1",
        "agent.wait",
        params,
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert result.error is not None
    assert result.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_agents_rpc_list_uses_config_backed_registry() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)
    await registry.create_agent(agent_id="ops", model="openai/test")

    result = await get_dispatcher().dispatch("r1", "agents.list", {}, _ctx(cfg, registry))

    assert result.error is None, result.error
    assert [agent["id"] for agent in result.payload["agents"]] == ["main", "ops"]
    assert result.payload["agents"][1]["model"] == "openai/test"


@pytest.mark.asyncio
async def test_agents_rpc_list_without_registry_returns_empty() -> None:
    result = await get_dispatcher().dispatch(
        "r1",
        "agents.list",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert result.error is None, result.error
    assert result.payload == {"agents": []}


@pytest.mark.asyncio
async def test_agent_identity_get_uses_production_agent_registry(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "IDENTITY.md").write_text(
        "Name: Mira\nEmoji: 🦐\nTheme: ember\nAvatar: assets/mira.png\n",
        encoding="utf-8",
    )
    cfg = GatewayConfig(workspace_dir=str(workspace))
    registry = AgentRegistry(cfg, persist_changes=False)

    result = await get_dispatcher().dispatch(
        "r1",
        "agent.identity.get",
        {"agentId": "main"},
        _ctx(cfg, registry),
    )

    assert result.error is None, result.error
    assert result.payload == {
        "agent_id": "main",
        "name": "Mira",
        "emoji": "🦐",
        "creature": None,
        "vibe": None,
        "theme": "ember",
        "avatar": "assets/mira.png",
    }


@pytest.mark.asyncio
async def test_models_rpc_list_without_provider_selector_returns_empty() -> None:
    result = await get_dispatcher().dispatch(
        "r1",
        "models.list",
        {},
        RpcContext(conn_id="test"),
    )

    assert result.error is None, result.error
    assert result.payload == {"models": [], "errors": []}


@pytest.mark.asyncio
async def test_models_rpc_list_selector_crash_returns_empty() -> None:
    # A selector whose list_models_detailed itself raises (as opposed to
    # per-provider failures, which it reports in ``errors``) still yields an
    # empty envelope rather than an RPC error.
    result = await get_dispatcher().dispatch(
        "r1",
        "models.list",
        {},
        RpcContext(conn_id="test", provider_selector=_FailingModelSelector()),
    )

    assert result.error is None, result.error
    assert result.payload == {"models": [], "errors": []}


class _DetailedModelSelector:
    async def list_models_detailed(self):
        from opensquilla.provider.selector import ModelListResult, ProviderListError
        from opensquilla.provider.types import ModelInfo

        return ModelListResult(
            models=[
                ModelInfo(provider="ollama", model_id="test-model-good").model_dump()
            ],
            errors=[
                ProviderListError(
                    provider="openrouter",
                    model_hint="openrouter/test-model-locked",
                    kind="auth_invalid",
                    detail="401 invalid api key ***",
                )
            ],
        )


@pytest.mark.asyncio
async def test_models_rpc_list_surfaces_classified_provider_errors() -> None:
    result = await get_dispatcher().dispatch(
        "r1",
        "models.list",
        {},
        RpcContext(conn_id="test", provider_selector=_DetailedModelSelector()),
    )

    assert result.error is None, result.error
    assert [m["id"] for m in result.payload["models"]] == ["test-model-good"]
    assert result.payload["errors"] == [
        {"provider": "openrouter", "kind": "auth_invalid", "detail": "401 invalid api key ***"}
    ]


@pytest.mark.asyncio
async def test_models_rpc_list_filters_rows_but_keeps_errors() -> None:
    # Filters narrow the rows only; a provider whose listing failed must stay
    # visible even when its rows are filtered away.
    result = await get_dispatcher().dispatch(
        "r1",
        "models.list",
        {"provider": "openrouter"},
        RpcContext(conn_id="test", provider_selector=_DetailedModelSelector()),
    )

    assert result.error is None, result.error
    assert result.payload["models"] == []
    assert [e["provider"] for e in result.payload["errors"]] == ["openrouter"]


@pytest.mark.asyncio
async def test_agents_rpc_create_accepts_explicit_id() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)

    result = await get_dispatcher().dispatch(
        "r1",
        "agents.create",
        {"id": "ops", "name": "Operations", "model": "openai/test"},
        _ctx(cfg, registry),
    )

    assert result.error is None, result.error
    assert result.payload["id"] == "ops"
    assert result.payload["name"] == "Operations"
    assert cfg.agents[0].model == "openai/test"


@pytest.mark.asyncio
async def test_agents_rpc_delete_removes_config_entry() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)
    await registry.create_agent(agent_id="ops")

    result = await get_dispatcher().dispatch(
        "r1",
        "agents.delete",
        {"id": "ops"},
        _ctx(cfg, registry),
    )

    assert result.error is None, result.error
    assert result.payload is None
    assert cfg.agents == []


@pytest.mark.asyncio
async def test_agents_rpc_create_duplicate_returns_agent_exists_code() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)
    await registry.create_agent(agent_id="ops")

    result = await get_dispatcher().dispatch(
        "r1",
        "agents.create",
        {"id": "ops"},
        _ctx(cfg, registry),
    )

    assert result.error is not None
    assert result.error.code == "agent.exists"
    assert result.error.details == {"agentId": "ops"}


@pytest.mark.asyncio
async def test_agents_rpc_delete_main_returns_builtin_immutable() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)

    result = await get_dispatcher().dispatch(
        "r1",
        "agents.delete",
        {"id": "main"},
        _ctx(cfg, registry),
    )

    assert result.error is not None
    assert result.error.code == "agent.builtin_immutable"


@pytest.mark.asyncio
async def test_agents_rpc_update_main_returns_builtin_immutable() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)

    result = await get_dispatcher().dispatch(
        "r1",
        "agents.update",
        {"id": "main", "name": "renamed"},
        _ctx(cfg, registry),
    )

    assert result.error is not None
    assert result.error.code == "agent.builtin_immutable"


@pytest.mark.asyncio
async def test_agents_rpc_update_missing_returns_agent_not_found() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)

    result = await get_dispatcher().dispatch(
        "r1",
        "agents.update",
        {"id": "ghost", "model": "openai/test"},
        _ctx(cfg, registry),
    )

    assert result.error is not None
    assert result.error.code == "agent.not_found"
    assert result.error.details == {"agentId": "ghost"}


@pytest.mark.asyncio
async def test_agents_rpc_delete_missing_returns_agent_not_found() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)

    result = await get_dispatcher().dispatch(
        "r1",
        "agents.delete",
        {"id": "ghost"},
        _ctx(cfg, registry),
    )

    assert result.error is not None
    assert result.error.code == "agent.not_found"


@pytest.mark.asyncio
async def test_agents_rpc_update_workspace_field_persists() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)
    await registry.create_agent(agent_id="ops")

    result = await get_dispatcher().dispatch(
        "r1",
        "agents.update",
        {"id": "ops", "workspace": "/tmp/ops"},
        _ctx(cfg, registry),
    )

    assert result.error is None, result.error
    assert cfg.agents[0].workspace == "/tmp/ops"


@pytest.mark.asyncio
async def test_agents_rpc_update_enabled_toggle_persists() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)
    await registry.create_agent(agent_id="ops")

    result = await get_dispatcher().dispatch(
        "r1",
        "agents.update",
        {"id": "ops", "enabled": False},
        _ctx(cfg, registry),
    )

    assert result.error is None, result.error
    assert cfg.agents[0].enabled is False


@pytest.mark.asyncio
async def test_agents_rpc_update_agent_dir_camelcase_persists() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)
    await registry.create_agent(agent_id="ops")

    result = await get_dispatcher().dispatch(
        "r1",
        "agents.update",
        {"id": "ops", "agentDir": ".opensquilla/ops-dir"},
        _ctx(cfg, registry),
    )

    assert result.error is None, result.error
    assert cfg.agents[0].agent_dir == ".opensquilla/ops-dir"
