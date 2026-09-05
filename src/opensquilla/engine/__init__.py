"""opensquilla.engine — dependency-light lazy Agent public surface.

Focused engine submodules, including the route-only Benchmark worker, must be
importable without eagerly loading the Agent, Provider, or tool stacks.  PEP
562 keeps all historical ``from opensquilla.engine import ...`` names intact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import Agent, ToolHandler
    from .context import ContextAssembly, ContextFiles
    from .subagent import (
        SubagentHandle,
        SubagentManager,
        SubagentRegistry,
        SubagentSpec,
    )
    from .types import (
        THINKING_BUDGETS,
        AgentConfig,
        AgentEvent,
        AgentState,
        ArtifactEvent,
        DoneEvent,
        ErrorEvent,
        RouterControlReplayEvent,
        RunHeartbeatEvent,
        StateChangeEvent,
        TextDeltaEvent,
        ThinkingEvent,
        ThinkingLevel,
        ToolCall,
        ToolResult,
        ToolResultEvent,
        ToolUseStartEvent,
        WarningEvent,
    )


# Map of lazy attribute name → (module_path, attribute_name). Loaded on
# first access via __getattr__; the imports themselves cascade tools/
# / channels/ / provider/ stacks that the type-stub consumers do not
# need.
_LAZY_MAP: dict[str, tuple[str, str]] = {
    "THINKING_BUDGETS": ("opensquilla.engine.types", "THINKING_BUDGETS"),
    "AgentConfig": ("opensquilla.engine.types", "AgentConfig"),
    "AgentEvent": ("opensquilla.engine.types", "AgentEvent"),
    "AgentState": ("opensquilla.engine.types", "AgentState"),
    "ArtifactEvent": ("opensquilla.engine.types", "ArtifactEvent"),
    "DoneEvent": ("opensquilla.engine.types", "DoneEvent"),
    "ErrorEvent": ("opensquilla.engine.types", "ErrorEvent"),
    "RouterControlReplayEvent": ("opensquilla.engine.types", "RouterControlReplayEvent"),
    "RunHeartbeatEvent": ("opensquilla.engine.types", "RunHeartbeatEvent"),
    "StateChangeEvent": ("opensquilla.engine.types", "StateChangeEvent"),
    "TextDeltaEvent": ("opensquilla.engine.types", "TextDeltaEvent"),
    "ThinkingEvent": ("opensquilla.engine.types", "ThinkingEvent"),
    "ThinkingLevel": ("opensquilla.engine.types", "ThinkingLevel"),
    "ToolCall": ("opensquilla.engine.types", "ToolCall"),
    "ToolResult": ("opensquilla.engine.types", "ToolResult"),
    "ToolResultEvent": ("opensquilla.engine.types", "ToolResultEvent"),
    "ToolUseStartEvent": ("opensquilla.engine.types", "ToolUseStartEvent"),
    "WarningEvent": ("opensquilla.engine.types", "WarningEvent"),
    "Agent": ("opensquilla.engine.agent", "Agent"),
    "ToolHandler": ("opensquilla.engine.agent", "ToolHandler"),
    "ContextAssembly": ("opensquilla.engine.context", "ContextAssembly"),
    "ContextFiles": ("opensquilla.engine.context", "ContextFiles"),
    "SubagentHandle": ("opensquilla.engine.subagent", "SubagentHandle"),
    "SubagentManager": ("opensquilla.engine.subagent", "SubagentManager"),
    "SubagentRegistry": ("opensquilla.engine.subagent", "SubagentRegistry"),
    "SubagentSpec": ("opensquilla.engine.subagent", "SubagentSpec"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_MAP:
        import importlib

        mod_path, attr = _LAZY_MAP[name]
        value = getattr(importlib.import_module(mod_path), attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "THINKING_BUDGETS",
    # Types (lazy)
    "AgentConfig",
    "AgentEvent",
    "AgentState",
    "ArtifactEvent",
    "ContextAssembly",
    "ContextFiles",
    "DoneEvent",
    "ErrorEvent",
    "RouterControlReplayEvent",
    "RunHeartbeatEvent",
    "StateChangeEvent",
    "SubagentHandle",
    "SubagentManager",
    "SubagentRegistry",
    "SubagentSpec",
    "TextDeltaEvent",
    "ThinkingEvent",
    "ThinkingLevel",
    "ToolCall",
    "ToolHandler",
    "ToolResult",
    "ToolResultEvent",
    "ToolUseStartEvent",
    "WarningEvent",
    "Agent",
]
