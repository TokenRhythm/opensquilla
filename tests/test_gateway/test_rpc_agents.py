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
async def test_models_rpc_non_tokenrhythm_enrichment_ignores_warm_shared_catalog() -> None:
    from opensquilla.provider.model_catalog import ModelCatalog, set_shared_catalog
    from opensquilla.provider.selector import ModelListResult
    from opensquilla.provider.types import ModelInfo

    model_id = "synthetic-non-tokenrhythm-cold-boundary"
    shared = ModelCatalog()
    shared.set_user_overrides(
        {
            f"synthetic-provider/{model_id}": {
                "supports_reasoning": True,
                "reasoning_format": "deepseek",
            }
        }
    )

    class _Selector:
        async def list_models_detailed(self):
            return ModelListResult(
                models=[
                    ModelInfo(
                        provider="synthetic-provider",
                        model_id=model_id,
                    ).model_dump()
                ]
            )

    set_shared_catalog(shared)
    try:
        result = await get_dispatcher().dispatch(
            "r1",
            "models.list",
            {},
            RpcContext(conn_id="test", provider_selector=_Selector()),
        )
    finally:
        set_shared_catalog(None)

    assert result.error is None, result.error
    assert result.payload["models"][0]["source"] == "synthesized"
    assert result.payload["models"][0]["reasoningFormat"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["agents.create", "agents.update", "agents.delete"])
async def test_retired_agent_admin_rpc_cannot_mutate_existing_profiles(method: str) -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)
    await registry.create_agent(agent_id="ops", model="openai/test")
    before = cfg.model_dump()

    assert method not in get_dispatcher().methods()

    result = await get_dispatcher().dispatch(
        "r1", method, {"id": "ops", "name": "Changed"}, _ctx(cfg, registry)
    )

    assert result.error is not None
    assert result.error.code == "METHOD_NOT_FOUND"
    assert cfg.model_dump() == before


@pytest.mark.asyncio
async def test_models_configured_scope_adds_ready_profile_defaults_without_discovery(monkeypatch):
    from opensquilla.gateway.config import LlmProviderProfile
    from opensquilla.provider.selector import ProviderConfig

    monkeypatch.setattr("opensquilla.provider.deployment.environment_value", lambda _name: "")
    cfg = GatewayConfig()
    cfg.llm_profiles = {
        "openai": LlmProviderProfile(model="same-model", api_key="synthetic-openai"),
        "anthropic": LlmProviderProfile(model="same-model", api_key="synthetic-anthropic"),
        "deepseek": LlmProviderProfile(model="unavailable"),
        "openai:work": LlmProviderProfile(model="named-only", api_key="synthetic-named"),
        "not-a-provider": LlmProviderProfile(model="unknown", api_key="synthetic-unknown"),
    }
    selector = _DetailedModelSelector()
    selector.current_config = ProviderConfig(provider="ollama", model="test-model-good")
    ctx = RpcContext(conn_id="test", config=cfg, provider_selector=selector)
    active = await get_dispatcher().dispatch("active", "models.list", {}, ctx)
    assert [(m["provider"], m["id"]) for m in active.payload["models"]] == [
        ("ollama", "test-model-good"),
    ]
    configured = await get_dispatcher().dispatch(
        "configured", "models.list", {"scope": "configured"}, ctx,
    )
    assert configured.error is None
    # Keep the exact response shape accepted by previously shipped clients.
    assert set(configured.payload) == {"models", "errors"}
    assert [(m["provider"], m["id"]) for m in configured.payload["models"]] == [
        ("ollama", "test-model-good"), ("openai", "same-model"),
        ("anthropic", "same-model"),
    ]
    assert configured.payload["models"][1]["metadata"] == {
        "catalogScope": "configured_default",
    }
    assert [e["provider"] for e in configured.payload["errors"]] == ["deepseek"]
    assert "synthetic" not in str(configured.payload)


@pytest.mark.asyncio
async def test_models_cache_only_is_network_free_and_exposes_cache_miss(monkeypatch):
    from opensquilla.provider.selector import ProviderConfig

    async def unexpected(**_kwargs):
        pytest.fail("cache-only must not query an upstream provider")

    monkeypatch.setattr("opensquilla.onboarding.probe.discover_provider_models", unexpected)
    selector = _DetailedModelSelector()
    selector.current_config = ProviderConfig(
        provider="custom", model="example-model", base_url="http://127.0.0.1:9999/v1",
    )
    cfg = GatewayConfig()
    cfg.llm.provider = "custom"
    cfg.llm.model = "example-model"
    cfg.llm.base_url = "http://127.0.0.1:9999/v1"
    result = await get_dispatcher().dispatch(
        "cache", "models.list", {"scope": "configured", "cacheOnly": True},
        RpcContext(conn_id="test", config=cfg, provider_selector=selector),
    )
    assert result.error is None
    assert result.payload["catalog"]["cacheHit"] is False
    assert result.payload["catalog"]["stale"] is True


@pytest.mark.asyncio
async def test_models_configured_scope_deduplicates_and_preserves_active_deployment(monkeypatch):
    from opensquilla.gateway.config import LlmProviderProfile
    from opensquilla.provider.selector import ProviderConfig

    monkeypatch.setattr("opensquilla.provider.deployment.environment_value", lambda _name: "")
    cfg = GatewayConfig()
    cfg.llm_profiles = {
        "ollama": LlmProviderProfile(model="stale-inactive-model"),
        "OPENAI": LlmProviderProfile(model="configured", api_key="synthetic-openai"),
        "openai": LlmProviderProfile(model="configured", api_key="synthetic-openai"),
    }
    selector = _DetailedModelSelector()
    selector.current_config = ProviderConfig(provider="ollama", model="test-model-good")
    result = await get_dispatcher().dispatch(
        "r", "models.list", {"scope": "configured", "provider": "openai"},
        RpcContext(conn_id="test", config=cfg, provider_selector=selector),
    )
    assert result.error is None
    assert [(m["provider"], m["id"]) for m in result.payload["models"]] == [
        ("openai", "configured"),
    ]
    assert result.payload["errors"] == []


@pytest.mark.asyncio
async def test_models_configured_scope_can_use_ready_profiles_without_primary(monkeypatch):
    from opensquilla.gateway.config import LlmProviderProfile

    monkeypatch.setattr("opensquilla.provider.deployment.environment_value", lambda _name: "")
    cfg = GatewayConfig()
    cfg.llm_profiles = {"openai": LlmProviderProfile(model="configured", api_key="synthetic-key")}
    result = await get_dispatcher().dispatch(
        "r", "models.list", {"scope": "configured"}, RpcContext(conn_id="test", config=cfg),
    )
    assert result.error is None
    assert [(m["provider"], m["id"]) for m in result.payload["models"]] == [
        ("openai", "configured"),
    ]


@pytest.mark.asyncio
async def test_models_catalog_rejects_unknown_scope():
    result = await get_dispatcher().dispatch(
        "r", "models.list", {"scope": "everything"}, RpcContext(conn_id="test"),
    )
    assert result.error is not None


@pytest.mark.asyncio
async def test_models_configured_scope_uses_complete_saved_provider_discovery(monkeypatch):
    from opensquilla.gateway.config import LlmProviderProfile
    from opensquilla.onboarding.probe import ProviderModelsDiscoverResult
    from opensquilla.provider.selector import ProviderConfig

    monkeypatch.setattr("opensquilla.provider.deployment.environment_value", lambda _name: "")
    cfg = GatewayConfig()
    cfg.llm_profiles = {
        "tokenrhythm": LlmProviderProfile(model="glm-5.1", api_key="synthetic-tokenrhythm"),
        "openrouter": LlmProviderProfile(model="glm-5.1", api_key="synthetic-openrouter"),
        "deepseek": LlmProviderProfile(model="private-model", api_key="synthetic-deepseek"),
    }
    calls = []

    async def discover(**kwargs):
        calls.append(kwargs)
        provider = kwargs["provider_id"]
        if provider in {"ollama", "deepseek"}:
            return ProviderModelsDiscoverResult(ok=True, provider_id=provider)
        ids = ["glm-5.1", "minimax-m2.7"] if provider == "tokenrhythm" else ["glm-5.1"]
        return ProviderModelsDiscoverResult(
            ok=True, provider_id=provider, source="live", models=[{
                "id": model, "name": model, "contextWindow": 200000,
                "maxOutputTokens": 8000, "capabilities": ["chat", "tools"],
            } for model in ids],
        )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.discover_selectable_provider_models", discover,
    )
    selector = _DetailedModelSelector()
    selector.current_config = ProviderConfig(provider="ollama", model="test-model-good")
    result = await get_dispatcher().dispatch(
        "r", "models.list", {"scope": "configured"},
        RpcContext(conn_id="test", config=cfg, provider_selector=selector),
    )
    assert result.error is None
    assert {(m["provider"], m["id"]) for m in result.payload["models"]} == {
        ("ollama", "test-model-good"), ("tokenrhythm", "glm-5.1"),
        ("tokenrhythm", "minimax-m2.7"), ("openrouter", "glm-5.1"),
        ("deepseek", "private-model"),
    }
    assert result.payload["errors"] == []
    assert all(call["persist_catalog"] and call["catalog_config"] is cfg for call in calls)
    assert all(call["allow_default_api_key_env"] is False for call in calls)
    assert "synthetic" not in str(result.payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "auth_invalid", "transport_transient", "cold_transient", "empty",
])
async def test_models_configured_scope_never_restores_unauthorized_static_rows(
    monkeypatch, failure,
):
    from opensquilla.gateway.config import LlmProviderProfile
    from opensquilla.onboarding.probe import ProviderModelsDiscoverResult
    from opensquilla.provider.selector import ProviderConfig

    monkeypatch.setattr("opensquilla.provider.deployment.environment_value", lambda _name: "")
    cfg = GatewayConfig()
    cfg.llm_profiles = {
        "tokenrhythm": LlmProviderProfile(model="minimax-m2.7", api_key="synthetic-tokenrhythm"),
        "openai": LlmProviderProfile(model="private-model", api_key="synthetic-openai"),
    }

    async def discover(**kwargs):
        provider = kwargs["provider_id"]
        if provider != "tokenrhythm":
            return ProviderModelsDiscoverResult(ok=True, provider_id=provider)
        rows = [{"id": "glm-5.1", "name": "GLM", "contextWindow": 200000,
                 "maxOutputTokens": 8000, "capabilities": ["chat"]}]
        return ProviderModelsDiscoverResult(
            ok=failure == "empty", provider_id=provider,
            failure_kind=(
                "" if failure == "empty"
                else "transport_transient" if failure == "cold_transient" else failure
            ),
            source="live" if failure == "transport_transient" else "none",
            models=rows if failure == "transport_transient" else [],
        )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.discover_selectable_provider_models", discover,
    )
    selector = _DetailedModelSelector()
    selector.current_config = ProviderConfig(provider="ollama", model="test-model-good")
    result = await get_dispatcher().dispatch(
        "r", "models.list", {"scope": "configured"},
        RpcContext(conn_id="test", config=cfg, provider_selector=selector),
    )
    assert result.error is None
    identities = {(m["provider"], m["id"]) for m in result.payload["models"]}
    assert (("tokenrhythm", "minimax-m2.7") in identities) == (failure == "cold_transient")
    assert (("tokenrhythm", "glm-5.1") in identities) == (failure == "transport_transient")
    assert ("openai", "private-model") in identities


@pytest.mark.asyncio
async def test_models_configured_scope_does_not_call_parallel_runtime_listing(monkeypatch):
    from opensquilla.provider.selector import ProviderConfig

    class SavedSelector:
        current_config = ProviderConfig(provider="openai", model="private-id", api_key="synthetic")

        async def list_models_detailed(self):
            raise AssertionError("configured picker must share settings discovery only")

    result = await get_dispatcher().dispatch(
        "r", "models.list", {"scope": "configured"},
        RpcContext(conn_id="test", config=GatewayConfig(), provider_selector=SavedSelector()),
    )
    assert result.error is None
    assert [(m["provider"], m["id"]) for m in result.payload["models"]] == [
        ("openai", "private-id"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("listed", [True, False])
async def test_models_configured_scope_discovers_connection_without_default_model(
    monkeypatch, listed,
):
    from opensquilla.onboarding.probe import ProviderModelsDiscoverResult

    cfg = GatewayConfig(llm_profiles={
        "custom": {"base_url": "http://127.0.0.1:11434/v1", "api_key": "synthetic-key"},
    })
    calls = []

    async def discover(**kwargs):
        calls.append(kwargs["provider_id"])
        return ProviderModelsDiscoverResult(
            ok=True, provider_id="custom", source="live" if listed else "none",
            models=[{
                "id": "server-model", "name": "Server Model", "contextWindow": 32000,
                "maxOutputTokens": 4096, "capabilities": ["chat"],
            }] if listed else [],
        )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.discover_selectable_provider_models", discover,
    )
    result = await get_dispatcher().dispatch(
        "r", "models.list", {"scope": "configured"}, RpcContext(conn_id="test", config=cfg),
    )
    assert result.error is None
    assert calls == ["custom"]
    assert [m["id"] for m in result.payload["models"]] == (["server-model"] if listed else [])
    assert result.payload["errors"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_models_configured_scope_observes_parked_pool_credentials_without_acquiring(
    monkeypatch, exhausted,
):
    from opensquilla.gateway.llm_runtime import ProfileCredentialPools
    from opensquilla.onboarding.probe import ProviderModelsDiscoverResult
    from opensquilla.provider.failures import ProviderFailureKind

    names = ["CATALOG_POOL_FIRST", "CATALOG_POOL_SECOND"]
    monkeypatch.setenv(names[0], "synthetic-rejected")
    monkeypatch.setenv(names[1], "synthetic-healthy")
    pools = ProfileCredentialPools()
    first = pools.acquire_for_session("openrouter", names, "previous-turn")
    assert first is not None and first.env_name == names[0]
    pools.report_failure("openrouter", "previous-turn", ProviderFailureKind.AUTH_INVALID)
    if exhausted:
        second = pools.acquire_for_session("openrouter", names, "previous-turn")
        assert second is not None and second.env_name == names[1]
        pools.report_failure("openrouter", "previous-turn", ProviderFailureKind.AUTH_INVALID)

    def unexpected_acquisition(*_args, **_kwargs):
        raise AssertionError("Opening a model menu must not acquire or pin a credential")

    monkeypatch.setattr(pools, "acquire_for_session", unexpected_acquisition)
    monkeypatch.setattr("opensquilla.gateway.llm_runtime.profile_credential_pools", lambda: pools)
    cfg = GatewayConfig(llm_profiles={
        "openrouter": {"model": "configured-model", "api_key_env_pool": names},
    })
    calls = []

    async def discover(**kwargs):
        calls.append(kwargs["api_key"])
        return ProviderModelsDiscoverResult(
            ok=True, provider_id="openrouter", source="live", models=[{
                "id": "healthy-model", "name": "Healthy Model", "contextWindow": 32000,
                "maxOutputTokens": 4096, "capabilities": ["chat"],
            }],
        )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.discover_selectable_provider_models", discover,
    )
    result = await get_dispatcher().dispatch(
        "r", "models.list", {"scope": "configured"}, RpcContext(conn_id="test", config=cfg),
    )
    assert result.error is None
    if exhausted:
        assert calls == []
        assert result.payload["models"] == []
        assert result.payload["errors"] == [{
            "provider": "openrouter", "kind": "deployment_unavailable",
            "detail": "credential_pool_exhausted",
        }]
    else:
        assert calls == ["synthetic-healthy"]
        assert [m["id"] for m in result.payload["models"]] == ["healthy-model"]
        assert result.payload["errors"] == []
