from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opensquilla.gateway.orchestration_routing import select_child_tree_route

pytestmark = pytest.mark.asyncio


async def test_child_selection_calls_existing_tree_router_once(monkeypatch) -> None:
    calls = []

    async def fake_apply_squilla_router(ctx):
        calls.append(ctx)
        ctx.model = "tree-model"
        ctx.metadata.update(
            {
                "routing_applied": True,
                "routed_model": "tree-model",
                "routed_provider": "provider-a",
                "routed_tier": "c2",
                "routing_source": "v4_phase3",
                "routing_confidence": 0.91,
                "thinking_level": "xhigh",
            }
        )
        return ctx

    monkeypatch.setattr(
        "opensquilla.gateway.orchestration_routing.apply_squilla_router",
        fake_apply_squilla_router,
    )
    config = SimpleNamespace(
        squilla_router=SimpleNamespace(enabled=True, routing_timeout_seconds=1.0)
    )

    route = await select_child_tree_route(
        "Inspect the scheduler",
        session_key="agent:main:subagent:child-1",
        baseline_model="baseline-model",
        config=config,
    )

    assert len(calls) == 1
    assert calls[0].message == "Inspect the scheduler"
    assert calls[0].model == "tree-model"
    assert calls[0].metadata["subagent_spawn_routing"] is True
    assert route is not None
    assert route.model == "tree-model"
    assert route.provider == "provider-a"
    assert route.tier == "c2"
    assert route.thinking_level == "xhigh"


async def test_disabled_tree_router_returns_no_route_without_call(monkeypatch) -> None:
    calls = 0

    async def fake_apply_squilla_router(ctx):
        nonlocal calls
        calls += 1
        return ctx

    monkeypatch.setattr(
        "opensquilla.gateway.orchestration_routing.apply_squilla_router",
        fake_apply_squilla_router,
    )
    config = SimpleNamespace(squilla_router=SimpleNamespace(enabled=False))

    route = await select_child_tree_route(
        "Inspect",
        session_key="agent:main:subagent:child-1",
        baseline_model="baseline-model",
        config=config,
    )

    assert route is None
    assert calls == 0


async def test_child_selection_has_no_orchestration_timeout(monkeypatch) -> None:
    async def fake_apply_squilla_router(ctx):
        await asyncio.sleep(0.02)
        ctx.metadata.update({"routing_applied": True, "routed_model": "tree-model"})
        return ctx

    monkeypatch.setattr(
        "opensquilla.gateway.orchestration_routing.apply_squilla_router",
        fake_apply_squilla_router,
    )
    config = SimpleNamespace(
        squilla_router=SimpleNamespace(enabled=True, routing_timeout_seconds=0.001)
    )

    route = await select_child_tree_route(
        "Inspect",
        session_key="agent:main:subagent:child-1",
        baseline_model="baseline-model",
        config=config,
    )

    assert route is not None
    assert route.model == "tree-model"
