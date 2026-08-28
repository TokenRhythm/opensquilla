"""Scheduler-owned adapter for cron route/tool-context construction."""

from __future__ import annotations

from typing import Any

from opensquilla.tools.types import ToolContext


def build_cron_route_envelope(*args: Any, **kwargs: Any) -> Any:
    """Build a cron route envelope through the gateway routing implementation."""
    from opensquilla.gateway.routing import build_cron_route_envelope as _build

    return _build(*args, **kwargs)


def tool_context_from_envelope(*args: Any, **kwargs: Any) -> ToolContext:
    """Build a ToolContext from a route envelope through the gateway adapter."""
    from opensquilla.gateway.routing import tool_context_from_envelope as _build

    return _build(*args, **kwargs)
