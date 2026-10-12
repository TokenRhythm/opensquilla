"""Thin RPC surface for durable session Goals."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from typing import Any, cast

from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.session.goals import (
    GoalConflictError,
    GoalValidationError,
    normalize_goal_objective,
)

_d = get_dispatcher()


def _require_params(params: dict | None) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return params


def _string_param(
    params: dict | None,
    *names: str,
    required: bool = True,
) -> str | None:
    values = _require_params(params)
    for name in names:
        if name not in values:
            continue
        value = values[name]
        if not isinstance(value, str):
            raise ValueError(f"params.{name} must be a string")
        value = value.strip()
        if value:
            return value
        if required:
            raise ValueError(f"params.{name} must not be blank")
        return None
    if required:
        raise ValueError(f"params.{names[0]} is required")
    return None


def _int_param(params: dict | None, *names: str) -> int:
    values = _require_params(params)
    for name in names:
        if name not in values:
            continue
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"params.{name} must be a positive integer")
        return cast(int, value)
    raise ValueError(f"params.{names[0]} is required")


def _nonnegative_int_param(params: dict | None, *names: str) -> int:
    values = _require_params(params)
    for name in names:
        if name not in values:
            continue
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"params.{name} must be a non-negative integer")
        return cast(int, value)
    raise ValueError(f"params.{names[0]} is required")


def _bool_param(
    params: dict | None,
    *names: str,
    default: bool = False,
) -> bool:
    values = _require_params(params)
    for name in names:
        if name not in values:
            continue
        value = values[name]
        if not isinstance(value, bool):
            raise ValueError(f"params.{name} must be a boolean")
        return value
    return default


def _session_key(params: dict | None) -> str:
    from opensquilla.session.keys import canonicalize_session_key

    value = _string_param(params, "sessionKey", "session_key", "key")
    assert value is not None
    key = canonicalize_session_key(value)
    if not key:
        raise ValueError("params.sessionKey must not be blank")
    return key


def _source_kind(params: dict | None) -> str:
    source_kind = _string_param(
        params,
        "sourceKind",
        "source_kind",
        required=False,
    )
    if source_kind is None and isinstance(params, dict):
        source = params.get("source")
        if isinstance(source, dict):
            raw = source.get("caller_kind", source.get("callerKind"))
            if isinstance(raw, str):
                source_kind = raw.strip()
    return "cli" if source_kind == "cli" else "web"


def _goal_service(ctx: RpcContext) -> Any:
    runtime = getattr(ctx, "task_runtime", None)
    service = getattr(runtime, "goal_service", None)
    if service is None:
        raise RpcUnavailableError("Goal service is not configured")
    return service


def _source_scope(service: Any, ctx: RpcContext, source_kind: str) -> str:
    build = getattr(service, "source_scope", None)
    if callable(build):
        return str(build(ctx, source_kind=source_kind))
    return f"{source_kind}:{ctx.principal.role}"[:256]


async def _translate_goal_errors(
    operation: Awaitable[dict[str, Any]],
    *,
    service: Any | None = None,
) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], await operation)
    except GoalValidationError as exc:
        raise RpcHandlerError(
            exc.code,
            str(exc),
            retryable=False,
            accepted=False,
        ) from exc
    except GoalConflictError as exc:
        details: dict[str, Any] | None = None
        if exc.current is not None and exc.code in {
            "STALE_GOAL",
            "GOAL_ACTIVE",
            "GOAL_BUSY",
        }:
            snapshot = (
                await service.snapshot(exc.current)
                if service is not None and callable(getattr(service, "snapshot", None))
                else None
            )
            details = {"goal": snapshot}
        raise RpcHandlerError(
            exc.code,
            str(exc),
            details=details,
            retryable=exc.code in {"STALE_GOAL", "SESSION_GENERATION_CHANGED"},
            accepted=False,
        ) from exc


@_d.method("goals.capabilities", scope="operator.read")
async def _handle_goals_capabilities(params: dict | None, ctx: RpcContext) -> dict:
    # The key is accepted so callers can use a uniform session-scoped request;
    # capabilities themselves are process/config scoped and have no side effects.
    if params is not None:
        _session_key(params)
    service = _goal_service(ctx)
    config = getattr(service, "config", None)
    return {
        "supported": True,
        "executionEnabled": bool(service.execution_enabled),
        "maxTurns": int(getattr(config, "max_turns", 50)),
        "runtimeBudgetSeconds": int(
            getattr(config, "runtime_budget_seconds", 3600)
        ),
        "methods": [
            "goals.set",
            "goals.status",
            "goals.edit",
            "goals.pause",
            "goals.resume",
            "goals.reattach",
            "goals.clear",
        ],
    }


@_d.method("goals.status", scope="operator.read")
async def _handle_goals_status(params: dict | None, ctx: RpcContext) -> dict:
    service = _goal_service(ctx)
    return await _translate_goal_errors(
        service.status(_session_key(params)),
        service=service,
    )


def _uuid_v4_param(params: dict | None, *names: str) -> str:
    value = _string_param(params, *names)
    assert value is not None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"params.{names[0]} must be a canonical UUID v4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"params.{names[0]} must be a canonical UUID v4")
    return value


def _objective_param(params: dict | None) -> str:
    """Route every objective shape through the stable domain validator."""

    values = _require_params(params)
    raw: object = values.get("objective", values.get("message"))
    try:
        return normalize_goal_objective(raw)
    except GoalValidationError as exc:
        raise RpcHandlerError(
            exc.code,
            str(exc),
            retryable=False,
            accepted=False,
        ) from exc


@_d.method("goals.set", scope="operator.write")
async def _handle_goals_set(params: dict | None, ctx: RpcContext) -> dict:
    service = _goal_service(ctx)
    objective = _objective_param(params)
    client_request_id = _uuid_v4_param(
        params,
        "clientRequestId",
        "client_request_id",
    )
    client_message_id = _uuid_v4_param(
        params,
        "clientMessageId",
        "client_message_id",
    )
    assert client_request_id is not None
    assert client_message_id is not None
    return await _translate_goal_errors(
        service.set(
            ctx,
            session_key=_session_key(params),
            objective=objective,
            client_request_id=client_request_id,
            client_message_id=client_message_id,
            source_kind=_source_kind(params),
        ),
        service=service,
    )


def _mutation_params(params: dict | None) -> tuple[str, str, int, str]:
    expected_goal_id = _string_param(params, "expectedGoalId", "expected_goal_id")
    client_request_id = _uuid_v4_param(
        params,
        "clientRequestId",
        "client_request_id",
    )
    assert expected_goal_id is not None
    return (
        _session_key(params),
        expected_goal_id,
        _int_param(params, "expectedStateRevision", "expected_state_revision"),
        client_request_id,
    )


@_d.method("goals.edit", scope="operator.write")
async def _handle_goals_edit(params: dict | None, ctx: RpcContext) -> dict:
    service = _goal_service(ctx)
    key, goal_id, revision, request_id = _mutation_params(params)
    objective = _objective_param(params)
    source_kind = _source_kind(params)
    return await _translate_goal_errors(
        service.edit(
            ctx,
            session_key=key,
            expected_goal_id=goal_id,
            expected_state_revision=revision,
            objective=objective,
            client_request_id=request_id,
            source_scope=_source_scope(service, ctx, source_kind),
            source_kind=source_kind,
        ),
        service=service,
    )


@_d.method("goals.pause", scope="operator.write")
async def _handle_goals_pause(params: dict | None, ctx: RpcContext) -> dict:
    service = _goal_service(ctx)
    key, goal_id, revision, request_id = _mutation_params(params)
    source_kind = _source_kind(params)
    return await _translate_goal_errors(
        service.pause(
            session_key=key,
            expected_goal_id=goal_id,
            expected_state_revision=revision,
            client_request_id=request_id,
            source_scope=_source_scope(service, ctx, source_kind),
        ),
        service=service,
    )


@_d.method("goals.resume", scope="operator.write")
async def _handle_goals_resume(params: dict | None, ctx: RpcContext) -> dict:
    service = _goal_service(ctx)
    key, goal_id, revision, request_id = _mutation_params(params)
    source_kind = _source_kind(params)
    return await _translate_goal_errors(
        service.resume(
            ctx,
            session_key=key,
            expected_goal_id=goal_id,
            expected_state_revision=revision,
            client_request_id=request_id,
            source_scope=_source_scope(service, ctx, source_kind),
            source_kind=source_kind,
        ),
        service=service,
    )


@_d.method("goals.reattach", scope="operator.write")
async def _handle_goals_reattach(params: dict | None, ctx: RpcContext) -> dict:
    service = _goal_service(ctx)
    session_id = _string_param(params, "sessionId", "session_id")
    expected_goal_id = _string_param(
        params,
        "expectedGoalId",
        "expected_goal_id",
    )
    takeover = _bool_param(params, "takeover", default=False)
    continuity_token = _string_param(
        params,
        "continuityToken",
        "continuity_token",
        required=not takeover,
    )
    if continuity_token is not None and len(continuity_token) > 256:
        raise ValueError("params.continuityToken is too long")
    assert session_id is not None
    assert expected_goal_id is not None
    return await _translate_goal_errors(
        service.reattach(
            ctx,
            session_key=_session_key(params),
            session_id=session_id,
            epoch=_nonnegative_int_param(params, "epoch", "sessionEpoch", "session_epoch"),
            expected_goal_id=expected_goal_id,
            continuity_token=continuity_token,
            source_kind=_source_kind(params),
            takeover=takeover,
        ),
        service=service,
    )


@_d.method("goals.clear", scope="operator.write")
async def _handle_goals_clear(params: dict | None, ctx: RpcContext) -> dict:
    service = _goal_service(ctx)
    key, goal_id, revision, request_id = _mutation_params(params)
    source_kind = _source_kind(params)
    return await _translate_goal_errors(
        service.clear(
            session_key=key,
            expected_goal_id=goal_id,
            expected_state_revision=revision,
            client_request_id=request_id,
            source_scope=_source_scope(service, ctx, source_kind),
        ),
        service=service,
    )
