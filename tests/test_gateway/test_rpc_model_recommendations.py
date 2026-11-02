from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import tomli_w

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.model_routing import model_routing_snapshot
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.rpc_models import (
    _handle_models_recommendations_acknowledge,
    _handle_models_recommendations_apply,
    _handle_models_recommendations_get,
)
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES, READ_SCOPE, WRITE_SCOPE
from opensquilla.onboarding.router_policy import PrimaryProviderChangedError
from opensquilla.provider import recommendations


@pytest.fixture
def config(tmp_path: Path) -> GatewayConfig:
    return GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        state_dir=str(tmp_path / "state"),
        llm={"provider": "tokenrhythm", "model": "synthetic-primary"},
        squilla_router={"enabled": False, "rollout_phase": "observe"},
        llm_ensemble={"enabled": False, "selection_mode": "static_tokenrhythm_b5"},
    )


@pytest.fixture
def plans(config: GatewayConfig, monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], Any]:
    result = {}
    for provider in ("openrouter", "tokenrhythm"):
        for kind in ("router", "ensemble"):
            plan = recommendations.get_recommendation(config, provider, kind)
            plan["source"] = "remote"
            plan["revision"] = "a" * 64
            for index, row in enumerate(plan["rows"]):
                row["model"] = f"synthetic/{kind}-{index}"
            result[provider, kind] = plan
    monkeypatch.setattr(
        recommendations,
        "get_recommendation",
        lambda _config, provider, kind: copy.deepcopy(result[provider, kind]),
    )
    return result


def _ctx(config: GatewayConfig, scope: str = ADMIN_SCOPE) -> RpcContext:
    return RpcContext(
        conn_id="recommendations-test",
        config=config,
        principal=Principal(
            role="operator", scopes=frozenset({scope}), is_owner=False, authenticated=True,
        ),
    )


def _params(kind: str = "router", **overrides: Any) -> dict[str, Any]:
    return {"providerId": "tokenrhythm", "kind": kind, "revision": "a" * 64, **overrides}


async def test_get_starts_background_refresh_without_changing_user_config(
    config: GatewayConfig, plans: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        recommendations, "start_background_recommendation_refresh",
        lambda **kwargs: calls.append(kwargs["config"]),
    )
    before = config.model_dump(mode="python")

    result = await _handle_models_recommendations_get({}, _ctx(config))

    assert calls == [config]
    assert len(result["plans"]) == 4
    assert all(plan["hasUpdate"] for plan in result["plans"])
    assert all(not plan["notified"] for plan in result["plans"])
    assert config.model_dump(mode="python") == before
    assert not Path(config.config_path).exists()


async def test_get_compares_canonical_rows_and_never_announces_bundled_changes(
    config: GatewayConfig, plans: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.onboarding.recommendation_policy.current_recommendation_rows",
        lambda _config, provider, kind: list(reversed(plans[provider, kind]["rows"])),
    )
    result = await _handle_models_recommendations_get({}, _ctx(config))
    assert all(not plan["hasUpdate"] for plan in result["plans"])
    for plan in plans.values():
        plan["source"] = "bundled"
    monkeypatch.setattr(
        "opensquilla.onboarding.recommendation_policy.current_recommendation_rows",
        lambda *_args: [],
    )
    result = await _handle_models_recommendations_get({}, _ctx(config))
    assert all(not plan["hasUpdate"] for plan in result["plans"])


@pytest.mark.parametrize("kind", ["router", "ensemble"])
@pytest.mark.parametrize("mode", ["direct", "router", "ensemble"])
async def test_apply_persists_only_selected_plan_and_preserves_mode(
    config: GatewayConfig, plans: Any, kind: str, mode: str,
) -> None:
    from opensquilla.onboarding.recommendation_policy import current_recommendation_rows

    config.squilla_router.enabled = mode == "router"
    config.squilla_router.rollout_phase = "full" if mode == "router" else "observe"
    config.llm_ensemble.enabled = mode == "ensemble"
    untouched = "llm_ensemble" if kind == "router" else "squilla_router"
    before = getattr(config, untouched).model_dump(mode="python")
    primary = config.llm.model_dump(mode="python")
    ctx = _ctx(config)

    result = await _handle_models_recommendations_apply(_params(kind), ctx)

    assert result["mode"] == mode
    assert result["restart_required"] is False
    assert getattr(ctx.config, untouched).model_dump(mode="python") == before
    assert ctx.config.llm.model_dump(mode="python") == primary
    assert current_recommendation_rows(ctx.config, "tokenrhythm", kind) == plans[
        "tokenrhythm", kind
    ]["rows"]
    reloaded = GatewayConfig.load(config.config_path)
    assert model_routing_snapshot(reloaded)["mode"] == mode
    assert current_recommendation_rows(reloaded, "tokenrhythm", kind) == plans[
        "tokenrhythm", kind
    ]["rows"]


@pytest.mark.parametrize("kind", ["router", "ensemble"])
async def test_apply_with_account_selected_preserves_manual_connection_after_reload(
    config: GatewayConfig, plans: Any, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    from opensquilla import desktop_account as bridge
    from opensquilla.gateway.llm_runtime import resolve_llm_credential, resolve_llm_runtime_config

    saved = config.to_toml_dict()
    saved.setdefault("llm", {}).update(
        provider="tokenrhythm", api_key="synthetic-manual-key",
        base_url="https://manual.invalid/v1",
    )
    Path(config.config_path).write_text(tomli_w.dumps(saved), encoding="utf-8")
    config = GatewayConfig.load(config.config_path)
    before = config.llm.model_dump(mode="python")
    lease = "os_local_" + "a" * 43
    account_state = {
        "source": "account", "ready": True, "credential": lease,
        "accountId": "synthetic-account", "selectedAccountId": "synthetic-account",
    }
    monkeypatch.setattr(bridge, "_endpoint", "")
    monkeypatch.setattr(bridge, "_token", "")
    pinned = bridge.pin_account_state(account_state)
    try:
        ctx = _ctx(config)
        await _handle_models_recommendations_apply(_params(kind), ctx)

        assert ctx.config.llm.model_dump(mode="python") == before
        assert bridge.account_state() == account_state
        persisted = Path(config.config_path).read_text(encoding="utf-8")
        assert lease not in persisted
        reloaded = GatewayConfig.load(config.config_path)
        assert reloaded.llm.provider == "tokenrhythm"
        runtime = resolve_llm_runtime_config(reloaded)
        assert runtime.api_key == lease
        assert runtime.base_url == bridge.OFFICIAL_BASE
        assert resolve_llm_credential(reloaded).source == "account"
        with bridge.manual_credential_scope():
            runtime = resolve_llm_runtime_config(reloaded)
            assert runtime.api_key == "synthetic-manual-key"
            assert runtime.base_url == "https://manual.invalid/v1"
            assert resolve_llm_credential(reloaded).source == "explicit"
        assert bridge.account_state() == account_state
    finally:
        bridge.reset_account_state(pinned)


@pytest.mark.parametrize("kind", ["router", "ensemble"])
async def test_apply_rejects_changed_revision_without_saving(
    config: GatewayConfig, plans: Any, kind: str,
) -> None:
    before = config.model_dump(mode="python")
    with pytest.raises(RpcHandlerError) as error:
        await _handle_models_recommendations_apply(_params(kind, revision="b" * 64), _ctx(config))
    assert error.value.code == "CONFLICT"
    assert config.model_dump(mode="python") == before
    assert not Path(config.config_path).exists()


async def test_stale_router_provider_is_rejected_but_ensemble_can_use_other_provider(
    config: GatewayConfig, plans: Any,
) -> None:
    with pytest.raises(PrimaryProviderChangedError):
        await _handle_models_recommendations_apply(
            _params(providerId="openrouter"), _ctx(config),
        )
    assert not Path(config.config_path).exists()
    result = await _handle_models_recommendations_apply(
        _params("ensemble", providerId="openrouter"), _ctx(config),
    )
    assert result["mode"] == "direct"
    assert config.llm.provider == "tokenrhythm"


async def test_apply_persist_failure_leaves_live_config_and_runtime_unchanged(
    config: GatewayConfig, plans: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = config.model_dump(mode="python")
    reconciled = []
    ctx = _ctx(config)
    ctx.provider_selector = SimpleNamespace(sync_primary=lambda value: reconciled.append(value))

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("opensquilla.onboarding.config_store.persist_config", fail)
    with pytest.raises(OSError, match="synthetic disk failure"):
        await _handle_models_recommendations_apply(_params(), ctx)
    assert config.model_dump(mode="python") == before
    assert reconciled == []


async def test_acknowledge_changes_only_notification_state_and_rejects_old_revision(
    config: GatewayConfig, plans: Any,
) -> None:
    before = config.model_dump(mode="python")
    result = await _handle_models_recommendations_acknowledge(_params(), _ctx(config))
    assert result == {"acknowledged": True}
    assert recommendations.recommendation_notified(config, "tokenrhythm", "router", "a" * 64)
    assert not recommendations.recommendation_notified(config, "tokenrhythm", "ensemble", "a" * 64)
    assert config.model_dump(mode="python") == before
    assert not Path(config.config_path).exists()
    with pytest.raises(RpcHandlerError) as error:
        await _handle_models_recommendations_acknowledge(
            _params(revision="b" * 64), _ctx(config),
        )
    assert error.value.code == "CONFLICT"


@pytest.mark.parametrize(
    ("action", "allowed", "denied"),
    [("get", READ_SCOPE, None), ("apply", ADMIN_SCOPE, WRITE_SCOPE),
     ("acknowledge", WRITE_SCOPE, READ_SCOPE)],
)
async def test_recommendation_permissions_are_explicit_and_enforced(
    config: GatewayConfig, plans: Any, action: str, allowed: str, denied: str | None,
) -> None:
    method = f"models.recommendations.{action}"
    assert METHOD_SCOPES[method] == allowed
    params = {} if action == "get" else _params()
    if denied is not None:
        response = await get_dispatcher().dispatch("denied", method, params, _ctx(config, denied))
        assert response.error.code == "UNAUTHORIZED"
        assert not Path(config.config_path).exists()
    response = await get_dispatcher().dispatch("allowed", method, params, _ctx(config, allowed))
    assert response.error is None


@pytest.mark.parametrize(
    "params",
    [None, {}, _params(providerId="openai"), _params(kind="direct"), _params(revision=5),
     _params(revision=" "), _params(model="client-injected-model")],
)
async def test_apply_rejects_invalid_or_injected_params(
    config: GatewayConfig, params: Any,
) -> None:
    response = await get_dispatcher().dispatch(
        "invalid", "models.recommendations.apply", params, _ctx(config),
    )
    assert response.error.code == "INVALID_REQUEST"
    assert not Path(config.config_path).exists()
