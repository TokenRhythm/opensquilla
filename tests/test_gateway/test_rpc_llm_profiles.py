"""Wire and secret-safety tests for additive LLM profile RPCs."""

from __future__ import annotations

import tomllib

import pytest

import opensquilla.gateway.rpc_onboarding  # noqa: F401 - register handlers
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES
from opensquilla.onboarding.probe import (
    ProviderModelsDiscoverResult,
    ProviderProbeResult,
)


def _admin_ctx(config: GatewayConfig) -> RpcContext:
    return RpcContext(
        conn_id="profile-rpc",
        config=config,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "onboarding.llmProfile.probe",
            {"providerId": "openai", "model": "gpt-mini"},
        ),
        (
            "onboarding.llmProfile.draft.probe",
            {"providerId": "openai", "model": "gpt-mini"},
        ),
    ],
)
async def test_profile_probe_rpcs_bind_physical_usage_accounting(
    tmp_path,
    monkeypatch,
    method: str,
    params: dict[str, str],
) -> None:
    from opensquilla.engine.usage_accounting import current_usage_accounting_scope

    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm_profiles={"openai": {"api_key": "synthetic-profile-secret"}},
    )
    observed = []

    async def fake_probe(**kwargs):
        scope = current_usage_accounting_scope()
        assert scope is not None
        assert callable(kwargs.get("chat_stream_factory"))
        observed.append(scope.context)
        return ProviderProbeResult(ok=True, provider_id="openai", model="gpt-mini")

    monkeypatch.setattr("opensquilla.onboarding.probe.probe_llm_provider", fake_probe)
    ctx = _admin_ctx(cfg)
    ctx.usage_event_sink = object()

    response = await get_dispatcher().dispatch("profile-probe-usage", method, params, ctx)

    assert response.error is None, response.error
    assert response.payload["ok"] is True
    assert observed[0].run_kind == "onboarding_probe"
    assert observed[0].session_id


@pytest.mark.asyncio
async def test_profile_upsert_persists_but_never_echoes_secret(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(config_path=str(config_path))
    ctx = _admin_ctx(cfg)

    response = await get_dispatcher().dispatch(
        "profile-upsert",
        "onboarding.llmProfile.upsert",
        {
            "providerId": "openai",
            "model": "gpt-profile-direct",
            "apiKey": "synthetic-profile-secret",
            "apiKeyEnvPool": ["OPENAI_POOL_A"],
        },
        ctx,
    )

    assert response.error is None, response.error
    assert response.payload["entry"]["model"] == "gpt-profile-direct"
    assert response.payload["entry"]["api_key"] == "***"
    assert "synthetic-profile-secret" not in repr(response.payload)
    persisted = tomllib.loads(config_path.read_text())
    assert persisted["llm_profiles"]["openai"]["model"] == "gpt-profile-direct"
    assert persisted["llm_profiles"]["openai"]["api_key"] == "synthetic-profile-secret"
    assert ctx.config.llm_profiles["openai"].model == "gpt-profile-direct"
    assert ctx.config.llm_profiles["openai"].api_key_env_pool == ["OPENAI_POOL_A"]


@pytest.mark.asyncio
async def test_profile_upsert_keep_current_secret_fails_closed_on_origin_change(
    tmp_path,
) -> None:
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm_profiles={
            "custom": {
                "api_key": "synthetic-old-secret",
                "base_url": "https://old.example/v1",
            }
        },
    )
    ctx = _admin_ctx(cfg)

    response = await get_dispatcher().dispatch(
        "profile-origin",
        "onboarding.llmProfile.upsert",
        {
            "providerId": "custom",
            "keepCurrentSecret": True,
            "baseUrl": "https://new.example/v1",
        },
        ctx,
    )

    assert response.error is None, response.error
    assert ctx.config.llm_profiles["custom"].api_key == ""
    assert "synthetic-old-secret" not in config_path.read_text()


@pytest.mark.asyncio
async def test_profile_remove_rejects_referenced_provider(tmp_path) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm_profiles={"openai": {"api_key_env": "OPENAI_PROFILE_KEY"}},
    )
    cfg.squilla_router.tiers["c0"] = {"provider": "openai", "model": "gpt-mini"}

    response = await get_dispatcher().dispatch(
        "profile-remove",
        "onboarding.llmProfile.remove",
        {"providerId": "openai"},
        _admin_ctx(cfg),
    )

    assert response.error is not None
    assert response.error.code == "onboarding.llmProfile.invalid"
    assert "openai" in cfg.llm_profiles


@pytest.mark.asyncio
async def test_profile_probe_rejects_unstored_provider_even_with_registry_env(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-registry-secret")

    async def unexpected_probe(**kwargs):  # pragma: no cover - regression guard
        raise AssertionError(f"probe must not run: {sorted(kwargs)}")

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.probe_llm_provider",
        unexpected_probe,
    )
    response = await get_dispatcher().dispatch(
        "profile-probe-missing",
        "onboarding.llmProfile.probe",
        {"providerId": "openai", "model": "gpt-mini"},
        _admin_ctx(GatewayConfig(config_path=str(tmp_path / "config.toml"))),
    )

    assert response.error is not None
    assert response.error.code == "onboarding.llmProfile.invalid"
    assert "does not exist" in response.error.message
    assert "synthetic-registry-secret" not in repr(response)


@pytest.mark.asyncio
async def test_profile_discovery_rejects_unstored_provider_even_with_registry_env(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-registry-secret")

    async def unexpected_discover(**kwargs):  # pragma: no cover - regression guard
        raise AssertionError(f"discovery must not run: {sorted(kwargs)}")

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.discover_selectable_provider_models",
        unexpected_discover,
    )
    response = await get_dispatcher().dispatch(
        "profile-discover-missing",
        "onboarding.llmProfile.models.discover",
        {"providerId": "openai"},
        _admin_ctx(GatewayConfig(config_path=str(tmp_path / "config.toml"))),
    )

    assert response.error is not None
    assert response.error.code == "onboarding.llmProfile.invalid"
    assert "does not exist" in response.error.message
    assert "synthetic-registry-secret" not in repr(response)


@pytest.mark.asyncio
async def test_profile_probe_uses_resolved_profile_without_secret_in_result(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm_profiles={"openai": {"api_key": "synthetic-probe-secret"}},
    )
    captured: dict[str, object] = {}

    async def fake_probe(**kwargs):
        captured.update(kwargs)
        return ProviderProbeResult(
            ok=True,
            provider_id="openai",
            model="gpt-mini",
            latency_ms=23,
            first_response_ms=7,
        )

    monkeypatch.setattr("opensquilla.onboarding.probe.probe_llm_provider", fake_probe)
    response = await get_dispatcher().dispatch(
        "profile-probe",
        "onboarding.llmProfile.probe",
        {"providerId": "openai", "model": "gpt-mini"},
        _admin_ctx(cfg),
    )

    assert response.error is None, response.error
    assert captured["api_key"] == "synthetic-probe-secret"
    assert captured["allow_default_api_key_env"] is False
    assert response.payload["firstResponseMs"] == 7
    assert response.payload["totalMs"] == 23
    assert response.payload["latencyMs"] == 23
    assert "synthetic-probe-secret" not in repr(response.payload)


@pytest.mark.asyncio
async def test_profile_draft_probe_uses_unsaved_deployment_without_persisting(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    stored_secret = "synthetic-stored-draft-secret"
    draft_secret = "synthetic-unsaved-draft-secret"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm_profiles={
            "openai": {
                "api_key": stored_secret,
                "base_url": "https://api.openai.com/v1",
                "proxy": "http://127.0.0.1:8001",
            }
        },
    )
    before = cfg.model_dump(mode="python")
    captured: dict[str, object] = {}

    async def fake_probe(**kwargs):
        captured.update(kwargs)
        return ProviderProbeResult(
            ok=True,
            provider_id="openai",
            model="gpt-unsaved-draft",
            latency_ms=31,
            first_response_ms=11,
        )

    monkeypatch.setattr("opensquilla.onboarding.probe.probe_llm_provider", fake_probe)
    response = await get_dispatcher().dispatch(
        "profile-draft-probe",
        "onboarding.llmProfile.draft.probe",
        {
            "providerId": "openai",
            "model": "gpt-unsaved-draft",
            "apiKey": draft_secret,
            "baseUrl": "https://candidate.example/v2",
            "proxy": "http://127.0.0.1:9001",
            "keepCurrentSecret": False,
        },
        _admin_ctx(cfg),
    )

    assert response.error is None, response.error
    assert captured == {
        "provider_id": "openai",
        "model": "gpt-unsaved-draft",
        "api_key": draft_secret,
        "api_key_env": "",
        "base_url": "https://candidate.example/v2",
        "proxy": "http://127.0.0.1:9001",
        "allow_default_api_key_env": False,
    }
    assert cfg.model_dump(mode="python") == before
    assert not config_path.exists()
    assert response.payload["firstResponseMs"] == 11
    assert response.payload["totalMs"] == 31
    assert response.payload["latencyMs"] == 31
    assert stored_secret not in repr(response.payload)
    assert draft_secret not in repr(response.payload)


@pytest.mark.asyncio
async def test_profile_draft_probe_never_reuses_secret_across_endpoint_origins(
    tmp_path,
    monkeypatch,
) -> None:
    stored_secret = "synthetic-stored-origin-secret"
    registry_secret = "synthetic-registry-origin-secret"
    monkeypatch.setenv("OPENAI_API_KEY", registry_secret)
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm_profiles={
            "openai": {
                "api_key": stored_secret,
                "base_url": "https://api.openai.com/v1",
            }
        },
    )
    before = cfg.model_dump(mode="python")

    async def unexpected_probe(**kwargs):  # pragma: no cover - fail-closed guard
        raise AssertionError(f"probe must not run: {sorted(kwargs)}")

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.probe_llm_provider",
        unexpected_probe,
    )
    response = await get_dispatcher().dispatch(
        "profile-draft-origin-change",
        "onboarding.llmProfile.draft.probe",
        {
            "providerId": "openai",
            "model": "gpt-unsaved-draft",
            "baseUrl": "https://other.example/v1",
            "keepCurrentSecret": True,
        },
        _admin_ctx(cfg),
    )

    assert response.error is not None
    assert response.error.code == "onboarding.llmProfile.invalid"
    assert "not executable" in response.error.message
    assert cfg.model_dump(mode="python") == before
    assert not (tmp_path / "config.toml").exists()
    assert stored_secret not in repr(response)
    assert registry_secret not in repr(response)


@pytest.mark.asyncio
async def test_profile_probe_uses_and_parks_shared_pool_credentials(
    tmp_path,
    monkeypatch,
) -> None:
    from opensquilla.gateway.llm_runtime import reset_profile_credential_pools
    from opensquilla.provider.failures import ProviderFailureKind

    env_a = "OPENSQUILLA_TEST_PROFILE_RPC_POOL_A"
    env_b = "OPENSQUILLA_TEST_PROFILE_RPC_POOL_B"
    key_a = "synthetic-profile-rpc-key-a"
    key_b = "synthetic-profile-rpc-key-b"
    monkeypatch.setenv(env_a, key_a)
    monkeypatch.setenv(env_b, key_b)
    reset_profile_credential_pools()
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm_profiles={"openai": {"api_key_env_pool": [env_a, env_b]}},
    )
    ctx = _admin_ctx(cfg)
    seen_keys: list[str] = []

    async def fake_probe(**kwargs):
        seen_keys.append(str(kwargs["api_key"]))
        if len(seen_keys) == 1:
            return ProviderProbeResult(
                ok=False,
                provider_id="openai",
                model="gpt-mini",
                failure_kind=ProviderFailureKind.AUTH_INVALID.value,
            )
        return ProviderProbeResult(ok=True, provider_id="openai", model="gpt-mini")

    monkeypatch.setattr("opensquilla.onboarding.probe.probe_llm_provider", fake_probe)
    try:
        first = await get_dispatcher().dispatch(
            "profile-probe-pool-first",
            "onboarding.llmProfile.probe",
            {"providerId": "openai", "model": "gpt-mini"},
            ctx,
        )
        second = await get_dispatcher().dispatch(
            "profile-probe-pool-second",
            "onboarding.llmProfile.probe",
            {"providerId": "openai", "model": "gpt-mini"},
            ctx,
        )
    finally:
        reset_profile_credential_pools()

    assert first.error is None
    assert first.payload["ok"] is False
    assert second.error is None
    assert second.payload["ok"] is True
    assert seen_keys == [key_a, key_b]
    assert key_a not in repr(first.payload)
    assert key_b not in repr(second.payload)


@pytest.mark.asyncio
async def test_profile_model_discovery_uses_resolved_profile(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm_profiles={"openai": {"api_key": "synthetic-discovery-secret"}},
    )
    captured: dict[str, object] = {}

    async def fake_discover(**kwargs):
        captured.update(kwargs)
        return ProviderModelsDiscoverResult(
            ok=True,
            provider_id="openai",
            source="live",
            models=[{"id": "gpt-mini"}],
        )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.discover_selectable_provider_models",
        fake_discover,
    )
    response = await get_dispatcher().dispatch(
        "profile-discover",
        "onboarding.llmProfile.models.discover",
        {"providerId": "openai"},
        _admin_ctx(cfg),
    )

    assert response.error is None, response.error
    assert captured["api_key"] == "synthetic-discovery-secret"
    assert response.payload["models"] == [{"id": "gpt-mini"}]
    assert "synthetic-discovery-secret" not in repr(response.payload)


@pytest.mark.asyncio
async def test_profile_draft_model_discovery_uses_unsaved_deployment_without_persisting(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    stored_secret = "synthetic-stored-discovery-secret"
    draft_secret = "synthetic-draft-discovery-secret"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm_profiles={
            "openai": {
                "api_key": stored_secret,
                "base_url": "https://api.openai.com/v1",
            }
        },
    )
    before = cfg.model_dump(mode="python")
    captured: dict[str, object] = {}

    async def fake_discover(**kwargs):
        captured.update(kwargs)
        return ProviderModelsDiscoverResult(
            ok=True,
            provider_id="openai",
            source="live",
            models=[{"id": "gpt-draft-discovered"}],
        )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.discover_selectable_provider_models",
        fake_discover,
    )
    response = await get_dispatcher().dispatch(
        "profile-draft-discover",
        "onboarding.llmProfile.draft.models.discover",
        {
            "providerId": "openai",
            "apiKey": draft_secret,
            "baseUrl": "https://candidate.example/v2",
            "proxy": "http://127.0.0.1:9002",
            "keepCurrentSecret": False,
        },
        _admin_ctx(cfg),
    )

    assert response.error is None, response.error
    assert captured == {
        "provider_id": "openai",
        "api_key": draft_secret,
        "api_key_env": "",
        "base_url": "https://candidate.example/v2",
        "proxy": "http://127.0.0.1:9002",
        "allow_default_api_key_env": False,
    }
    assert response.payload["models"] == [{"id": "gpt-draft-discovered"}]
    assert cfg.model_dump(mode="python") == before
    assert not config_path.exists()
    assert stored_secret not in repr(response.payload)
    assert draft_secret not in repr(response.payload)


@pytest.mark.asyncio
async def test_profile_activate_persists_then_hot_syncs_without_secret_echo(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm={"provider": "openai", "model": "gpt-test", "api_key": "old-secret"},
        llm_profiles={"deepseek": {"api_key": "new-secret"}},
        squilla_router={"preset_binding": "follow_primary"},
    )

    class RecordingSelector:
        def __init__(self) -> None:
            self.synced = []

        def sync_primary(self, provider_config) -> None:
            self.synced.append(provider_config)

    selector = RecordingSelector()
    ctx = _admin_ctx(cfg)
    ctx.provider_selector = selector
    media_syncs: list[str] = []
    catalog_syncs: list[str] = []
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_onboarding._sync_image_generation",
        lambda config: media_syncs.append(config.llm.provider),
    )

    async def fake_refresh(config):
        catalog_syncs.append(config.llm.provider)

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.refresh_live_model_catalog",
        fake_refresh,
    )

    response = await get_dispatcher().dispatch(
        "profile-activate",
        "onboarding.llmProfile.activate",
        {"providerId": "deepseek", "model": "deepseek-chat"},
        ctx,
    )

    assert response.error is None, response.error
    assert response.payload["entry"] == {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "previousProvider": "openai",
        "active": True,
        "routerBinding": "follow_primary",
    }
    assert "new-secret" not in repr(response.payload)
    assert "old-secret" not in repr(response.payload)
    assert ctx.config.llm.provider == "deepseek"
    assert selector.synced[-1].provider == "deepseek"
    assert media_syncs == ["deepseek"]
    assert catalog_syncs == ["deepseek"]
    persisted = tomllib.loads(config_path.read_text())
    assert persisted["llm"]["provider"] == "deepseek"
    assert persisted["llm_profiles"]["openai"]["model"] == "gpt-test"
    assert persisted["llm_profiles"]["openai"]["api_key"] == "old-secret"


@pytest.mark.asyncio
async def test_profile_activate_rpc_omits_model_and_uses_provider_default(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    target_secret = "synthetic-default-model-target-secret"
    old_secret = "synthetic-default-model-old-secret"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm={"provider": "openai", "model": "gpt-old", "api_key": old_secret},
        # Legacy profiles have no model field. The activation RPC must remain
        # usable after upgrade by resolving DeepSeek's provider-scoped default.
        llm_profiles={"deepseek": {"api_key": target_secret}},
        squilla_router={"preset_binding": "follow_primary"},
    )

    async def fake_refresh(config):
        return None

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.refresh_live_model_catalog",
        fake_refresh,
    )
    response = await get_dispatcher().dispatch(
        "profile-activate-default-model",
        "onboarding.llmProfile.activate",
        {"providerId": "deepseek"},
        _admin_ctx(cfg),
    )

    assert response.error is None, response.error
    assert response.payload["entry"]["model"] == "deepseek-v4-flash"
    assert cfg.llm.provider == "deepseek"
    assert cfg.llm.model == "deepseek-v4-flash"
    persisted = tomllib.loads(config_path.read_text())
    assert persisted["llm"]["model"] == "deepseek-v4-flash"
    assert persisted["llm_profiles"]["openai"]["model"] == "gpt-old"
    assert target_secret not in repr(response.payload)
    assert old_secret not in repr(response.payload)


@pytest.mark.parametrize(
    ("tier_overrides", "expect_inline_tiers"),
    [
        ({}, False),
        (
            {
                "c0": {
                    "provider": "openai",
                    "model": "gpt-custom-fast",
                    "thinking": "low",
                }
            },
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_profile_activate_restart_round_trip_preserves_router_and_ensemble(
    tmp_path,
    monkeypatch,
    tier_overrides,
    expect_inline_tiers,
) -> None:
    from opensquilla.onboarding.config_store import persist_config
    from opensquilla.tools.builtin import media

    config_path = tmp_path / "config.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm={
            "provider": "openai",
            "model": "gpt-test",
            "api_key": "synthetic-old-primary-secret",
            "base_url": "https://api.openai.com/v1",
        },
        llm_profiles={
            "deepseek": {
                "api_key": "synthetic-new-primary-secret",
                "base_url": "https://api.deepseek.com/v1",
            }
        },
        squilla_router={
            "enabled": True,
            "tier_profile": "openai",
            "preset_binding": "custom",
            "cross_provider_tiers": True,
            "tier_provider_mismatch": "veto",
            "default_tier": "c2",
            "tiers": tier_overrides,
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "success_threshold": 0.5,
            "candidates": [
                {"provider": "openai", "model": "gpt-test", "role": "primary"},
                {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "role": "contrast",
                },
                {"provider": "openai", "model": "gpt-test", "role": "aggregator"},
            ],
        },
    )
    # Exercise the real sparse-persist path: activation starts from a config
    # loaded at gateway boot, with Router/Ensemble already represented on
    # disk, rather than from a fresh in-memory model whose first save writes
    # every non-default field.
    persist_config(cfg, path=config_path, backup=False)
    cfg = GatewayConfig.load(config_path, read_only=True)
    router_before = cfg.squilla_router.model_dump(mode="python")
    ensemble_before = cfg.llm_ensemble.model_dump(mode="python")

    async def fake_refresh(config):
        return None

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.refresh_live_model_catalog",
        fake_refresh,
    )
    try:
        response = await get_dispatcher().dispatch(
            "profile-activate-restart",
            "onboarding.llmProfile.activate",
            {"providerId": "deepseek", "model": "deepseek-chat"},
            _admin_ctx(cfg),
        )
    finally:
        media.configure_image_generation(None)

    assert response.error is None, response.error
    raw = tomllib.loads(config_path.read_text())
    assert raw["squilla_router"]["tier_profile"] == "openai"
    assert ("tiers" in raw["squilla_router"]) is expect_inline_tiers

    restarted = GatewayConfig.load(config_path, read_only=True)
    assert restarted.llm.provider == "deepseek"
    assert restarted.squilla_router.model_dump(mode="python") == router_before
    assert restarted.llm_ensemble.model_dump(mode="python") == ensemble_before
    assert "openai" in restarted.llm_profiles
    assert "deepseek" not in restarted.llm_profiles


@pytest.mark.asyncio
async def test_profile_activate_hot_media_sync_resolves_demoted_primary_profile(
    tmp_path,
    monkeypatch,
) -> None:
    from opensquilla.tools.builtin import media

    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm={
            "provider": "openai",
            "model": "gpt-test",
            "api_key": "synthetic-demoted-media-secret",
            "base_url": "https://profile-openai.example/v1",
        },
        llm_profiles={
            "deepseek": {
                "api_key": "synthetic-active-media-secret",
                "base_url": "https://api.deepseek.com/v1",
            }
        },
        squilla_router={
            "preset_binding": "custom",
            "cross_provider_tiers": True,
            "tiers": {
                "image_model": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "supports_image": True,
                    "image_only": True,
                }
            }
        },
    )

    async def fake_refresh(config):
        return None

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.refresh_live_model_catalog",
        fake_refresh,
    )
    try:
        response = await get_dispatcher().dispatch(
            "profile-activate-media",
            "onboarding.llmProfile.activate",
            {"providerId": "deepseek", "model": "deepseek-chat"},
            _admin_ctx(cfg),
        )
        resolved = media._resolve_vision_provider_config(
            default_model="openai/gpt-4o-mini"
        )
    finally:
        media.configure_image_generation(None)

    assert response.error is None, response.error
    assert resolved.provider == "openai"
    assert resolved.model == "gpt-4o-mini"
    assert resolved.api_key == "synthetic-demoted-media-secret"
    assert resolved.base_url == "https://profile-openai.example/v1"
    assert resolved.replay_provider_state is False
    assert "synthetic-demoted-media-secret" not in repr(resolved)


@pytest.mark.asyncio
async def test_profile_activate_pool_rejection_has_stable_code(tmp_path) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm_profiles={"deepseek": {"api_key_env_pool": ["DEEPSEEK_POOL_A"]}},
    )

    response = await get_dispatcher().dispatch(
        "profile-activate-pool",
        "onboarding.llmProfile.activate",
        {"providerId": "deepseek", "model": "deepseek-chat"},
        _admin_ctx(cfg),
    )

    assert response.error is not None
    assert response.error.code == "onboarding.llmProfile.primary_pool_unsupported"
    assert response.error.details["reason"] == "primary_pool_unsupported"
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.asyncio
async def test_profile_activate_router_conflict_has_stable_code_and_actions(
    tmp_path,
) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm={"provider": "openai", "model": "gpt-test", "api_key": "old-secret"},
        llm_profiles={"deepseek": {"api_key": "new-secret"}},
        squilla_router={
            "tier_profile": "openai",
            "preset_binding": "custom",
            "cross_provider_tiers": False,
        },
    )

    response = await get_dispatcher().dispatch(
        "profile-activate-router-conflict",
        "onboarding.llmProfile.activate",
        {"providerId": "deepseek", "model": "deepseek-chat"},
        _admin_ctx(cfg),
    )

    assert response.error is not None
    assert response.error.code == "onboarding.llmProfile.router_provider_conflict"
    assert response.error.details == {
        "reason": "router_provider_conflict",
        "providerId": "deepseek",
        "conflictProviders": ["openai"],
        "allowedRouterActions": [
            "use_recommended",
            "enable_cross_provider",
            "disable",
        ],
    }
    assert cfg.llm.provider == "openai"
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.asyncio
async def test_profile_activate_router_action_is_applied_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm={"provider": "openai", "model": "gpt-test", "api_key": "old-secret"},
        llm_profiles={"deepseek": {"api_key": "new-secret"}},
        squilla_router={
            "tier_profile": "openai",
            "preset_binding": "custom",
            "cross_provider_tiers": False,
            "default_tier": "c2",
        },
    )

    async def fake_refresh(config):
        return None

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.refresh_live_model_catalog",
        fake_refresh,
    )
    response = await get_dispatcher().dispatch(
        "profile-activate-router-action",
        "onboarding.llmProfile.activate",
        {
            "providerId": "deepseek",
            "model": "deepseek-chat",
            "routerAction": "use_recommended",
        },
        _admin_ctx(cfg),
    )

    assert response.error is None, response.error
    assert cfg.llm.provider == "deepseek"
    assert cfg.llm.model == "deepseek-chat"
    assert cfg.squilla_router.preset_binding == "follow_primary"
    assert cfg.squilla_router.tier_profile == "deepseek"
    assert cfg.squilla_router.default_tier == "c2"
    persisted = tomllib.loads((tmp_path / "config.toml").read_text())
    assert persisted["llm"]["provider"] == "deepseek"
    assert persisted["squilla_router"]["preset_binding"] == "follow_primary"


@pytest.mark.asyncio
async def test_profile_activate_persist_failure_leaves_live_runtime_untouched(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm={"provider": "openai", "model": "gpt-test", "api_key": "old-secret"},
        llm_profiles={"deepseek": {"api_key": "new-secret"}},
        squilla_router={"preset_binding": "follow_primary"},
    )
    ctx = _admin_ctx(cfg)
    sync_attempts: list[str] = []
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_onboarding._persist",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic write failure")),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_onboarding._sync_provider_selector",
        lambda *args: sync_attempts.append("selector"),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_onboarding._sync_image_generation",
        lambda *args: sync_attempts.append("media"),
    )

    response = await get_dispatcher().dispatch(
        "profile-activate-write-failure",
        "onboarding.llmProfile.activate",
        {"providerId": "deepseek", "model": "deepseek-chat"},
        ctx,
    )

    assert response.error is not None
    assert ctx.config.llm.provider == "openai"
    assert "deepseek" in ctx.config.llm_profiles
    assert sync_attempts == []


def test_profile_activate_requires_admin_scope() -> None:
    assert METHOD_SCOPES["onboarding.llmProfile.activate"] == ADMIN_SCOPE


def test_profile_draft_methods_require_admin_scope() -> None:
    assert METHOD_SCOPES["onboarding.llmProfile.draft.probe"] == ADMIN_SCOPE
    assert (
        METHOD_SCOPES["onboarding.llmProfile.draft.models.discover"] == ADMIN_SCOPE
    )
