"""Gateway Adapter for the transport-neutral Session lifecycle Module."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from opensquilla.application.session_lifecycle import (
    CreatedSession,
    CreateSession,
    DeleteSessions,
    DeleteSessionsResult,
    ForkedSession,
    ForkSession,
    ForkSessionSpec,
    NewSession,
    OptionalSessionBinding,
    RenamedSession,
    RenameSession,
    SessionAgentNotFoundError,
    SessionAvailabilityRequirement,
    SessionCreationKind,
    SessionCreationPolicyPort,
    SessionDeletionPort,
    SessionDeploymentModelRequiredError,
    SessionDisplayNameError,
    SessionDisplayNameErrorReason,
    SessionForked,
    SessionForkMode,
    SessionForkPoint,
    SessionIdentity,
    SessionLifecycle,
    SessionLifecycleEventsPort,
    SessionLifecycleStorePort,
    SessionLifecycleUnavailableError,
    SessionModelRequest,
    SessionUpdatedField,
    SessionWorkspaceBinding,
)
from opensquilla.gateway.project_workspace_runtime import map_project_workspace_error
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcUnavailableError
from opensquilla.gateway.session_events import build_sessions_changed_payload
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.project_workspaces import ProjectWorkspaceStateError
from opensquilla.run_mode import RunMode, config_run_mode, project_default_run_mode
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY, RunContext
from opensquilla.session.models import SessionStatus
from opensquilla.session.storage import SessionStorage

type DeploymentFields = tuple[bool, str | None, bool, str | None]
type DeploymentFieldsReader = Callable[[dict[str, Any]], DeploymentFields]
type SessionKeyFactory = Callable[[str, object], str]
type AgentIdNormalizer = Callable[[object], str]
type AgentModelReader = Callable[[RpcContext, str], str | None]
type AgentExistsReader = Callable[[RpcContext, str], Awaitable[bool]]
type DeploymentValidator = Callable[..., None]
type DeploymentModelError = Callable[[], None]
type SessionKeyReader = Callable[[dict[str, Any] | None], str]
type OptionalStringReader = Callable[..., str | None]
type ModelValueReader = Callable[[object], str | None]
type EffectiveAgentReader = Callable[[object, str], str]
type ForkExecutor = Callable[..., Awaitable[object]]
type RenameExecutor = Callable[..., Awaitable[dict[str, Any]]]
type DeleteExecutor = Callable[..., Awaitable[None]]
type EventEmitter = Callable[..., Awaitable[None]]


def _accepts_keyword_arg(
    call: Any,
    name: str,
    *,
    allow_var_keyword: bool,
) -> bool:
    try:
        parameters = inspect.signature(call).parameters
    except (TypeError, ValueError):
        return False
    parameter = parameters.get(name)
    accepts_named_keyword = parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }
    return accepts_named_keyword or (
        allow_var_keyword
        and any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    )


class _ProjectWorkspaceView(Protocol):
    @property
    def workspace_id(self) -> str: ...

    @property
    def path(self) -> object: ...


class _ValidatedWorkspaceView(Protocol):
    @property
    def workspace(self) -> _ProjectWorkspaceView: ...


type WorkspaceResolver = Callable[
    [object, str],
    Awaitable[_ValidatedWorkspaceView],
]


@dataclass(frozen=True, slots=True)
class GatewaySessionLifecycleCallbacks:
    """Existing Gateway primitives reused without copying their Implementations."""

    deployment_fields: DeploymentFieldsReader
    new_session_key: SessionKeyFactory
    normalize_agent_id: AgentIdNormalizer
    agent_model: AgentModelReader
    agent_exists: AgentExistsReader
    validate_deployment: DeploymentValidator
    raise_deployment_model_required: DeploymentModelError
    require_key: SessionKeyReader
    optional_string: OptionalStringReader
    optional_non_empty_aliased_string: OptionalStringReader
    model_value: ModelValueReader
    effective_agent_id: EffectiveAgentReader
    fork_session: ForkExecutor
    rename_session: RenameExecutor
    delete_session: DeleteExecutor
    emit_session_event: EventEmitter
    resolve_project_workspace: WorkspaceResolver


class GatewaySessionLifecyclePorts(
    SessionCreationPolicyPort,
    SessionLifecycleStorePort,
    SessionDeletionPort,
    SessionLifecycleEventsPort,
):
    """Request-scoped concrete Ports; ``RpcContext`` terminates here."""

    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewaySessionLifecycleCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks
        self._manager = context.session_manager
        self._storage = get_session_storage(self._manager)
        self._created_session_owners: dict[str, tuple[str, int]] = {}

    @property
    def available(self) -> bool:
        return self._manager is not None

    @property
    def deletion_available(self) -> bool:
        return self._manager is not None and self._storage is not None

    def new_session_key(self, agent_id: str, kind: SessionCreationKind) -> str:
        wire_kind: str | None = None if kind is SessionCreationKind.DEFAULT else kind.value
        return self._callbacks.new_session_key(agent_id, wire_kind)

    async def default_model(self, agent_id: str) -> str | None:
        return self._callbacks.agent_model(self._context, agent_id)

    async def agent_exists(self, agent_id: str) -> bool:
        return await self._callbacks.agent_exists(self._context, agent_id)

    def validate_deployment(
        self,
        *,
        session_key: str,
        provider: str | None,
        model: str | None,
        auth_profile: str | None,
    ) -> None:
        self._callbacks.validate_deployment(
            self._context,
            session_key=session_key,
            provider=provider,
            model=model,
            auth_profile=auth_profile,
        )

    async def resolve_workspace(self, workspace_id: str) -> SessionWorkspaceBinding:
        if self._storage is None:
            raise RpcUnavailableError(
                "sessions.create(workspaceId=...) requires session storage"
            )
        try:
            validated = await self._callbacks.resolve_project_workspace(
                self._storage,
                workspace_id,
            )
        except ProjectWorkspaceStateError as exc:
            raise map_project_workspace_error(
                exc,
                owner=self._context.principal.is_owner,
            ) from exc
        mode = project_default_run_mode(self._context.config)
        source = (
            "project_default"
            if mode is RunMode.SAFE
            and config_run_mode(self._context.config) is RunMode.FULL
            else "operator_default"
        )
        return SessionWorkspaceBinding(
            workspace_id=validated.workspace.workspace_id,
            path=str(validated.workspace.path),
            run_mode=mode.value,
            run_mode_source=source,
        )

    async def create(self, session: NewSession) -> SessionIdentity:
        if self._manager is None:
            raise RpcUnavailableError("sessions.create requires a session manager")
        create_kwargs: dict[str, Any] = {
            "session_key": session.session_key,
            "agent_id": session.agent_id,
            "display_name": session.display_name,
            "model": session.model,
        }
        if session.provider.present:
            create_kwargs["provider_override"] = session.provider.value
        if session.auth_profile.present:
            create_kwargs["auth_profile_override"] = session.auth_profile.value
            create_kwargs["auth_profile_override_source"] = (
                "rpc" if session.auth_profile.value else None
            )
        if session.workspace is not None:
            workspace = session.workspace
            create_kwargs["workspace_id"] = workspace.workspace_id
            create_kwargs["origin"] = {
                RUN_CONTEXT_ORIGIN_KEY: RunContext(
                    run_mode=RunMode(workspace.run_mode),
                    workspace=workspace.path,
                    run_mode_source=workspace.run_mode_source,
                    source=workspace.source,
                ).to_origin_payload()
            }
        created = await self._manager.create(**create_kwargs)
        session_id = getattr(created, "session_id", None)
        epoch = getattr(created, "epoch", None)
        if (
            isinstance(session_id, str)
            and session_id
            and isinstance(epoch, int)
            and not isinstance(epoch, bool)
            and epoch >= 0
        ):
            self._created_session_owners[str(created.session_key)] = (
                session_id,
                epoch,
            )
        return SessionIdentity(
            session_key=str(created.session_key),
            session_id=str(created.session_id),
        )

    async def append_initial_user_message(self, session_key: str, message: str) -> None:
        if self._manager is None:
            raise RpcUnavailableError(
                "sessions.create(message=...) requires a session manager"
            )
        append_message = self._manager.append_message
        durable_storage = isinstance(self._storage, SessionStorage)
        supports_exact_owner = all(
            _accepts_keyword_arg(
                append_message,
                name,
                allow_var_keyword=False,
            )
            for name in ("expected_session_id", "expected_session_epoch")
        )
        if durable_storage and not supports_exact_owner:
            raise RuntimeError("Session writer cannot enforce a durable owner")
        owner = self._created_session_owners.get(session_key)
        if supports_exact_owner and owner is None:
            raise RuntimeError("Created session has no durable owner")
        owner_kwargs = (
            {
                "expected_session_id": owner[0],
                "expected_session_epoch": owner[1],
            }
            if supports_exact_owner and owner is not None
            else {}
        )
        await append_message(
            session_key,
            role="user",
            content=message,
            **owner_kwargs,
        )

    async def rename(self, session_key: str, display_name: str) -> None:
        if self._manager is None:
            raise KeyError("No session manager available")
        if self._storage is None:
            raise KeyError("No session storage available")
        await self._callbacks.rename_session(
            {"key": session_key, "displayName": display_name},
            self._context,
            key=session_key,
            storage=self._storage,
        )

    async def fork_agent_id(self, parent_key: str) -> str:
        if self._storage is None:
            raise KeyError("No session storage available")
        parent = await self._storage.get_session(parent_key)
        if parent is None:
            raise KeyError(f"Session not found: {parent_key}")
        return self._callbacks.effective_agent_id(parent, parent_key)

    async def fork(self, spec: ForkSessionSpec) -> SessionIdentity:
        if self._manager is None:
            raise KeyError("No session manager available")
        if self._storage is None:
            raise KeyError("No session storage available")
        fork_kwargs: dict[str, Any] = {
            "fork_transcript": True,
            "status": SessionStatus.DONE,
        }
        if spec.point.mode is SessionForkMode.BEFORE_MESSAGE:
            fork_kwargs["fork_before_message_id"] = spec.point.anchor_id
        elif spec.point.mode is SessionForkMode.THROUGH_TURN:
            fork_kwargs["fork_through_turn_id"] = spec.point.anchor_id
        child = await self._callbacks.fork_session(
            self._context,
            self._storage,
            spec.parent_key,
            spec.child_key,
            explicit_title=spec.title,
            **fork_kwargs,
        )
        return SessionIdentity(
            session_key=str(getattr(child, "session_key")),
            session_id=str(getattr(child, "session_id")),
        )

    async def delete_one(self, canonical_key: str) -> None:
        if self._storage is None:
            raise KeyError("No session storage available")
        await self._callbacks.delete_session(
            canonical_key=canonical_key,
            ctx=self._context,
            storage=self._storage,
        )

    async def publish_forked(self, event: SessionForked) -> None:
        await self._callbacks.emit_session_event(
            self._context,
            event.child_key,
            "sessions.changed",
            build_sessions_changed_payload(
                event.child_key,
                "forked",
                run_status="idle",
            ),
        )


class _GatewayDeletionPort(SessionDeletionPort):
    """Expose delete availability separately from the manager-backed store."""

    def __init__(self, ports: GatewaySessionLifecyclePorts) -> None:
        self._ports = ports

    @property
    def available(self) -> bool:
        return self._ports.deletion_available

    async def delete_one(self, canonical_key: str) -> None:
        await self._ports.delete_one(canonical_key)


class GatewaySessionLifecycleAdapter:
    """Decode v4 requests, compose the Application Module, and project results."""

    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewaySessionLifecycleCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks
        self._ports = GatewaySessionLifecyclePorts(context, callbacks)
        self._application = SessionLifecycle(
            creation_policy=self._ports,
            store=self._ports,
            deletion=_GatewayDeletionPort(self._ports),
            events=self._ports,
        )

    async def create(self, params: object) -> dict[str, Any]:
        values = dict(params) if isinstance(params, Mapping) else {}
        agent_id = self._callbacks.normalize_agent_id(values.get("agentId") or "main")
        display_name_value = values.get("displayName")
        display_name = cast(str | None, display_name_value)
        message = values.get("message")
        if message is not None and not isinstance(message, str):
            raise ValueError("params.message must be a string")
        raw_model = values.get("model")
        model = self._callbacks.model_value(raw_model)
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
        ) = self._callbacks.deployment_fields(values)
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
            self._callbacks.raise_deployment_model_required()
            raise AssertionError("deployment error callback returned")
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
        key = self._callbacks.require_key(values)
        assert isinstance(values, dict)
        title = values.get("title")
        if title is not None and not isinstance(title, str):
            raise ValueError("params.title must be a string")
        before_message_id = self._callbacks.optional_string(
            values,
            "beforeMessageId",
            "before_message_id",
        )
        through_turn_id = self._callbacks.optional_non_empty_aliased_string(
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
        key = self._callbacks.require_key(values)
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
    "GatewaySessionLifecycleCallbacks",
    "GatewaySessionLifecyclePorts",
    "created_session_to_v4",
    "deleted_sessions_to_v4",
    "forked_session_to_v4",
    "renamed_session_to_v4",
]
