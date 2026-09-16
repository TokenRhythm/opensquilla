"""Synthetic regression coverage for Router reset and candidate reactivation."""

from __future__ import annotations

import tomllib
from unittest.mock import AsyncMock, MagicMock

import pytest

from opensquilla.application.provider_configuration import ModelRouting
from opensquilla.contracts.generated.v4.gateway_contract_registry import GATEWAY_METHOD_CONTRACTS
from opensquilla.gateway.adapters.provider_configuration import GatewayModelRoutingPolicyPort
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.rpc_models import _handle_models_routing_reset_recommended
from opensquilla.gateway.scopes import ADMIN_SCOPE, WRITE_SCOPE
from opensquilla.onboarding.mutations import upsert_router
from opensquilla.onboarding.router_policy import (
    PrimaryProviderChangedError,
    RouterProviderConflictError,
    validate_router_candidate,
    validate_router_reactivation,
)


def legacy_config(**kwargs) -> GatewayConfig:
    config = GatewayConfig(
        llm={"provider": "tokenrhythm", "model": "glm-5.2"},
        squilla_router={
            "enabled": False,
            "preset_binding": "custom",
            "default_tier": "c2",
            "rollout_phase": "prompt_only",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter_b5",
            "proposer_max_retries": 3,
        },
        **kwargs,
    )
    config.squilla_router.tiers["c0"] = {"provider": "openrouter", "model": "custom/model"}
    return config


@pytest.mark.parametrize("activate", [False, True])
def test_reset_preserves_strategy_and_ensemble_and_uses_inline_tokenrhythm(activate):
    config = legacy_config()
    source = config.model_dump(mode="python")
    prepared = GatewayModelRoutingPolicyPort().prepare_recommended(
        config,
        "tokenrhythm",
        activate_router=activate,
    )
    candidate = prepared.config
    assert config.model_dump(mode="python") == source
    assert candidate.llm.model_dump() == config.llm.model_dump()
    assert candidate.squilla_router.default_tier == "c2"
    assert candidate.squilla_router.preset_binding == "follow_primary"
    assert candidate.squilla_router.tier_profile is None
    assert {tier["provider"] for tier in candidate.squilla_router.tiers.values()} == {"tokenrhythm"}
    assert candidate.squilla_router.enabled is activate
    assert candidate.squilla_router.rollout_phase == ("full" if activate else "prompt_only")
    before_ensemble = config.llm_ensemble.model_dump()
    if activate:
        before_ensemble["enabled"] = False
    assert candidate.llm_ensemble.model_dump() == before_ensemble
    before_router = config.squilla_router.model_dump()
    after_router = candidate.squilla_router.model_dump()
    for field in ("tiers", "tier_profile", "preset_binding"):
        before_router.pop(field)
        after_router.pop(field)
    if activate:
        before_router.update(enabled=True, rollout_phase="full")
    assert after_router == before_router


def test_reset_expected_primary_fails_without_mutating_source():
    config = legacy_config()
    source = config.model_dump()
    with pytest.raises(PrimaryProviderChangedError):
        GatewayModelRoutingPolicyPort().prepare_recommended(
            config, "openrouter", activate_router=True
        )
    assert config.model_dump() == source


def test_ensemble_to_router_checks_newly_executable_drafts():
    config = legacy_config()
    validate_router_candidate(config)
    with pytest.raises(RouterProviderConflictError) as error:
        GatewayModelRoutingPolicyPort().prepare(config, "router")
    assert error.value.details == {
        "reason": "router_provider_conflict",
        "providerId": "tokenrhythm",
        "conflictProviders": ["openrouter"],
        "allowedRouterActions": ["use_recommended"],
    }
    assert config.llm_ensemble.enabled is True


def test_router_configure_checks_final_candidate_after_ensemble_turns_off():
    config = legacy_config()
    with pytest.raises(RouterProviderConflictError):
        upsert_router(config, mode="custom")


def test_unrelated_legacy_save_and_retired_image_tier_do_not_conflict():
    config = legacy_config()
    config.squilla_router.enabled = True
    config.llm_ensemble.enabled = False
    same = config.model_copy(deep=True)
    same.squilla_router.default_tier = "c1"
    validate_router_reactivation(config, same)
    for tier in same.squilla_router.tiers.values():
        tier["provider"] = "tokenrhythm"
    same.squilla_router.tiers["image_model"] = {"provider": "foreign", "model": "old-image"}
    validate_router_candidate(same)


@pytest.mark.parametrize(
    "writer", ["config.set", "config.patch", "config.patch.safe", "config.apply"]
)
async def test_explicit_config_reactivation_conflicts_with_zero_writes(writer, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# unchanged sentinel\n")
    config = legacy_config(config_path=str(path))
    config.llm_ensemble.enabled = False
    before = config.model_dump()
    if writer == "config.set":
        params = {"path": "squilla_router.enabled", "value": True}
    elif writer == "config.apply":
        payload = config.model_dump(mode="python")
        payload["squilla_router"]["enabled"] = True
        params = {"config": payload}
    else:
        params = {"patches": {"squilla_router.enabled": True}}
    result = await get_dispatcher().dispatch(
        "reactivate", writer, params, RpcContext(conn_id="test", config=config)
    )
    assert result.error.code == "ROUTER_PROVIDER_CONFLICT"
    assert result.error.details["allowedRouterActions"] == ["use_recommended"]
    assert config.model_dump() == before
    assert path.read_text() == "# unchanged sentinel\n"


@pytest.mark.parametrize(
    "provider,activate", [("tokenrhythm", False), ("tokenrhythm", True), ("openai", False)]
)
async def test_reset_persists_and_reloads_without_losing_settings(provider, activate, tmp_path):
    path = tmp_path / "config.toml"
    config = legacy_config(config_path=str(path))
    config.llm.provider = provider
    config.llm.model = "test-model"
    ensemble = config.llm_ensemble.model_dump()
    result = await _handle_models_routing_reset_recommended(
        {"providerId": provider, "activateRouter": activate},
        RpcContext(conn_id="test", config=config),
    )
    durable = tomllib.loads(path.read_text(encoding="utf-8"))
    restored = GatewayConfig(**durable)
    assert restored.squilla_router.preset_binding == "follow_primary"
    assert restored.squilla_router.default_tier == "c2"
    assert restored.squilla_router.enabled is activate
    assert {tier["provider"] for tier in restored.squilla_router.tiers.values()} == {provider}
    if activate:
        ensemble["enabled"] = False
    assert restored.llm_ensemble.model_dump() == ensemble
    assert result["restart_required"] is False


async def test_reset_primary_mismatch_wire_is_conflict_without_writes(tmp_path):
    path = tmp_path / "config.toml"
    config = legacy_config(config_path=str(path))
    result = await get_dispatcher().dispatch(
        "mismatch",
        "models.routing.resetRecommended",
        {
            "providerId": "openai",
            "activateRouter": True,
        },
        RpcContext(conn_id="test", config=config),
    )
    assert result.error.code == "CONFLICT"
    assert result.error.details == {"reason": "primary_changed"}
    assert not path.exists()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"providerId": " "},
        {"providerId": "tokenrhythm", "activateRouter": None},
        {"providerId": "tokenrhythm", "activateRouter": "true"},
        {"providerId": "tokenrhythm", "unexpected": True},
    ],
)
async def test_reset_rejects_invalid_production_params(params, tmp_path):
    path = tmp_path / "config.toml"
    result = await get_dispatcher().dispatch(
        "invalid",
        "models.routing.resetRecommended",
        params,
        RpcContext(conn_id="test", config=legacy_config(config_path=str(path))),
    )
    assert result.error.code == "INVALID_REQUEST"
    assert not path.exists()


async def test_reset_is_independently_registered_admin_only(tmp_path):
    descriptor = GATEWAY_METHOD_CONTRACTS["models.routing.resetRecommended"]
    entry = get_dispatcher().get_entry("models.routing.resetRecommended")
    assert descriptor.scope == ADMIN_SCOPE
    assert descriptor.guest_allowed is False
    assert entry.required_scope == ADMIN_SCOPE
    assert get_dispatcher().get_entry("models.routing.set").required_scope == WRITE_SCOPE
    principal = Principal(
        role="operator", scopes=frozenset({WRITE_SCOPE}), is_owner=False, authenticated=True
    )
    result = await get_dispatcher().dispatch(
        "write-only",
        "models.routing.resetRecommended",
        {
            "providerId": "tokenrhythm",
        },
        RpcContext(
            conn_id="test",
            config=legacy_config(config_path=str(tmp_path / "config.toml")),
            principal=principal,
        ),
    )
    assert result.error.code == "UNAUTHORIZED"


@pytest.mark.parametrize("failure", ["prepare-runtime", "persist"])
async def test_reset_transaction_failure_does_not_install_or_reconcile(failure):
    config = legacy_config()
    port = MagicMock()
    port.active_config.return_value = config
    runtime = MagicMock()
    runtime.reconcile = AsyncMock()
    runtime.publish_changed = AsyncMock()
    if failure == "prepare-runtime":
        runtime.prepare_reconciliation.side_effect = OSError("synthetic failure")
    else:
        port.persist_candidate.side_effect = OSError("synthetic failure")
    with pytest.raises(OSError, match="synthetic failure"):
        await ModelRouting(port, GatewayModelRoutingPolicyPort(), runtime).reset_recommended(
            "tokenrhythm"
        )
    port.install_candidate.assert_not_called()
    runtime.reconcile.assert_not_called()
    runtime.publish_changed.assert_not_called()
    if failure == "prepare-runtime":
        port.persist_candidate.assert_not_called()


@pytest.mark.parametrize("source", ["reset", "patch"])
async def test_pure_tier_changes_publish_existing_routing_event(source, tmp_path, monkeypatch):
    from opensquilla.gateway import websocket
    from opensquilla.gateway.rpc_config import _handle_config_patch
    from opensquilla.gateway.scopes import READ_SCOPE

    config = GatewayConfig(
        config_path=str(tmp_path / "event.toml"),
        llm={"provider": "tokenrhythm", "model": "test-model"},
    )
    config.squilla_router.tiers["c0"]["model"] = "old-model"
    sent = []

    async def send_event(event, payload):
        sent.append((event, payload))

    subscriber = MagicMock()
    subscriber.principal = Principal(
        role="operator",
        scopes=frozenset({READ_SCOPE}),
        is_owner=False,
        authenticated=True,
    )
    subscriber.send_event = send_event
    registry = MagicMock()
    registry.all.return_value = [subscriber]
    monkeypatch.setattr(websocket, "get_registry", lambda: registry)
    ctx = RpcContext(conn_id="test", config=config, subscription_manager=object())
    if source == "reset":
        await _handle_models_routing_reset_recommended({"providerId": "tokenrhythm"}, ctx)
    else:
        await _handle_config_patch({"patches": {"squilla_router.tiers.c0.model": "new-model"}}, ctx)
    assert len(sent) == 1
    assert sent[0][0] == "models.routing.changed"
    assert sent[0][1]["mode"] == "router"


@pytest.mark.parametrize("transition", ["rollout", "ensemble_role", "cross_provider"])
def test_explicit_candidate_reactivation_cases(transition):
    before = legacy_config()
    before.squilla_router.enabled = True
    if transition == "rollout":
        before.llm_ensemble.enabled = False
        before.squilla_router.rollout_phase = "observe"
        after = before.model_copy(deep=True)
        after.squilla_router.rollout_phase = "full"
    elif transition == "cross_provider":
        before.llm_ensemble.enabled = False
        before.squilla_router.cross_provider_tiers = True
        after = before.model_copy(deep=True)
        after.squilla_router.cross_provider_tiers = False
    else:
        after = before.model_copy(deep=True)
        after.llm_ensemble.selection_mode = "router_dynamic"
    with pytest.raises(RouterProviderConflictError):
        validate_router_reactivation(before, after)


async def test_reset_transaction_prepares_then_persists_once_before_install():
    config = legacy_config()
    operations = []
    port = MagicMock()
    port.active_config.return_value = config
    port.persist_candidate.side_effect = lambda *_args, **_kwargs: operations.append("persist")
    port.install_candidate.side_effect = lambda candidate: (
        operations.append("install"),
        candidate,
    )[1]
    runtime = MagicMock()
    runtime.prepare_reconciliation.side_effect = lambda _candidate: operations.append(
        "prepare-runtime"
    )
    runtime.reconcile = AsyncMock(side_effect=lambda *_args: operations.append("reconcile"))
    runtime.publish_changed = AsyncMock(
        side_effect=lambda *_args, **_kwargs: operations.append("publish")
    )
    await ModelRouting(port, GatewayModelRoutingPolicyPort(), runtime).reset_recommended(
        "tokenrhythm"
    )
    assert operations == ["prepare-runtime", "persist", "install", "reconcile", "publish"]
    port.persist_candidate.assert_called_once()


async def test_guest_cannot_reset_recommended(tmp_path):
    from types import SimpleNamespace

    ctx = RpcContext(
        conn_id="guest", config=legacy_config(config_path=str(tmp_path / "guest.toml"))
    )
    ctx.principal = SimpleNamespace(role="guest", capabilities=("guest",), scopes=frozenset())
    result = await get_dispatcher().dispatch(
        "guest",
        "models.routing.resetRecommended",
        {
            "providerId": "tokenrhythm",
        },
        ctx,
    )
    assert result.error is not None
    assert not (tmp_path / "guest.toml").exists()
