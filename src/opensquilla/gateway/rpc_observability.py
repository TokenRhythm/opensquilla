"""Runtime status generated-Contract registration."""

from __future__ import annotations

from typing import Any

from opensquilla.gateway.adapters.observability_contract import (
    register_observability_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.rpc.registry import _status


async def _runtime_status_contract(
    params: dict[str, Any] | None,
    context: RpcContext,
) -> dict[str, Any]:
    return await _status(params, context)


register_observability_contract(
    get_dispatcher(),
    "status",
    _runtime_status_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
