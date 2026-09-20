"""Connection-local transport control; never changes domain authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opensquilla.gateway.websocket import WsConnection

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


def _recovery_connection(ctx: RpcContext) -> WsConnection:
    from opensquilla.gateway.websocket import get_registry

    connection = get_registry().get(ctx.conn_id)
    if connection is None or connection.principal != ctx.principal:
        raise RpcHandlerError("UNAUTHORIZED", "Connection identity is no longer current")
    if not connection._recovery_enabled or not connection.flow_enabled:
        raise RpcHandlerError("FLOW_DISABLED", "Recovery capability was not negotiated",
                              accepted=False)
    return connection


async def _handle_sessions_messages_resume(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.gateway.recovery_scheduler import CURRENT_RECOVERY_OPERATION
    from opensquilla.gateway.session_services import read_session_identity
    from opensquilla.gateway.snapshot_transfer import SnapshotTransferError
    from opensquilla.gateway.websocket import get_registry
    from opensquilla.session.keys import canonicalize_session_key

    try:
        validate_recovery_params("sessions.messages.resume", params)
    except ValueError as exc:
        raise RpcHandlerError("INVALID_REQUEST", str(exc), accepted=False) from exc
    assert isinstance(params, dict)
    params = {**params, "key": canonicalize_session_key(params["key"])}
    connection = _recovery_connection(ctx)
    proof = connection.installed_snapshot_proof(params)
    if proof is not None:
        return proof
    operation = CURRENT_RECOVERY_OPERATION.get()
    transfer = operation.transfer if operation is not None else connection.snapshot_registry().get(
        params["key"], params["sync_revision"], params["snapshot_id"],
    )
    flow = connection._flow
    assert flow is not None
    try:
        if transfer is None or transfer.closed or transfer.lease_token is None:
            raise SnapshotTransferError("SNAPSHOT_STALE")
        if connection._subscriptions.get_message_subscription_token(
            ctx.conn_id, params["key"],
        ) != transfer.lease_token:
            raise SnapshotTransferError("SNAPSHOT_STALE")
        if any(delivery.owner is transfer and (not delivery.acknowledged or not delivery.sent)
               for delivery in flow.deliveries.values()):
            raise SnapshotTransferError("SNAPSHOT_BUSY")
        dirty_revision = flow.dirty_revision(params["key"])
        identity = transfer.begin_install(params)
        current_identity = await read_session_identity(ctx.session_manager, params["key"])
        if (
            get_registry().get(ctx.conn_id) is not connection
            or connection.principal != ctx.principal
            or current_identity != identity
            or (operation is not None and not operation.current())
        ):
            raise SnapshotTransferError("SNAPSHOT_STALE")
        # Synchronous final CAS, notice freeze, replay and proof construction;
        # dispatcher validation/send_res enqueue does not yield in queue mode.
        return connection.install_snapshot(params, transfer, dirty_revision)
    except SnapshotTransferError as exc:
        raise RpcHandlerError(exc.code, "Snapshot installation is no longer available",
                              retryable=exc.code == "SNAPSHOT_BUSY", accepted=False,
                              retry_after_ms=100 if exc.code == "SNAPSHOT_BUSY" else None) from exc


async def _handle_sessions_messages_snapshot_release(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    from opensquilla.session.keys import canonicalize_session_key

    try:
        validate_recovery_params("sessions.messages.snapshot.release", params)
    except ValueError as exc:
        raise RpcHandlerError("INVALID_REQUEST", str(exc), accepted=False) from exc
    assert isinstance(params, dict)
    connection = _recovery_connection(ctx)
    key = canonicalize_session_key(params["key"])
    connection.retire_snapshot(key, params["sync_revision"], params.get("snapshot_id"))
    return {**params, "key": key, "retired": True}


_handle_sessions_messages_resume_contract = register_connection_recovery_contract(
    get_dispatcher(), "sessions.messages.resume", _handle_sessions_messages_resume,
    internal_error=RpcHandlerError, guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_sessions_messages_snapshot_release_contract = register_connection_recovery_contract(
    get_dispatcher(), "sessions.messages.snapshot.release",
    _handle_sessions_messages_snapshot_release,
    internal_error=RpcHandlerError, guest_allowed_checker=is_guest_rpc_method_allowed,
)
