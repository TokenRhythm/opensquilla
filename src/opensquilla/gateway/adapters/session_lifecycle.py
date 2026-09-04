"""Gateway Adapter for the transport-neutral Session lifecycle Module."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NoReturn, cast

from opensquilla.application.session_lifecycle import (
    CreatedSession,
    CreateSession,
    DeleteSessions,
    DeleteSessionsResult,
    ForkedSession,
    ForkSession,
    OptionalSessionBinding,
    RenamedSession,
    RenameSession,
    SessionAgentNotFoundError,
    SessionAvailabilityRequirement,
    SessionCreationKind,
    SessionDeploymentModelRequiredError,
    SessionDisplayNameError,
    SessionDisplayNameErrorReason,
    SessionForkMode,
    SessionForkPoint,
    SessionLifecycle,
    SessionLifecycleUnavailableError,
    SessionModelRequest,
    SessionUpdatedField,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcUnavailableError
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.session.keys import canonicalize_session_key, normalize_agent_id

type DeploymentFields = tuple[bool, str | None, bool, str | None]


def _model_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _aliased_optional_string(
    values: Mapping[str, Any],
    *names: str,
) -> tuple[bool, str | None]:
    resolved: list[str | None] = []
    for name in names:
        if name not in values:
            continue
        value = values[name]
        if value is not None and not isinstance(value, str):
            raise ValueError(f"params.{name} must be a string or null")
        resolved.append(value.strip() or None if isinstance(value, str) else None)
    if not resolved:
        return False, None
    if any(value != resolved[0] for value in resolved[1:]):
        raise ValueError(f"params aliases for {names[0]} must agree")
    return True, resolved[0]


def _deployment_fields(values: Mapping[str, Any]) -> DeploymentFields:
    provider_present, provider = _aliased_optional_string(
        values,
        "provider",
        "providerOverride",
        "provider_override",
    )
    auth_profile_present, auth_profile = _aliased_optional_string(
        values,
        "authProfile",
        "authProfileOverride",
        "auth_profile",
        "auth_profile_override",
    )
    return (
        provider_present,
        provider.lower() if provider else None,
        auth_profile_present,
        auth_profile,
    )


def _require_key(values: dict[str, Any] | None) -> str:
    if not isinstance(values, dict) or "key" not in values:
        raise ValueError("params.key is required")
    key = values["key"]
    if not isinstance(key, str):
        raise ValueError("params.key must be a string")
    return canonicalize_session_key(key)


def _optional_string(values: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        if name not in values:
            continue
        value = values[name]
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"params.{name} must be a string")
        return value.strip() or None
    return None


def _optional_non_empty_aliased_string(
    values: Mapping[str, Any],
    *names: str,
) -> str | None:
    present = [(name, values[name]) for name in names if name in values]
    if not present:
        return None
    normalized: list[tuple[str, str]] = []
    for name, value in present:
        if not isinstance(value, str):
            raise ValueError(f"params.{name} must be a string")
        value = value.strip()
        if not value:
            raise ValueError(f"params.{name} must not be empty")
        normalized.append((name, value))
    if len({value for _, value in normalized}) != 1:
        joined = " and ".join(f"params.{name}" for name, _ in normalized)
        raise ValueError(f"{joined} must match when both aliases are provided")
    return normalized[0][1]


def _raise_deployment_model_required() -> NoReturn:
    raise RpcHandlerError(
        code="INVALID_PARAMS",
        message="A session provider binding requires an explicit model.",
        details={"reason": "session_deployment_requires_explicit_model"},
    )


class GatewaySessionLifecycleAdapter:
    """Decode v4 requests and project one injected Application Module."""

    def __init__(
        self,
        context: RpcContext,
        application: SessionLifecycle,
    ) -> None:
        self._context = context
        self._application = application

    async def create(self, params: object) -> dict[str, Any]:
        values = dict(params) if isinstance(params, Mapping) else {}
        agent_id = normalize_agent_id(values.get("agentId") or "main")
        display_name_value = values.get("displayName")
        display_name = cast(str | None, display_name_value)
        message = values.get("message")
        if message is not None and not isinstance(message, str):
            raise ValueError("params.message must be a string")
        raw_model = values.get("model")
        model = _model_value(raw_model)
        raw_kind = values.get("kind") or values.get("sessionKind")
        normalized_kind = str(raw_kind or "").strip().lower().replace("_", "-")
        if normalized_kind == "web":
            normalized_kind = "webchat"
        kind = (
            SessionCreationKind.CLI
            if normalized_kind == "cli"
            else SessionCreationKind.WEBCHAT
            if normalized_kind == "webchat"
            else SessionCreationKind.DEFAULT
        )
        raw_workspace_id = values.get("workspaceId", values.get("workspace_id"))
        workspace_id: str | None = None
        if raw_workspace_id is not None:
            if not isinstance(raw_workspace_id, str) or not raw_workspace_id.strip():
                raise ValueError("workspaceId must be a non-empty string")
            workspace_id = raw_workspace_id.strip()
            if not self._context.principal.is_owner:
                raise RpcHandlerError(
                    "OWNER_REQUIRED",
                    "Project workspaces require a locally proven owner.",
                )
        (
            provider_present,
            provider,
            auth_profile_present,
            auth_profile,
        ) = _deployment_fields(values)
        command = CreateSession(
            agent_id=agent_id,
            display_name=display_name,
            initial_message=cast(str | None, message),
            model=SessionModelRequest(
                value=model,
                explicitly_supplied="model" in values,
                string_supplied=isinstance(raw_model, str),
            ),
            kind=kind,
            workspace_id=workspace_id,
            provider=OptionalSessionBinding(provider_present, provider),
            auth_profile=OptionalSessionBinding(auth_profile_present, auth_profile),
        )
        try:
            result = await self._application.create(command)
        except SessionDeploymentModelRequiredError:
            _raise_deployment_model_required()
        except SessionAgentNotFoundError as exc:
            raise RpcHandlerError(
                "agent.not_found",
                str(exc),
                details={"agentId": exc.agent_id},
            ) from exc
        except SessionLifecycleUnavailableError as exc:
            messages = {
                SessionAvailabilityRequirement.CREATE_WITH_MESSAGE: (
                    "sessions.create(message=...) requires a session manager"
                ),
                SessionAvailabilityRequirement.CREATE_WITH_DEPLOYMENT: (
                    "sessions.create deployment overrides require a session manager"
                ),
                SessionAvailabilityRequirement.CREATE_WITH_WORKSPACE: (
                    "sessions.create(workspaceId=...) requires a session manager"
                ),
            }
            raise RpcUnavailableError(messages[exc.requirement]) from exc
        return created_session_to_v4(result)

    async def fork(self, params: object, *, require_through_turn: bool) -> dict[str, Any]:
        values = cast(dict[str, Any] | None, params)
        key = _require_key(values)
        assert isinstance(values, dict)
        title = values.get("title")
        if title is not None and not isinstance(title, str):
            raise ValueError("params.title must be a string")
        before_message_id = _optional_string(
            values,
            "beforeMessageId",
            "before_message_id",
        )
        through_turn_id = _optional_non_empty_aliased_string(
            values,
            "throughTurnId",
            "through_turn_id",
        )
        if require_through_turn:
            if any(name in values for name in ("beforeMessageId", "before_message_id")):
                raise ValueError("sessions.forkThroughTurn does not accept beforeMessageId")
            if through_turn_id is None:
                raise ValueError("params.throughTurnId is required")
        if before_message_id and through_turn_id:
            raise ValueError("beforeMessageId and throughTurnId are mutually exclusive")
        point = (
            SessionForkPoint(SessionForkMode.THROUGH_TURN, through_turn_id)
            if through_turn_id is not None
            else SessionForkPoint(SessionForkMode.BEFORE_MESSAGE, before_message_id)
            if before_message_id is not None
            else SessionForkPoint(SessionForkMode.FULL)
        )
        try:
            result = await self._application.fork(
                ForkSession(parent_key=key, title=cast(str | None, title), point=point)
            )
        except SessionLifecycleUnavailableError as exc:
            raise KeyError("No session manager available") from exc
        return forked_session_to_v4(result)

    async def rename(self, params: object) -> dict[str, Any]:
        values = cast(dict[str, Any] | None, params)
        key = _require_key(values)
        assert isinstance(values, dict)
        unexpected = sorted(set(values) - {"key", "displayName"})
        if unexpected:
            raise RpcHandlerError(
                code="INVALID_PARAMS",
                message="sessions.rename accepts only key and displayName.",
                details={"unexpected_fields": unexpected},
            )
        display_name = values.get("displayName")
        if not isinstance(display_name, str) or not display_name.strip():
            raise RpcHandlerError(
                code="INVALID_PARAMS",
                message="displayName must be a non-empty string.",
                details={"field": "displayName"},
            )
        try:
            result = await self._application.rename(
                RenameSession(session_key=key, display_name=display_name)
            )
        except SessionLifecycleUnavailableError as exc:
            raise KeyError("No session manager available") from exc
        except SessionDisplayNameError as exc:
            message = (
                "displayName must be a non-empty string."
                if exc.reason is SessionDisplayNameErrorReason.REQUIRED
                else "displayName must be at most 512 characters."
            )
            raise RpcHandlerError(
                code="INVALID_PARAMS",
                message=message,
                details={"field": "displayName", "maxLength": exc.max_chars},
            ) from exc
        return renamed_session_to_v4(result)

    async def delete(self, params: object) -> dict[str, Any]:
        if self._context.session_manager is None:
            raise KeyError("No session manager available")
        if get_session_storage(self._context.session_manager) is None:
            raise KeyError("No session storage available")
        values = params if isinstance(params, Mapping) else None
        keys: Sequence[object] = ()
        if values is not None:
            if "keys" in values:
                candidate = values["keys"]
                keys = cast(Sequence[object], candidate)
            elif "key" in values:
                keys = (values["key"],)
        if not keys:
            raise ValueError("params.key or params.keys is required")
        result = await self._application.delete(DeleteSessions(cast(tuple[str, ...], tuple(keys))))
        return deleted_sessions_to_v4(result)


def created_session_to_v4(result: CreatedSession) -> dict[str, Any]:
    payload: dict[str, Any] = {"key": result.key, "sessionId": result.session_id}
    if result.seeded_message:
        payload["seededMessage"] = True
    if result.note is not None:
        payload["note"] = result.note
    return payload


def forked_session_to_v4(result: ForkedSession) -> dict[str, Any]:
    payload: dict[str, Any] = {"key": result.key, "parentKey": result.parent_key}
    if result.mode is SessionForkMode.THROUGH_TURN:
        payload["forkMode"] = "through_turn"
        payload["throughTurnId"] = result.through_turn_id
    return payload


def renamed_session_to_v4(result: RenamedSession) -> dict[str, Any]:
    wire_fields = {
        SessionUpdatedField.DISPLAY_NAME: "displayName",
    }
    return {
        "key": result.key,
        "updated": [wire_fields[field] for field in result.updated_fields],
    }


def deleted_sessions_to_v4(result: DeleteSessionsResult) -> dict[str, Any]:
    return {
        "deleted": list(result.deleted),
        "errors": [
            f"{failure.requested_key}: {failure.message}"
            for failure in result.failures
        ],
    }


__all__ = [
    "GatewaySessionLifecycleAdapter",
    "created_session_to_v4",
    "deleted_sessions_to_v4",
    "forked_session_to_v4",
    "renamed_session_to_v4",
]
