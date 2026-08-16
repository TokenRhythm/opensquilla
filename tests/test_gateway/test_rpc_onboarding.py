"""RPC tests for onboarding handlers."""

from __future__ import annotations

import platform
import tomllib

import httpx
import pytest

import opensquilla.gateway.rpc_onboarding  # noqa: F401  ensures registration
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, get_dispatcher


def _env_command(env_key: str) -> str:
    if platform.system().lower().startswith("win"):
        return f'$env:{env_key} = "<your-key>"'
    return f'export {env_key}="<your-key>"'


def _admin_ctx() -> RpcContext:
    return RpcContext(
        conn_id="t",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


def _read_ctx() -> RpcContext:
    return RpcContext(
        conn_id="t",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=False,
            authenticated=True,
        ),
    )


@pytest.mark.asyncio
async def test_onboarding_status_works_with_read_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch("r1", "onboarding.status", {}, _read_ctx())
    assert res.error is None, res.error
    assert "needsOnboarding" in res.payload
    assert "configPath" in res.payload
    assert "sections" in res.payload
    assert "sectionDetails" in res.payload
    assert "memory_embedding" in res.payload["sections"]


@pytest.mark.asyncio
async def test_onboarding_catalog_returns_providers_and_channels(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch("r1", "onboarding.catalog", {}, _read_ctx())
    assert res.error is None, res.error
    payload = res.payload
    assert "providers" in payload
    assert "channels" in payload
    assert "searchProviders" in payload
    assert "routerProfiles" in payload
    assert "imageGenerationProviders" in payload
    assert "audioProviders" in payload
    assert "memoryEmbeddingProviders" in payload
    types = {c["type"] for c in payload["channels"]}
    assert {"slack", "telegram", "matrix", "discord"} <= types
    search_provider_ids = {p["providerId"] for p in payload["searchProviders"]}
    assert {"bocha", "brave", "duckduckgo", "iqs"} <= search_provider_ids
    image_provider_ids = {p["providerId"] for p in payload["imageGenerationProviders"]}
    assert {"openai", "openrouter"} <= image_provider_ids
    audio_provider_ids = {p["providerId"] for p in payload["audioProviders"]}
    assert {"elevenlabs"} <= audio_provider_ids
    assert all("whatYouNeed" in p for p in payload["audioProviders"])
    memory_provider_ids = {p["providerId"] for p in payload["memoryEmbeddingProviders"]}
    assert {
        "auto",
        "local",
        "openai",
        "openai-compatible",
        "ollama",
        "none",
    } <= memory_provider_ids
    assert all("whatYouNeed" in p for p in payload["memoryEmbeddingProviders"])
    router_profile_ids = {p["profileId"] for p in payload["routerProfiles"]["profiles"]}
    assert {"openrouter", "deepseek", "openai"} <= router_profile_ids


@pytest.mark.asyncio
async def test_provider_configure_redacts_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "x", "apiKey": "sk-test"},
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["entry"]["api_key"] == "***"
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_provider_configure_can_atomically_enable_openrouter_image_default(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_path))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {
            "providerId": "openrouter",
            "model": "openai/gpt-test",
            "apiKey": "synthetic-openrouter-key",
            "imageGenerationIntent": "enable_provider_default",
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    change = res.payload["entry"]["capabilityChanges"]["imageGeneration"]
    assert change["applied"] is True
    persisted = tomllib.loads(config_path.read_text())
    assert persisted["image_generation"] == {
        "enabled": True,
        "binding": "follow_llm",
        "primary": "openrouter/google/gemini-3.1-flash-image-preview",
    }

    status = await get_dispatcher().dispatch(
        "r2",
        "onboarding.status",
        {},
        _read_ctx(),
    )
    assert status.error is None, status.error
    image_state = status.payload["imageGenerationState"]
    assert image_state["mode"] == "follow_llm"
    assert image_state["effective"]["providerId"] == "openrouter"


@pytest.mark.asyncio
async def test_provider_configure_image_default_intent_preserves_explicit_off(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "c.toml"
    config_path.write_text("[image_generation]\nenabled = false\n")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_path))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {
            "providerId": "openrouter",
            "model": "openai/gpt-test",
            "apiKey": "synthetic-openrouter-key",
            "imageGenerationIntent": "enable_provider_default",
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    change = res.payload["entry"]["capabilityChanges"]["imageGeneration"]
    assert change["applied"] is False
    assert change["reason"] == "operator_configuration_preserved"
    persisted = tomllib.loads(config_path.read_text())
    assert persisted["image_generation"] == {"enabled": False}


@pytest.mark.asyncio
async def test_provider_configure_can_omit_model_for_router_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "deepseek", "apiKeyEnv": "DEEPSEEK_API_KEY"},
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["entry"]["model"] == "deepseek-v4-flash"
    data = tomllib.loads((tmp_path / "c.toml").read_text())
    assert data["llm"]["model"] == "deepseek-v4-flash"
    assert data["squilla_router"]["tier_profile"] == "deepseek"


@pytest.mark.asyncio
async def test_router_configure_recommended_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {"mode": "recommended"},
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.squilla_router.enabled is True
    assert ctx.config.squilla_router.tier_profile == "deepseek"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["squilla_router"]["tier_profile"] == "deepseek"
    assert "tiers" not in persisted["squilla_router"]


@pytest.mark.asyncio
async def test_router_configure_accepts_tier_overrides_without_rebinding_direct_model(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "openai", "model": "gpt-5.4-mini"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {
            "mode": "recommended",
            "defaultTier": "c2",
            "tiers": {
                "c2": {"provider": "openai", "model": "gpt-5.5-custom"},
                "image_model": {
                    "provider": "openai",
                    "model": "gpt-5.4-mini",
                    "supportsImage": True,
                },
            },
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.llm.model == "gpt-5.4-mini"
    assert ctx.config.squilla_router.default_tier == "c2"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["llm"]["model"] == "gpt-5.4-mini"
    assert persisted["squilla_router"]["tiers"]["c2"]["model"] == "gpt-5.5-custom"
    assert persisted["squilla_router"]["tiers"]["image_model"]["supports_image"] is True


@pytest.mark.asyncio
async def test_router_configure_persists_image_model_as_image_capable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "openrouter", "model": "z-ai/glm-5.1"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {
            "mode": "openrouter-mix",
            "defaultTier": "t1",
            "tiers": {
                "image_model": {
                    "provider": "openrouter",
                    "model": "anthropic/claude-opus-4.8",
                    "supportsImage": False,
                },
            },
        },
        ctx,
    )

    assert res.error is None, res.error
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    image_tier = persisted["squilla_router"]["tiers"]["image_model"]
    assert image_tier["model"] == "anthropic/claude-opus-4.8"
    assert image_tier["supports_image"] is True
    assert image_tier["image_only"] is True
    assert ctx.config.squilla_router.tiers["image_model"]["supports_image"] is True
    assert ctx.config.squilla_router.tiers["image_model"]["image_only"] is True


@pytest.mark.asyncio
async def test_router_configure_persists_cross_provider_tiers(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "openai", "model": "gpt-5.4-mini"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {
            "mode": "custom",
            "crossProviderTiers": True,
            "tierProviderMismatch": "veto",
            "tiers": {
                "c0": {"provider": "openai", "model": "gpt-5.4-mini"},
                "c1": {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
                "c2": {"provider": "openrouter", "model": "z-ai/glm-5.2"},
                "c3": {"provider": "openai", "model": "gpt-5.5"},
            },
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.squilla_router.cross_provider_tiers is True
    assert ctx.config.squilla_router.tier_provider_mismatch == "veto"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["squilla_router"]["cross_provider_tiers"] is True
    assert persisted["squilla_router"]["tier_provider_mismatch"] == "veto"


@pytest.mark.asyncio
async def test_router_configure_mixed_default_preserves_primary_deployment(
    tmp_path,
    monkeypatch,
):
    """A foreign default tier must not become the primary fallback model."""
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    sync_calls: list[object] = []

    class FakeSelector:
        def sync_primary(self, provider_config):
            sync_calls.append(provider_config)

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        llm={"provider": "dashscope", "model": "qwen3.7-plus"}
    )
    ctx.config.config_path = str(tmp_path / "c.toml")
    ctx.provider_selector = FakeSelector()

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {
            "mode": "custom",
            "defaultTier": "c0",
            "crossProviderTiers": True,
            "tierProviderMismatch": "veto",
            "tiers": {
                "c0": {
                    "provider": "volcengine",
                    "model": "doubao-seed-1-6-251015",
                },
                "c1": {"provider": "dashscope", "model": "qwen3.7-plus"},
                "c2": {"provider": "dashscope", "model": "qwen3.7-plus"},
                "c3": {"provider": "dashscope", "model": "qwen3.7-plus"},
            },
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.squilla_router.default_tier == "c0"
    assert ctx.config.squilla_router.tiers["c0"]["provider"] == "volcengine"
    assert (
        ctx.config.squilla_router.tiers["c0"]["model"]
        == "doubao-seed-1-6-251015"
    )
    assert ctx.config.llm.provider == "dashscope"
    assert ctx.config.llm.model == "qwen3.7-plus"
    assert len(sync_calls) == 1
    assert sync_calls[0].provider == "dashscope"
    assert sync_calls[0].model == "qwen3.7-plus"

    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["llm"]["provider"] == "dashscope"
    assert persisted["llm"]["model"] == "qwen3.7-plus"
    assert persisted["squilla_router"]["default_tier"] == "c0"
    assert (
        persisted["squilla_router"]["tiers"]["c0"]["model"]
        == "doubao-seed-1-6-251015"
    )


@pytest.mark.asyncio
async def test_router_configure_rejects_image_model_as_default_tier(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(llm={"provider": "openrouter", "model": "m"})
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.configure",
        {"mode": "recommended", "defaultTier": "image_model"},
        ctx,
    )

    assert res.error is not None
    assert "defaultTier must reference a text tier" in res.error.message


@pytest.mark.asyncio
async def test_provider_configure_recomputes_existing_router_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        squilla_router={
            "tier_profile": "deepseek",
            "preset_binding": "follow_primary",
        },
    )
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {
            "providerId": "openai",
            "model": "gpt-5.4-mini",
            "apiKeyEnv": "OPENAI_API_KEY",
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.llm.provider == "openai"
    assert ctx.config.squilla_router.tier_profile == "openai"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["squilla_router"]["tier_profile"] == "openai"
    assert "tiers" not in persisted["squilla_router"]


@pytest.mark.asyncio
async def test_provider_configure_recomputes_openrouter_mix_router(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        llm={"provider": "openrouter", "model": "deepseek/x"},
        squilla_router={"preset_binding": "follow_primary"},
    )
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {
            "providerId": "deepseek",
            "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.llm.provider == "deepseek"
    assert ctx.config.squilla_router.enabled is True
    assert ctx.config.squilla_router.tier_profile == "deepseek"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["squilla_router"]["tier_profile"] == "deepseek"
    assert "tiers" not in persisted["squilla_router"]


@pytest.mark.asyncio
async def test_router_catalog_rpc(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.router.catalog",
        {},
        _read_ctx(),
    )

    assert res.error is None, res.error
    profile_ids = {p["profileId"] for p in res.payload["profiles"]}
    assert {"openrouter", "deepseek"} <= profile_ids


@pytest.mark.asyncio
async def test_ensemble_configure_partial_payload_updates_and_persists(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        llm_ensemble={
            "enabled": False,
            "selection_mode": "router_dynamic",
            "model_options": ["custom/model-a"],
        }
    )
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.ensemble.configure",
        {"enabled": True},
        ctx,
    )

    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["restartRequired"] is False
    assert res.payload["entry"]["enabled"] is True
    # Omitted params keep the operator's explicit values.
    assert res.payload["entry"]["selection_mode"] == "router_dynamic"
    assert res.payload["entry"]["model_options"] == ["custom/model-a"]
    assert ctx.config.llm_ensemble.enabled is True
    assert ctx.config.llm_ensemble.selection_mode == "router_dynamic"
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["llm_ensemble"]["enabled"] is True
    assert persisted["llm_ensemble"]["selection_mode"] == "router_dynamic"
    assert persisted["llm_ensemble"]["model_options"] == ["custom/model-a"]


@pytest.mark.asyncio
async def test_ensemble_configure_accepts_full_camel_case_payload(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.ensemble.configure",
        {
            "enabled": True,
            "selectionMode": "router_dynamic",
            "modelOptions": ["custom/model-a", "custom/model-b"],
            "minSuccessfulProposers": 2,
            "proposerMaxRetries": 2,
            "allFailedPolicy": "error",
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"] == {
        "enabled": True,
        "selection_mode": "router_dynamic",
        "model_options": ["custom/model-a", "custom/model-b"],
        "min_successful_proposers": 2,
        "proposer_max_retries": 2,
        "all_failed_policy": "error",
    }
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert persisted["llm_ensemble"]["selection_mode"] == "router_dynamic"
    assert persisted["llm_ensemble"]["min_successful_proposers"] == 2
    assert persisted["llm_ensemble"]["proposer_max_retries"] == 2
    assert persisted["llm_ensemble"]["all_failed_policy"] == "error"


@pytest.mark.asyncio
async def test_ensemble_configure_rejects_out_of_range_proposer_retries(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.ensemble.configure",
        {"proposerMaxRetries": 11},
        _admin_ctx(),
    )

    assert res.error is not None
    assert res.error.code == "onboarding.ensemble.invalid"
    assert "proposer_max_retries must be between 0 and 10" in res.error.message


@pytest.mark.asyncio
async def test_ensemble_configure_rejects_unknown_selection_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.ensemble.configure",
        {"selectionMode": "static_unknown"},
        _admin_ctx(),
    )

    assert res.error is not None
    assert res.error.code == "onboarding.ensemble.invalid"


@pytest.mark.asyncio
async def test_ensemble_configure_requires_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.ensemble.configure",
        {"enabled": False},
        _read_ctx(),
    )

    assert res.error is not None
    assert res.error.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_channel_upsert_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.upsert",
        {
            "entry": {
                "type": "slack",
                "name": "w",
                "token": "supersecret",
                "signing_secret": "signing-secret",
            }
        },
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["restartRequired"] is True
    assert res.payload["entry"]["token"] == "***"


@pytest.mark.asyncio
async def test_channel_upsert_rejects_slack_webhook_without_signing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.upsert",
        {"entry": {"type": "slack", "name": "w", "token": "supersecret"}},
        _admin_ctx(),
    )

    assert res.error is not None
    assert "signing_secret" in res.error.message


@pytest.mark.asyncio
async def test_channel_upsert_rejects_slack_socket_without_app_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.upsert",
        {
            "entry": {
                "type": "slack",
                "name": "w",
                "token": "supersecret",
                "connection_mode": "socket",
            }
        },
        _admin_ctx(),
    )

    assert res.error is not None
    assert "app_token" in res.error.message


@pytest.mark.asyncio
async def test_channel_probe_validates_and_redacts_without_persisting(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.probe",
        {
            "entry": {
                "type": "telegram",
                "name": "tg",
                "token": "123:secret",
                "transport_name": "polling",
            }
        },
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["status"] == "validated"
    assert res.payload["probeKind"] == "local_validation"
    assert res.payload["entry"]["token"] == "***"
    assert "123:secret" not in str(res.payload)
    assert not target.exists()


@pytest.mark.asyncio
async def test_search_configure_redacts_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.search.configure",
        {"providerId": "brave", "apiKey": "brave-secret", "maxResults": 3},
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["entry"]["api_key"] == "***"


@pytest.mark.asyncio
async def test_search_configure_accepts_webui_string_max_results(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.search.configure",
        {"providerId": "duckduckgo", "maxResults": "5"},
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["max_results"] == 5


@pytest.mark.asyncio
async def test_image_generation_configure_redacts_api_key(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKey": "sk-or",
        },
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["restartRequired"] is False
    assert res.payload["entry"]["api_key"] == "***"

    data = tomllib.loads(target.read_text())
    assert data["image_generation"]["enabled"] is True
    assert (
        data["image_generation"]["primary"]
        == "openrouter/google/gemini-3.1-flash-image-preview"
    )
    assert data["image_generation"]["providers"]["openrouter"]["api_key"] == "sk-or"


@pytest.mark.asyncio
async def test_image_generation_configure_accepts_exact_legacy_direct_key_payload(
    tmp_path,
    monkeypatch,
):
    from opensquilla.gateway.config import GatewayConfig

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ctx = _admin_ctx()
    ctx.config = GatewayConfig(config_path=str(target))
    dispatcher = get_dispatcher()

    public = await dispatcher.dispatch("r0", "config.get", {}, ctx)
    assert public.error is None, public.error
    provider = public.payload["image_generation"]["providers"]["openrouter"]
    # The 0.5.0 form hydrates the default env name, does not clear it when a
    # direct key is entered, and always emits the fallback array.
    legacy_payload = {
        "providerId": "openrouter",
        "primary": "openrouter/google/gemini-3.1-flash-image-preview",
        "apiKey": "sk-legacy-direct",
        "apiKeyEnv": provider["api_key_env"],
        "baseUrl": provider["base_url"],
        "fallbacks": [],
    }

    res = await dispatcher.dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        legacy_payload,
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.image_generation.providers.openrouter.api_key == "sk-legacy-direct"
    assert ctx.config.image_generation.providers.openrouter.api_key_env == ""
    assert res.payload["entry"]["api_key_source"] == "explicit"


@pytest.mark.asyncio
async def test_image_generation_configure_normalizes_0_5_0_provider_switch_payload(
    tmp_path,
    monkeypatch,
):
    from opensquilla.gateway.config import GatewayConfig

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ctx = _admin_ctx()
    ctx.config = GatewayConfig(config_path=str(target))
    dispatcher = get_dispatcher()

    public = await dispatcher.dispatch("r0", "config.get", {}, ctx)
    assert public.error is None, public.error
    image_config = public.payload["image_generation"]
    # In 0.5.0, changing only the provider updates the env field but leaves
    # the previous provider's non-empty default model and endpoint in place.
    legacy_switch_payload = {
        "providerId": "openrouter",
        "primary": image_config["primary"],
        "apiKey": "sk-legacy-switch",
        "apiKeyEnv": image_config["providers"]["openrouter"]["api_key_env"],
        "baseUrl": image_config["providers"]["openai"]["base_url"],
        "enabled": True,
        "fallbacks": [],
    }

    res = await dispatcher.dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        legacy_switch_payload,
        ctx,
    )

    assert res.error is None, res.error
    image_config = ctx.config.image_generation
    assert image_config.primary == "openrouter/google/gemini-3.1-flash-image-preview"
    provider = image_config.providers.openrouter
    assert provider.api_key == "sk-legacy-switch"
    assert provider.api_key_env == ""
    assert provider.base_url == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_image_generation_configure_normalizes_custom_0_5_0_provider_switch_payload(
    tmp_path,
    monkeypatch,
):
    from opensquilla.gateway.config import GatewayConfig

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        config_path=str(target),
        image_generation={
            "enabled": True,
            "primary": "openai/custom-image-model",
            "fallbacks": ["openai/gpt-image-1"],
            "providers": {
                "openai": {"base_url": "https://images.example.test/v1"},
            },
        },
    )
    dispatcher = get_dispatcher()

    # 0.5.0 keeps all source-provider fields after changing the provider. The
    # target env name is the only field its change handler replaces.
    legacy_switch_payload = {
        "providerId": "openrouter",
        "primary": "openai/custom-image-model",
        "apiKey": "sk-legacy-switch",
        "apiKeyEnv": "OPENROUTER_API_KEY",
        "baseUrl": "https://images.example.test/v1",
        "enabled": True,
        "fallbacks": ["openai/gpt-image-1"],
    }

    res = await dispatcher.dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        legacy_switch_payload,
        ctx,
    )

    assert res.error is None, res.error
    image_config = ctx.config.image_generation
    assert image_config.primary == "openrouter/google/gemini-3.1-flash-image-preview"
    assert image_config.fallbacks == ["openai/gpt-image-1"]
    provider = image_config.providers.openrouter
    assert provider.api_key == "sk-legacy-switch"
    assert provider.base_url == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_image_generation_legacy_config_get_resave_preserves_direct_key_and_fallbacks(
    tmp_path,
    monkeypatch,
):
    from opensquilla.gateway.config import GatewayConfig

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        config_path=str(target),
        image_generation={
            "enabled": True,
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "fallbacks": ["openai/gpt-image-1"],
            "providers": {
                "openrouter": {
                    "api_key": "sk-stored-direct",
                    "api_key_env": "OPENROUTER_API_KEY",
                }
            },
        },
    )
    dispatcher = get_dispatcher()

    public = await dispatcher.dispatch("r0", "config.get", {}, ctx)
    assert public.error is None, public.error
    provider = public.payload["image_generation"]["providers"]["openrouter"]
    assert provider["api_key"] == "[redacted]"
    legacy_payload = {
        "providerId": "openrouter",
        "primary": public.payload["image_generation"]["primary"],
        # The write-only 0.5.0 key field stays blank, so apiKey is omitted.
        "apiKeyEnv": provider["api_key_env"],
        "baseUrl": provider["base_url"],
        "fallbacks": [],
    }

    res = await dispatcher.dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        legacy_payload,
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.image_generation.providers.openrouter.api_key == "sk-stored-direct"
    assert ctx.config.image_generation.fallbacks == ["openai/gpt-image-1"]
    assert res.payload["entry"]["api_key_source"] == "explicit"


@pytest.mark.asyncio
async def test_image_generation_configure_replaces_direct_key_and_resets_optional_fields(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    first = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKey": "sk-direct",
            "baseUrl": "https://images.example.test/v1",
            "fallbacks": ["openai/gpt-image-1"],
        },
        _admin_ctx(),
    )
    assert first.error is None, first.error

    second = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKeyEnv": "OPENSQUILLA_TEST_IMAGE_KEY",
            "credentialMode": "env",
            "baseUrl": "",
            "fallbacks": [],
            "clearFallbacks": True,
        },
        _admin_ctx(),
    )
    assert second.error is None, second.error

    data = tomllib.loads(target.read_text())
    provider = data["image_generation"]["providers"]["openrouter"]
    assert provider.get("api_key", "") == ""
    assert provider["api_key_env"] == "OPENSQUILLA_TEST_IMAGE_KEY"
    assert provider["base_url"] == "https://openrouter.ai/api/v1"
    assert data["image_generation"]["fallbacks"] == []


@pytest.mark.asyncio
async def test_image_generation_configure_can_use_custom_env_reference(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_TEST_IMAGE_KEY", "sk-image-env")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKeyEnv": "OPENSQUILLA_TEST_IMAGE_KEY",
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["api_key_source"] == "env"
    assert res.payload["entry"]["api_key_env"] == "OPENSQUILLA_TEST_IMAGE_KEY"
    data = tomllib.loads(target.read_text())
    provider = data["image_generation"]["providers"]["openrouter"]
    # Sparse persistence omits the default empty api_key; either way the
    # key material must not be baked into the file.
    assert provider.get("api_key", "") == ""
    assert provider["api_key_env"] == "OPENSQUILLA_TEST_IMAGE_KEY"


@pytest.mark.asyncio
async def test_image_generation_configure_can_save_missing_custom_env_reference(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_TEST_IMAGE_KEY", raising=False)

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "apiKeyEnv": "OPENSQUILLA_TEST_IMAGE_KEY",
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["api_key_source"] == "missing_env"
    assert res.payload["entry"]["api_key_env"] == "OPENSQUILLA_TEST_IMAGE_KEY"
    data = tomllib.loads(target.read_text())
    provider = data["image_generation"]["providers"]["openrouter"]
    # Sparse persistence omits the default empty api_key; either way the
    # key material must not be baked into the file.
    assert provider.get("api_key", "") == ""
    assert provider["api_key_env"] == "OPENSQUILLA_TEST_IMAGE_KEY"


@pytest.mark.asyncio
async def test_image_generation_configure_can_disable_without_visible_key(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google/gemini-3.1-flash-image-preview",
            "enabled": False,
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["enabled"] is False
    assert res.payload["entry"]["api_key_source"] == "none"

    data = tomllib.loads(target.read_text())
    # Sparse persistence omits enabled=False (the built-in default); if the
    # key is present it must record the disabled state.
    assert data["image_generation"].get("enabled", False) is False


@pytest.mark.asyncio
async def test_image_generation_configure_can_disable_legacy_invalid_config(
    tmp_path,
    monkeypatch,
):
    from opensquilla.gateway.config import GatewayConfig

    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        config_path=str(target),
        image_generation={
            "enabled": True,
            "primary": "openrouter/google//image",
            "fallbacks": ["openai/"],
            "providers": {
                "openrouter": {
                    "api_key": "sk-synthetic-image",
                    "base_url": "not-a-url",
                }
            },
        },
    )

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {
            "providerId": "openrouter",
            "primary": "openrouter/google//image",
            "baseUrl": "not-a-url",
            "fallbacks": ["openai/"],
            "enabled": False,
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.image_generation.enabled is False
    data = tomllib.loads(target.read_text())
    assert data["image_generation"]["enabled"] is False
    assert data["image_generation"]["primary"] == "openrouter/google//image"
    assert data["image_generation"]["fallbacks"] == ["openai/"]
    assert (
        data["image_generation"]["providers"]["openrouter"]["base_url"]
        == "not-a-url"
    )


@pytest.mark.asyncio
async def test_onboarding_status_requires_image_generation_enable_for_llm_fallback(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _read_ctx()
    ctx.config = GatewayConfig()
    ctx.config.llm.provider = "openrouter"
    ctx.config.llm.api_key = "sk-or"

    res = await get_dispatcher().dispatch("r1", "onboarding.status", {}, ctx)

    assert res.error is None, res.error
    assert res.payload["imageGenerationConfigured"] is False
    assert res.payload["imageGenerationEnabled"] is False
    assert res.payload["imageGenerationSource"] == "none"
    assert res.payload["imageGenerationProvider"] == ""


@pytest.mark.asyncio
async def test_onboarding_status_marks_legacy_image_endpoint_mismatch_degraded(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _read_ctx()
    ctx.config = GatewayConfig()
    ctx.config.image_generation.enabled = True
    ctx.config.image_generation.primary = (
        "openrouter/google/gemini-3.1-flash-image-preview"
    )
    openrouter_provider = ctx.config.image_generation.providers.openrouter
    openrouter_provider.api_key = "sk-synthetic-image"
    openrouter_provider.base_url = "https://api.openai.com/v1"

    res = await get_dispatcher().dispatch("r1", "onboarding.status", {}, ctx)

    assert res.error is None, res.error
    assert res.payload["sections"]["image_generation"] == "degraded"
    assert res.payload["imageGenerationConfigured"] is False
    assert res.payload["imageGenerationEnabled"] is True
    assert res.payload["imageGenerationProvider"] == "openrouter"
    assert res.payload["imageGenerationSource"] == "explicit"
    detail = res.payload["sectionDetails"]["image_generation"]
    assert detail["actionRequired"] is True
    assert detail["blocking"] is False
    assert detail["detail"] == (
        "openrouter (endpoint/provider mismatch: configured openai official endpoint)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
async def test_onboarding_status_exposes_missing_env_keys_for_optional_capabilities(
    tmp_path,
    monkeypatch,
    system_name,
):
    monkeypatch.setattr(platform, "system", lambda: system_name)
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _read_ctx()
    ctx.config = GatewayConfig()
    ctx.config.llm.provider = "openrouter"
    ctx.config.llm.model = "deepseek/deepseek-v4-flash"
    ctx.config.llm.api_key = "sk-or"
    ctx.config.search_provider = "brave"
    ctx.config.search_api_key_env = "BRAVE_SEARCH_API_KEY"
    ctx.config.image_generation.enabled = True
    ctx.config.image_generation.primary = "openai/gpt-image-1"
    ctx.config.image_generation.providers.openai.api_key_env = "OPENAI_IMAGE_KEY"
    ctx.config.memory.embedding.provider = "openai"
    ctx.config.memory.embedding.remote.api_key_env = "OPENAI_EMBEDDINGS_API_KEY"
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDINGS_API_KEY", raising=False)

    res = await get_dispatcher().dispatch("r1", "onboarding.status", {}, ctx)

    assert res.error is None, res.error
    assert res.payload["searchProvider"] == "brave"
    assert res.payload["searchSource"] == "missing_env"
    assert res.payload["searchEnvKey"] == "BRAVE_SEARCH_API_KEY"
    assert res.payload["sections"]["image_generation"] == "degraded"
    assert res.payload["sectionDetails"]["image_generation"]["actionRequired"] is True
    assert res.payload["imageGenerationSource"] == "missing_env"
    assert res.payload["imageGenerationProvider"] == "openai"
    assert res.payload["imageGenerationEnvKey"] == "OPENAI_IMAGE_KEY"
    assert res.payload["memoryEmbeddingSource"] == "missing_env"
    assert res.payload["memoryEmbeddingEnvKey"] == "OPENAI_EMBEDDINGS_API_KEY"
    assert res.payload["envRecoveryCommands"] == [
        {
            "section": "memory_embedding",
            "label": "Set memory key",
            "command": _env_command("OPENAI_EMBEDDINGS_API_KEY"),
        },
        {
            "section": "search",
            "label": "Set search key",
            "command": _env_command("BRAVE_SEARCH_API_KEY"),
        },
        {
            "section": "image_generation",
            "label": "Set image key",
            "command": _env_command("OPENAI_IMAGE_KEY"),
        },
    ]


@pytest.mark.asyncio
async def test_image_generation_configure_can_enable_llm_fallback(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.llm.provider = "openrouter"
    ctx.config.llm.api_key = "sk-or"

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.configure",
        {"providerId": "openrouter"},
        ctx,
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["enabled"] is True
    assert res.payload["entry"]["api_key_source"] == "llm_fallback"

    data = tomllib.loads(target.read_text())
    assert data["image_generation"]["enabled"] is True
    # Sparse persistence omits untouched provider entries; either way no
    # key material may be baked into the file for the llm_fallback source.
    providers = data["image_generation"].get("providers", {})
    assert providers.get("openrouter", {}).get("api_key", "") == ""


@pytest.mark.asyncio
async def test_audio_configure_redacts_api_key_and_persists_tts_defaults(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.audio.configure",
        {
            "providerId": "elevenlabs",
            "apiKey": "el-secret",
            "baseUrl": "https://audio.example",
            "ttsVoice": "voice_custom",
            "ttsModel": "eleven_turbo_v2_5",
            "languageCode": "zh-CN",
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["restartRequired"] is False
    assert res.payload["entry"]["api_key"] == "***"
    assert res.payload["entry"]["enabled"] is True

    data = tomllib.loads(target.read_text())
    assert data["audio"]["enabled"] is True
    assert data["audio"]["providers"]["elevenlabs"]["api_key"] == "el-secret"
    assert data["audio"]["providers"]["elevenlabs"]["base_url"] == "https://audio.example"
    assert data["audio"]["tts"]["voice"] == "voice_custom"
    assert data["audio"]["tts"]["model"] == "eleven_turbo_v2_5"
    assert data["audio"]["tts"]["language_code"] == "zh-CN"


@pytest.mark.asyncio
async def test_audio_configure_can_save_missing_env_reference(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.audio.configure",
        {
            "providerId": "elevenlabs",
            "apiKeyEnv": "ELEVENLABS_API_KEY",
            "enabled": True,
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["api_key_source"] == "missing_env"
    assert res.payload["entry"]["api_key_env"] == "ELEVENLABS_API_KEY"

    status = await get_dispatcher().dispatch("r2", "onboarding.status", {}, _read_ctx())
    assert status.error is None, status.error
    assert status.payload["sections"]["audio"] == "degraded"
    assert status.payload["audioSource"] == "missing_env"
    assert status.payload["audioEnvKey"] == "ELEVENLABS_API_KEY"


@pytest.mark.asyncio
async def test_memory_embedding_configure_redacts_remote_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.memory_embedding.configure",
        {
            "providerId": "openai",
            "model": "text-embedding-3-small",
            "apiKey": "mem-secret",
            "baseUrl": "https://api.openai.com/v1",
        },
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["changed"] is True
    assert res.payload["restartRequired"] is True
    assert res.payload["entry"]["remote"]["api_key"] == "***"


@pytest.mark.asyncio
async def test_memory_embedding_configure_can_use_env_key_reference(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.memory_embedding.configure",
        {
            "providerId": "openai",
            "model": "text-embedding-3-small",
            "apiKeyEnv": "OPENAI_EMBEDDINGS_API_KEY",
        },
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["entry"]["remote"]["api_key_env"] == "OPENAI_EMBEDDINGS_API_KEY"
    data = tomllib.loads(target.read_text())
    remote = data["memory"]["embedding"]["remote"]
    assert remote["api_key_env"] == "OPENAI_EMBEDDINGS_API_KEY"
    assert "api_key" not in remote


@pytest.mark.asyncio
async def test_memory_embedding_configure_updates_ctx_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.memory_embedding.configure",
        {"providerId": "local", "onnxDir": "models/bge"},
        ctx,
    )
    assert res.error is None, res.error
    assert ctx.config.memory.embedding.requested_provider == "local"
    assert ctx.config.memory.embedding.local.onnx_dir == "models/bge"


@pytest.mark.asyncio
async def test_memory_embedding_configure_auto_can_store_remote_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.memory_embedding.configure",
        {
            "providerId": "auto",
            "model": "text-embedding-3-small",
            "apiKey": "mem-secret",
            "baseUrl": "https://embeddings.example/v1",
        },
        ctx,
    )

    assert res.error is None, res.error
    assert ctx.config.memory.embedding.requested_provider == "auto"
    assert ctx.config.memory.embedding.remote.api_key == "mem-secret"
    assert ctx.config.memory.embedding.remote.base_url == "https://embeddings.example/v1"
    assert res.payload["entry"]["remote"]["api_key"] == "***"


@pytest.mark.asyncio
async def test_admin_required_for_mutations(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "x", "apiKey": "k"},
        _read_ctx(),
    )
    assert res.error is not None
    assert res.error.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_provider_configure_writes_to_active_config_path(tmp_path, monkeypatch):
    # Gateway booted from ./opensquilla.toml — RPC must respect ctx.config.config_path.
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "wrong.toml"))
    project_config = tmp_path / "project.toml"

    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(project_config)

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "x", "apiKey": "sk-test"},
        ctx,
    )
    assert res.error is None, res.error
    assert project_config.exists()
    assert not (tmp_path / "wrong.toml").exists()
    assert res.payload["configPath"] == str(project_config)


@pytest.mark.asyncio
async def test_provider_configure_updates_ctx_config_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")

    await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "deepseek/x", "apiKey": "sk-new"},
        ctx,
    )
    # The running gateway's config should now reflect the change.
    assert ctx.config.llm.provider == "openrouter"
    assert ctx.config.llm.model == "deepseek/x"
    assert ctx.config.llm.api_key == "sk-new"


@pytest.mark.asyncio
async def test_provider_configure_does_not_persist_runtime_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    target = tmp_path / "c.toml"
    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(target)
    ctx.config.llm.provider = "openrouter"
    ctx.config.llm.model = "m1"
    # Leave base_url at the provider default so a model-only resave stays on
    # the same endpoint origin and the runtime-cached key is reused.
    ctx.config.llm.base_url = ""
    ctx.config.llm.api_key = "from-env"
    ctx.config.mark_runtime_secret("llm.api_key")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "m2"},
        ctx,
    )

    assert res.error is None, res.error
    data = tomllib.loads(target.read_text())
    assert "api_key" not in data["llm"]
    assert ctx.config.llm.api_key == "from-env"


@pytest.mark.asyncio
async def test_provider_configure_persists_explicit_replacement_for_env_key(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "startup-key")
    from opensquilla.gateway.config import GatewayConfig

    target = tmp_path / "c.toml"
    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "m1",
            "api_key": "startup-key",
            "api_key_env": "OPENROUTER_API_KEY",
        },
        auth={"mode": "token", "token": "runtime-auth-token"},
    )
    ctx.config.config_path = str(target)
    ctx.config.mark_runtime_secret("llm.api_key")
    ctx.config.mark_runtime_secret("auth.token")

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "m2", "apiKey": "replacement-key"},
        ctx,
    )

    assert res.error is None, res.error
    data = tomllib.loads(target.read_text())
    assert data["llm"]["api_key"] == "replacement-key"
    assert "api_key_env" not in data["llm"]
    assert "token" not in data["auth"]
    assert ctx.config.llm.api_key == "replacement-key"
    assert "llm.api_key" not in ctx.config._runtime_secret_paths
    assert "auth.token" in ctx.config._runtime_secret_paths

    reveal = await get_dispatcher().dispatch(
        "r2",
        "onboarding.provider.credential.reveal",
        {"providerId": "openrouter"},
        ctx,
    )
    assert reveal.error is None, reveal.error
    assert reveal.payload["source"] == "explicit"
    assert reveal.payload["apiKey"] == "replacement-key"

    # Desktop still exports its original onboarding key on restart. The
    # explicit replacement in TOML must remain authoritative over that stale
    # environment value after a fresh config load/runtime resolution.
    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

    reloaded = GatewayConfig.load(target)
    runtime = resolve_llm_runtime_config(reloaded)
    assert runtime.api_key == "replacement-key"
    assert runtime.api_key_from_env is False


@pytest.mark.asyncio
async def test_provider_configure_calls_provider_selector_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig

    sync_calls: list[object] = []

    class FakeSelector:
        def sync_primary(self, provider_config):
            sync_calls.append(provider_config)

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")
    ctx.provider_selector = FakeSelector()

    await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {"providerId": "openrouter", "model": "m", "apiKey": "k"},
        ctx,
    )
    assert len(sync_calls) == 1
    assert sync_calls[0].provider == "openrouter"
    assert sync_calls[0].model == "m"
    assert sync_calls[0].api_key == "k"


@pytest.mark.asyncio
async def test_provider_configure_syncs_env_key_to_provider_selector(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    from opensquilla.gateway.config import GatewayConfig

    sync_calls: list[object] = []

    class FakeSelector:
        def sync_primary(self, provider_config):
            sync_calls.append(provider_config)

    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")
    ctx.provider_selector = FakeSelector()

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.provider.configure",
        {
            "providerId": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
            "apiKeyEnv": "OPENROUTER_API_KEY",
        },
        ctx,
    )

    assert res.error is None, res.error
    assert len(sync_calls) == 1
    assert sync_calls[0].api_key == "from-env"
    assert "llm.api_key" in ctx.config._runtime_secret_paths
    persisted = tomllib.loads((tmp_path / "c.toml").read_text())
    assert "api_key" not in persisted["llm"]


@pytest.mark.asyncio
async def test_provider_configure_refreshes_shared_live_catalog_before_return(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.provider.model_catalog import ModelCatalog, set_shared_catalog

    fetches: list[str] = []

    from opensquilla.provider.tokenrhythm_catalog import (
        parse_tokenrhythm_declared,
        parse_tokenrhythm_published,
    )

    async def fake_public_fetch(**kwargs) -> dict:
        fetches.append("published")
        return parse_tokenrhythm_published(
            {
                "data": [
                    {
                        "id": "qwen3.7-max",
                        "type": "chat",
                        "status": "online",
                        "contextWindow": 1_000_000,
                        "maxOutputTokens": 131_072,
                    }
                ]
            }
        )

    async def fake_auth_fetch(*args, **kwargs) -> dict:
        fetches.append("declared")
        return parse_tokenrhythm_declared(
            {
                "data": [
                    {
                        "id": "qwen3.7-max",
                        "context_length": 1_000_000,
                        "max_completion_tokens": 131_072,
                    }
                ]
            }
        )

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fake_public_fetch,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fake_auth_fetch,
    )
    catalog = ModelCatalog()
    set_shared_catalog(catalog)
    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")
    ctx.config.state_dir = str(tmp_path / "state")

    try:
        res = await get_dispatcher().dispatch(
            "r1",
            "onboarding.provider.configure",
            {
                "providerId": "tokenrhythm",
                "model": "qwen3.7-max",
                "apiKey": "dummy-tokenrhythm-key",
            },
            ctx,
        )

        assert res.error is None, res.error
        assert fetches == ["published", "declared"]
        persisted = tomllib.loads((tmp_path / "c.toml").read_text())
        assert persisted["llm"]["provider"] == "tokenrhythm"
        assert catalog.resolve_entry("qwen3.7-max", provider="tokenrhythm").source == "live"
        assert catalog.resolve_max_tokens("qwen3.7-max", provider="tokenrhythm") == 131_072
    finally:
        from opensquilla.gateway.model_catalog_refresh import (
            install_tokenrhythm_catalog_coordinator,
        )

        install_tokenrhythm_catalog_coordinator(None)
        set_shared_catalog(None)


@pytest.mark.asyncio
async def test_provider_configure_survives_live_catalog_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.provider.model_catalog import ModelCatalog, set_shared_catalog

    async def failing_fetch(*args, **kwargs) -> dict:
        raise OSError("synthetic catalog outage")

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        failing_fetch,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        failing_fetch,
    )
    catalog = ModelCatalog()
    set_shared_catalog(catalog)
    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.config.config_path = str(tmp_path / "c.toml")
    ctx.config.state_dir = str(tmp_path / "state")

    try:
        res = await get_dispatcher().dispatch(
            "r1",
            "onboarding.provider.configure",
            {
                "providerId": "tokenrhythm",
                "model": "qwen3.7-max",
                "apiKey": "dummy-tokenrhythm-key",
            },
            ctx,
        )

        assert res.error is None, res.error
        assert ctx.config.llm.provider == "tokenrhythm"
        assert catalog.resolve_max_tokens("qwen3.7-max", provider="tokenrhythm") == 131_072
        assert catalog.resolve_entry("qwen3.7-max", provider="tokenrhythm").source == "corrections"
    finally:
        from opensquilla.gateway.model_catalog_refresh import (
            install_tokenrhythm_catalog_coordinator,
        )

        install_tokenrhythm_catalog_coordinator(None)
        set_shared_catalog(None)


@pytest.mark.asyncio
async def test_channel_disable_then_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    d = get_dispatcher()
    await d.dispatch(
        "r1",
        "onboarding.channel.upsert",
        {"entry": {"type": "slack", "name": "w", "token": "t", "signing_secret": "ss"}},
        _admin_ctx(),
    )
    res = await d.dispatch("r2", "onboarding.channel.disable", {"name": "w"}, _admin_ctx())
    assert res.error is None
    assert res.payload["enabled"] is False
    res2 = await d.dispatch("r3", "onboarding.channel.remove", {"name": "w"}, _admin_ctx())
    assert res2.error is None
    assert res2.payload["changed"] is True


def _stub_openai_transport(monkeypatch, response):
    transport = httpx.MockTransport(lambda request: response)
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched)


@pytest.mark.asyncio
async def test_models_discover_requires_admin_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.models.discover",
        {"providerId": "openrouter", "apiKey": "sk-test"},
        _read_ctx(),
    )
    assert res.error is not None
    assert "scope" in res.error.message.lower()


@pytest.mark.asyncio
async def test_models_discover_lists_live_models(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    _stub_openai_transport(
        monkeypatch,
        httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"data": [{"id": "gpt-x", "name": "GPT X", "context_length": 32000}]}',
        ),
    )
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.models.discover",
        {"providerId": "openrouter", "apiKey": "sk-test"},
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["ok"] is True
    assert res.payload["source"] == "live"
    assert [m["id"] for m in res.payload["models"]] == ["gpt-x"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active_url", "candidate_url"),
    [
        (
            "https://tokenrhythm.studio/v1",
            "HTTPS://TOKENRHYTHM.STUDIO:443/v1/",
        ),
        ("https://tokenrhythm.studio", "https://tokenrhythm.studio/v1"),
        ("https://tokenrhythm.studio/v1", "https://tokenrhythm.studio"),
    ],
)
async def test_models_discover_equivalent_active_url_can_persist_forced_refresh(
    tmp_path, monkeypatch, active_url: str, candidate_url: str
) -> None:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.onboarding.probe import ProviderModelsDiscoverResult

    captured: dict[str, object] = {}

    async def fake_discover(**kwargs):
        captured.update(kwargs)
        return ProviderModelsDiscoverResult(
            ok=True,
            provider_id="tokenrhythm",
            source="live",
            models=[],
        )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.discover_selectable_provider_models",
        fake_discover,
    )
    ctx = _admin_ctx()
    ctx.config = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm={
            "provider": "tokenrhythm",
            "api_key": "synthetic-tokenrhythm-key",
            "base_url": active_url,
        },
    )

    result = await get_dispatcher().dispatch(
        "equivalent-active-url",
        "onboarding.models.discover",
        {
            "providerId": "tokenrhythm",
            "baseUrl": candidate_url,
            "forceRefresh": True,
        },
        ctx,
    )

    assert result.error is None, result.error
    assert captured["force_refresh"] is True
    assert captured["persist_catalog"] is True
    assert captured["catalog_config"] is ctx.config
    assert captured["api_key"] == "synthetic-tokenrhythm-key"


@pytest.mark.asyncio
async def test_models_discover_unverified_provider_stays_empty_without_build(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))

    def _unexpected_build(*_args, **_kwargs):
        raise AssertionError("unverified providers must not be built for selector discovery")

    monkeypatch.setattr("opensquilla.onboarding.probe.build_provider", _unexpected_build)

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.models.discover",
        {"providerId": "openai", "apiKey": "synthetic-key"},
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload == {
        "ok": True,
        "failureKind": "",
        "detail": "",
        "source": "none",
        "models": [],
        "catalog": None,
    }


@pytest.mark.asyncio
async def test_image_models_discover_requires_admin_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.models.discover",
        {"providerId": "openrouter"},
        _read_ctx(),
    )

    assert res.error is not None
    assert "scope" in res.error.message.lower()


@pytest.mark.asyncio
async def test_image_models_discover_returns_image_specific_catalog(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))

    async def _discover(provider_id: str):
        assert provider_id == "openrouter"
        return {
            "ok": True,
            "providerId": provider_id,
            "source": "live",
            "models": [{"id": "vendor/image-live"}],
        }

    monkeypatch.setattr(
        "opensquilla.onboarding.image_generation_model_discovery."
        "discover_image_generation_models",
        _discover,
    )

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.models.discover",
        {"providerId": "openrouter"},
        _admin_ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["source"] == "live"
    assert res.payload["models"] == [{"id": "vendor/image-live"}]


@pytest.mark.asyncio
async def test_image_models_discover_rejects_unknown_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.imageGeneration.models.discover",
        {"providerId": "not-a-provider"},
        _admin_ctx(),
    )

    assert res.error is not None
    assert res.error.code == "onboarding.imageGeneration.invalid"


@pytest.fixture()
def _clean_channels_reconciler():
    from opensquilla.gateway.channels_bridge import reset_channels_reconciler

    reset_channels_reconciler()
    yield
    reset_channels_reconciler()


@pytest.mark.asyncio
async def test_channel_upsert_applies_live_when_reconciler_succeeds(
    tmp_path, monkeypatch, _clean_channels_reconciler
):
    from opensquilla.gateway.channels_bridge import register_channels_reconciler

    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    calls: list[int] = []

    async def _reconciler() -> dict[str, str]:
        calls.append(1)
        return {"w": "started"}

    register_channels_reconciler(_reconciler)
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.upsert",
        {
            "entry": {
                "type": "slack",
                "name": "w",
                "token": "supersecret",
                "signing_secret": "signing-secret",
                "app_token": "xapp-token",
            }
        },
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert calls == [1]
    # Applied live: nothing waits on a restart, and the outcome is itemized.
    assert res.payload["restartRequired"] is False
    assert res.payload["liveApply"] == {"w": "started"}


@pytest.mark.asyncio
async def test_channel_upsert_stays_restart_gated_for_webhook_outcomes(
    tmp_path, monkeypatch, _clean_channels_reconciler
):
    from opensquilla.gateway.channels_bridge import register_channels_reconciler

    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))

    async def _reconciler() -> dict[str, str]:
        return {"w": "pending_restart"}

    register_channels_reconciler(_reconciler)
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.upsert",
        {
            "entry": {
                "type": "slack",
                "name": "w",
                "token": "supersecret",
                "signing_secret": "signing-secret",
            }
        },
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True
    assert res.payload["liveApply"] == {"w": "pending_restart"}


@pytest.mark.asyncio
async def test_channel_upsert_failed_start_does_not_flag_restart(
    tmp_path, monkeypatch, _clean_channels_reconciler
):
    # A bad entry is not fixed by restarting: it stays visible through
    # channels.status start errors and is retried via channels.restart.
    from opensquilla.gateway.channels_bridge import register_channels_reconciler

    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))

    async def _reconciler() -> dict[str, str]:
        return {"w": "failed"}

    register_channels_reconciler(_reconciler)
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.upsert",
        {
            "entry": {
                "type": "slack",
                "name": "w",
                "token": "supersecret",
                "signing_secret": "signing-secret",
            }
        },
        _admin_ctx(),
    )
    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False
    assert res.payload["liveApply"] == {"w": "failed"}


@pytest.mark.asyncio
async def test_channel_remove_degrades_honestly_when_reconciler_raises(
    tmp_path, monkeypatch, _clean_channels_reconciler
):
    from opensquilla.gateway.channels_bridge import register_channels_reconciler

    config_path = tmp_path / "c.toml"
    config_path.write_text(
        '[[channels.channels]]\ntype = "slack"\nname = "w"\n'
        'token = "supersecret"\nsigning_secret = "ss"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_path))

    async def _reconciler() -> dict[str, str]:
        raise RuntimeError("manager exploded")

    register_channels_reconciler(_reconciler)
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.channel.remove",
        {"name": "w"},
        _admin_ctx(),
    )
    # Config change persisted and applied; the live swap failed, so the
    # response falls back to the pre-reconcile restart contract.
    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True
    assert res.payload["liveApply"] is None
