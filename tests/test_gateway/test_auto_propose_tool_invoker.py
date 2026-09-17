from __future__ import annotations

import pytest

from opensquilla.gateway.boot import _make_auto_propose_tool_context
from opensquilla.skills.meta.orchestrator import make_tool_invoker_from_handler
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolSpec


@pytest.mark.asyncio
async def test_auto_propose_tool_invoker_uses_preflight_allowlist() -> None:
    registry = ToolRegistry()

    async def allowed() -> str:
        return "ok"

    async def blocked() -> str:
        raise AssertionError("preflight should reject before handler execution")

    registry.register(
        ToolSpec(name="allowed", description="ok", parameters={}),
        allowed,
    )
    registry.register(
        ToolSpec(name="blocked", description="blocked", parameters={}),
        blocked,
    )
    invoker = make_tool_invoker_from_handler(
        tool_handler=build_tool_handler(
            registry,
            _make_auto_propose_tool_context(allowed_tools=frozenset({"allowed"})),
        ),
    )

    assert await invoker("allowed", {}) == "ok"
    with pytest.raises(RuntimeError, match="PolicyDenied"):
        await invoker("blocked", {})
