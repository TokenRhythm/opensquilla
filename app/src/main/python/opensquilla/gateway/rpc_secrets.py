"""Secrets domain RPC handlers (Tier 2 stubs)."""

from __future__ import annotations

from typing import Any

from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher

_d = get_dispatcher()


@_d.method("secrets.reload", scope="operator.admin")
async def _handle_secrets_reload(params: dict | None, ctx: RpcContext) -> None:
    raise RpcUnavailableError("secrets.reload is not supported without a configured secret store")


@_d.method("secrets.resolve", scope="operator.admin")
async def _handle_secrets_resolve(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "refs" not in params:
        raise ValueError("params.refs is required")
    refs = params["refs"]
    if not isinstance(refs, list):
        raise ValueError("params.refs must be a list")
    raise RpcUnavailableError(
        "secrets.resolve is disabled until a bounded secret store is configured"
    )
