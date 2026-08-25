from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.route_plan import pin_route_plan
from opensquilla.engine.router_decision import build_router_decision_event
from opensquilla.provider.types import ModelCapabilities


def _turn() -> TurnContext:
    return TurnContext(
        message="original request",
        session_key="agent:main:route-plan",
        config=None,
        provider=None,
        model="routed/model",
        tool_defs=[],
        system_prompt="system",
        metadata={
            "routed_tier": "c2",
            "routed_provider": "provider-a",
            "routed_model": "routed/model",
            "routing_source": "classifier",
            "routing_applied": True,
            "thinking_level": "high",
            "prompt_policy": "P2",
            "router_fallback_chain": [
                {
                    "tier": "c1",
                    "provider": "provider-a",
                    "model": "fallback/model",
                }
            ],
        },
    )


def test_route_plan_is_pinned_once_with_capability_snapshot() -> None:
    turn = _turn()
    first = pin_route_plan(
        turn,
        turn_id="turn-1",
        provider="provider-a",
        model="routed/model",
        context_window=128_000,
        capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=False,
            reasoning_format="openrouter",
        ),
        effective_thinking=True,
        fallback_capabilities={
            ("provider-a", "fallback/model"): (
                32_000,
                8_192,
                ModelCapabilities(supports_tools=False),
            ),
        },
    )
    assert first is not None
    assert first.as_dict() == turn.metadata["route_plan"]
    assert first.fallback_chain[0].model == "fallback/model"
    assert first.fallback_chain[0].capabilities.context_window == 32_000
    assert first.fallback_chain[0].capabilities.effective_max_tokens == 8_192
    assert first.fallback_chain[0].capabilities.supports_tools is False
    assert first.capabilities.context_window == 128_000
    assert first.capabilities.effective_max_tokens == 0
    assert first.capabilities.supports_reasoning is True
    assert first.version == 2
    assert first.router_tier_snapshot is None

    turn.metadata["routed_model"] = "must-not-replace-the-plan"
    second = pin_route_plan(
        turn,
        turn_id="turn-1",
        provider="provider-b",
        model="another/model",
        context_window=1,
        capabilities=None,
        effective_thinking=False,
    )
    assert second is first
    assert second.model == "routed/model"
    with pytest.raises(FrozenInstanceError):
        second.model = "mutated"  # type: ignore[misc]


def test_router_event_uses_pinned_plan_not_mutable_execution_metadata() -> None:
    turn = _turn()
    plan = pin_route_plan(
        turn,
        turn_id="turn-2",
        provider="provider-a",
        model="routed/model",
        context_window=64_000,
        capabilities=ModelCapabilities(),
        effective_thinking=False,
    )
    assert plan is not None

    turn.metadata["routed_model"] = "provider-fallback/model"
    turn.metadata["routing_source"] = "fallback"
    event = build_router_decision_event(turn)

    assert event is not None
    assert event.model == "routed/model"
    assert event.source == "classifier"
    assert event.fallback is False
    assert event.context_window == 64_000


def test_route_plan_adds_deduplicated_selector_execution_candidates() -> None:
    turn = _turn()
    turn.metadata["selector_execution_chain"] = [
        {
            "provider": "provider-b",
            "model": "routed/model",
        },
        {
            "provider": "provider-a",
            "model": "fallback/model",
        },
        {
            "provider": "provider-b",
            "model": "configured/fallback",
        },
    ]

    plan = pin_route_plan(
        turn,
        turn_id="turn-3",
        provider="provider-b",
        model="routed/model",
        context_window=64_000,
        capabilities=ModelCapabilities(supports_tools=True),
        effective_thinking=False,
        fallback_capabilities={
            ("provider-a", "fallback/model"): (
                32_000,
                ModelCapabilities(supports_tools=True),
            ),
            ("provider-b", "routed/model"): (
                64_000,
                ModelCapabilities(supports_tools=True),
            ),
            ("provider-b", "configured/fallback"): (
                128_000,
                ModelCapabilities(supports_tools=True),
            ),
        },
    )

    assert plan is not None
    assert [
        (item.provider, item.model)
        for item in plan.fallback_chain
    ] == [
        ("provider-a", "fallback/model"),
        ("provider-b", "routed/model"),
        ("provider-b", "configured/fallback"),
    ]
    assert plan.fallback_chain[-1].capabilities.context_window == 128_000
    assert plan.fallback_chain[-1].capabilities.supports_tools is True


def test_route_plan_freezes_text_candidates_aliases_ensemble_and_winner() -> None:
    turn = _turn()
    turn.config = SimpleNamespace(
        squilla_router=SimpleNamespace(
            tiers={
                "t0": {"provider": "provider-a", "model": "fast/model"},
                "t1": {"provider": "provider-b", "model": "balanced/model"},
                "c2": {"provider": "provider-c", "model": ""},
                "c3": {
                    "provider": "provider-d",
                    "model": "quality/model",
                    "ensemble_enabled": True,
                },
                "image_model": {
                    "provider": "provider-image",
                    "model": "image/model",
                    "supports_image": True,
                    "image_only": True,
                },
            }
        ),
        llm_ensemble=SimpleNamespace(
            enabled=False,
            selection_mode="custom_b5",
            model_fields_set={"selection_mode"},
        ),
    )

    plan = pin_route_plan(
        turn,
        turn_id="turn-snapshot-text",
        provider="provider-c",
        model="routed/model",
        context_window=64_000,
        capabilities=ModelCapabilities(),
        effective_thinking=False,
    )

    assert plan is not None
    snapshot = plan.as_dict()["router_tier_snapshot"]
    assert snapshot == {
        "version": 1,
        "request_kind": "text",
        "tiers": [
            {
                "tier": "c0",
                "provider": "provider-a",
                "model": "fast/model",
                "execution_kind": "single_model",
            },
            {
                "tier": "c1",
                "provider": "provider-b",
                "model": "balanced/model",
                "execution_kind": "single_model",
            },
            {
                "tier": "c2",
                "provider": "provider-a",
                "model": "routed/model",
                "execution_kind": "single_model",
            },
            {
                "tier": "c3",
                "provider": "provider-d",
                "model": "quality/model",
                "execution_kind": "ensemble",
            },
        ],
    }
    assert turn.metadata["route_plan"]["router_tier_snapshot"] == snapshot

    turn.config.squilla_router.tiers["t0"]["model"] = "changed/model"
    assert plan.as_dict()["router_tier_snapshot"] == snapshot
    event = build_router_decision_event(turn)
    assert event is not None
    assert event.router_tier_snapshot == snapshot


def test_route_plan_freezes_only_executable_image_candidates() -> None:
    turn = _turn()
    turn.metadata.update(
        {
            "routed_tier": "image_model",
            "routed_provider": "image-provider",
            "routed_model": "image/winner",
            "routing_source": "image_route",
        }
    )
    turn.config = SimpleNamespace(
        squilla_router=SimpleNamespace(
            tiers={
                "c0": {
                    "provider": "vision-provider",
                    "model": "vision/fallback",
                    "supports_image": True,
                },
                "c1": {"provider": "text-provider", "model": "text/only"},
                "c3": {
                    "provider": "fusion-provider",
                    "model": "vision/fusion-draft",
                    "supports_image": True,
                    "ensemble_enabled": True,
                },
                "image_model": {
                    "provider": "old-image-provider",
                    "model": "image/old-config",
                    "supports_image": True,
                    "image_only": True,
                },
            }
        ),
        llm_ensemble=SimpleNamespace(
            enabled=False,
            selection_mode="custom_b5",
            model_fields_set={"selection_mode"},
        ),
    )

    plan = pin_route_plan(
        turn,
        turn_id="turn-snapshot-image",
        provider="image-provider",
        model="image/winner",
        context_window=32_000,
        capabilities=ModelCapabilities(supports_vision=True),
        effective_thinking=False,
    )

    assert plan is not None and plan.router_tier_snapshot is not None
    assert plan.router_tier_snapshot.as_dict() == {
        "version": 1,
        "request_kind": "image",
        "tiers": [
            {
                "tier": "c0",
                "provider": "vision-provider",
                "model": "vision/fallback",
                "execution_kind": "single_model",
            },
            {
                "tier": "image_model",
                "provider": "image-provider",
                "model": "image/winner",
                "execution_kind": "single_model",
            },
        ],
    }
