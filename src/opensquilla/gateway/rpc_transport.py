"""Connection-local transport control; never changes domain authority."""

from __future__ import annotations

from typing import Any

from opensquilla.gateway.adapters.connection_recovery_contract import (
    register_connection_recovery_contract,
    validate_recovery_params,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher


async def _handle_transport_flow_update(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.gateway.websocket import get_registry

    try:
        validate_recovery_params("transport.flow.update", params)
    except ValueError as exc:
        raise RpcHandlerError("INVALID_REQUEST", str(exc), accepted=False) from exc
    assert isinstance(params, dict)
    connection = get_registry().get(ctx.conn_id)
    if connection is None or connection.principal != ctx.principal:
        raise RpcHandlerError("UNAUTHORIZED", "Connection identity is no longer current")
    if not getattr(connection, "flow_enabled", False):
        raise RpcHandlerError("FLOW_DISABLED", "Consumption feedback was not negotiated")
    try:
        resumes = params.get("resume", [])
        if len(resumes) > 1:
            # One frozen transfer per connection also means one installation
            # per control, with no later identity-read await before apply.
            raise ValueError("Only one snapshot installation is admitted per update")
        if resumes:
            from opensquilla.gateway.session_services import read_session_identity

            resume = resumes[0]
            frozen_identity = connection.snapshot_install_identity(resume)
            current_identity = await read_session_identity(
                getattr(ctx, "session_manager", None), resume["key"],
            )
            # No await from this comparison to apply_flow_update. A reset or
            # delete-recreate cannot hide behind the process generation and C=0.
            if (
                get_registry().get(ctx.conn_id) is not connection
                or connection.principal != ctx.principal
                or connection.snapshot_install_identity(resume) != frozen_identity
                or current_identity != frozen_identity
            ):
                if (
                    get_registry().get(ctx.conn_id) is connection
                    and connection._subscriptions is not None
                    and connection._subscriptions.get_message_subscription_token(
                        ctx.conn_id, resume["key"]
                    ) is not None
                ):
                    connection._mark_flow_dirty({"session_key": resume["key"]})
                raise ValueError("Snapshot session identity is no longer current")
        return connection.apply_flow_update(params)
    except ValueError as exc:
        raise RpcHandlerError("INVALID_REQUEST", str(exc), accepted=False) from exc


_handle_transport_flow_update_contract = register_connection_recovery_contract(
    get_dispatcher(),
    "transport.flow.update",
    _handle_transport_flow_update,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
