from __future__ import annotations

import asyncio
import tomllib
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.gateway import websocket as gateway_websocket
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.model_routing import (
    apply_model_routing_mode,
    capture_model_routing_config,
    model_routing_mode_for_write,
    model_routing_patches,
    model_routing_snapshot,
)
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.rpc_config import (
    _handle_config_apply,
    _handle_config_patch,
    _handle_config_patch_safe,
    _handle_config_set,
)
from opensquilla.gateway.rpc_models import (
    _handle_models_routing_get,
    _handle_models_routing_set,
)
from opensquilla.gateway.rpc_onboarding import (
    _ensemble_configure,
    _router_configure,
)
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES, READ_SCOPE, WRITE_SCOPE
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskRecord
from opensquilla.tools.policy import apply_tool_policy_from_config
from opensquilla.tools.types import ToolContext


def _ctx(config: GatewayConfig) -> RpcContext:
    return RpcContext(conn_id="routing-test", config=config)


def _routing_event_ctx(
    config: GatewayConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RpcContext, list[tuple[str, dict[str, Any]]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    async def send_event(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    subscriber = SimpleNamespace(
        principal=Principal(
            role="operator",
            scopes=frozenset({READ_SCOPE}),
            is_owner=False,
            authenticated=True,
        ),
        send_event=send_event,
    )
    registry = SimpleNamespace(all=lambda: [subscriber])
    monkeypatch.setattr(gateway_websocket, "get_registry", lambda: registry)
    return (
        RpcContext(
            conn_id="routing-events",
            config=config,
            subscription_manager=object(),
            principal=Principal(
                role="operator",
                scopes=frozenset({ADMIN_SCOPE}),
                is_owner=True,
                authenticated=True,
            ),
        ),
        events,
    )


async def _apply_admin_routing_write(
    case: str,
    *,
    enabled: bool,
    config: GatewayConfig,
    ctx: RpcContext,
) -> dict[str, Any]:
    if case == "config.set":
        return await _handle_config_set(
            {"path": "llm_ensemble.enabled", "value": enabled},
            ctx,
        )
    if case == "config.patch":
        return await _handle_config_patch(
            {"patches": {"llm_ensemble.enabled": enabled}},
            ctx,
        )
    if case == "config.apply":
        payload = config.model_dump(mode="python")
        payload["llm_ensemble"]["enabled"] = enabled
        return await _handle_config_apply({"config": payload}, ctx)
    if case == "config.reload":
        assert config.config_path is not None
        with open(config.config_path, "w", encoding="utf-8") as config_file:
            config_file.write(
                "\n".join(
                    [
                        "[llm_ensemble]",
                        f"enabled = {'true' if enabled else 'false'}",
                        "",
                        "[squilla_router]",
                        "enabled = false",
                        'rollout_phase = "observe"',
                    ]
                )
                + "\n"
            )
        result = await get_dispatcher().dispatch(
            "routing-reload",
            "config.reload",
            {},
            ctx,
        )
        assert result.error is None, result.error
        assert isinstance(result.payload, dict)
        return result.payload
    raise AssertionError(f"unknown config write case: {case}")


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (GatewayConfig(squilla_router={"enabled": False}), "direct"),
        (
            GatewayConfig(
                squilla_router={"enabled": True, "rollout_phase": "observe"}
            ),
            "direct",
        ),
        (GatewayConfig(), "router"),
        (GatewayConfig(llm_ensemble={"enabled": True}), "ensemble"),
    ],
)
def test_model_routing_snapshot_maps_config_to_one_public_mode(
    config: GatewayConfig,
    expected: str,
) -> None:
    assert model_routing_snapshot(config)["mode"] == expected


@pytest.mark.parametrize(
    ("selection_mode", "router_enabled"),
    [
        ("static_openrouter_b5", False),
        ("static_tokenrhythm_b5", False),
        ("custom_b5", False),
        ("router_dynamic", True),
        ("future_mode", True),
    ],
)
def test_ensemble_patch_preserves_router_dependency_compatibility(
    selection_mode: str,
    router_enabled: bool,
) -> None:
    # Use a config-like object so the compatibility branch remains covered for
    # older/future selection tokens that the current Pydantic model rejects.
    config = SimpleNamespace(
        llm_ensemble=SimpleNamespace(selection_mode=selection_mode)
    )
    patches = model_routing_patches(config, "ensemble")
    assert patches["squilla_router.enabled"] is router_enabled
    assert patches["llm_ensemble.enabled"] is True
    assert patches["squilla_router.rollout_phase"] == "full"


async def test_models_routing_set_persists_and_returns_canonical_snapshot(tmp_path) -> None:
    path = tmp_path / "config.toml"
    config = GatewayConfig(config_path=str(path))

    result = await _handle_models_routing_set({"mode": "direct"}, _ctx(config))

    assert result["mode"] == "direct"
    assert result["router_enabled"] is False
    assert result["ensemble_enabled"] is False
    assert result["restart_required"] is False
    reloaded = GatewayConfig.load(str(path))
    assert model_routing_snapshot(reloaded)["mode"] == "direct"
    persisted = tomllib.loads(path.read_text())
    assert persisted["squilla_router"]["enabled"] is False


async def test_models_routing_get_is_read_only() -> None:
    config = GatewayConfig(llm_ensemble={"enabled": True})
    before = config.model_dump()

    result = await _handle_models_routing_get(None, _ctx(config))

    assert result["mode"] == "ensemble"
    assert config.model_dump() == before


async def test_models_routing_set_rejects_unknown_mode(tmp_path) -> None:
    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    with pytest.raises(ValueError, match="direct, router, or ensemble"):
        await _handle_models_routing_set({"mode": "automatic"}, _ctx(config))


def test_models_routing_rpc_scopes_are_explicit() -> None:
    assert METHOD_SCOPES["models.routing.get"] == READ_SCOPE
    assert METHOD_SCOPES["models.routing.set"] == WRITE_SCOPE


async def test_onboarding_router_configure_broadcasts_one_canonical_change(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "router.toml"),
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
        squilla_router={"enabled": False, "rollout_phase": "observe"},
    )
    ctx, events = _routing_event_ctx(config, monkeypatch)

    await _router_configure({"mode": "recommended"}, ctx)

    assert config.llm_ensemble.enabled is False
    assert config.squilla_router.enabled is True
    assert config.squilla_router.rollout_phase == "full"

    assert events == [
        (
            "models.routing.changed",
            {
                **model_routing_snapshot(config),
                "source": "onboarding.router.configure",
            },
        )
    ]


async def test_router_configure_ladder_maintenance_preserves_observe_rollout(
    tmp_path,
) -> None:
    """A tier/default-tier save on an already-enabled router is maintenance,
    not a strategy switch: it must not escalate an operator's shadow
    ``rollout_phase='observe'`` to live ``'full'`` routing."""
    path = tmp_path / "router-maintenance.toml"
    config = GatewayConfig(
        config_path=str(path),
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": True, "rollout_phase": "observe"},
    )

    await _router_configure({"mode": "recommended", "defaultTier": "c2"}, _ctx(config))

    assert config.squilla_router.enabled is True
    assert config.squilla_router.default_tier == "c2"
    assert config.squilla_router.rollout_phase == "observe"
    reloaded = GatewayConfig.load(str(path))
    assert reloaded.squilla_router.rollout_phase == "observe"


async def test_router_configure_ladder_maintenance_keeps_live_ensemble(
    tmp_path,
) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "router-ensemble-keep.toml"),
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
        squilla_router={"enabled": True, "rollout_phase": "full"},
    )

    await _router_configure({"mode": "recommended", "defaultTier": "c1"}, _ctx(config))

    assert config.llm_ensemble.enabled is True
    assert config.squilla_router.enabled is True
    assert model_routing_snapshot(config)["mode"] == "ensemble"


async def test_onboarding_ensemble_configure_broadcasts_one_canonical_change(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "ensemble.toml"),
        llm_ensemble={"enabled": False, "selection_mode": "router_dynamic"},
        squilla_router={"enabled": False, "rollout_phase": "observe"},
    )
    ctx, events = _routing_event_ctx(config, monkeypatch)

    await _ensemble_configure({"enabled": True}, ctx)

    assert config.llm_ensemble.enabled is True
    assert config.squilla_router.enabled is True
    assert config.squilla_router.rollout_phase == "full"
    reloaded = GatewayConfig.load(str(tmp_path / "ensemble.toml"))
    assert model_routing_snapshot(reloaded)["mode"] == "ensemble"
    assert reloaded.squilla_router.enabled is True
    assert reloaded.squilla_router.rollout_phase == "full"

    assert events == [
        (
            "models.routing.changed",
            {
                **model_routing_snapshot(config),
                "source": "onboarding.ensemble.configure",
            },
        )
    ]


async def test_models_routing_set_reuses_safe_patch_broadcast_exactly_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GatewayConfig(config_path=str(tmp_path / "routing-set.toml"))
    ctx, events = _routing_event_ctx(config, monkeypatch)

    await _handle_models_routing_set({"mode": "direct"}, ctx)

    assert events == [
        (
            "models.routing.changed",
            {
                **model_routing_snapshot(config),
                "source": "config.patch.safe",
            },
        )
    ]


@pytest.mark.parametrize(
    "case",
    ["config.set", "config.patch", "config.apply", "config.reload"],
)
async def test_admin_config_hot_apply_broadcasts_one_canonical_routing_change(
    case: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / f"{case}.toml"),
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": False, "rollout_phase": "observe"},
    )
    ctx, events = _routing_event_ctx(config, monkeypatch)

    response = await _apply_admin_routing_write(
        case,
        enabled=True,
        config=config,
        ctx=ctx,
    )

    expected = {
        **model_routing_snapshot(config),
        "source": case,
    }
    assert events == [("models.routing.changed", expected)]
    assert response["model_routing"] == expected


@pytest.mark.parametrize(
    "case",
    ["config.set", "config.patch", "config.apply", "config.reload"],
)
async def test_admin_config_hot_apply_does_not_broadcast_unchanged_routing(
    case: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / f"noop-{case}.toml"),
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": False, "rollout_phase": "observe"},
    )
    ctx, events = _routing_event_ctx(config, monkeypatch)

    response = await _apply_admin_routing_write(
        case,
        enabled=False,
        config=config,
        ctx=ctx,
    )

    assert events == []
    assert "model_routing" not in response


async def test_safe_patch_noop_does_not_broadcast_model_routing_change(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "safe-noop.toml"),
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": False, "rollout_phase": "observe"},
    )
    ctx, events = _routing_event_ctx(config, monkeypatch)

    response = await _handle_config_patch_safe(
        {"patches": {"llm_ensemble.enabled": False}},
        ctx,
    )

    assert events == []
    assert "model_routing" not in response


async def test_unrelated_router_patch_preserves_existing_rollout_fields(tmp_path) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "router-detail.toml"),
        llm_ensemble={"enabled": False},
        squilla_router={
            "enabled": True,
            "rollout_phase": "observe",
            "default_tier": "c1",
        },
    )

    await _handle_config_patch(
        {"patch": {"squilla_router": {"default_tier": "c2"}}},
        _ctx(config),
    )

    assert config.squilla_router.default_tier == "c2"
    assert config.squilla_router.enabled is True
    assert config.squilla_router.rollout_phase == "observe"


async def test_legacy_safe_patch_ensemble_enable_repairs_router_dependency_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "legacy-safe.toml"),
        llm_ensemble={"enabled": False, "selection_mode": "router_dynamic"},
        squilla_router={"enabled": False, "rollout_phase": "observe"},
    )
    ctx, events = _routing_event_ctx(config, monkeypatch)

    response = await _handle_config_patch_safe(
        {"patches": {"llm_ensemble.enabled": True}},
        ctx,
    )

    assert response["patched"] == ["llm_ensemble.enabled"]
    assert config.llm_ensemble.enabled is True
    assert config.squilla_router.enabled is True
    assert config.squilla_router.rollout_phase == "full"
    expected = {
        **model_routing_snapshot(config),
        "source": "config.patch.safe",
    }
    assert events == [("models.routing.changed", expected)]
    assert response["model_routing"] == expected

    reloaded = GatewayConfig.load(str(tmp_path / "legacy-safe.toml"))
    assert model_routing_snapshot(reloaded)["mode"] == "ensemble"
    assert reloaded.squilla_router.enabled is True
    assert reloaded.squilla_router.rollout_phase == "full"


async def test_reasserting_ensemble_disabled_preserves_active_router(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value-identical ``llm_ensemble.enabled=false`` re-save (a settings
    form that always sends its fields, a repeated CLI disable) must not
    silently drop an active Router back to direct mode."""
    config = GatewayConfig(
        config_path=str(tmp_path / "reassert-ensemble-off.toml"),
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": True, "rollout_phase": "full"},
    )
    ctx, events = _routing_event_ctx(config, monkeypatch)

    await _handle_config_set({"path": "llm_ensemble.enabled", "value": False}, ctx)

    assert config.squilla_router.enabled is True
    assert config.squilla_router.rollout_phase == "full"
    assert model_routing_snapshot(config)["mode"] == "router"
    assert events == []


async def test_reasserting_ensemble_disabled_preserves_prompt_only_router(
    tmp_path,
) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "reassert-prompt-only.toml"),
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": True, "rollout_phase": "prompt_only"},
    )

    await _handle_config_patch_safe(
        {"patches": {"llm_ensemble.enabled": False}},
        _ctx(config),
    )

    assert config.squilla_router.enabled is True
    assert config.squilla_router.rollout_phase == "prompt_only"


async def test_reasserting_router_enabled_preserves_observe_phase(tmp_path) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "reassert-router-on.toml"),
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": True, "rollout_phase": "observe"},
    )

    await _handle_config_set({"path": "squilla_router.enabled", "value": True}, _ctx(config))

    assert config.squilla_router.enabled is True
    assert config.squilla_router.rollout_phase == "observe"


async def test_onboarding_ensemble_disable_reassert_preserves_active_router(
    tmp_path,
) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "onboard-ensemble-reassert.toml"),
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": True, "rollout_phase": "full"},
    )

    await _ensemble_configure({"enabled": False}, _ctx(config))

    assert config.squilla_router.enabled is True
    assert config.squilla_router.rollout_phase == "full"
    assert model_routing_snapshot(config)["mode"] == "router"


async def test_genuine_ensemble_disable_still_reconciles_to_direct(tmp_path) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "genuine-ensemble-off.toml"),
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
        squilla_router={"enabled": True, "rollout_phase": "full"},
    )

    await _handle_config_set({"path": "llm_ensemble.enabled", "value": False}, _ctx(config))

    assert config.llm_ensemble.enabled is False
    assert config.squilla_router.enabled is False
    assert config.squilla_router.rollout_phase == "observe"
    assert model_routing_snapshot(config)["mode"] == "direct"


def test_unchanged_boolean_writes_select_no_mode_with_previous_snapshot() -> None:
    previous = GatewayConfig(
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": True, "rollout_phase": "observe"},
    )
    config = GatewayConfig(
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": True, "rollout_phase": "observe"},
    )

    assert (
        model_routing_mode_for_write(
            config, {"llm_ensemble.enabled"}, previous=previous
        )
        is None
    )
    assert (
        model_routing_mode_for_write(
            config,
            {"llm_ensemble.enabled", "squilla_router.enabled"},
            previous=previous,
        )
        is None
    )
    # A genuine toggle in a pair write still resolves from final values.
    config.llm_ensemble.enabled = True
    assert (
        model_routing_mode_for_write(
            config,
            {"llm_ensemble.enabled", "squilla_router.enabled"},
            previous=previous,
        )
        == "ensemble"
    )


def test_legacy_single_boolean_writes_select_one_canonical_mode() -> None:
    config = GatewayConfig(
        llm_ensemble={"enabled": False, "selection_mode": "router_dynamic"},
        squilla_router={"enabled": False, "rollout_phase": "observe"},
    )
    config.llm_ensemble.enabled = True

    mode = model_routing_mode_for_write(config, {"llm_ensemble.enabled"})

    assert mode == "ensemble"
    assert apply_model_routing_mode(config, mode) == {
        "squilla_router.enabled": True,
        "squilla_router.rollout_phase": "full",
    }
    assert model_routing_snapshot(config)["mode"] == "ensemble"


@pytest.mark.parametrize(
    ("ensemble_enabled", "router_enabled", "selection_mode", "expected_mode", "expected_router"),
    [
        (True, True, "static_openrouter_b5", "ensemble", False),
        (True, False, "router_dynamic", "ensemble", True),
        (False, True, "router_dynamic", "router", True),
        (False, False, "router_dynamic", "direct", False),
    ],
)
def test_complete_legacy_boolean_matrix_has_explicit_mode_precedence(
    ensemble_enabled: bool,
    router_enabled: bool,
    selection_mode: str,
    expected_mode: str,
    expected_router: bool,
) -> None:
    config = GatewayConfig(
        llm_ensemble={
            "enabled": ensemble_enabled,
            "selection_mode": selection_mode,
        },
        squilla_router={
            "enabled": router_enabled,
            # The explicit booleans, not an old advanced phase, choose mode.
            "rollout_phase": "observe",
        },
    )

    mode = model_routing_mode_for_write(
        config,
        {"llm_ensemble.enabled", "squilla_router.enabled"},
    )
    assert mode == expected_mode
    apply_model_routing_mode(config, mode)

    assert model_routing_snapshot(config)["mode"] == expected_mode
    assert config.squilla_router.enabled is expected_router
    assert config.squilla_router.rollout_phase == (
        "observe" if expected_mode == "direct" else "full"
    )


def test_non_control_router_settings_do_not_select_a_model_routing_mode() -> None:
    config = GatewayConfig(
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": False, "rollout_phase": "observe"},
    )

    assert (
        model_routing_mode_for_write(
            config,
            {
                "squilla_router",
                "squilla_router.default_tier",
                "squilla_router.rollout_phase",
            },
        )
        is None
    )


@pytest.mark.parametrize("phase", ["prompt_only", "observe"])
@pytest.mark.parametrize("writer", ["config.set", "config.patch.safe"])
async def test_advanced_rollout_phase_writes_round_trip_without_normalization(
    phase: str,
    writer: str,
    tmp_path,
) -> None:
    path = tmp_path / f"{writer}-{phase}.toml"
    config = GatewayConfig(
        config_path=str(path),
        llm_ensemble={"enabled": False},
        squilla_router={"enabled": True, "rollout_phase": "full"},
    )
    if writer == "config.set":
        await _handle_config_set(
            {"path": "squilla_router.rollout_phase", "value": phase},
            _ctx(config),
        )
    else:
        await _handle_config_patch_safe(
            {"patches": {"squilla_router.rollout_phase": phase}},
            _ctx(config),
        )

    assert config.squilla_router.enabled is True
    assert config.squilla_router.rollout_phase == phase
    reloaded = GatewayConfig.load(str(path))
    assert reloaded.squilla_router.enabled is True
    assert reloaded.squilla_router.rollout_phase == phase


async def test_live_ensemble_selection_change_preserves_prompt_only_phase(
    tmp_path,
) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "selection-mode.toml"),
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
        squilla_router={"enabled": True, "rollout_phase": "prompt_only"},
    )

    await _handle_config_set(
        {
            "path": "llm_ensemble.selection_mode",
            "value": "static_openrouter_b5",
        },
        _ctx(config),
    )

    assert config.llm_ensemble.enabled is True
    assert config.squilla_router.enabled is False
    assert config.squilla_router.rollout_phase == "prompt_only"


def test_capture_model_routing_config_isolated_from_live_control_writes() -> None:
    config = GatewayConfig(squilla_router={"enabled": False, "rollout_phase": "observe"})

    accepted = capture_model_routing_config(config)
    config.llm_ensemble.enabled = True
    config.squilla_router.enabled = True
    config.squilla_router.rollout_phase = "full"

    assert model_routing_snapshot(accepted)["mode"] == "direct"
    assert model_routing_snapshot(config)["mode"] == "ensemble"
    assert not hasattr(accepted, "tools")


async def test_task_runtime_freezes_strategy_but_uses_live_tool_policy() -> None:
    records: dict[str, AgentTaskRecord] = {}
    storage = MagicMock()

    async def create(record: AgentTaskRecord) -> None:
        records[record.task_id] = record

    async def update(task_id: str, **values: Any) -> None:
        record = records[task_id]
        for key, value in values.items():
            if hasattr(record, key):
                object.__setattr__(record, key, value)

    async def get(task_id: str) -> AgentTaskRecord | None:
        return records.get(task_id)

    async def list_tasks(**_kwargs: Any) -> list[AgentTaskRecord]:
        return list(records.values())

    storage.create_agent_task = create
    storage.update_agent_task = update
    storage.get_agent_task = get
    storage.list_agent_tasks = list_tasks

    config = GatewayConfig(
        squilla_router={"enabled": False, "rollout_phase": "observe"},
        tools={"deny": []},
    )
    observed: list[tuple[str, bool]] = []
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()

    from opensquilla.engine.runtime import TurnRunner, accepted_turn_config_scope

    runner = TurnRunner.__new__(TurnRunner)
    runner._config = config

    async def handler(run: Any) -> None:
        if run.message == "blocker":
            blocker_started.set()
            await release_blocker.wait()
            return
        with accepted_turn_config_scope(run.accepted_config):
            turn_config = runner._turn_config()
            tool_context = apply_tool_policy_from_config(
                ToolContext(),
                available_tools=["exec_command"],
                config=turn_config,
            )
            observed.append(
                (
                    model_routing_snapshot(turn_config)["mode"],
                    "exec_command" in tool_context.denied_tools,
                )
            )

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        accepted_config_provider=lambda: capture_model_routing_config(config),
        max_concurrency=1,
    )
    envelope = RouteEnvelope(
        source_kind=SourceKind.CLI,
        source_name="tui",
        agent_id="main",
        session_key="agent:main:routing-acceptance",
        input_provenance={"kind": "test"},
    )

    blocker = await runtime.enqueue(envelope, "blocker")
    await asyncio.wait_for(blocker_started.wait(), timeout=2.0)
    first = await runtime.enqueue(envelope, "first")
    # Hot apply replaces top-level policy submodels in place.  The already
    # accepted routing mode stays direct, but the queued turn must see this
    # newly revoked tool when it begins execution.
    config.tools = config.tools.model_copy(update={"deny": ["exec_command"]})
    config.llm_ensemble.enabled = True
    config.squilla_router.enabled = True
    config.squilla_router.rollout_phase = "full"
    release_blocker.set()
    await runtime.wait(blocker.task_id, timeout=2.0)
    await runtime.wait(first.task_id, timeout=2.0)

    second = await runtime.enqueue(envelope, "second")
    await runtime.wait(second.task_id, timeout=2.0)

    assert observed == [("direct", True), ("ensemble", True)]
