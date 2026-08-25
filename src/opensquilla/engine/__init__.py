"""opensquilla.engine — Agent core state machine.

The public surface is **lazy** for everything except ``.types``:
``from opensquilla.engine import Agent`` continues to work (PEP 562
``__getattr__`` resolves the symbol on first access), but
``import opensquilla.engine.types`` does NOT transitively drag in the
``agent`` / ``context`` / ``subagent`` modules — keeping tooling that
only needs the type stubs (mypy probes, IDE inspection, packaging
gates, the public-tool-surface lint at ``test_public_tool_surface.py``)
fast and dependency-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .types import (
    THINKING_BUDGETS,
    AgentConfig,
    AgentEvent,
    AgentState,
    ArtifactEvent,
    DoneEvent,
    ErrorEvent,
    ProviderActivityEvent,
    RouterControlReplayEvent,
    RunHeartbeatEvent,
    StateChangeEvent,
    TextDeltaEvent,
    ThinkingEndEvent,
    ThinkingEvent,
    ThinkingLevel,
    ThinkingStartEvent,
    ToolCall,
    ToolResult,
    ToolResultEvent,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    WarningEvent,
)

if TYPE_CHECKING:
    from .agent import Agent, ToolHandler
    from .context import ContextAssembly, ContextFiles
    from .subagent import (
        SubagentHandle,
        SubagentManager,
        SubagentRegistry,
        SubagentSpec,
        SubagentUsage,
    )


# Map of lazy attribute name → (module_path, attribute_name). Loaded on
# first access via __getattr__; the imports themselves cascade tools/
# / channels/ / provider/ stacks that the type-stub consumers do not
# need.
_LAZY_MAP: dict[str, tuple[str, str]] = {
    "Agent": ("opensquilla.engine.agent", "Agent"),
    "ToolHandler": ("opensquilla.engine.agent", "ToolHandler"),
    "ContextAssembly": ("opensquilla.engine.context", "ContextAssembly"),
    "ContextFiles": ("opensquilla.engine.context", "ContextFiles"),
    "SubagentHandle": ("opensquilla.engine.subagent", "SubagentHandle"),
    "SubagentManager": ("opensquilla.engine.subagent", "SubagentManager"),
    "SubagentRegistry": ("opensquilla.engine.subagent", "SubagentRegistry"),
    "SubagentSpec": ("opensquilla.engine.subagent", "SubagentSpec"),
    "SubagentUsage": ("opensquilla.engine.subagent", "SubagentUsage"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_MAP:
        import importlib

        mod_path, attr = _LAZY_MAP[name]
        return getattr(importlib.import_module(mod_path), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "THINKING_BUDGETS",
    # Types (eager)
    "AgentConfig",
    "AgentEvent",
    "AgentState",
    "ArtifactEvent",
    "ContextAssembly",
    "ContextFiles",
    "DoneEvent",
    "ErrorEvent",
    "ProviderActivityEvent",
    "RouterControlReplayEvent",
    "RunHeartbeatEvent",
    "StateChangeEvent",
    "SubagentHandle",
    "SubagentManager",
    "SubagentRegistry",
    "SubagentSpec",
    "SubagentUsage",
    "TextDeltaEvent",
    "ThinkingEndEvent",
    "ThinkingEvent",
    "ThinkingLevel",
    "ThinkingStartEvent",
    "ToolCall",
    "ToolHandler",
    "ToolResult",
    "ToolResultEvent",
    "ToolUseDeltaEvent",
    "ToolUseEndEvent",
    "ToolUseStartEvent",
    "WarningEvent",
    "Agent",
]
