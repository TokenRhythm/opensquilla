from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps.squilla_router import apply_squilla_router
from opensquilla.gateway.config import (
    ROUTER_TIER_PROFILE_IDS,
    SquillaRouterConfig,
    _router_tier_profile_defaults,
)
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.router_control import (
    RouterControlHoldStore,
    RouterControlValidationError,
    build_router_control_targets,
    render_router_control_prompt_block,
    resolve_router_control_target,
)


def _router_cfg(tiers: dict) -> SquillaRouterConfig:
    return SquillaRouterConfig(
        enabled=True,
        rollout_phase="full",
        require_router_runtime=False,
        auto_thinking=False,
        tiers=tiers,
        default_tier="c1" if "c1" in tiers else next(iter(tiers)),
    )


def test_router_control_targets_generalize_to_every_profile() -> None:
    for profile in sorted(ROUTER_TIER_PROFILE_IDS):
        tiers = _router_tier_profile_defaults(profile)
        targets = build_router_control_targets(_router_cfg(tiers))
        target_ids = {target.target_id for target in targets}

        for tier_name, tier_cfg in tiers.items():
            if tier_cfg.get("image_only"):
                assert f"tier:{tier_name}" not in target_ids
                continue
            assert f"tier:{tier_name}" in target_ids


def test_model_targets_are_rejected_by_local_validation() -> None:
    cfg = _router_cfg(
        {
            "c0": {"provider": "openrouter", "model": "same/model", "supports_image": False},
            "c1": {"provider": "openrouter", "model": "other/model", "supports_image": False},
            "c3": {"provider": "openrouter", "model": "same/model", "supports_image": False},
        }
    )

    with pytest.raises(RouterControlValidationError):
        resolve_router_control_target(cfg, "model:same/model")


def test_natural_language_aliases_are_rejected_by_local_validation() -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))

    with pytest.raises(RouterControlValidationError):
        resolve_router_control_target(cfg, "Claude Opus 4.8")


def test_legacy_tier_target_aliases_resolve_to_canonical_routes() -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))

    target = resolve_router_control_target(cfg, "tier:t3")

    assert target.target_id == "tier:c3"
    assert target.tier == "c3"


def test_hold_store_expires_by_explicit_turn_count_and_sliding_idle_time() -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))
    target = resolve_router_control_target(cfg, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold(
        "agent:main:test",
        target,
        evidence="use c3",
        now_monotonic=100.0,
        turns_remaining=1,
        ttl_seconds=10.0,
    )

    first = store.get_valid("agent:main:test", now_monotonic=101.0, decrement=True)
    assert first is not None
    assert first.tier == "c3"
    assert store.get_valid("agent:main:test", now_monotonic=102.0) is None

    store.set_hold(
        "agent:main:test",
        target,
        evidence="use c3 again",
        now_monotonic=100.0,
        ttl_seconds=10.0,
    )
    assert store.get_valid("agent:main:test", now_monotonic=109.0, decrement=True) is not None
    assert store.get_valid("agent:main:test", now_monotonic=118.0) is not None
    assert store.get_valid("agent:main:test", now_monotonic=119.0) is None


def test_hold_store_deepcopy_preserves_session_identity_for_ttl_refresh() -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))
    target = resolve_router_control_target(cfg, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold(
        "agent:main:test",
        target,
        evidence="use c3",
        now_monotonic=100.0,
        ttl_seconds=10.0,
    )

    copied = copy.deepcopy(store)

    assert copied is store
    assert copied.get_valid("agent:main:test", now_monotonic=109.0, decrement=True) is not None
    assert store.get_valid("agent:main:test", now_monotonic=118.0) is not None
    assert store.get_valid("agent:main:test", now_monotonic=119.0) is None


@pytest.mark.asyncio
async def test_squilla_router_refreshes_hold_idle_ttl_through_copied_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))
    target = resolve_router_control_target(cfg, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold(
        "agent:main:test-refresh",
        target,
        evidence="use c3",
        now_monotonic=100.0,
        ttl_seconds=10.0,
    )
    now = [109.0]
    monkeypatch.setattr("opensquilla.router_control.time.monotonic", lambda: now[0])

    metadata = copy.deepcopy({"router_control_hold_store": store})
    ctx = TurnContext(
        message="continue this",
        session_key="agent:main:test-refresh",
        config=SimpleNamespace(squilla_router=cfg),
        provider=None,
        model="default-model",
        tool_defs=[],
        system_prompt="system",
        metadata=metadata,
    )

    out = await apply_squilla_router(ctx)

    assert out.metadata["routing_source"] == "router_control_hold"
    now[0] = 118.0
    assert store.get_valid("agent:main:test-refresh") is not None
    now[0] = 119.0
    assert store.get_valid("agent:main:test-refresh") is None


@pytest.mark.asyncio
async def test_squilla_router_applies_hold_before_normal_classification(monkeypatch) -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))
    target = resolve_router_control_target(cfg, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold("agent:main:test", target, evidence="use c3")

    def fail_strategy(_cfg: object) -> object:
        raise AssertionError("router classification should not run while hold is valid")

    monkeypatch.setattr("opensquilla.engine.steps.squilla_router._get_strategy", fail_strategy)
    ctx = TurnContext(
        message="review this",
        session_key="agent:main:test",
        config=SimpleNamespace(squilla_router=cfg),
        provider=None,
        model="default-model",
        tool_defs=[],
        system_prompt="system",
        metadata={"router_control_hold_store": store},
    )

    out = await apply_squilla_router(ctx)

    assert out.model == "anthropic/claude-opus-4.8"
    assert out.metadata["routing_source"] == "router_control_hold"
    assert out.metadata["router_control_hold_applied"] is True
    assert out.metadata["router_control_target_tier"] == "c3"
    assert [item["tier"] for item in out.metadata["router_fallback_chain"]] == [
        "c2",
        "c1",
        "c0",
    ]


@pytest.mark.asyncio
async def test_large_attachment_clamps_held_tier_to_proven_capacity(monkeypatch) -> None:
    tiers = {
        "c0": {"provider": "openrouter", "model": "held-small"},
        "c3": {"provider": "openrouter", "model": "capacity-large"},
    }
    cfg = _router_cfg(tiers)
    target = resolve_router_control_target(cfg, "tier:c0")
    store = RouterControlHoldStore()
    store.set_hold("agent:main:test-capacity-hold", target, evidence="use c0")
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openrouter/held-small": {
                "context_window": 128_000,
                "max_output_tokens": 10_000,
            },
            "openrouter/capacity-large": {
                "context_window": 300_000,
                "max_output_tokens": 10_000,
            },
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    monkeypatch.setattr(
        "opensquilla.engine.steps.squilla_router._get_strategy",
        lambda _cfg: pytest.fail("a valid hold must bypass classification"),
    )
    ctx = TurnContext(
        message="review the attached archive",
        session_key="agent:main:test-capacity-hold",
        config=SimpleNamespace(squilla_router=cfg),
        provider=None,
        model="default-model",
        tool_defs=[],
        system_prompt="system",
        metadata={
            "router_control_hold_store": store,
            "attachment_material_estimated_tokens": 50_000,
        },
    )

    out = await apply_squilla_router(ctx)

    assert out.metadata["router_control_hold_applied"] is True
    assert out.metadata["router_control_hold_capacity_clamped"] is True
    assert out.metadata["routing_source"] == "large_context_floor"
    assert out.metadata["routed_tier"] == "c3"
    assert out.model == "capacity-large"
    assert out.metadata["router_fallback_chain"] == []


@pytest.mark.asyncio
async def test_large_attachment_foreign_hold_veto_fails_closed(monkeypatch) -> None:
    tiers = {
        "c0": {"provider": "openrouter", "model": "active-small"},
        "c3": {"provider": "foreign", "model": "held-large"},
    }
    cfg = _router_cfg(tiers)
    cfg.tier_provider_mismatch = "veto"
    target = resolve_router_control_target(cfg, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold("agent:main:test-foreign-hold", target, evidence="use c3")
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "foreign/held-large": {
                "context_window": 300_000,
                "max_output_tokens": 10_000,
            }
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    ctx = TurnContext(
        message="review the attached archive",
        session_key="agent:main:test-foreign-hold",
        config=SimpleNamespace(
            squilla_router=cfg,
            llm=SimpleNamespace(
                provider="openrouter",
                context_window_tokens=0,
                max_tokens=0,
            ),
        ),
        provider=None,
        model="active-small",
        tool_defs=[],
        system_prompt="system",
        metadata={
            "router_control_hold_store": store,
            "attachment_material_estimated_tokens": 90_000,
        },
    )

    out = await apply_squilla_router(ctx)

    assert out.metadata["large_context_capacity_blocked"] is True
    assert "routed_tier" not in out.metadata


@pytest.mark.asyncio
async def test_image_attachments_bypass_text_hold(monkeypatch) -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))
    target = resolve_router_control_target(cfg, "tier:c3")
    store = RouterControlHoldStore()
    store.set_hold("agent:main:test-image", target, evidence="use c3")

    def fail_strategy(_cfg: object) -> object:
        raise AssertionError("image route should not classify")

    monkeypatch.setattr("opensquilla.engine.steps.squilla_router._get_strategy", fail_strategy)
    ctx = TurnContext(
        message="what is in this image?",
        session_key="agent:main:test-image",
        config=SimpleNamespace(squilla_router=cfg),
        provider=None,
        model="default-model",
        tool_defs=[],
        system_prompt="system",
        attachments=[{"mime": "image/png"}],
        metadata={"router_control_hold_store": store},
    )

    out = await apply_squilla_router(ctx)

    assert out.metadata["routing_source"] == "image_route"
    assert out.metadata.get("router_control_hold_applied") is not True
    assert out.model == "moonshotai/kimi-k2.6"


def test_prompt_block_contains_canonical_targets_not_aliases() -> None:
    cfg = _router_cfg(_router_tier_profile_defaults("openrouter"))

    block = render_router_control_prompt_block(cfg)

    assert "router_control" in block
    assert "tier:c3" in block
    assert (
        '"target_id":"tier:c3","label":"claude-opus-4.8",'
        '"execution_kind":"single_model"'
    ) in block
    assert "tier:t3" not in block
    assert "model:z-ai/glm-5.2" not in block
    assert "description" not in block
    assert "Use labels only to map explicit model-name requests" in block
    assert "must choose one target_id exactly" in block


def test_ensemble_tier_is_presented_as_virtual_model_without_changing_anchor() -> None:
    cfg = _router_cfg(
        {
            "c0": {
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-flash",
                "supports_image": False,
            },
            "c3": {
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.8",
                "supports_image": False,
                "ensemble_enabled": True,
            },
        }
    )

    target = resolve_router_control_target(cfg, "tier:c3")
    block = render_router_control_prompt_block(cfg)

    assert target.model == "anthropic/claude-opus-4.8"
    assert target.provider == "openrouter"
    assert target.display_label == "multi-model fusion"
    assert target.execution_kind == "ensemble"
    assert (
        '"target_id":"tier:c3","label":"multi-model fusion",'
        '"execution_kind":"ensemble"'
    ) in block
    assert "describe the selected route as multi-model fusion" in block
    assert "internal anchor model" in block


def test_explicit_single_model_c3_keeps_physical_model_label() -> None:
    cfg = _router_cfg(
        {
            "c3": {
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.8",
                "supports_image": False,
                "ensemble_enabled": False,
            }
        }
    )

    target = resolve_router_control_target(cfg, "tier:c3")

    assert target.display_label == "claude-opus-4.8"
    assert target.execution_kind == "single_model"
