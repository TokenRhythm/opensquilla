"""opensquilla.tools — Tool Registry + built-in tools."""

from opensquilla.tools import builtin as _builtin  # noqa: F401 — side-effect: register tools
from opensquilla.tools.registry import ToolRegistry, get_default_registry, tool
from opensquilla.tools.types import (
    CallerKind,
    PlanAccess,
    RegisteredTool,
    ToolContext,
    ToolError,
    ToolSpec,
)

__all__ = [
    "ToolRegistry",
    "get_default_registry",
    "tool",
    "CallerKind",
    "PlanAccess",
    "ToolContext",
    "ToolSpec",
    "RegisteredTool",
    "ToolError",
]
