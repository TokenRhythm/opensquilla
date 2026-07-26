"""Approvals domain RPC handlers backed by ApprovalQueue."""

from __future__ import annotations

import asyncio
from typing import Any

from opensquilla.application.approval_queue import get_approval_queue
from opensquilla.application.approval_rpc import (
    approval_extend_rpc_payload,
    approval_forget_rpc_payload,
    approval_lookup_status_rpc_payload,
    approval_request_rpc_payload,
    approval_resolve_rpc_payload,
    approval_settings_rpc_payload,
    approval_snapshot_rpc_payload,
    approval_status_rpc_payload,
    approval_wait_decision_rpc_payload,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.sandbox.escalation import (
    apply_sandbox_approval_choice,
    deny_matching_pending_sandbox_approvals,
    is_sandbox_approval_kind,
    remember_sandbox_approval_denial,
    validate_sandbox_approval_choice,
)

_d = get_dispatcher()

_NON_OWNER_SANDBOX_APPROVAL_CHOICES = frozenset(
    {
        "allow_once",
        "allow_same_type",
        "deny",
    }
)

_APPROVAL_CLAIM_JOIN_TIMEOUT_SECONDS = 0.5
_APPROVAL_CLAIM_POLL_SECONDS = 0.01


def _sandbox_choice_requires_owner(choice: str | None) -> bool:
    normalized_choice = str(choice or "").strip()
    if not normalized_choice:
        return True
    return normalized_choice not in _NON_OWNER_SANDBOX_APPROVAL_CHOICES


def _require_owner_for_approval_resolution(ctx: RpcContext) -> None:
    if not getattr(ctx.principal, "is_owner", False):
        raise RpcHandlerError(
            "UNAUTHORIZED",
            "exec.approval.resolve requires owner principal.",
        )


def _require_owner_for_sandbox_approval_resolution(
    ctx: RpcContext,
    *,
    choice: str | None,
) -> None:
    if not _sandbox_choice_requires_owner(choice):
        return
    _require_owner_for_approval_resolution(ctx)


def _complete_sandbox_resolution_claim(
    queue: Any,
    approval_id: str,
    claim_token: str,
) -> None:
    try:
        queue.complete_claimed_resolution(
            approval_id,
            claim_token,
        )
    except Exception:
        queue.complete_claimed_resolution(
            approval_id,
            claim_token,
        )


async def _join_active_resolution(queue: Any, approval_id: str) -> Any:
    """Wait briefly for another surface's claimed decision to become canonical.

    Sandbox resolution keeps a claim while it persists the decision and applies
    its side effect. A losing surface should observe that result, not race a
    second claim and surface a false failure. If the first claim is released
    after an apply error, returning the reopened entry lets this resolver try
    normally. The bounded wait keeps an abandoned claim from hanging the RPC.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _APPROVAL_CLAIM_JOIN_TIMEOUT_SECONDS
    pending = queue.get(approval_id)
    while pending.claim_token is not None and loop.time() < deadline:
        await asyncio.sleep(_APPROVAL_CLAIM_POLL_SECONDS)
        pending = queue.get(approval_id)
    return pending


@_d.method("exec.approvals.get", scope="operator.approvals")
async def _handle_exec_approvals_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    queue = get_approval_queue()
    return approval_settings_rpc_payload(queue.get_settings())


@_d.method("exec.approvals.set", scope="operator.approvals")
async def _handle_exec_approvals_set(params: dict | None, ctx: RpcContext) -> None:
    if not isinstance(params, dict) or "mode" not in params:
        raise ValueError("params.mode is required")
    queue = get_approval_queue()
    queue.set_settings(
        mode=params["mode"],
        allow_patterns=params.get("allowPatterns"),
        deny_patterns=params.get("denyPatterns"),
    )
    return None


@_d.method("exec.approvals.node.get", scope="operator.admin")
async def _handle_exec_approvals_node_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "nodeId" not in params:
        raise ValueError("params.nodeId is required")
    queue = get_approval_queue()
    node_id = params["nodeId"]
    return approval_settings_rpc_payload(
        queue.get_settings(node_id=node_id),
        node_id=node_id,
        inherited=not queue.has_node_settings(node_id),
    )


@_d.method("exec.approvals.node.set", scope="operator.admin")
async def _handle_exec_approvals_node_set(params: dict | None, ctx: RpcContext) -> None:
    if not isinstance(params, dict) or "nodeId" not in params:
        raise ValueError("params.nodeId is required")
    if "mode" not in params:
        raise ValueError("params.mode is required")
    queue = get_approval_queue()
    queue.set_settings(
        mode=params["mode"],
        allow_patterns=params.get("allowPatterns"),
        deny_patterns=params.get("denyPatterns"),
        node_id=params["nodeId"],
    )
    return None


@_d.method("exec.approval.request", scope="operator.approvals")
async def _handle_exec_approval_request(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params required: toolName, args, sessionKey")
    for field in ("toolName", "args", "sessionKey"):
        if field not in params:
            raise ValueError(f"params.{field} is required")
    return approval_request_rpc_payload(
        get_approval_queue(),
        namespace="exec",
        params=params,
        node_id=params.get("nodeId"),
    )


@_d.method("exec.approval.waitDecision", scope="operator.approvals")
async def _handle_exec_approval_wait_decision(
    params: dict | None, ctx: RpcContext
) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    queue = get_approval_queue()
    return await approval_wait_decision_rpc_payload(
        queue,
        params["id"],
        timeout_seconds=params.get("timeoutSeconds"),
    )


@_d.method("exec.approval.status", scope="operator.approvals")
async def _handle_exec_approval_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or not str(params.get("id") or "").strip():
        raise ValueError("params.id is required")
    return approval_lookup_status_rpc_payload(
        get_approval_queue(),
        str(params["id"]).strip(),
        namespace="exec",
    )


@_d.method("exec.approval.snapshot", scope="operator.approvals")
async def _handle_exec_approval_snapshot(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Return a diagnostic snapshot for approval state."""
    queue = get_approval_queue()
    return approval_snapshot_rpc_payload(queue)


@_d.method("exec.approval.forget", scope="operator.approvals")
async def _handle_exec_approval_forget(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Compatibility no-op for removed cached intent approvals."""
    if isinstance(params, dict):
        target = params.get("target")
    else:
        target = None
    return approval_forget_rpc_payload(target)


@_d.method("exec.approval.resolve", scope="operator.approvals")
async def _handle_exec_approval_resolve(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    if "approved" not in params:
        raise ValueError("params.approved is required")
    # "Allow always" / rememberIntent were a no-op placebo: the resolver accepted
    # them and never suppressed a re-prompt. They are removed rather than left to
    # silently promise a guarantee. Reject a truthy value loudly so clients (and
    # any external caller) cannot re-grow the placebo; re-prompt fatigue is
    # addressed by choosing a broader run mode instead.
    if bool(params.get("allowAlways", False)) or bool(params.get("rememberIntent", False)):
        raise RpcHandlerError(
            "UNSUPPORTED_PARAM",
            "'Allow always' / rememberIntent is no longer supported (it never "
            "suppressed re-prompts). Switch to a broader run mode via /sandbox to "
            "reduce approval prompts.",
        )
    choice = params.get("choice")
    queue = get_approval_queue()
    approved = bool(params["approved"])
    pending = queue.get(params["id"])
    if pending.claim_token is not None:
        pending = await _join_active_resolution(queue, params["id"])
        if pending.claim_token is not None:
            payload = approval_status_rpc_payload(
                queue,
                params["id"],
                queue.get_settings().mode,
            )
            payload["resolutionInProgress"] = True
            return payload
    # Cross-surface first-valid-resolution wins.  A WebUI decision can land
    # after the TUI keypress future completes but before this RPC reaches the
    # queue.  Return the canonical result instead of presenting the losing
    # surface with a false red failure (and never replay sandbox side effects).
    if pending.resolved:
        return approval_status_rpc_payload(
            queue,
            params["id"],
            queue.get_settings().mode,
        )
    normalized_choice = str(choice).strip() if isinstance(choice, str) and choice.strip() else None
    sandbox_approval = is_sandbox_approval_kind(pending.params.get("approvalKind"))
    if sandbox_approval and approved:
        _require_owner_for_sandbox_approval_resolution(ctx, choice=normalized_choice)

    validate_sandbox_approval_choice(
        pending.params,
        choice=normalized_choice,
        approved=approved,
    )

    if sandbox_approval and approved:
        claim_token = queue.claim_resolution(params["id"])
        try:
            queue.finalize_claimed_resolution(
                params["id"],
                claim_token,
                approved,
                elevated_mode=None,
            )
        except Exception:
            queue.release_resolution_claim(params["id"], claim_token)
            raise
        try:
            await apply_sandbox_approval_choice(
                pending.params,
                choice=normalized_choice,
                approved=True,
                session_manager=ctx.session_manager,
                config=ctx.config,
            )
        except Exception:
            queue.reopen_resolved_approval(params["id"], expected_approved=True)
            raise
        _complete_sandbox_resolution_claim(
            queue,
            params["id"],
            claim_token,
        )
        return approval_status_rpc_payload(queue, params["id"], queue.get_settings().mode)

    queue.resolve(
        params["id"],
        approved,
        elevated_mode=None,
        allow_idempotent=not sandbox_approval,
    )
    if sandbox_approval and not approved:
        remember_sandbox_approval_denial(pending.params, params["id"])
        deny_matching_pending_sandbox_approvals(
            queue,
            pending.params,
            exclude_approval_id=params["id"],
        )

    return approval_status_rpc_payload(queue, params["id"], queue.get_settings().mode)


_EXTEND_DEFAULT_SECONDS = 300.0
_EXTEND_MAX_SECONDS = 3600.0


def _coerce_extend_seconds(raw: Any) -> float:
    if raw is None:
        return _EXTEND_DEFAULT_SECONDS
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("params.seconds must be a number") from exc
    if seconds <= 0:
        raise ValueError("params.seconds must be positive")
    return min(seconds, _EXTEND_MAX_SECONDS)


@_d.method("exec.approval.extend", scope="operator.approvals")
async def _handle_exec_approval_extend(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    seconds = _coerce_extend_seconds(params.get("seconds"))
    queue = get_approval_queue()
    return approval_extend_rpc_payload(queue, params["id"], seconds)


@_d.method("plugin.approval.extend", scope="operator.approvals")
async def _handle_plugin_approval_extend(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    seconds = _coerce_extend_seconds(params.get("seconds"))
    queue = get_approval_queue()
    return approval_extend_rpc_payload(queue, params["id"], seconds)


@_d.method("plugin.approval.request", scope="operator.approvals")
async def _handle_plugin_approval_request(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params required: pluginId, version, permissions")
    for field in ("pluginId", "version", "permissions"):
        if field not in params:
            raise ValueError(f"params.{field} is required")
    return approval_request_rpc_payload(
        get_approval_queue(),
        namespace="plugin",
        params=params,
    )


@_d.method("plugin.approval.waitDecision", scope="operator.approvals")
async def _handle_plugin_approval_wait_decision(
    params: dict | None, ctx: RpcContext
) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    queue = get_approval_queue()
    return await approval_wait_decision_rpc_payload(
        queue,
        params["id"],
        timeout_seconds=params.get("timeoutSeconds"),
    )


@_d.method("plugin.approval.status", scope="operator.approvals")
async def _handle_plugin_approval_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or not str(params.get("id") or "").strip():
        raise ValueError("params.id is required")
    return approval_lookup_status_rpc_payload(
        get_approval_queue(),
        str(params["id"]).strip(),
        namespace="plugin",
    )


@_d.method("plugin.approval.resolve", scope="operator.approvals")
async def _handle_plugin_approval_resolve(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    if "approved" not in params:
        raise ValueError("params.approved is required")
    queue = get_approval_queue()
    approval_id = params["id"]
    # Match exec approvals' cross-surface contract: the first valid decision is
    # canonical, and a later surface receives that outcome even if its stale
    # click requested the opposite result.
    if queue.get(approval_id).resolved:
        return approval_status_rpc_payload(queue, approval_id, queue.get_settings().mode)
    try:
        return approval_resolve_rpc_payload(queue, approval_id, bool(params["approved"]))
    except ValueError:
        # Close the get/resolve race without hiding an unrelated queue error.
        if queue.get(approval_id).resolved:
            return approval_status_rpc_payload(queue, approval_id, queue.get_settings().mode)
        raise
