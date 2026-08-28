"""Cron domain RPC handlers (Tier 2)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TypeGuard

import structlog

from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.project_workspaces import (
    ProjectWorkspaceStateError,
    resolve_validated_project_workspace,
)
from opensquilla.sandbox.run_context import resolve_default_run_mode
from opensquilla.sandbox.run_mode_policy import (
    coerce_run_mode_for_principal,
    default_run_mode_for_principal,
    principal_has_host_execute,
)
from opensquilla.scheduler.payloads import (
    REMINDER_KIND,
    SYSTEM_EVENT_KIND,
    make_agent_turn_payload,
    make_reminder_payload,
    make_system_event_payload,
    payload_agent_id,
    payload_kind,
    payload_text,
)
from opensquilla.scheduler.schedule_normalizer import (
    coerce_schedule,
    coerce_schedule_from_params,
)
from opensquilla.scheduler.types import (
    DeliveryConfig,
    DeliveryMode,
    FailureDestination,
    JobStatus,
    ReplyTargetSnapshot,
    ScheduleKind,
    SessionTarget,
)

_d = get_dispatcher()
log = structlog.get_logger(__name__)
_MAX_IDEMPOTENCY_KEY_LENGTH = 256


def _require_scheduler(ctx: RpcContext) -> Any:
    scheduler = getattr(ctx, "cron_scheduler", None)
    if scheduler is None:
        raise RpcUnavailableError("Cron scheduler is not available")
    return scheduler


def _job_to_wire(j: Any) -> dict[str, Any]:
    """Map internal CronJob (dataclass or dict) to the wire format the Cron UI expects."""
    d = asdict(j) if hasattr(j, "__dataclass_fields__") else dict(j)
    status = d.get("status", "pending")
    # Normalise status enum to string
    status_str = status.value if hasattr(status, "value") else str(status)
    payload = d.get("payload") or {}
    session_target = str(d.get("session_target", "isolated"))
    wake_mode = d.get("wake_mode", "now")
    wake_mode_str = wake_mode.value if hasattr(wake_mode, "value") else str(wake_mode)
    raw_delivery = d.get("delivery")
    # Webhook delivery is permitted for any sessionTarget (including main); only
    # channel/announce modes are suppressed for main because the heartbeat
    # pipeline handles routing there. Suppressing webhook for main caused
    # round-trip data loss on read-back.
    if session_target == "main" and raw_delivery is not None:
        if isinstance(raw_delivery, dict):
            mode_value = raw_delivery.get("mode") or ""
        else:
            mode_attr = getattr(raw_delivery, "mode", None)
            mode_value = getattr(mode_attr, "value", "") or str(mode_attr or "")
        mode_norm = mode_value.strip().lower() if isinstance(mode_value, str) else ""
        delivery = raw_delivery if mode_norm == "webhook" else None
    else:
        delivery = raw_delivery
    text = payload_text(payload, session_target)
    kind = payload_kind(payload, session_target)
    schedule_kind_value = d.get("schedule_kind", "cron")
    schedule_kind_str = (
        schedule_kind_value.value
        if hasattr(schedule_kind_value, "value")
        else str(schedule_kind_value)
    )
    last_run = d.get("last_run_at")
    last_status = (
        "error"
        if d.get("last_error")
        else ("ok" if last_run is not None else None)
    )
    return {  # noqa: PIE810 — wire schema favors flat literal dict
        "id": d.get("id"),
        "name": d.get("name", ""),
        # Always serve the normalized expression so the WebUI cron editor can
        # parse it as a 5-field cron. Historical raw text lives in scheduleRaw.
        "expression": d.get("cron_expr", "") or "",
        "prompt": text,
        "message": text,
        "text": text,
        "payloadKind": kind,
        "agentId": payload_agent_id(payload, "main"),
        "workspaceId": payload.get("_workspace_id", ""),
        "workspaceName": payload.get("_workspace_name", ""),
        "workspaceUnavailable": bool(payload.get("_workspace_unavailable")),
        "templateId": payload.get("_template_id", ""),
        "status": status_str,
        "lastStatus": last_status,
        "last_status": last_status,
        "enabled": (
            bool(d.get("enabled", True)) and status_str not in ("paused", "disabled", "deleted")
        ),
        "next_run": _iso(d.get("next_run_at")),
        "last_run": _iso(last_run),
        "lastResult": d.get("last_error"),
        "run_count": d.get("run_count", 0),
        "error_count": d.get("error_count", 0),
        "created_at": _iso(d.get("created_at")),
        "schedule_kind": schedule_kind_str,
        "scheduleKind": schedule_kind_str,
        "schedule_raw": d.get("schedule_raw", ""),
        "scheduleRaw": d.get("schedule_raw", ""),
        "tz": d.get("tz", "") or "",
        "session_target": session_target,
        "sessionTarget": session_target,
        "targetSessionKey": d.get("session_key", ""),
        "originSessionKey": d.get("origin_session_key", ""),
        "timeout_seconds": d.get("timeout_seconds", 600),
        "wakeMode": wake_mode_str,
        "consecutive_errors": d.get("consecutive_errors", 0),
        "delivery": _delivery_to_wire(delivery),
        "toolPolicy": _tool_policy_to_wire(d.get("tool_policy")),
        "runMode": d.get("run_mode", "") or "",
        "elevated": d.get("elevated", "") or "",
        "executionTarget": d.get("execution_target", "") or "",
        "deduplicated": bool(d.get("deduplicated", False)),
    }


async def _apply_workspace_binding(
    params: dict[str, Any],
    payload: dict[str, Any],
    ctx: RpcContext,
) -> None:
    """Validate and persist an optional project-workspace binding."""
    if "workspaceId" not in params and "workspace_id" not in params:
        return
    raw = params.get("workspaceId", params.get("workspace_id"))
    workspace_id = raw.strip() if isinstance(raw, str) else ""
    payload.pop("_workspace_id", None)
    payload.pop("_workspace_name", None)
    payload.pop("_workspace_unavailable", None)
    if not workspace_id:
        return
    storage = get_session_storage(getattr(ctx, "session_manager", None))
    if storage is None:
        raise ValueError("Project workspace storage is unavailable")
    try:
        validated = await resolve_validated_project_workspace(storage, workspace_id)
    except ProjectWorkspaceStateError as exc:
        raise ValueError(f"Project workspace is unavailable: {exc.reason}") from exc
    payload["_workspace_id"] = workspace_id
    payload["_workspace_name"] = validated.workspace.display_name


_WORKSPACE_REQUIRED_TEMPLATE_IDS = frozenset(
    {"weekly-report", "project-risk", "knowledge-review"}
)


def _apply_template_contract(params: dict[str, Any], payload: dict[str, Any]) -> None:
    """Persist template identity and fail closed for project-based automations."""
    raw_template_id = params.get("templateId", params.get("template_id"))
    template_id = raw_template_id.strip() if isinstance(raw_template_id, str) else ""
    if template_id:
        payload["_template_id"] = template_id
    if template_id in _WORKSPACE_REQUIRED_TEMPLATE_IDS and not payload.get("_workspace_id"):
        raise ValueError("This automation template requires a project workspace")


def _idempotency_key_from_params(params: dict[str, Any]) -> str:
    raw = params.get("idempotencyKey", params.get("idempotency_key"))
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("idempotencyKey must be a string")
    key = raw.strip()
    if not key:
        raise ValueError("idempotencyKey must not be empty")
    if len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(
            f"idempotencyKey must be at most {_MAX_IDEMPOTENCY_KEY_LENGTH} characters"
        )
    return key


def _failure_destination_to_wire(fd: Any) -> dict[str, Any] | None:
    if fd is None:
        return None
    if isinstance(fd, dict):
        return {
            "mode": fd.get("mode", "none"),
            "channelName": fd.get("channel_name", ""),
            "channelId": fd.get("channel_id", ""),
            "accountId": fd.get("account_id", ""),
            "webhookUrl": fd.get("webhook_url", "") or "",
        }
    mode = getattr(fd, "mode", None)
    mode_str = getattr(mode, "value", str(mode)) if mode is not None else "none"
    return {
        "mode": mode_str,
        "channelName": getattr(fd, "channel_name", ""),
        "channelId": getattr(fd, "channel_id", ""),
        "accountId": getattr(fd, "account_id", ""),
        "webhookUrl": getattr(fd, "webhook_url", "") or "",
    }


def _delivery_to_wire(delivery: Any) -> dict[str, Any]:
    if delivery is None:
        return {"mode": "none"}
    if isinstance(delivery, dict):
        return {
            "mode": delivery.get("mode", "none"),
            "channelName": delivery.get("channel_name", ""),
            "channelId": delivery.get("channel_id", ""),
            "accountId": delivery.get("account_id", ""),
            "threadId": delivery.get("thread_id", ""),
            "webhookUrl": delivery.get("webhook_url", "") or "",
            "bestEffort": bool(delivery.get("best_effort", False)),
            "failureDestination": _failure_destination_to_wire(
                delivery.get("failure_destination")
            ),
        }
    return {
        "mode": (
            getattr(delivery.mode, "value", str(delivery.mode))
            if hasattr(delivery, "mode")
            else "none"
        ),
        "channelName": getattr(delivery, "channel_name", ""),
        "channelId": getattr(delivery, "channel_id", ""),
        "accountId": getattr(delivery, "account_id", ""),
        "threadId": getattr(delivery, "thread_id", ""),
        "webhookUrl": getattr(delivery, "webhook_url", "") or "",
        "bestEffort": bool(getattr(delivery, "best_effort", False)),
        "failureDestination": _failure_destination_to_wire(
            getattr(delivery, "failure_destination", None)
        ),
    }


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value if str(item).strip()]
    raise ValueError("toolPolicy list fields must be strings or arrays")


def _normalize_tool_policy(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("toolPolicy must be an object")
    result: dict[str, Any] = {}
    if "profile" in raw:
        profile = raw.get("profile")
        result["profile"] = None if profile is None else str(profile)
    for key in ("allow", "deny"):
        if key in raw:
            result[key] = _as_string_list(raw.get(key))
    if "alsoAllow" in raw or "also_allow" in raw:
        result["also_allow"] = _as_string_list(raw.get("alsoAllow", raw.get("also_allow")))
    return result


def _tool_policy_from_params(params: dict[str, Any]) -> dict[str, Any]:
    if "toolPolicy" not in params and "tool_policy" not in params:
        return {}
    return _normalize_tool_policy(params.get("toolPolicy", params.get("tool_policy")))


def _tool_policy_to_wire(policy: Any) -> dict[str, Any]:
    normalized = _normalize_tool_policy(policy or {})
    return {
        "profile": normalized.get("profile"),
        "allow": normalized.get("allow", []),
        "alsoAllow": normalized.get("also_allow", []),
        "deny": normalized.get("deny", []),
    }


def _manual_run_to_wire(result: Any) -> dict[str, Any]:
    status = getattr(result, "status", "")
    status_str = status.value if hasattr(status, "value") else str(status)
    execution = getattr(result, "execution", None)
    if status_str == "accepted" and execution is not None:
        return {
            "success": execution.success,
            "status": status_str,
            "runId": execution.id,
            "reply": execution.summary,
            "error": execution.error,
            "duration_ms": (
                int((execution.finished_at - execution.started_at).total_seconds() * 1000)
                if execution.finished_at and execution.started_at
                else None
            ),
        }

    body = {
        "success": False,
        "status": status_str or "blocked",
        "reason": getattr(result, "reason", "") or status_str,
        "error": getattr(result, "error", None),
    }
    current_status = getattr(result, "current_status", "")
    if current_status:
        body["currentStatus"] = current_status
    backoff_until = getattr(result, "backoff_until", None)
    if backoff_until is not None:
        body["backoffUntil"] = _iso(backoff_until)
    return body


def _iso(dt: object) -> str | None:
    """Convert datetime to ISO string, pass through strings, return None otherwise."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


def _coerce_schedule(raw: Any) -> tuple[ScheduleKind, str, str]:
    if not isinstance(raw, dict):
        raise ValueError(
            "schedule must be an object {kind:'cron'|'every'|'at', ...}; "
            f"got {type(raw).__name__}"
        )
    return coerce_schedule(raw)


def _schedule_from_params(params: dict[str, Any]) -> tuple[ScheduleKind, str, str]:
    """Resolve the structured schedule from RPC params.

    Preferred shape: ``params["schedule"]`` is a discriminated-union object.
    CLI shim: when ``schedule`` is absent and ``expression`` is a non-empty
    string, treat it as ``{kind:'cron', expr, tz}`` so legacy CLI callers
    (cli/cron_cmd.py) keep working without an extra wrapper.
    """
    return coerce_schedule_from_params(params)


def _resolve_session_target(params: dict[str, Any]) -> SessionTarget:
    raw = params.get("sessionTarget")
    if isinstance(raw, str) and raw.strip():
        return SessionTarget(raw)
    payload_kind_param = params.get("payloadKind")
    if payload_kind_param == SYSTEM_EVENT_KIND:
        return SessionTarget.MAIN
    return SessionTarget.ISOLATED


def _resolve_target_session_key(
    params: dict[str, Any],
    session_target: SessionTarget,
) -> str:
    if session_target == SessionTarget.MAIN:
        return (
            params.get("targetSessionKey")
            or params.get("target_session_key")
            or params.get("sessionKey")
            or params.get("session_key")
            or ""
        )
    if session_target in (SessionTarget.CURRENT, SessionTarget.SESSION):
        return (
            params.get("targetSessionKey")
            or params.get("target_session_key")
            or params.get("sessionKey")
            or params.get("session_key")
            or params.get("originSessionKey")
            or ""
        )
    return params.get("targetSessionKey") or params.get("target_session_key") or ""


def _resolve_origin_session_key(params: dict[str, Any], session_target: SessionTarget) -> str:
    if session_target == SessionTarget.MAIN:
        return ""
    return (
        params.get("originSessionKey")
        or params.get("sessionKey")
        or params.get("session_key")
        or ""
    )


def _resolve_wake_mode(params: dict[str, Any], current: Any = "now") -> str:
    raw = params.get("wakeMode", params.get("wake_mode", current))
    value = raw.value if hasattr(raw, "value") else str(raw or "now")
    value = value.strip().lower()
    if value not in {"now", "next-heartbeat"}:
        raise ValueError("wakeMode must be 'now' or 'next-heartbeat'")
    return value


def _is_webhook_delivery(delivery_raw: Any) -> TypeGuard[dict[str, Any]]:
    if not isinstance(delivery_raw, dict):
        return False
    mode = delivery_raw.get("mode")
    return isinstance(mode, str) and mode.strip().lower() == "webhook"


def _build_failure_destination(raw: Any) -> FailureDestination | None:
    """Parse delivery.failureDestination wire payload into a FailureDestination."""
    from opensquilla.scheduler.delivery import validate_webhook_url

    if not isinstance(raw, dict):
        return None
    mode_str = raw.get("mode", "")
    if not isinstance(mode_str, str):
        return None
    mode_norm = mode_str.strip().lower()
    if mode_norm not in ("channel", "webhook", "announce"):
        return None
    if mode_norm == "announce":
        mode_norm = "channel"
    if mode_norm == "webhook":
        url = raw.get("webhookUrl") or raw.get("to") or ""
        if not url:
            raise ValueError(
                "failureDestination mode='webhook' requires webhookUrl"
            )
        validate_webhook_url(str(url))
        return FailureDestination(
            mode=DeliveryMode.WEBHOOK,
            webhook_url=str(url),
            webhook_token=str(raw.get("webhookToken") or raw.get("token") or ""),
        )
    return FailureDestination(
        mode=DeliveryMode.CHANNEL,
        channel_name=str(raw.get("channelName") or raw.get("channel") or ""),
        channel_id=str(raw.get("channelId") or raw.get("to") or ""),
        account_id=str(raw.get("accountId") or ""),
        thread_id=str(raw.get("threadId") or ""),
    )


def _build_webhook_delivery(delivery_raw: dict[str, Any]) -> DeliveryConfig:
    """Construct a webhook DeliveryConfig from an RPC delivery payload."""
    from opensquilla.scheduler.delivery import validate_webhook_url

    url = delivery_raw.get("webhookUrl") or delivery_raw.get("to") or ""
    token = delivery_raw.get("webhookToken") or delivery_raw.get("token") or ""
    best_effort = bool(delivery_raw.get("bestEffort", False))
    validate_webhook_url(str(url))
    failure_destination = _build_failure_destination(
        delivery_raw.get("failureDestination")
    )
    return DeliveryConfig(
        mode=DeliveryMode.WEBHOOK,
        webhook_url=str(url),
        webhook_token=str(token),
        best_effort=best_effort,
        failure_destination=failure_destination,
    )


def _parse_delivery_overrides(delivery_raw: Any) -> dict[str, str] | None:
    if not isinstance(delivery_raw, dict) or not delivery_raw.get("channelName"):
        return None
    return {
        "channel_name": delivery_raw["channelName"],
        "channel_id": delivery_raw.get("channelId", ""),
        "account_id": delivery_raw.get("accountId", ""),
        "thread_id": delivery_raw.get("threadId", ""),
    }


def _ensure_delivery_supported(
    *,
    session_target: SessionTarget,
    delivery_raw: Any,
) -> None:
    if _is_webhook_delivery(delivery_raw):
        # Webhook delivery is permitted for any sessionTarget.
        return
    if session_target != SessionTarget.MAIN:
        return
    if isinstance(delivery_raw, dict) and delivery_raw.get("mode") == "none":
        return
    if _parse_delivery_overrides(delivery_raw) is not None:
        raise ValueError(
            'cron channel delivery config is only supported for sessionTarget="isolated"'
        )


def _originating_reply_target(ctx: RpcContext) -> ReplyTargetSnapshot | None:
    envelope = getattr(ctx, "originating_envelope", None)
    target = getattr(envelope, "reply_target", None)
    if target is None or getattr(target, "kind", None) != "channel":
        return None
    return ReplyTargetSnapshot(
        channel_name=getattr(target, "channel_name", "") or "",
        channel_type=getattr(target, "channel_type", "") or "",
        to=getattr(target, "to", "") or "",
        account_id=getattr(target, "account_id", "") or "",
        thread_id=getattr(target, "thread_id", "") or "",
        request_id=getattr(envelope, "session_id", None),
    )


def _build_payload(
    params: dict[str, Any],
    session_target: SessionTarget,
    *,
    require_text: bool = True,
) -> tuple[str, dict[str, str]]:
    raw_text = params.get("text")
    if raw_text is None:
        raw_text = params.get("prompt")
    if raw_text is None:
        raw_text = params.get("message")
    text = raw_text if isinstance(raw_text, str) else ""
    kind = params.get("payloadKind")
    if not isinstance(kind, str) or not kind:
        kind = SYSTEM_EVENT_KIND if session_target == SessionTarget.MAIN else REMINDER_KIND
    agent_id = params.get("agentId", "main")
    if require_text and not text.strip():
        raise ValueError("Cron text is required")
    if kind == SYSTEM_EVENT_KIND:
        if session_target != SessionTarget.MAIN:
            raise ValueError("payloadKind='system_event' requires sessionTarget='main'")
        return kind, make_system_event_payload(text, agent_id)
    if kind == REMINDER_KIND:
        if session_target == SessionTarget.MAIN:
            raise ValueError("payloadKind='reminder' cannot use sessionTarget='main'")
        return kind, make_reminder_payload(text, agent_id)
    if session_target == SessionTarget.MAIN:
        raise ValueError("payloadKind='agent_turn' cannot use sessionTarget='main'")
    return kind, make_agent_turn_payload(text, agent_id)


def _handler_key_for_payload_kind(kind: str) -> str:
    if kind == SYSTEM_EVENT_KIND:
        return "system_event"
    if kind == REMINDER_KIND:
        return "static_message"
    return "agent_run"


@_d.method("cron.list", scope="operator.read")
async def _handle_cron_list(params: dict | None, ctx: RpcContext) -> list[dict]:
    scheduler = getattr(ctx, "cron_scheduler", None)
    if scheduler is None:
        return []
    jobs = await scheduler.list_jobs()
    result = [_job_to_wire(j) for j in jobs]
    agent_id = (params or {}).get("agentId")
    if agent_id:
        result = [j for j in result if j.get("agentId") == agent_id]
    return result


@_d.method("cron.status", scope="operator.read")
async def _handle_cron_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    scheduler = _require_scheduler(ctx)
    job = await scheduler.get_job(params["id"])
    if job is None:
        raise KeyError(f"Cron job not found: {params['id']}")
    return _job_to_wire(job)


@_d.method("cron.add", scope="operator.admin")
async def _handle_cron_add(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params required: schedule (object) or expression (string)")
    schedule_kind, schedule_value, schedule_tz = _schedule_from_params(params)
    session_target = _resolve_session_target(params)
    payload_kind_name, payload = _build_payload(params, session_target, require_text=True)
    log.info(
        "cron.workspace_contract",
        workspace_requested=bool(params.get("workspaceId") or params.get("workspace_id")),
        template_id=str(params.get("templateId") or params.get("template_id") or ""),
    )
    await _apply_workspace_binding(params, payload, ctx)
    _apply_template_contract(params, payload)
    text = payload_text(payload, session_target)
    target_session_key = _resolve_target_session_key(params, session_target)
    origin_session_key = _resolve_origin_session_key(params, session_target)
    delivery_raw = params.get("delivery")
    _ensure_delivery_supported(session_target=session_target, delivery_raw=delivery_raw)
    scheduler = _require_scheduler(ctx)
    default_run_mode, _source = await resolve_default_run_mode(
        ctx.session_manager,
        ctx.config,
    )
    if ctx.config is None:
        default_run_mode = default_run_mode_for_principal(ctx.principal)
    run_mode = coerce_run_mode_for_principal(default_run_mode, ctx.principal)
    creator_is_owner = ctx.principal.is_owner
    creator_host_execute = principal_has_host_execute(ctx.principal)

    # Webhook delivery bypasses session-based channel inference entirely.
    if _is_webhook_delivery(delivery_raw):
        webhook_delivery = _build_webhook_delivery(delivery_raw)
        return await _finalize_cron_add(
            scheduler=scheduler,
            params=params,
            text=text,
            payload=payload,
            payload_kind_name=payload_kind_name,
            session_target=session_target,
            target_session_key=target_session_key,
            origin_session_key=origin_session_key,
            delivery=webhook_delivery,
            schedule_kind=schedule_kind,
            schedule_value=schedule_value,
            schedule_tz=schedule_tz,
            run_mode=run_mode.value,
            creator_is_owner=creator_is_owner,
            creator_host_execute=creator_host_execute,
        )

    # Infer or parse delivery config
    user_overrides = _parse_delivery_overrides(delivery_raw)

    delivery: DeliveryConfig | None = None
    try:
        from opensquilla.scheduler.delivery import infer_delivery

        sm = getattr(ctx, "session_manager", None)
        if sm is not None and session_target != SessionTarget.MAIN:
            storage = getattr(sm, "_storage", sm)
            sk = origin_session_key
            delivery = await infer_delivery(
                session_storage=storage,
                session_key=sk,
                user_overrides=user_overrides,
            )
    except Exception:
        pass
    if user_overrides is not None and delivery is None:
        delivery = DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name=user_overrides["channel_name"],
            channel_id=user_overrides["channel_id"],
            account_id=user_overrides["account_id"],
            thread_id=user_overrides["thread_id"],
        )
    elif (
        session_target != SessionTarget.MAIN
        and user_overrides is None
        and (snapshot := _originating_reply_target(ctx)) is not None
    ):
        delivery = delivery or DeliveryConfig()
        delivery.originating_reply_target = snapshot

    if isinstance(delivery_raw, dict) and delivery_raw.get("failureDestination") is not None:
        if delivery is None:
            delivery = DeliveryConfig()
        delivery.failure_destination = _build_failure_destination(
            delivery_raw["failureDestination"]
        )

    return await _finalize_cron_add(
        scheduler=scheduler,
        params=params,
        text=text,
        payload=payload,
        payload_kind_name=payload_kind_name,
        session_target=session_target,
        target_session_key=target_session_key,
        origin_session_key=origin_session_key,
        delivery=delivery,
        schedule_kind=schedule_kind,
        schedule_value=schedule_value,
        schedule_tz=schedule_tz,
        run_mode=run_mode.value,
        creator_is_owner=creator_is_owner,
        creator_host_execute=creator_host_execute,
    )


async def _finalize_cron_add(
    *,
    scheduler: Any,
    params: dict[str, Any],
    text: str,
    payload: dict[str, Any],
    payload_kind_name: str,
    session_target: SessionTarget,
    target_session_key: str,
    origin_session_key: str,
    delivery: DeliveryConfig | None,
    schedule_kind: ScheduleKind,
    schedule_value: str,
    schedule_tz: str,
    run_mode: str,
    creator_is_owner: bool,
    creator_host_execute: bool,
) -> dict[str, Any]:
    tz_value = (
        schedule_tz
        or (params.get("tz") if isinstance(params.get("tz"), str) else "")
        or (params.get("timezone") if isinstance(params.get("timezone"), str) else "")
        or ""
    )
    jitter_seconds: float | None = None
    if "jitterSeconds" in params or "staggerSeconds" in params:
        raw_jitter = params.get("jitterSeconds", params.get("staggerSeconds"))
        if isinstance(raw_jitter, (int, float)) and raw_jitter >= 0:
            jitter_seconds = float(raw_jitter)
    elif params.get("exact") is True:
        jitter_seconds = 0.0
    job = await scheduler.add_job(
        name=params.get("name") or text,
        # Only an explicit JSON false changes the legacy create default.  This
        # keeps missing or malformed values backward-compatible while making
        # the Control UI's Enabled switch effective on the initial write.
        enabled=params.get("enabled") is not False,
        handler_key=_handler_key_for_payload_kind(payload_kind_name),
        payload=payload,
        session_target=session_target,
        session_key=target_session_key,
        timeout_seconds=float(params.get("timeout", 600)),
        wake_mode=_resolve_wake_mode(params),
        delivery=delivery,
        origin_session_key=origin_session_key,
        tool_policy=_tool_policy_from_params(params),
        tz=tz_value,
        jitter_seconds=jitter_seconds,
        creator_is_owner=creator_is_owner,
        creator_host_execute=creator_host_execute,
        run_mode=run_mode,
        idempotency_key=_idempotency_key_from_params(params),
        schedule_kind=schedule_kind,
        schedule_value=schedule_value,
        schedule_tz=tz_value,
    )
    # Populate ws_topic
    if job.delivery and not job.delivery.ws_topic:
        job.delivery.ws_topic = f"cron:{job.id}"
        try:
            await scheduler.update_job(job.id, delivery=job.delivery)
        except Exception:
            pass
    return _job_to_wire(job)


# Alias: cron.js sends cron.create for new jobs
_d.method("cron.create", scope="operator.admin")(_handle_cron_add)


@_d.method("cron.update", scope="operator.admin")
async def _handle_cron_update(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    scheduler = _require_scheduler(ctx)

    patch = {}
    if "name" in params:
        patch["name"] = params["name"]

    tz_was_supplied = "tz" in params or "timezone" in params
    if "schedule" in params or "expression" in params:
        sched_kind, sched_value, sched_tz = _schedule_from_params(params)
        patch["schedule_kind"] = sched_kind
        patch["schedule_value"] = sched_value
        schedule_raw = params.get("schedule")
        schedule_tz_was_supplied = (
            isinstance(schedule_raw, dict) and "tz" in schedule_raw
        )
        if sched_kind == ScheduleKind.CRON and (
            sched_tz or tz_was_supplied or schedule_tz_was_supplied
        ):
            patch["schedule_tz"] = sched_tz

    if tz_was_supplied:
        tz_value = params.get("tz") if "tz" in params else params.get("timezone")
        patch["tz"] = tz_value if isinstance(tz_value, str) else ""

    if "enabled" in params:
        # Resolve the enabled toggle but DO NOT early-return: fall through so
        # sibling field updates in the same request (text/schedule/…) are
        # applied too, and so a DISABLED/FAILED job (not just PAUSED) can be
        # revived — those are exactly the states a user runs `--enabled` to fix.
        job = await scheduler.get_job(params["id"])
        if params["enabled"]:
            revivable = {
                JobStatus.PAUSED.value,
                JobStatus.DISABLED.value,
                JobStatus.FAILED.value,
            }
            if job is not None and job.status.value in revivable:
                await scheduler.resume_job(params["id"])
        else:
            await scheduler.pause_job(params["id"])

    current_job = await scheduler.get_job(params["id"])
    if current_job is None:
        raise KeyError(f"Cron job not found: {params['id']}")

    if (
        tz_was_supplied
        and "schedule_kind" not in patch
        and current_job.schedule_kind == ScheduleKind.CRON
    ):
        patch["schedule_kind"] = ScheduleKind.CRON
        patch["schedule_value"] = current_job.cron_expr
        patch["schedule_tz"] = patch.get("tz", "")

    payload_related = any(
        key in params
        for key in (
            "text",
            "prompt",
            "message",
            "payloadKind",
            "agentId",
            "sessionTarget",
            "targetSessionKey",
            "target_session_key",
            "originSessionKey",
            "sessionKey",
            "session_key",
        )
    )
    if payload_related:
        current_text = payload_text(current_job.payload, current_job.session_target)
        merged_params = {
            "text": params.get(
                "text",
                params.get("prompt", params.get("message", current_text)),
            ),
            "payloadKind": params.get(
                "payloadKind",
                payload_kind(current_job.payload, current_job.session_target),
            ),
            "agentId": params.get("agentId", payload_agent_id(current_job.payload)),
            "sessionTarget": params.get(
                "sessionTarget",
                getattr(current_job.session_target, "value", str(current_job.session_target)),
            ),
            "originSessionKey": params.get(
                "originSessionKey",
                params.get(
                    "sessionKey",
                    params.get("session_key", current_job.origin_session_key),
                ),
            ),
        }
        session_target = _resolve_session_target(merged_params)
        if session_target == SessionTarget.MAIN:
            merged_params["targetSessionKey"] = params.get(
                "targetSessionKey",
                params.get(
                    "target_session_key",
                    params.get(
                        "sessionKey",
                        params.get("session_key", current_job.session_key),
                    ),
                ),
            )
        else:
            merged_params["targetSessionKey"] = params.get(
                "targetSessionKey",
                params.get("target_session_key", current_job.session_key),
            )
        payload_kind_name, payload = _build_payload(
            merged_params,
            session_target,
            require_text=False,
        )
        # Preserve trusted scheduler metadata when legacy clients rebuild only
        # the task payload. Explicit workspace/template patches below remain
        # authoritative and can replace or remove the inherited values.
        for key in (
            "_workspace_id",
            "_workspace_name",
            "_workspace_unavailable",
            "_template_id",
        ):
            if key in current_job.payload:
                payload[key] = current_job.payload[key]
        patch["handler_key"] = _handler_key_for_payload_kind(payload_kind_name)
        patch["payload"] = payload
        patch["session_target"] = session_target
        patch["session_key"] = _resolve_target_session_key(merged_params, session_target)
        patch["origin_session_key"] = _resolve_origin_session_key(merged_params, session_target)
        if session_target == SessionTarget.MAIN and "delivery" not in params:
            patch["delivery"] = DeliveryConfig()

    if "workspaceId" in params or "workspace_id" in params:
        workspace_payload = dict(patch.get("payload", current_job.payload))
        await _apply_workspace_binding(params, workspace_payload, ctx)
        patch["payload"] = workspace_payload

    if "templateId" in params or "template_id" in params:
        template_payload = dict(patch.get("payload", current_job.payload))
        if not template_payload.get("_template_id") and current_job.payload.get(
            "_template_id"
        ):
            template_payload["_template_id"] = current_job.payload["_template_id"]
        _apply_template_contract(params, template_payload)
        patch["payload"] = template_payload

    if "payload" in patch:
        effective_template = patch["payload"].get("_template_id", "")
        _apply_template_contract(
            {"templateId": effective_template},
            patch["payload"],
        )

    if "timeout" in params:
        patch["timeout_seconds"] = float(params["timeout"])

    if "wakeMode" in params or "wake_mode" in params:
        patch["wake_mode"] = _resolve_wake_mode(
            params,
            getattr(current_job, "wake_mode", "now"),
        )

    if "delivery" in params:
        delivery_raw = params.get("delivery")
        effective_target = patch.get("session_target", current_job.session_target)
        _ensure_delivery_supported(session_target=effective_target, delivery_raw=delivery_raw)
        if isinstance(delivery_raw, dict) and delivery_raw.get("mode") == "none":
            patch["delivery"] = DeliveryConfig()
        elif _is_webhook_delivery(delivery_raw):
            new_delivery = _build_webhook_delivery(delivery_raw)
            new_delivery.ws_topic = current_job.delivery.ws_topic
            patch["delivery"] = new_delivery
        elif isinstance(delivery_raw, dict) and delivery_raw.get("channelName"):
            patch["delivery"] = DeliveryConfig(
                mode=DeliveryMode.CHANNEL,
                channel_name=delivery_raw["channelName"],
                channel_id=delivery_raw.get("channelId") or delivery_raw.get("to", ""),
                account_id=delivery_raw.get("accountId", ""),
                thread_id=delivery_raw.get("threadId", ""),
                ws_topic=current_job.delivery.ws_topic,
                best_effort=bool(delivery_raw.get("bestEffort", False)),
                failure_destination=_build_failure_destination(
                    delivery_raw.get("failureDestination")
                ),
            )
        elif (
            isinstance(delivery_raw, dict)
            and delivery_raw.get("failureDestination") is not None
        ):
            # Standalone FD patch: keep the existing primary delivery target,
            # only update the failure_destination side.
            existing = current_job.delivery
            patch["delivery"] = DeliveryConfig(
                mode=existing.mode,
                channel_name=existing.channel_name,
                channel_id=existing.channel_id,
                account_id=existing.account_id,
                thread_id=existing.thread_id,
                ws_topic=existing.ws_topic,
                originating_reply_target=existing.originating_reply_target,
                webhook_url=existing.webhook_url,
                webhook_token=existing.webhook_token,
                best_effort=existing.best_effort,
                failure_destination=_build_failure_destination(
                    delivery_raw["failureDestination"]
                ),
            )

    if "toolPolicy" in params or "tool_policy" in params:
        patch["tool_policy"] = _tool_policy_from_params(params)

    if patch:
        job = await scheduler.update_job(params["id"], **patch)
    else:
        job = current_job
    if job is None:
        raise KeyError(f"Cron job not found: {params['id']}")
    return _job_to_wire(job)


@_d.method("cron.remove", scope="operator.admin")
async def _handle_cron_remove(params: dict | None, ctx: RpcContext) -> None:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    scheduler = _require_scheduler(ctx)
    await scheduler.remove_job(params["id"])
    return None


@_d.method("cron.run", scope="operator.admin")
async def _handle_cron_run(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    scheduler = _require_scheduler(ctx)
    result = await scheduler.run_job_now(params["id"])
    return _manual_run_to_wire(result)


@_d.method("cron.runs", scope="operator.read")
async def _handle_cron_runs(params: dict | None, ctx: RpcContext) -> list[dict]:
    if not isinstance(params, dict):
        raise ValueError("params.id is required")
    job_id = params.get("id") or params.get("job_id")
    if not job_id:
        raise ValueError("params.id is required")
    limit = params.get("limit", 20)
    scheduler = getattr(ctx, "cron_scheduler", None)
    if scheduler is None:
        return []
    runs = await scheduler.get_runs(job_id, limit=limit)
    return [
        {
            "id": r.id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "success": r.success,
            "status": "ok" if r.success else "error",
            "duration_ms": (
                int((r.finished_at - r.started_at).total_seconds() * 1000)
                if r.started_at and r.finished_at
                else None
            ),
            "error": r.error,
            "summary": r.summary,
            "sessionKey": r.session_key or None,
            "deliveryStatus": r.delivery_status or None,
        }
        for r in runs
    ]


@_d.method("cron.subscribe", scope="operator.read")
async def _handle_cron_subscribe(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Subscribe this connection to cron events."""
    sub_mgr = getattr(ctx, "subscription_manager", None)
    if sub_mgr is None:
        return {"ok": False, "error": "subscription_manager not available"}
    conn_id = getattr(ctx, "conn_id", None)
    if not conn_id:
        return {"ok": False, "error": "no connection context"}
    job_id = (params or {}).get("jobId")
    topic = f"cron:{job_id}" if job_id else "cron:*"
    sub_mgr.subscribe_topic(conn_id, topic)
    return {"ok": True, "topic": topic}


@_d.method("cron.unsubscribe", scope="operator.read")
async def _handle_cron_unsubscribe(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Unsubscribe this connection from cron events."""
    sub_mgr = getattr(ctx, "subscription_manager", None)
    if sub_mgr is None:
        return {"ok": False, "error": "subscription_manager not available"}
    conn_id = getattr(ctx, "conn_id", None)
    if not conn_id:
        return {"ok": False, "error": "no connection context"}
    job_id = (params or {}).get("jobId")
    topic = f"cron:{job_id}" if job_id else "cron:*"
    sub_mgr.unsubscribe_topic(conn_id, topic)
    return {"ok": True, "topic": topic}
