"""Offline probe: verify router_max_tier mapping envelope -> ToolContext."""

import sys

sys.path.insert(0, r"D:\AIstudio\Harness\OpenSquilla-QinLuza-Studio\src")

from opensquilla.gateway.routing import (
    build_subagent_route_envelope,
    tool_context_from_envelope,
)

env = build_subagent_route_envelope(
    session_key="agent:main:subagent:probetest",
    parent_session_key="agent:main:main",
    agent_id="main",
    spawn_depth=1,
    max_tier="c0",
)
print("envelope.metadata[router_max_tier] =", env.metadata.get("router_max_tier"))

try:
    tc = tool_context_from_envelope(env)
    val = getattr(tc, "router_max_tier", "<NO ATTR>")
    print("ToolContext.router_max_tier =", val)
except Exception as exc:  # noqa: BLE001
    print("tool_context_from_envelope raised:", type(exc).__name__, exc)
