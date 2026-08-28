"""Diagnostics-mode RPC handlers."""

from __future__ import annotations

from typing import Any

from opensquilla.gateway.diagnostics import (
    DiagnosticsState,
    diagnostics_status_payload,
)
from opensquilla.gateway.rpc import RpcContext, get_dispatcher

_d = get_dispatcher()


def _state(ctx: RpcContext) -> DiagnosticsState:
    state = getattr(ctx, "diagnostics_state", None)
    if isinstance(state, DiagnosticsState):
        return state
    state = DiagnosticsState.from_config(getattr(ctx, "config", None))
    ctx.diagnostics_state = state
    return state


@_d.method("diagnostics.status", scope="operator.read")
async def _handle_diagnostics_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return diagnostics_status_payload(_state(ctx), getattr(ctx, "config", None))


@_d.method("diagnostics.set", scope="operator.admin")
async def _handle_diagnostics_set(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if "enabled" not in params:
        raise ValueError("params.enabled is required")
    enabled = bool(params.get("enabled"))
    raw = bool(params.get("raw", False))
    state = _state(ctx)
    state.set_runtime(enabled=enabled, raw=raw)
    return diagnostics_status_payload(state, getattr(ctx, "config", None))
