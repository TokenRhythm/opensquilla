"""Authenticated, read-only lookup of durable turn acceptance."""

from __future__ import annotations

from typing import Any

from opensquilla.gateway.adapters.turn_receipt_contract import (
    register_turn_receipt_contract,
    validate_turn_receipt_params,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.gateway.turn_receipts import (
    can_read_turn_receipts,
    decode_turn_receipt_query,
    read_turn_receipt,
)


async def _handle_turns_receipt_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not can_read_turn_receipts(ctx.principal):
        raise RpcHandlerError(
            "UNAUTHORIZED", "Turn receipts require authenticated operator read access",
        )
    try:
        validate_turn_receipt_params(params)
        assert isinstance(params, dict)
        query = decode_turn_receipt_query(params, principal_role=ctx.role)
    except (ValueError, KeyError, TypeError) as exc:
        raise RpcHandlerError("INVALID_REQUEST", "Invalid original turn receipt request") from exc
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise RpcUnavailableError("Session storage is unavailable")
    return await read_turn_receipt(storage, query)


_handle_turns_receipt_get_contract = register_turn_receipt_contract(
    get_dispatcher(), _handle_turns_receipt_get,
    internal_error=RpcHandlerError, guest_allowed_checker=is_guest_rpc_method_allowed,
)
