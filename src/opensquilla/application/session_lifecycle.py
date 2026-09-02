"""Transport-neutral Session lifecycle use cases.

The Module owns lifecycle policy and ordering.  Gateway request aliases,
authorization, wire field names, concrete SessionManager calls, writer fences,
and event payload projection stay behind the Ports below.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from opensquilla.session_key import canonicalize_session_key


class SessionCreationKind(StrEnum):
    DEFAULT = "default"
    CLI = "cli"
    WEBCHAT = "webchat"


@dataclass(frozen=True, slots=True)
class OptionalSessionBinding:
    """Presence-aware optional value used by create-time deployment bindings."""

    present: bool = False
    value: str | None = None


@dataclass(frozen=True, slots=True)
class SessionModelRequest:
    value: str | None = None
    explicitly_supplied: bool = False
    string_supplied: bool = False


@dataclass(frozen=True, slots=True)
class CreateSession:
    agent_id: str
    display_name: str | None
    initial_message: str | None
    model: SessionModelRequest
    kind: SessionCreationKind
    workspace_id: str | None
    provider: OptionalSessionBinding
    auth_profile: OptionalSessionBinding


@dataclass(frozen=True, slots=True)
class SessionWorkspaceBinding:
    workspace_id: str
    path: str
    run_mode: str
    run_mode_source: str
    source: str = "project_workspace"


@dataclass(frozen=True, slots=True)
class NewSession:
    session_key: str
    agent_id: str
    display_name: str | None
    model: str | None
    workspace: SessionWorkspaceBinding | None
    provider: OptionalSessionBinding
    auth_profile: OptionalSessionBinding


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    session_key: str
    session_id: str


@dataclass(frozen=True, slots=True)
class CreatedSession:
    key: str
    session_id: str
    seeded_message: bool = False
    note: str | None = None


class SessionForkMode(StrEnum):
    FULL = "full"
    BEFORE_MESSAGE = "before_message"
    THROUGH_TURN = "through_turn"


@dataclass(frozen=True, slots=True)
class SessionForkPoint:
    mode: SessionForkMode
    anchor_id: str | None = None

    def __post_init__(self) -> None:
        if self.mode is SessionForkMode.FULL:
            if self.anchor_id is not None:
                raise ValueError("a full fork cannot declare an anchor")
            return
        if not isinstance(self.anchor_id, str) or not self.anchor_id.strip():
            raise ValueError("an anchored fork requires a non-empty anchor")


@dataclass(frozen=True, slots=True)
class ForkSession:
    parent_key: str
    title: str | None
    point: SessionForkPoint


@dataclass(frozen=True, slots=True)
class ForkSessionSpec:
    parent_key: str
    child_key: str
    title: str | None
    point: SessionForkPoint


@dataclass(frozen=True, slots=True)
class ForkedSession:
    key: str
    parent_key: str
    mode: SessionForkMode
    through_turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionForked:
    child_key: str
    parent_key: str


@dataclass(frozen=True, slots=True)
class RenameSession:
    session_key: str
    display_name: str


class SessionUpdatedField(StrEnum):
    DISPLAY_NAME = "display_name"


@dataclass(frozen=True, slots=True)
class RenamedSession:
    key: str
    updated_fields: tuple[SessionUpdatedField, ...]


@dataclass(frozen=True, slots=True)
class DeleteSessions:
    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionDeleteFailure:
    requested_key: str
    message: str


@dataclass(frozen=True, slots=True)
class DeleteSessionsResult:
    deleted: tuple[str, ...]
    failures: tuple[SessionDeleteFailure, ...]


class SessionLifecycleError(RuntimeError):
    """Base class for lifecycle errors projected by a transport Adapter."""


class SessionAvailabilityRequirement(StrEnum):
    CREATE_WITH_MESSAGE = "create_with_message"
    CREATE_WITH_DEPLOYMENT = "create_with_deployment"
    CREATE_WITH_WORKSPACE = "create_with_workspace"
    FORK = "fork"
    RENAME = "rename"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class SessionLifecycleUnavailableError(SessionLifecycleError):
    requirement: SessionAvailabilityRequirement

    def __str__(self) -> str:
        return f"session lifecycle unavailable for {self.requirement.value}"


@dataclass(frozen=True, slots=True)
class SessionAgentNotFoundError(SessionLifecycleError):
    agent_id: str

    def __str__(self) -> str:
        return f"Agent '{self.agent_id}' does not exist"


class SessionDeploymentModelRequiredError(SessionLifecycleError):
    def __str__(self) -> str:
        return "An explicit model is required for a session deployment override"


class SessionDisplayNameErrorReason(StrEnum):
    REQUIRED = "required"
    TOO_LONG = "too_long"


@dataclass(frozen=True, slots=True)
class SessionDisplayNameError(SessionLifecycleError):
    reason: SessionDisplayNameErrorReason
    max_chars: int

    def __str__(self) -> str:
        if self.reason is SessionDisplayNameErrorReason.REQUIRED:
            return "display_name must be a non-empty string"
        return f"display_name must be at most {self.max_chars} characters"


class SessionCreationPolicyPort(Protocol):
    def new_session_key(self, agent_id: str, kind: SessionCreationKind) -> str: ...

    async def default_model(self, agent_id: str) -> str | None: ...

    async def agent_exists(self, agent_id: str) -> bool: ...

    def validate_deployment(
        self,
        *,
        session_key: str,
        provider: str | None,
        model: str | None,
        auth_profile: str | None,
    ) -> None: ...

    async def resolve_workspace(self, workspace_id: str) -> SessionWorkspaceBinding: ...


class SessionLifecycleStorePort(Protocol):
    @property
    def available(self) -> bool: ...

    async def create(self, session: NewSession) -> SessionIdentity: ...

    async def append_initial_user_message(self, session_key: str, message: str) -> None: ...

    async def rename(self, session_key: str, display_name: str) -> None: ...

    async def fork_agent_id(self, parent_key: str) -> str: ...

    async def fork(self, spec: ForkSessionSpec) -> SessionIdentity: ...


class SessionDeletionPort(Protocol):
    @property
    def available(self) -> bool: ...

    async def delete_one(self, canonical_key: str) -> None: ...


class SessionLifecycleEventsPort(Protocol):
    async def publish_forked(self, event: SessionForked) -> None: ...


class SessionLifecycle:
    """Application Module for create, fork, rename, and delete semantics."""

    _MAX_DISPLAY_NAME_CHARS = 512

    def __init__(
        self,
        *,
        creation_policy: SessionCreationPolicyPort,
        store: SessionLifecycleStorePort,
        deletion: SessionDeletionPort,
        events: SessionLifecycleEventsPort,
    ) -> None:
        self._creation_policy = creation_policy
        self._store = store
        self._deletion = deletion
        self._events = events

    async def create(self, command: CreateSession) -> CreatedSession:
        session_key = self._creation_policy.new_session_key(command.agent_id, command.kind)
        requested_model = command.model.value
        model = requested_model or await self._creation_policy.default_model(command.agent_id)
        deployment_requested = bool(command.provider.value or command.auth_profile.value)
        if deployment_requested:
            if not (
                command.model.explicitly_supplied
                and command.model.string_supplied
                and requested_model is not None
            ):
                raise SessionDeploymentModelRequiredError()
            self._creation_policy.validate_deployment(
                session_key=session_key,
                provider=command.provider.value,
                model=requested_model,
                auth_profile=command.auth_profile.value,
            )

        if not await self._creation_policy.agent_exists(command.agent_id):
            raise SessionAgentNotFoundError(command.agent_id)

        if not self._store.available:
            if command.initial_message:
                raise SessionLifecycleUnavailableError(
                    SessionAvailabilityRequirement.CREATE_WITH_MESSAGE
                )
            if command.provider.present or command.auth_profile.present:
                raise SessionLifecycleUnavailableError(
                    SessionAvailabilityRequirement.CREATE_WITH_DEPLOYMENT
                )
            if command.workspace_id is not None:
                raise SessionLifecycleUnavailableError(
                    SessionAvailabilityRequirement.CREATE_WITH_WORKSPACE
                )
            return CreatedSession(
                key=session_key,
                session_id=session_key.rsplit(":", 1)[-1],
                note="session manager not available",
            )

        workspace = (
            await self._creation_policy.resolve_workspace(command.workspace_id)
            if command.workspace_id is not None
            else None
        )
        identity = await self._store.create(
            NewSession(
                session_key=session_key,
                agent_id=command.agent_id,
                display_name=command.display_name,
                model=model,
                workspace=workspace,
                provider=command.provider,
                auth_profile=command.auth_profile,
            )
        )
        seeded = False
        if command.initial_message:
            await self._store.append_initial_user_message(
                identity.session_key,
                command.initial_message,
            )
            seeded = True
        return CreatedSession(
            key=identity.session_key,
            session_id=identity.session_id,
            seeded_message=seeded,
        )

    async def fork(self, command: ForkSession) -> ForkedSession:
        if not self._store.available:
            raise SessionLifecycleUnavailableError(SessionAvailabilityRequirement.FORK)
        parent_key = canonicalize_session_key(command.parent_key)
        agent_id = await self._store.fork_agent_id(parent_key)
        child_key = self._creation_policy.new_session_key(
            agent_id,
            SessionCreationKind.WEBCHAT,
        )
        child = await self._store.fork(
            ForkSessionSpec(
                parent_key=parent_key,
                child_key=child_key,
                title=command.title,
                point=command.point,
            )
        )
        await self._events.publish_forked(
            SessionForked(child_key=child.session_key, parent_key=parent_key)
        )
        return ForkedSession(
            key=child.session_key,
            parent_key=parent_key,
            mode=command.point.mode,
            through_turn_id=(
                command.point.anchor_id
                if command.point.mode is SessionForkMode.THROUGH_TURN
                else None
            ),
        )

    async def rename(self, command: RenameSession) -> RenamedSession:
        if not self._store.available:
            raise SessionLifecycleUnavailableError(SessionAvailabilityRequirement.RENAME)
        normalized = command.display_name.strip()
        if not normalized:
            raise SessionDisplayNameError(
                SessionDisplayNameErrorReason.REQUIRED,
                self._MAX_DISPLAY_NAME_CHARS,
            )
        if len(normalized) > self._MAX_DISPLAY_NAME_CHARS:
            raise SessionDisplayNameError(
                SessionDisplayNameErrorReason.TOO_LONG,
                self._MAX_DISPLAY_NAME_CHARS,
            )
        key = canonicalize_session_key(command.session_key)
        await self._store.rename(key, normalized)
        return RenamedSession(key=key, updated_fields=(SessionUpdatedField.DISPLAY_NAME,))

    async def delete(self, command: DeleteSessions) -> DeleteSessionsResult:
        if not self._deletion.available:
            raise SessionLifecycleUnavailableError(SessionAvailabilityRequirement.DELETE)
        if not command.keys:
            raise ValueError("at least one session key is required")
        deleted: list[str] = []
        failures: list[SessionDeleteFailure] = []
        for requested_key in command.keys:
            try:
                await self._settle_irreversible_delete(
                    self._deletion.delete_one(canonicalize_session_key(requested_key))
                )
                deleted.append(requested_key)
            except Exception as exc:
                failures.append(
                    SessionDeleteFailure(
                        requested_key=requested_key,
                        message=str(exc),
                    )
                )
        return DeleteSessionsResult(tuple(deleted), tuple(failures))

    @staticmethod
    async def _settle_irreversible_delete(operation_awaitable: Awaitable[None]) -> None:
        operation = asyncio.ensure_future(operation_awaitable)
        caller = asyncio.current_task()
        cancellation: asyncio.CancelledError | None = None
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except BaseException:
                # Inspect caller cancellation before choosing between it and a
                # terminal operation error that raced with the cancel request.
                break
        if cancellation is None and caller is not None and caller.cancelling():
            cancellation = asyncio.CancelledError()
        if cancellation is not None:
            with contextlib.suppress(BaseException):
                operation.result()
            raise cancellation
        operation.result()


__all__ = [
    "CreateSession",
    "CreatedSession",
    "DeleteSessions",
    "DeleteSessionsResult",
    "ForkSession",
    "ForkSessionSpec",
    "ForkedSession",
    "NewSession",
    "OptionalSessionBinding",
    "RenameSession",
    "RenamedSession",
    "SessionAgentNotFoundError",
    "SessionAvailabilityRequirement",
    "SessionCreationKind",
    "SessionCreationPolicyPort",
    "SessionDeleteFailure",
    "SessionDeletionPort",
    "SessionDeploymentModelRequiredError",
    "SessionDisplayNameError",
    "SessionDisplayNameErrorReason",
    "SessionForkMode",
    "SessionForkPoint",
    "SessionForked",
    "SessionIdentity",
    "SessionLifecycle",
    "SessionLifecycleError",
    "SessionLifecycleEventsPort",
    "SessionLifecycleStorePort",
    "SessionLifecycleUnavailableError",
    "SessionModelRequest",
    "SessionUpdatedField",
    "SessionWorkspaceBinding",
]
