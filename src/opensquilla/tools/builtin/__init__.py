"""Register all built-in tools by importing each submodule."""

from __future__ import annotations

from importlib import import_module

import structlog

_FATAL_MODULES = frozenset({"shell", "patch", "filesystem"})
_NAMES = [
    "admin",
    "agents",
    "artifacts",
    "artifact_editing",
    "document_editing",
    "code_exec",
    "file_authoring",
    "filesystem",
    "git",
    "goal_control",
    "media",
    "messaging",
    "meta_tools",
    "nodes",
    "patch",
    "plan_control",
    "router_control",
    "sessions",
    "session_search",
    "shell",
    "submit_tool",
    "tool_results",
    "web",
    "web_fetch",
]

log = structlog.get_logger(__name__)

for _name in _NAMES:
    try:
        globals()[_name] = import_module(f"{__name__}.{_name}")
    except Exception as exc:
        if _name in _FATAL_MODULES:
            raise
        log.warning("builtin_tool.import_failed", module=_name, error=str(exc))
        continue

__all__ = _NAMES
