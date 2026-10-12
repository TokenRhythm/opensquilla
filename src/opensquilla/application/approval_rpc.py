"""RPC payload helpers for approval queue surfaces."""

from __future__ import annotations

from typing import Any

from opensquilla.application.approval_queue import ApprovalQueue, ApprovalSettings


def approval_settings_rpc_payload(
    settings: ApprovalSettings,
    *,
    node_id: str | None = None,
    inherited: bool | None = None,
) -> dict[str, Any]:
    """Build the RPC wire payload for approval settings."""

    payload: dict[str, Any] = {
        "mode": settings.mode,
        "allowPatterns": list(settings.allow_patterns),
        "denyPatterns": list(settings.deny_patterns),
    }
    if node_id is not None:
        payload["nodeId"] = node_id
    if inherited is not None:
        payload["inherited"] = inherited
    return payload


def approval_status_rpc_payload(
    queue: ApprovalQueue,
    approval_id: str,
    mode: str,
) -> dict[str, Any]:
    """Build the RPC wire payload for one approval status."""

    status = queue.status(approval_id)
    resolved_mode = status["params"].get("approvalMode", mode)
    return {
        "id": status["id"],
        "mode": resolved_mode,
        "approved": status["approved"],
        "resolved": status["resolved"],
        "resolution": status.get("resolution", ""),
        "deadline": status.get("deadline"),
        "consumed": status["consumed"],
        "pending": not status["resolved"],
    }


def approval_lookup_status_rpc_payload(
    queue: ApprovalQueue,
    approval_id: str,
    *,
    namespace: str,
) -> dict[str, Any]:
    """Return reconnect-safe status for one approval id.

    Unlike the historical resolve payload, this lookup distinguishes an
    actively claimed resolution from both a normally pending request and a
    settled result.  Missing ids are a normal recovery outcome, while looking
    an id up through the wrong namespace is rejected so exec/plugin semantics
    cannot be crossed accidentally.
    """

    try:
        entry = queue.get(approval_id)
    except KeyError:
        return {
            "found": False,
            "id": approval_id,
            "namespace": namespace,
            "pending": False,
            "resolutionInProgress": False,
            "resolved": False,
        }

    if entry.namespace != namespace:
        raise ValueError(f"Approval does not belong to {namespace} namespace: {approval_id}")

    resolution_in_progress = entry.claim_token is not None
    resolved = bool(entry.resolved and not resolution_in_progress)
    return {
        "found": True,
        "id": entry.approval_id,
        "namespace": entry.namespace,
        "pending": bool(not entry.resolved and not resolution_in_progress),
        "resolutionInProgress": resolution_in_progress,
        "resolved": resolved,
        "approved": bool(entry.approved) if resolved else False,
        "resolution": str(entry.resolution or "") if resolved else "",
        "consumed": bool(entry.consumed) if resolved else False,
        "deadline": entry.deadline,
    }


def approval_request_rpc_payload(
    queue: ApprovalQueue,
    *,
    namespace: str,
    params: dict[str, Any],
    node_id: str | None = None,
) -> dict[str, Any]:
    """Create an approval request and return its status payload."""

    settings = queue.get_settings(node_id=node_id)
    request_params = dict(params)
    request_params["approvalMode"] = settings.mode
    approval_id = queue.request(namespace=namespace, params=request_params)
    if settings.mode == "auto-approve":
        queue.resolve(approval_id, True)
    elif settings.mode == "auto-deny":
        queue.resolve(approval_id, False)
    return approval_status_rpc_payload(queue, approval_id, settings.mode)


async def approval_wait_decision_rpc_payload(
    queue: ApprovalQueue,
    approval_id: str,
    *,
    timeout_seconds: Any = None,
) -> dict[str, Any]:
    """Wait for an approval decision and return its status payload."""

    status = queue.status(approval_id)
    if not status["resolved"]:
        await queue.wait(
            approval_id,
            timeout=float(timeout_seconds) if timeout_seconds is not None else None,
        )
    return approval_status_rpc_payload(queue, approval_id, queue.get_settings().mode)


def approval_snapshot_rpc_payload(queue: ApprovalQueue) -> dict[str, Any]:
    """Build the diagnostic snapshot payload for approval state."""

    return {
        "mode": queue.get_settings().mode,
    }


def approval_forget_rpc_payload(target: Any = None) -> dict[str, Any]:
    """Compatibility no-op for the removed intent approval cache."""

    if isinstance(target, str) and target.strip():
        stripped = target.strip()
        return {"scope": "noop", "target": stripped}
    return {"scope": "noop"}


def approval_extend_rpc_payload(
    queue: ApprovalQueue,
    approval_id: str,
    seconds: float,
) -> dict[str, Any]:
    """Push a pending approval's deadline out and return its status payload."""

    deadline = queue.extend(approval_id, seconds)
    payload = approval_status_rpc_payload(queue, approval_id, queue.get_settings().mode)
    payload["deadline"] = deadline
    return payload


def approval_resolve_rpc_payload(
    queue: ApprovalQueue,
    approval_id: str,
    approved: bool,
    *,
    elevated_mode: str | None = None,
) -> dict[str, Any]:
    """Resolve an approval and return its status payload."""

    del elevated_mode
    queue.resolve(
        approval_id,
        approved,
        elevated_mode=None,
    )
    return approval_status_rpc_payload(queue, approval_id, queue.get_settings().mode)


__all__ = [
    "approval_extend_rpc_payload",
    "approval_forget_rpc_payload",
    "approval_lookup_status_rpc_payload",
    "approval_request_rpc_payload",
    "approval_resolve_rpc_payload",
    "approval_settings_rpc_payload",
    "approval_snapshot_rpc_payload",
    "approval_status_rpc_payload",
    "approval_wait_decision_rpc_payload",
]
