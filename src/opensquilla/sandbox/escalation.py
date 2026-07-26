"""Structured sandbox escalation proposals and choice application helpers."""

from __future__ import annotations

import copy
import json
import os
import weakref
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from opensquilla.sandbox.domain_validation import domain_matches
from opensquilla.sandbox.elevation import ApprovalReviewerName, ElevationAction
from opensquilla.sandbox.network_guard import NetworkDecision
from opensquilla.sandbox.package_bundles import expand_package_bundle
from opensquilla.sandbox.path_validation import MountDecision, normalize_path
from opensquilla.sandbox.run_context import (
    RUN_CONTEXT_ORIGIN_KEY,
    DomainGrant,
    MountGrant,
    PackageBundleGrant,
    RunContext,
    TemporaryGrant,
    get_run_context,
    persist_run_context,
)
from opensquilla.sandbox.run_context_service import (
    add_domain_grant,
    add_mount_grant,
    enable_bundle_grant,
)
from opensquilla.sandbox.run_mode import RunMode

SANDBOX_APPROVAL_KINDS = frozenset({"sandbox_network", "sandbox_path"})
_RESOLVED_RUN_CONTEXT_OVERLAYS: dict[tuple[str, str | None], RunContext] = {}
_RESOLVED_RUN_CONTEXT_PERSISTORS: dict[tuple[str, str | None], tuple[Any, Any]] = {}
_RESOLVED_RUN_CONTEXT_BINDINGS: dict[
    tuple[str, str | None],
    _WorkspaceBindingIdentity,
] = {}
_RESOLVED_RUN_CONTEXT_BINDING_ALIASES: dict[
    tuple[str, str | None],
    tuple[str, str | None],
] = {}
_APPROVAL_RUN_CONTEXT_GENERATIONS: dict[
    str,
    _ApprovalRunContextGeneration | None,
] = {}
_APPROVAL_GENERATION_OWNED_IDS: set[str] = set()
_APPROVAL_QUEUE_LISTENER_REMOVERS: weakref.WeakKeyDictionary[Any, Any] = (
    weakref.WeakKeyDictionary()
)
_DENIED_SANDBOX_APPROVALS: dict[str, str] = {}
_DURABLE_TEMPORARY_GRANT_SOURCES = frozenset({"saved", "route_metadata", "metadata"})


@dataclass(frozen=True)
class _WorkspaceBindingIdentity:
    canonical_path: str | None
    device: int | None
    inode: int | None


@dataclass(frozen=True)
class _ApprovalRunContextGeneration:
    approval_key: str
    session_key: str
    workspace_alias: tuple[str, str | None]
    binding: _WorkspaceBindingIdentity | None
    context: RunContext


class _AuthoritativeRunContextManager:
    def __init__(
        self,
        session_manager: Any,
        session_key: str,
        context: RunContext,
    ) -> None:
        self._session_manager = session_manager
        self._session_key = session_key
        self._context = context
        self._session: Any | None = None

    async def get_session(self, session_key: str) -> Any | None:
        if session_key == self._session_key and self._session is not None:
            return copy.copy(self._session)
        get_session = getattr(self._session_manager, "get_session", None)
        if callable(get_session):
            session = await get_session(session_key)
        else:
            storage = getattr(self._session_manager, "_storage", None)
            storage_get = getattr(storage, "get_session", None)
            session = await storage_get(session_key) if callable(storage_get) else None
        if session_key != self._session_key or session is None:
            return session
        seeded = copy.copy(session)
        origin = (
            dict(getattr(seeded, "origin", None))
            if isinstance(getattr(seeded, "origin", None), dict)
            else {}
        )
        origin[RUN_CONTEXT_ORIGIN_KEY] = self._context.to_origin_payload()
        seeded.origin = origin
        self._session = seeded
        return copy.copy(seeded)

    async def update(self, session_key: str, **fields: Any) -> Any:
        update = getattr(self._session_manager, "update", None)
        if not callable(update):
            raise RuntimeError("Session manager does not support update")
        updated = await update(session_key, **fields)
        if session_key == self._session_key:
            self._session = copy.copy(updated)
        return updated

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session_manager, name)


def _choice(
    choice_id: str,
    label: str,
    *,
    approved: bool = True,
    style: str = "ghost",
    description: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": choice_id,
        "label": label,
        "approved": approved,
        "style": style,
    }
    if description:
        payload["description"] = description
    return payload


def _standard_approval_choices() -> list[dict[str, object]]:
    return [
        _choice("allow_once", "Allow once", style="primary"),
        _choice("allow_same_type", "Allow same type"),
        _choice("deny", "Deny", approved=False, style="danger"),
    ]


def build_network_approval_params(
    decision: NetworkDecision,
    *,
    session_key: str | None,
    workspace: str | None,
    fingerprint: str,
    reviewer: ApprovalReviewerName = "user",
) -> dict[str, object] | None:
    if decision.status != "ask" or decision.reason != "unknown_domain":
        return None
    params: dict[str, object] = {
        "approvalKind": "sandbox_network",
        "host": decision.normalized_host,
        "fingerprint": fingerprint,
    }
    if session_key:
        params["sessionKey"] = session_key
    if workspace:
        params["workspace"] = workspace
    _add_network_reviewer_params(
        params,
        reviewer=reviewer,
        network_target=decision.normalized_host,
        fingerprint=fingerprint,
        workspace=workspace,
    )
    return params


def build_package_bundle_approval_params(
    bundle_id: str,
    *,
    session_key: str | None,
    workspace: str | None,
    fingerprint: str,
    reviewer: ApprovalReviewerName = "user",
) -> dict[str, object]:
    normalized_bundle_id = str(bundle_id or "").strip()
    if not expand_package_bundle(normalized_bundle_id):
        raise ValueError("unknown_package_bundle")
    params: dict[str, object] = {
        "approvalKind": "sandbox_network",
        "bundle_id": normalized_bundle_id,
        "fingerprint": fingerprint,
    }
    if session_key:
        params["sessionKey"] = session_key
    if workspace:
        params["workspace"] = workspace
    _add_network_reviewer_params(
        params,
        reviewer=reviewer,
        network_target=f"bundle:{normalized_bundle_id}",
        fingerprint=fingerprint,
        workspace=workspace,
    )
    return params


def _add_network_reviewer_params(
    params: dict[str, object],
    *,
    reviewer: ApprovalReviewerName,
    network_target: str,
    fingerprint: str,
    workspace: str | None,
) -> None:
    if reviewer == "user":
        params["reviewer"] = "user"
        params["humanActionable"] = True
        params["choices"] = _standard_approval_choices()
        return
    action = ElevationAction(
        tool_name="sandbox_network",
        action_kind="network.access",
        argv=("sandbox_network", network_target),
        cwd=str(workspace or ""),
        sandbox_permissions="require_escalated",
        justification="Allow one exact managed-network target for the current action.",
        network_targets=(network_target,),
        content_digest=fingerprint,
        risk_markers=("managed_network_access",),
    )
    params.update(
        {
            "reviewer": "auto_review",
            "humanActionable": False,
            "reviewFingerprint": action.fingerprint(),
            "action": action.canonical_payload(),
        }
    )


def grant_auto_review_network_once(params: dict[str, Any]) -> bool:
    """Install one in-memory, fingerprint-bound grant after rule approval."""

    if (
        params.get("approvalKind") != "sandbox_network"
        or params.get("reviewer") != "auto_review"
        or params.get("humanActionable") is not False
    ):
        return False
    session_key = str(params.get("sessionKey") or "").strip()
    fingerprint = str(params.get("fingerprint") or "").strip()
    bundle_id = str(params.get("bundle_id") or params.get("bundleId") or "").strip()
    host = str(params.get("host") or "").strip()
    value = bundle_id or host
    if not session_key or not fingerprint or not value:
        return False
    workspace = _workspace_param(params)
    context = current_tool_run_context()
    if context is None:
        context = resolved_run_context_overlay(session_key, workspace)
    if context is None:
        context = RunContext(
            run_mode=RunMode.STANDARD,
            workspace=workspace,
            source="resolved_overlay",
        )
    grant = TemporaryGrant(
        kind="bundle" if bundle_id else "domain",
        value=value,
        fingerprint=fingerprint,
    )
    if grant not in context.temporary_grants:
        context = replace(
            context,
            temporary_grants=context.temporary_grants + (grant,),
            source="resolved_overlay",
        )
    remember_resolved_run_context(session_key, workspace, context)

    try:
        from opensquilla.tools.types import current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        ctx = None
    if (
        ctx is not None
        and str(getattr(ctx, "session_key", None) or "").strip() == session_key
        and _normalize_workspace(getattr(ctx, "workspace_dir", None))
        == _normalize_workspace(workspace)
    ):
        ctx.sandbox_run_context = context
    return True


def build_path_approval_params(
    decision: MountDecision,
    *,
    session_key: str | None,
    workspace: str | None,
) -> dict[str, object] | None:
    if decision.status != "request":
        return None
    params: dict[str, object] = {
        "approvalKind": "sandbox_path",
        "path": decision.normalized_path,
        "access": decision.access,
        "choices": _standard_approval_choices(),
    }
    if session_key:
        params["sessionKey"] = session_key
    if workspace:
        params["workspace"] = workspace
    return params


def is_sandbox_approval_kind(approval_kind: str | None) -> bool:
    return str(approval_kind or "").strip() in SANDBOX_APPROVAL_KINDS


def request_sandbox_approval(
    params: dict[str, object] | None,
    *,
    approval_id: str | None = None,
    message: str,
    denied_message: str | None = None,
) -> dict[str, object] | None:
    from opensquilla.tools.run_mode import full_host_access_active

    if full_host_access_active():
        return None

    from opensquilla.gateway.approval_queue import get_approval_queue

    if not isinstance(params, dict):
        raise ValueError("sandbox_approval_params_required")

    captured_generation = _capture_current_tool_run_context_for_approval(params)

    if _current_tool_context_is_channel():
        admin_identity = _channel_admin_approval_identity()
        if admin_identity is None:
            return _approval_payload(
                "approval_denied",
                "",
                params,
                message=_channel_sandbox_approval_disabled_message(),
            )
        # A channel-admin turn may ask for approval: stamping senderId routes
        # the prompt back into the originating chat (card / ``/approve <code>``)
        # where only that sender can resolve it. Non-admin channel callers keep
        # the hard deny above — pairing alone never unlocks host-side asks.
        sender_id, session_key = admin_identity
        params.setdefault("senderId", sender_id)
        if session_key:
            params.setdefault("sessionKey", session_key)

    queue = get_approval_queue()
    created_generation = False
    if approval_id is None:
        denied_approval_id = denied_sandbox_approval_id(params)
        if denied_approval_id is not None:
            approval_id = denied_approval_id
            status = "approval_denied"
        else:
            approval_id = pending_sandbox_approval_id(queue, params)
            if approval_id is not None:
                status = "approval_pending"
            else:
                approval_id = queue.request(namespace="exec", params=params)
                status = "approval_required"
                created_generation = True
    else:
        entry = queue.get(approval_id)
        if entry.namespace != "exec":
            raise ValueError(f"Approval does not belong to exec namespace: {approval_id}")
        matching_approval = True
        try:
            _validate_matching_approval_params(entry.params, params)
        except ValueError:
            matching_approval = False
        if not entry.resolved:
            status = "approval_pending"
        elif not entry.approved:
            remember_sandbox_approval_denial(params, approval_id)
            status = "approval_denied"
        elif matching_approval:
            approval_id = queue.request(namespace="exec", params=params)
            status = "approval_required"
            created_generation = True
        else:
            approval_id = pending_sandbox_approval_id(queue, params)
            if approval_id is not None:
                status = "approval_pending"
            else:
                approval_id = queue.request(namespace="exec", params=params)
                status = "approval_required"
                created_generation = True
    if status in {"approval_required", "approval_pending"}:
        _bind_approval_run_context_generation(
            queue,
            approval_id,
            captured_generation,
            created=created_generation,
        )
    if status == "approval_denied":
        message = denied_message or _default_denied_sandbox_approval_message()
    return _approval_payload(status, approval_id, params, message=message)


def _default_denied_sandbox_approval_message() -> str:
    return (
        "The user denied this sandbox request. Do not ask for the same access "
        "again in this turn. Explain that the requested operation cannot "
        "continue from the current sandbox unless the user changes sandbox "
        "settings."
    )


def _channel_sandbox_approval_disabled_message() -> str:
    return (
        "Channel sandbox approvals are disabled. Ask a channel admin to run "
        "/sandbox full for this session, or retry from WebUI/CLI where sandbox "
        "approvals can be resolved."
    )


def _current_tool_context_is_channel() -> bool:
    try:
        from opensquilla.tools.types import CallerKind, current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        return False
    if ctx is None:
        return False
    caller_kind = getattr(ctx, "caller_kind", None)
    return caller_kind is CallerKind.CHANNEL or str(caller_kind) == CallerKind.CHANNEL.value


def _channel_admin_approval_identity() -> tuple[str, str] | None:
    """Sender/session identity for a channel-admin turn, else ``None``.

    ``is_owner`` on a channel ToolContext is set from
    ``channel_admin_senders`` at dispatch time; the sender id recorded here is
    the one the channel resolver later compares against, so a missing sender
    id means the prompt could never be resolved and we fall back to the deny.
    """
    try:
        from opensquilla.tools.types import current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        return None
    if ctx is None or not getattr(ctx, "is_owner", False):
        return None
    sender_id = str(getattr(ctx, "sender_id", "") or "").strip()
    if not sender_id:
        return None
    session_key = str(getattr(ctx, "session_key", "") or "").strip()
    return sender_id, session_key


def _capture_current_tool_run_context_for_approval(
    params: dict[str, object],
) -> _ApprovalRunContextGeneration | None:
    session_key = str(params.get("sessionKey") or "").strip()
    if not session_key:
        return None
    workspace = _workspace_param(params)
    try:
        from opensquilla.tools.types import current_tool_context

        tool_context = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        return None
    if tool_context is None:
        return None
    if str(getattr(tool_context, "session_key", None) or "").strip() != session_key:
        return None
    context = getattr(tool_context, "sandbox_run_context", None)
    if getattr(tool_context, "_sandbox_run_context_fresh", False):
        context = current_tool_run_context()
    if not isinstance(context, RunContext):
        return None
    context_workspace = context.workspace or getattr(
        tool_context,
        "workspace_dir",
        None,
    )
    if _normalize_workspace(context_workspace) != _normalize_workspace(workspace):
        return None
    approval_key = _sandbox_approval_generation_key(params)
    workspace_alias = _binding_alias_key(session_key, workspace)
    if approval_key is None or workspace_alias is None:
        return None
    return _ApprovalRunContextGeneration(
        approval_key=approval_key,
        session_key=session_key,
        workspace_alias=workspace_alias,
        binding=_workspace_binding_identity(workspace),
        context=context,
    )


def _bind_approval_run_context_generation(
    queue: Any,
    approval_id: str,
    generation: _ApprovalRunContextGeneration | None,
    *,
    created: bool,
) -> None:
    approval_id = str(approval_id or "").strip()
    if not approval_id:
        return
    _ensure_approval_generation_cleanup_listener(queue)
    if created:
        _APPROVAL_GENERATION_OWNED_IDS.add(approval_id)
        if approval_id not in _APPROVAL_RUN_CONTEXT_GENERATIONS:
            _APPROVAL_RUN_CONTEXT_GENERATIONS[approval_id] = generation
            if generation is not None:
                remember_resolved_run_context(
                    generation.session_key,
                    generation.workspace_alias[1],
                    generation.context,
                    guard_workspace=True,
                )
        return
    if approval_id in _APPROVAL_GENERATION_OWNED_IDS:
        return
    # A pending request from another generation/process has no provable
    # captured authority in this process. Mark it owned but leave the snapshot
    # absent so a later apply fails closed instead of adopting this retry's
    # current workspace identity.
    _APPROVAL_GENERATION_OWNED_IDS.add(approval_id)


def _ensure_approval_generation_cleanup_listener(queue: Any) -> None:
    if queue in _APPROVAL_QUEUE_LISTENER_REMOVERS:
        return

    def _cleanup(event: str, info: dict[str, Any]) -> None:
        if event != "resolved":
            return
        approval_id = str(info.get("id") or "").strip()
        if approval_id:
            _APPROVAL_RUN_CONTEXT_GENERATIONS.pop(approval_id, None)

    add_listener = getattr(queue, "add_event_listener", None)
    if callable(add_listener):
        _APPROVAL_QUEUE_LISTENER_REMOVERS[queue] = add_listener(_cleanup)


def _approval_mutation_manager(
    session_manager: Any,
    session_key: str,
    workspace: str | None,
    authoritative_context: RunContext | None = None,
) -> Any:
    context = authoritative_context
    if context is None:
        context = resolved_run_context_overlay(session_key, workspace)
    if context is None:
        return session_manager
    return _AuthoritativeRunContextManager(
        session_manager,
        session_key,
        context,
    )


def _approval_run_context_for_choice(
    params: dict[str, Any],
    approval_id: str | None,
) -> RunContext | None:
    approval_key = _sandbox_approval_generation_key(params)
    matching_authority_exists = any(
        generation is not None and generation.approval_key == approval_key
        for generation in _APPROVAL_RUN_CONTEXT_GENERATIONS.values()
    )
    normalized_id = str(approval_id or "").strip()
    if not normalized_id:
        if matching_authority_exists:
            from opensquilla.project_workspaces import ProjectWorkspaceStateError

            raise ProjectWorkspaceStateError("unavailable")
        return None
    if normalized_id not in _APPROVAL_GENERATION_OWNED_IDS:
        if matching_authority_exists:
            from opensquilla.project_workspaces import ProjectWorkspaceStateError

            raise ProjectWorkspaceStateError("unavailable")
        # Direct queue callers predate generation-bound project authority.
        return None
    if normalized_id not in _APPROVAL_RUN_CONTEXT_GENERATIONS:
        from opensquilla.project_workspaces import ProjectWorkspaceStateError

        raise ProjectWorkspaceStateError("unavailable")
    generation = _APPROVAL_RUN_CONTEXT_GENERATIONS[normalized_id]
    if generation is None:
        if matching_authority_exists:
            from opensquilla.project_workspaces import ProjectWorkspaceStateError

            raise ProjectWorkspaceStateError("unavailable")
        return None
    if approval_key != generation.approval_key:
        from opensquilla.project_workspaces import ProjectWorkspaceStateError

        raise ProjectWorkspaceStateError("unavailable")
    session_key = _require_session_key(params)
    workspace = _workspace_param(params)
    workspace_alias = _binding_alias_key(session_key, workspace)
    if (
        session_key != generation.session_key
        or workspace_alias != generation.workspace_alias
    ):
        from opensquilla.project_workspaces import ProjectWorkspaceStateError

        raise ProjectWorkspaceStateError("unavailable")
    current_binding = _workspace_binding_identity(workspace)
    if generation.binding is None or current_binding is None:
        from opensquilla.project_workspaces import ProjectWorkspaceStateError

        raise ProjectWorkspaceStateError("unavailable")
    if current_binding != generation.binding:
        from opensquilla.project_workspaces import ProjectWorkspaceStateError

        raise ProjectWorkspaceStateError("canonical_changed")
    return generation.context


def remember_sandbox_approval_denial(
    params: dict[str, Any] | None,
    approval_id: str,
) -> None:
    key = _sandbox_approval_key(params)
    if key is not None:
        _DENIED_SANDBOX_APPROVALS[key] = approval_id


def denied_sandbox_approval_id(params: dict[str, Any] | None) -> str | None:
    key = _sandbox_approval_key(params)
    if key is None:
        return None
    return _DENIED_SANDBOX_APPROVALS.get(key)


def pending_sandbox_approval_id(queue: Any, params: dict[str, Any] | None) -> str | None:
    key = _pending_sandbox_approval_key(params)
    if key is None:
        return None
    for pending in queue.list_pending("exec"):
        approval_id = str(pending.get("id") or "")
        if not approval_id:
            continue
        if _pending_sandbox_approval_key(pending.get("params")) == key:
            return approval_id
    return None


def clear_sandbox_approval_denials(session_key: str | None = None) -> None:
    target_session = str(session_key or "").strip()
    if not target_session:
        _DENIED_SANDBOX_APPROVALS.clear()
        return

    for key in list(_DENIED_SANDBOX_APPROVALS):
        try:
            payload = json.loads(key)
        except (TypeError, json.JSONDecodeError):
            continue
        if str(payload.get("sessionKey") or "").strip() == target_session:
            _DENIED_SANDBOX_APPROVALS.pop(key, None)


def deny_matching_pending_sandbox_approvals(
    queue: Any,
    params: dict[str, Any] | None,
    *,
    exclude_approval_id: str | None = None,
) -> int:
    key = _sandbox_approval_key(params)
    if key is None:
        return 0
    count = 0
    for pending in queue.list_pending("exec"):
        approval_id = str(pending.get("id") or "")
        if not approval_id or approval_id == exclude_approval_id:
            continue
        pending_params = pending.get("params")
        if _sandbox_approval_key(pending_params) != key:
            continue
        queue.resolve(approval_id, False, allow_idempotent=True)
        remember_sandbox_approval_denial(pending_params, approval_id)
        count += 1
    return count


def validate_sandbox_approval_choice(
    params: dict[str, Any] | None,
    *,
    choice: str | None,
    approved: bool,
) -> dict[str, Any] | None:
    if not isinstance(params, dict):
        return None
    approval_kind = str(params.get("approvalKind") or "").strip()
    if approval_kind not in SANDBOX_APPROVAL_KINDS:
        return None
    choice_id = str(choice or "").strip()
    if not choice_id:
        raise ValueError("choice_required_for_sandbox_approval")
    raw_choices = params.get("choices")
    if not isinstance(raw_choices, list):
        raise ValueError("sandbox_choices_missing")
    for item in raw_choices:
        if isinstance(item, dict) and str(item.get("id") or "").strip() == choice_id:
            choice_payload = dict(item)
            if bool(choice_payload.get("approved", True)) != approved:
                raise ValueError("choice_approved_mismatch")
            return choice_payload
    raise ValueError(f"unknown_sandbox_choice:{choice_id}")


async def apply_sandbox_approval_choice(
    params: dict[str, Any] | None,
    *,
    approval_id: str | None = None,
    choice: str | None,
    approved: bool,
    session_manager: Any,
    config: Any,
) -> None:
    if not approved or not isinstance(params, dict):
        return

    approval_kind = str(params.get("approvalKind") or "").strip()
    if not approval_kind or not choice:
        return

    validate_sandbox_approval_choice(params, choice=choice, approved=approved)
    authoritative_context = _approval_run_context_for_choice(
        params,
        approval_id,
    )

    if approval_kind == "sandbox_network":
        await _apply_network_choice(
            params,
            choice,
            session_manager=session_manager,
            config=config,
            authoritative_context=authoritative_context,
        )
    elif approval_kind == "sandbox_path":
        await _apply_path_choice(
            params,
            choice,
            session_manager=session_manager,
            config=config,
            authoritative_context=authoritative_context,
        )
    if approval_id:
        _APPROVAL_RUN_CONTEXT_GENERATIONS.pop(str(approval_id), None)


def context_with_temporary_network_grants(context: Any, *, fingerprint: str) -> Any:
    if not getattr(context, "temporary_grants", ()):
        return context

    domains = list(getattr(context, "domains", ()))
    bundles = list(getattr(context, "bundles", ()))
    seen = {grant.domain for grant in domains}
    seen_bundles = {grant.bundle_id for grant in bundles}
    changed = False
    for grant in context.temporary_grants:
        if grant.expires_after != "once" or grant.fingerprint != fingerprint:
            continue
        if grant.kind == "domain":
            if grant.value in seen:
                continue
            domains.append(
                DomainGrant(
                    domain=grant.value,
                    scope="once",
                    source="temporary",
                )
            )
            seen.add(grant.value)
            changed = True
        elif grant.kind == "bundle":
            if grant.value in seen_bundles or not expand_package_bundle(grant.value):
                continue
            bundles.append(
                PackageBundleGrant(
                    bundle_id=grant.value,
                    scope="once",
                    source="temporary",
                )
            )
            seen_bundles.add(grant.value)
            changed = True
    if not changed:
        return context
    return replace(context, domains=tuple(domains), bundles=tuple(bundles))


def current_tool_run_context() -> RunContext | None:
    try:
        from opensquilla.tools.types import current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        return None
    if ctx is None:
        return None
    base = getattr(ctx, "sandbox_run_context", None)
    if not isinstance(base, RunContext):
        base = None
    elif base.source in _DURABLE_TEMPORARY_GRANT_SOURCES and base.temporary_grants:
        base = replace(base, temporary_grants=())
    overlay = resolved_run_context_overlay(
        getattr(ctx, "session_key", None),
        getattr(ctx, "workspace_dir", None),
    )
    return merge_run_context_overlay(
        base,
        overlay,
        authoritative_base=bool(
            getattr(ctx, "_sandbox_run_context_fresh", False)
        ),
    )


def current_tool_mounts() -> list[dict[str, object]]:
    try:
        from opensquilla.tools.types import current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        return []
    if ctx is None:
        return []
    context = current_tool_run_context()
    if context is not None:
        return [{"path": mount.path, "access": mount.access} for mount in context.mounts]
    merged: dict[str, dict[str, object]] = {}
    raw_mounts = getattr(ctx, "sandbox_mounts", None)
    if isinstance(raw_mounts, list):
        for mount in raw_mounts:
            if not isinstance(mount, dict):
                continue
            path = str(mount.get("path") or "").strip()
            if not path:
                continue
            merged[path] = {
                "path": path,
                "access": str(mount.get("access") or "ro").strip() or "ro",
            }
    return list(merged.values())


def grant_temporary_mount_for_current_tool(
    decision: MountDecision,
    *,
    prefer_file: bool = False,
) -> bool:
    if decision.status != "request" or decision.access not in {"ro", "rw"}:
        return False
    try:
        from opensquilla.tools.types import current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        return False
    if ctx is None:
        return False

    path = _temporary_mount_path(decision, prefer_file=prefer_file)
    access = decision.access
    ctx.sandbox_mounts = [
        mount
        for mount in list(getattr(ctx, "sandbox_mounts", ()) or ())
        if str(mount.get("path") or "").strip() != path
    ] + [{"path": path, "access": access}]

    context = current_tool_run_context()
    if context is None:
        context = RunContext(
            run_mode=RunMode.TRUSTED,
            workspace=getattr(ctx, "workspace_dir", None),
            source="temporary",
        )
    grant = MountGrant(path=path, access=access, scope="chat")
    mounts = tuple(mount for mount in context.mounts if mount.path != path) + (grant,)
    updated = replace(context, mounts=mounts, source="resolved_overlay")
    ctx.sandbox_run_context = updated
    remember_resolved_run_context(
        getattr(ctx, "session_key", None),
        getattr(ctx, "workspace_dir", None),
        updated,
    )
    return True


def _temporary_mount_path(decision: MountDecision, *, prefer_file: bool = False) -> str:
    if decision.access != "rw":
        return decision.normalized_path
    candidate = Path(decision.normalized_path).expanduser().resolve(strict=False)
    if candidate.exists() and candidate.is_dir():
        return str(candidate)
    if os.name == "nt" and candidate.exists():
        return str(candidate)
    if prefer_file and candidate.exists():
        return str(candidate)
    return str(candidate.parent)


def resolved_run_context_overlay(
    session_key: str | None,
    workspace: str | None,
) -> RunContext | None:
    key = _overlay_key(session_key, workspace)
    if key is None:
        return None
    alias_key = _binding_alias_key(session_key, workspace)
    if alias_key is not None:
        key = _RESOLVED_RUN_CONTEXT_BINDING_ALIASES.get(alias_key, key)
    expected_binding = _RESOLVED_RUN_CONTEXT_BINDINGS.get(key)
    if expected_binding is not None:
        from opensquilla.project_workspaces import ProjectWorkspaceStateError

        current_binding = _workspace_binding_identity(workspace)
        if (
            expected_binding.canonical_path is None
            or current_binding is None
        ):
            raise ProjectWorkspaceStateError("unavailable")
        if current_binding != expected_binding:
            raise ProjectWorkspaceStateError("canonical_changed")
    return _RESOLVED_RUN_CONTEXT_OVERLAYS.get(key)


def remember_resolved_run_context(
    session_key: str | None,
    workspace: str | None,
    context: RunContext,
    *,
    session_manager: Any | None = None,
    config: Any | None = None,
    guard_workspace: bool = False,
) -> None:
    key = _overlay_key(session_key, workspace)
    if key is None:
        return
    _RESOLVED_RUN_CONTEXT_OVERLAYS[key] = context
    if guard_workspace and key[1] is not None:
        alias_key = _binding_alias_key(session_key, workspace)
        if alias_key is not None:
            _RESOLVED_RUN_CONTEXT_BINDING_ALIASES[alias_key] = key
        _RESOLVED_RUN_CONTEXT_BINDINGS[key] = (
            _workspace_binding_identity(workspace)
            or _WorkspaceBindingIdentity(
                canonical_path=None,
                device=None,
                inode=None,
            )
        )
    if session_manager is not None and config is not None:
        _RESOLVED_RUN_CONTEXT_PERSISTORS[key] = (session_manager, config)


def reset_resolved_run_context_overlays() -> None:
    _RESOLVED_RUN_CONTEXT_OVERLAYS.clear()
    _RESOLVED_RUN_CONTEXT_PERSISTORS.clear()
    _RESOLVED_RUN_CONTEXT_BINDINGS.clear()
    _RESOLVED_RUN_CONTEXT_BINDING_ALIASES.clear()
    _APPROVAL_RUN_CONTEXT_GENERATIONS.clear()
    _APPROVAL_GENERATION_OWNED_IDS.clear()
    for remove_listener in list(_APPROVAL_QUEUE_LISTENER_REMOVERS.values()):
        remove_listener()
    _APPROVAL_QUEUE_LISTENER_REMOVERS.clear()
    _DENIED_SANDBOX_APPROVALS.clear()


def prune_once_mount_grants(session_key: str | None = None) -> int:
    """Drop ``scope=='once'`` mount grants from the resolved run-context overlays.

    "Allow once" must authorize at most the granting turn and then re-prompt
    (issue #418). The grant is applied to the in-memory overlay when the user
    approves; calling this at the start of the NEXT turn expires it so a later
    access to the same path is re-evaluated instead of silently allowed for the
    whole session. Returns the number of ``once`` mounts pruned. When
    ``session_key`` is set, only that session's overlays are touched.
    """
    target = str(session_key or "").strip()
    pruned = 0
    for key in list(_RESOLVED_RUN_CONTEXT_OVERLAYS):
        if target and key[0] != target:
            continue
        context = _RESOLVED_RUN_CONTEXT_OVERLAYS[key]
        once_mounts = [m for m in context.mounts if m.scope == "once"]
        if not once_mounts:
            continue
        remaining = tuple(m for m in context.mounts if m.scope != "once")
        pruned += len(once_mounts)
        updated = replace(context, mounts=remaining)
        _RESOLVED_RUN_CONTEXT_OVERLAYS[key] = updated
    return pruned


def consume_temporary_network_grant(
    *,
    session_key: str | None,
    workspace: str | None,
    host: str,
    fingerprint: str,
) -> bool:
    if not fingerprint:
        return False

    overlay = resolved_run_context_overlay(session_key, workspace)
    consumed = False
    if overlay is not None:
        updated = _without_matching_temporary_network_grants(
            overlay,
            host=host,
            fingerprint=fingerprint,
        )
        if updated is not overlay:
            remember_resolved_run_context(session_key, workspace, updated)
            consumed = True

    try:
        from opensquilla.tools.types import current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        return consumed

    if ctx is None:
        return consumed
    run_context = getattr(ctx, "sandbox_run_context", None)
    if not isinstance(run_context, RunContext):
        return consumed
    if (
        _normalize_workspace(getattr(ctx, "workspace_dir", None)) != _normalize_workspace(workspace)
        or str(getattr(ctx, "session_key", None) or "").strip() != str(session_key or "").strip()
    ):
        return consumed
    updated = _without_matching_temporary_network_grants(
        run_context,
        host=host,
        fingerprint=fingerprint,
    )
    if updated is run_context:
        return consumed
    ctx.sandbox_run_context = updated
    return True


async def consume_persisted_temporary_network_grant(
    *,
    session_key: str | None,
    workspace: str | None,
    host: str,
    fingerprint: str,
    session_manager: Any | None = None,
    config: Any | None = None,
) -> bool:
    key = _overlay_key(session_key, workspace)
    if key is None or not fingerprint:
        return False
    manager = session_manager
    cfg = config
    if manager is None or cfg is None:
        persisted = _RESOLVED_RUN_CONTEXT_PERSISTORS.get(key)
        if persisted is None:
            return False
        manager, cfg = persisted
    authoritative = resolved_run_context_overlay(key[0], workspace)
    try:
        persisted_existing = await get_run_context(
            manager,
            key[0],
            config=cfg,
            workspace=workspace,
        )
    except Exception:
        return False
    persisted_updated = _without_matching_temporary_network_grants(
        persisted_existing,
        host=host,
        fingerprint=fingerprint,
    )
    if persisted_updated is persisted_existing:
        return False
    updated = (
        _without_matching_temporary_network_grants(
            authoritative,
            host=host,
            fingerprint=fingerprint,
        )
        if authoritative is not None
        else persisted_updated
    )
    try:
        persisted_context = await persist_run_context(manager, key[0], updated)
    except Exception:
        return False
    remember_resolved_run_context(
        key[0],
        workspace,
        persisted_context,
        session_manager=manager,
        config=cfg,
    )
    return True


def has_temporary_network_grant(context: RunContext | None, *, host: str, fingerprint: str) -> bool:
    if context is None or not fingerprint:
        return False
    return any(
        grant.expires_after == "once"
        and grant.fingerprint == fingerprint
        and (
            (grant.kind == "domain" and domain_matches(grant.value, host))
            or (
                grant.kind == "bundle"
                and any(
                    domain_matches(domain, host) for domain in expand_package_bundle(grant.value)
                )
            )
        )
        for grant in context.temporary_grants
    )


def merge_run_context_overlay(
    base: RunContext | None,
    overlay: RunContext | None,
    *,
    authoritative_base: bool = False,
) -> RunContext | None:
    if overlay is None:
        return base
    if base is None:
        return overlay
    if authoritative_base:
        return RunContext(
            run_mode=base.run_mode,
            workspace=base.workspace,
            mounts=_merge_mount_grants(base.mounts, overlay.mounts),
            domains=_merge_grants(base.domains, overlay.domains),
            bundles=_merge_grants(base.bundles, overlay.bundles),
            public_network=_merge_grants(
                base.public_network,
                overlay.public_network,
            ),
            temporary_grants=_merge_temporary_grants(
                base.temporary_grants,
                overlay.temporary_grants,
            ),
            run_mode_source=base.run_mode_source,
            source=base.source,
        )
    return RunContext(
        run_mode=overlay.run_mode,
        workspace=overlay.workspace or base.workspace,
        mounts=overlay.mounts,
        domains=overlay.domains,
        bundles=overlay.bundles,
        public_network=overlay.public_network,
        temporary_grants=_merge_temporary_grants(base.temporary_grants, overlay.temporary_grants),
        run_mode_source=overlay.run_mode_source,
        source=overlay.source,
    )


def _merge_grants(
    base: tuple[Any, ...],
    overlay: tuple[Any, ...],
) -> tuple[Any, ...]:
    merged = list(base)
    for grant in overlay:
        if grant not in merged:
            merged.append(grant)
    return tuple(merged)


def _merge_mount_grants(
    base: tuple[MountGrant, ...],
    overlay: tuple[MountGrant, ...],
) -> tuple[MountGrant, ...]:
    merged: dict[str, MountGrant] = {}
    for grant in (*base, *overlay):
        try:
            key = os.path.normcase(str(normalize_path(grant.path)))
        except (OSError, RuntimeError, ValueError):
            key = os.path.normcase(str(grant.path).strip())
        existing = merged.get(key)
        if (
            existing is not None
            and existing.access == "rw"
            and grant.access != "rw"
        ):
            continue
        merged[key] = grant
    return tuple(merged.values())


def _approval_payload(
    status: str,
    approval_id: str,
    params: dict[str, object],
    *,
    message: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "approval_id": approval_id,
        "message": message,
    }
    for key in (
        "approvalKind",
        "choices",
        "path",
        "access",
        "host",
        "bundle_id",
        "workspace",
        "sessionKey",
        "fingerprint",
    ):
        if key in params:
            payload[key] = params[key]
    return payload


def _validate_matching_approval_params(
    existing: dict[str, Any],
    expected: dict[str, object],
) -> None:
    if str(existing.get("approvalKind") or "") != str(expected.get("approvalKind") or ""):
        raise ValueError("approval_does_not_match_requested_sandbox_action")
    for key in (
        "path",
        "host",
        "bundle_id",
        "access",
        "fingerprint",
        "sessionKey",
        "workspace",
    ):
        existing_value = existing.get(key)
        expected_value = expected.get(key)
        if expected_value is None:
            continue
        if existing_value != expected_value:
            raise ValueError("approval_does_not_match_requested_sandbox_action")


async def _apply_network_choice(
    params: dict[str, Any],
    choice: str,
    *,
    session_manager: Any,
    config: Any,
    authoritative_context: RunContext | None = None,
) -> None:
    session_key = _require_session_key(params)
    workspace = _workspace_param(params)
    bundle_id = str(params.get("bundle_id") or params.get("bundleId") or "").strip()

    if choice not in {"allow_once", "allow_same_type"}:
        raise ValueError(f"unknown_network_choice:{choice}")

    if choice == "allow_once":
        fingerprint = _require_text(params, "fingerprint")
        value = bundle_id or _require_text(params, "host")
        kind = "bundle" if bundle_id else "domain"
        existing = authoritative_context
        if existing is None:
            existing = resolved_run_context_overlay(session_key, workspace)
        if existing is None:
            existing = await get_run_context(
                session_manager,
                session_key,
                config=config,
                workspace=workspace,
            )
        grant = TemporaryGrant(
            kind=kind,
            value=value,
            fingerprint=fingerprint,
        )
        if grant in existing.temporary_grants:
            return
        updated = replace(
            existing,
            temporary_grants=existing.temporary_grants + (grant,),
            source="resolved_overlay",
        )
        remember_resolved_run_context(
            session_key,
            workspace,
            updated,
            session_manager=session_manager,
            config=config,
        )
        return

    if bundle_id:
        mutation_manager = _approval_mutation_manager(
            session_manager,
            session_key,
            workspace,
            authoritative_context,
        )
        updated = await enable_bundle_grant(
            mutation_manager,
            session_key,
            bundle_id=bundle_id,
            scope="chat",
            config=config,
            workspace=workspace,
        )
        remember_resolved_run_context(
            session_key,
            workspace,
            updated,
            session_manager=session_manager,
            config=config,
        )
        return

    host = _require_text(params, "host")
    mutation_manager = _approval_mutation_manager(
        session_manager,
        session_key,
        workspace,
        authoritative_context,
    )
    updated = await add_domain_grant(
        mutation_manager,
        session_key,
        domain=host,
        scope="chat",
        config=config,
        workspace=workspace,
    )
    remember_resolved_run_context(
        session_key,
        workspace,
        updated,
        session_manager=session_manager,
        config=config,
    )


async def _apply_path_choice(
    params: dict[str, Any],
    choice: str,
    *,
    session_manager: Any,
    config: Any,
    authoritative_context: RunContext | None = None,
) -> None:
    session_key = _require_session_key(params)
    workspace = _workspace_param(params)
    path = _require_text(params, "path")
    requested_access = str(params.get("access") or "").strip()

    if choice not in {"allow_once", "allow_same_type"}:
        raise ValueError(f"unknown_path_choice:{choice}")
    if requested_access not in {"ro", "rw"}:
        raise ValueError("path_access_required")

    mutation_manager = _approval_mutation_manager(
        session_manager,
        session_key,
        workspace,
        authoritative_context,
    )
    updated = await add_mount_grant(
        mutation_manager,
        session_key,
        path=path,
        access=requested_access,
        scope="once" if choice == "allow_once" else "chat",
        config=config,
        workspace=workspace,
    )
    remember_resolved_run_context(
        session_key,
        workspace,
        updated,
        session_manager=session_manager,
        config=config,
    )


def _require_session_key(params: dict[str, Any]) -> str:
    value = params.get("sessionKey") or params.get("session_id")
    text = str(value or "").strip()
    if not text:
        raise ValueError("session_key_required")
    return text


def _require_text(params: dict[str, Any], key: str) -> str:
    text = str(params.get(key) or "").strip()
    if not text:
        raise ValueError(f"{key}_required")
    return text


def _workspace_param(params: dict[str, Any]) -> str | None:
    workspace = str(params.get("workspace") or "").strip()
    return workspace or None


def _sandbox_approval_key(params: dict[str, Any] | None) -> str | None:
    if not isinstance(params, dict):
        return None
    approval_kind = str(params.get("approvalKind") or "").strip()
    if approval_kind not in SANDBOX_APPROVAL_KINDS:
        return None
    fields: dict[str, object] = {
        "kind": approval_kind,
        "sessionKey": str(params.get("sessionKey") or "").strip(),
        "workspace": _normalize_workspace(str(params.get("workspace") or "").strip()),
    }
    if approval_kind == "sandbox_path":
        fields["path"] = str(params.get("path") or "").strip()
        fields["access"] = str(params.get("access") or "").strip()
    elif approval_kind == "sandbox_network":
        bundle_id = str(params.get("bundle_id") or params.get("bundleId") or "").strip()
        if bundle_id:
            fields["bundle_id"] = bundle_id
        else:
            fields["host"] = str(params.get("host") or "").strip().casefold()
        fields["fingerprint"] = str(params.get("fingerprint") or "").strip()
    return json.dumps(fields, ensure_ascii=False, sort_keys=True)


def _sandbox_approval_generation_key(
    params: dict[str, Any] | None,
) -> str | None:
    if not isinstance(params, dict):
        return None
    approval_kind = str(params.get("approvalKind") or "").strip()
    if approval_kind not in SANDBOX_APPROVAL_KINDS:
        return None
    session_key = str(params.get("sessionKey") or "").strip()
    workspace_alias = _binding_alias_key(
        session_key,
        _workspace_param(params),
    )
    fields: dict[str, object] = {
        "kind": approval_kind,
        "sessionKey": session_key,
        "workspace": workspace_alias[1] if workspace_alias is not None else None,
    }
    if approval_kind == "sandbox_path":
        fields["path"] = str(params.get("path") or "").strip()
        fields["access"] = str(params.get("access") or "").strip()
    else:
        bundle_id = str(params.get("bundle_id") or params.get("bundleId") or "").strip()
        if bundle_id:
            fields["bundle_id"] = bundle_id
        else:
            fields["host"] = str(params.get("host") or "").strip().casefold()
        fields["fingerprint"] = str(params.get("fingerprint") or "").strip()
    return json.dumps(fields, ensure_ascii=False, sort_keys=True)


def _pending_sandbox_approval_key(params: dict[str, Any] | None) -> str | None:
    if not isinstance(params, dict):
        return None
    approval_kind = str(params.get("approvalKind") or "").strip()
    if approval_kind not in SANDBOX_APPROVAL_KINDS:
        return None
    fields: dict[str, object] = {
        "kind": approval_kind,
        "sessionKey": str(params.get("sessionKey") or "").strip(),
        "workspace": _normalize_workspace(str(params.get("workspace") or "").strip()),
    }
    if approval_kind == "sandbox_path":
        fields["path"] = str(params.get("path") or "").strip()
        fields["access"] = str(params.get("access") or "").strip()
    elif approval_kind == "sandbox_network":
        bundle_id = str(params.get("bundle_id") or params.get("bundleId") or "").strip()
        if params.get("reviewer") == "auto_review":
            fields["network"] = (
                f"bundle:{bundle_id}"
                if bundle_id
                else str(params.get("host") or "").strip().casefold()
            )
            fields["fingerprint"] = str(params.get("fingerprint") or "").strip()
        else:
            fields["network"] = f"bundle:{bundle_id}" if bundle_id else "public"
    return json.dumps(fields, ensure_ascii=False, sort_keys=True)


def _overlay_key(session_key: str | None, workspace: str | None) -> tuple[str, str | None] | None:
    key = str(session_key or "").strip()
    if not key:
        return None
    return key, _normalize_workspace(workspace)


def _normalize_workspace(workspace: str | None) -> str | None:
    text = str(workspace or "").strip()
    if not text:
        return None
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return text


def _binding_alias_key(
    session_key: str | None,
    workspace: str | None,
) -> tuple[str, str | None] | None:
    key = str(session_key or "").strip()
    if not key:
        return None
    text = str(workspace or "").strip()
    if not text:
        return key, None
    try:
        lexical = os.path.abspath(os.path.expanduser(text))
        normalized = os.path.normcase(lexical).replace("\\", "/")
    except (OSError, RuntimeError, ValueError):
        normalized = text
    return key, normalized


def _workspace_binding_identity(
    workspace: str | None,
) -> _WorkspaceBindingIdentity | None:
    text = str(workspace or "").strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser().resolve(strict=True)
        if not candidate.is_dir():
            return None
        with os.scandir(candidate):
            pass
        stat_result = candidate.stat()
    except (OSError, RuntimeError, ValueError):
        return None
    return _WorkspaceBindingIdentity(
        canonical_path=os.path.normcase(str(candidate)).replace("\\", "/"),
        device=int(stat_result.st_dev),
        inode=int(stat_result.st_ino),
    )


def _merge_temporary_grants(
    base: tuple[TemporaryGrant, ...],
    overlay: tuple[TemporaryGrant, ...],
) -> tuple[TemporaryGrant, ...]:
    merged: dict[tuple[str, str, str, str], TemporaryGrant] = {
        (grant.kind, grant.value, grant.fingerprint, grant.expires_after): grant for grant in base
    }
    for grant in overlay:
        merged[(grant.kind, grant.value, grant.fingerprint, grant.expires_after)] = grant
    return tuple(merged.values())


def _without_matching_temporary_network_grants(
    context: RunContext,
    *,
    host: str,
    fingerprint: str,
) -> RunContext:
    grants = tuple(
        grant
        for grant in context.temporary_grants
        if not (
            grant.expires_after == "once"
            and grant.fingerprint == fingerprint
            and (
                (grant.kind == "domain" and domain_matches(grant.value, host))
                or (
                    grant.kind == "bundle"
                    and any(
                        domain_matches(domain, host)
                        for domain in expand_package_bundle(grant.value)
                    )
                )
            )
        )
    )
    if grants == context.temporary_grants:
        return context
    return replace(context, temporary_grants=grants, source="saved")


__all__ = [
    "apply_sandbox_approval_choice",
    "build_network_approval_params",
    "build_package_bundle_approval_params",
    "build_path_approval_params",
    "clear_sandbox_approval_denials",
    "consume_persisted_temporary_network_grant",
    "consume_temporary_network_grant",
    "context_with_temporary_network_grants",
    "current_tool_mounts",
    "current_tool_run_context",
    "grant_auto_review_network_once",
    "grant_temporary_mount_for_current_tool",
    "has_temporary_network_grant",
    "merge_run_context_overlay",
    "prune_once_mount_grants",
    "remember_resolved_run_context",
    "reset_resolved_run_context_overlays",
    "resolved_run_context_overlay",
    "validate_sandbox_approval_choice",
]
