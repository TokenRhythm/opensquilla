"""Application-owned Session read lifecycle seams.

The v4 Gateway currently exposes four distinct read shapes for one Session:
live snapshots, durable history, preview summaries, and the metadata returned
by ``sessions.messages.subscribe`` / ``sessions.messages.hydrate``.  The first
three already have transport-neutral application implementations.  This
module composes them and owns the remaining metadata orchestration without
accepting ``RpcContext`` or importing a Gateway, WebSocket, or persistence
type.

Authentication, scope checks, connection registration, replay delivery, and
legacy wire aliases stay in the Gateway Adapter.  The narrow Ports below
provide only the projections needed to assemble one metadata result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from opensquilla.application.conversation_runtime import (
    ConversationSnapshotApplication,
    ProjectedConversationSnapshot,
)
from opensquilla.application.session_history import (
    HistoryPage,
    SessionHistoryApplication,
    SessionHistoryQuery,
)
from opensquilla.application.session_transcript import (
    SessionPreviewQuery,
    SessionPreviewResult,
    SessionTranscriptApplication,
)

type JsonObject = Mapping[str, Any]


class SessionMetadataFacet(StrEnum):
    """Transport-neutral metadata facets that may be deferred after fast ACK."""

    WORKSPACE = "workspace"
    PROJECT_WORKSPACE = "project_workspace"
    TASKS = "tasks"
    TASK_GROUPS = "task_groups"
    RUN_MODE = "run_mode"
    PENDING_INPUTS = "pending_inputs"
    COLLABORATION = "collaboration"
    ROUTING = "routing"
    PLAN = "plan"
    GOAL = "goal"
    EPOCH = "epoch"


DEFERRED_METADATA_FACETS: tuple[SessionMetadataFacet, ...] = (
    SessionMetadataFacet.WORKSPACE,
    SessionMetadataFacet.PROJECT_WORKSPACE,
    SessionMetadataFacet.TASKS,
    SessionMetadataFacet.TASK_GROUPS,
    SessionMetadataFacet.RUN_MODE,
    SessionMetadataFacet.PENDING_INPUTS,
    SessionMetadataFacet.COLLABORATION,
    SessionMetadataFacet.ROUTING,
    SessionMetadataFacet.PLAN,
    SessionMetadataFacet.GOAL,
    SessionMetadataFacet.EPOCH,
)


@dataclass(frozen=True, slots=True)
class SessionRunModeLock:
    """Authorization-projected run-mode lock for the current principal."""

    locked: bool
    run_mode: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class SessionTaskState:
    """Durable/runtime task ownership needed by Session hydration."""

    tasks: tuple[JsonObject, ...]
    active_task: JsonObject | None
    last_task: JsonObject | None
    run_status: str
    active_task_group_ids: tuple[str, ...]
    run_mode_lock: SessionRunModeLock
    queued_task_ids: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class SessionWorkspaceState:
    """Workspace binding with optional expanded project projection."""

    workspace_id: str | None
    project_workspace: JsonObject | None
    project_workspace_deferred: bool


@dataclass(frozen=True, slots=True)
class SessionPlanningState:
    """Persisted collaboration, Plan, Goal, and epoch projections."""

    collaboration: JsonObject | None
    current_plan: JsonObject | None
    active_plan_run: JsonObject | None
    goal: JsonObject | None
    epoch: int | None


class SessionStreamPositionReader(Protocol):
    """Port for the cursor captured before storage-backed metadata reads."""

    def current_stream_seq(self, session_key: str) -> int: ...


class SessionTaskStateReader(Protocol):
    """Port for task ownership and the principal-projected run-mode lock."""

    async def read_task_state(self, session_key: str) -> SessionTaskState: ...


class SessionWorkspaceStateReader(Protocol):
    """Port for the persisted workspace binding and optional project details."""

    async def read_workspace_state(
        self,
        session_key: str,
        *,
        include_project_workspace: bool,
    ) -> SessionWorkspaceState: ...


class SessionPendingInputReader(Protocol):
    """Port for unresolved user-input projections owned by TaskRuntime."""

    async def read_pending_inputs(self, session_key: str) -> Sequence[JsonObject]: ...


class SessionRoutingStateReader(Protocol):
    """Port for the durable Session routing projection."""

    async def read_routing(self, session_key: str) -> JsonObject: ...


class SessionPlanningStateReader(Protocol):
    """Port for persisted collaboration, Plan, Goal, and epoch state."""

    async def read_planning_state(self, session_key: str) -> SessionPlanningState: ...


@dataclass(frozen=True, slots=True)
class SessionMetadataQuery:
    """Normalized request for a complete Session metadata projection."""

    session_key: str
    include_project_workspace: bool = False


@dataclass(frozen=True, slots=True)
class SessionReadMetadata:
    """Canonical metadata result projected later by a Gateway Adapter."""

    key: str
    workspace_id: str | None
    project_workspace: JsonObject | None
    project_workspace_deferred: bool
    tasks: tuple[JsonObject, ...]
    active_task: JsonObject | None
    last_task: JsonObject | None
    run_status: str
    queued_task_ids: tuple[str, ...] | None
    active_task_group_ids: tuple[str, ...]
    run_mode_lock: SessionRunModeLock
    pending_user_inputs: tuple[JsonObject, ...]
    collaboration: JsonObject | None
    routing: JsonObject | None
    current_plan: JsonObject | None
    active_plan_run: JsonObject | None
    goal: JsonObject | None
    goal_snapshot_stream_seq: int | None
    epoch: int | None
    hydration_complete: bool
    deferred_fields: tuple[SessionMetadataFacet, ...]


def _copy_object(value: JsonObject | None) -> dict[str, Any] | None:
    return dict(value) if value is not None else None


def _copy_objects(values: Sequence[JsonObject]) -> tuple[dict[str, Any], ...]:
    return tuple(dict(value) for value in values)


def _require_session_key(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("session_key must be a non-empty string")
    return value


def deferred_session_read_metadata(session_key: str) -> SessionReadMetadata:
    """Build the storage-free metadata used by a fast subscription ACK.

    Keeping this constructor outside the composed application lets the
    Gateway acknowledge an already-registered subscriber without assembling
    any storage-backed Ports.  It still returns the same canonical domain
    result as :meth:`SessionReadApplication.deferred_metadata`.
    """

    return SessionReadMetadata(
        key=_require_session_key(session_key),
        workspace_id=None,
        project_workspace=None,
        project_workspace_deferred=True,
        tasks=(),
        active_task=None,
        last_task=None,
        run_status="idle",
        queued_task_ids=None,
        active_task_group_ids=(),
        run_mode_lock=SessionRunModeLock(locked=True, source="deferred"),
        pending_user_inputs=(),
        collaboration=None,
        routing=None,
        current_plan=None,
        active_plan_run=None,
        goal=None,
        goal_snapshot_stream_seq=None,
        epoch=None,
        hydration_complete=False,
        deferred_fields=DEFERRED_METADATA_FACETS,
    )


class SessionReadApplication:
    """Compose all transport-neutral Session read use cases.

    The Gateway Adapter remains responsible for request parsing, authorization,
    connection subscription registration, replay sends, compatibility aliases,
    and final v4 payload projection.  This application owns only read policy
    that is stable across transports.
    """

    def __init__(
        self,
        *,
        snapshots: ConversationSnapshotApplication,
        history: SessionHistoryApplication,
        transcript: SessionTranscriptApplication,
        stream_positions: SessionStreamPositionReader,
        tasks: SessionTaskStateReader,
        workspaces: SessionWorkspaceStateReader,
        pending_inputs: SessionPendingInputReader,
        routing: SessionRoutingStateReader,
        planning: SessionPlanningStateReader,
    ) -> None:
        self._snapshots = snapshots
        self._history = history
        self._transcript = transcript
        self._stream_positions = stream_positions
        self._tasks = tasks
        self._workspaces = workspaces
        self._pending_inputs = pending_inputs
        self._routing = routing
        self._planning = planning

    def read_snapshot(
        self,
        session_key: str,
        *,
        client_caps: frozenset[str] = frozenset(),
    ) -> ProjectedConversationSnapshot:
        """Read the capability-projected in-memory live snapshot."""

        return self._snapshots.read(
            _require_session_key(session_key),
            client_caps=client_caps,
        )

    async def read_history(self, query: SessionHistoryQuery) -> HistoryPage:
        """Read a canonical page or active-transcript fallback."""

        _require_session_key(query.session_key)
        return await self._history.read_page(query)

    async def read_previews(self, query: SessionPreviewQuery) -> SessionPreviewResult:
        """Read bounded Session preview projections."""

        return await self._transcript.preview(query)

    def deferred_metadata(self, session_key: str) -> SessionReadMetadata:
        """Return the storage-free partial state used by a fast subscribe ACK."""

        return deferred_session_read_metadata(session_key)

    async def read_metadata(self, query: SessionMetadataQuery) -> SessionReadMetadata:
        """Assemble one authoritative Session metadata snapshot.

        The live cursor is captured first.  Any Goal mutation published after
        that point carries a greater sequence and therefore wins over this
        potentially slower storage snapshot at the client/runtime seam.
        """

        key = _require_session_key(query.session_key)
        goal_snapshot_stream_seq = self._stream_positions.current_stream_seq(key)
        if (
            isinstance(goal_snapshot_stream_seq, bool)
            or not isinstance(goal_snapshot_stream_seq, int)
            or goal_snapshot_stream_seq < 0
        ):
            raise ValueError("current_stream_seq must be a non-negative integer")

        task_state = await self._tasks.read_task_state(key)
        workspace = await self._workspaces.read_workspace_state(
            key,
            include_project_workspace=query.include_project_workspace,
        )
        pending_inputs = await self._pending_inputs.read_pending_inputs(key)
        routing = await self._routing.read_routing(key)
        planning = await self._planning.read_planning_state(key)

        deferred_fields = (
            (SessionMetadataFacet.PROJECT_WORKSPACE,)
            if workspace.project_workspace_deferred
            else ()
        )
        return SessionReadMetadata(
            key=key,
            workspace_id=workspace.workspace_id,
            project_workspace=_copy_object(workspace.project_workspace),
            project_workspace_deferred=workspace.project_workspace_deferred,
            tasks=_copy_objects(task_state.tasks),
            active_task=_copy_object(task_state.active_task),
            last_task=_copy_object(task_state.last_task),
            run_status=task_state.run_status,
            queued_task_ids=(
                tuple(task_state.queued_task_ids)
                if task_state.queued_task_ids is not None
                else None
            ),
            active_task_group_ids=tuple(task_state.active_task_group_ids),
            run_mode_lock=task_state.run_mode_lock,
            pending_user_inputs=_copy_objects(pending_inputs),
            collaboration=_copy_object(planning.collaboration),
            routing=_copy_object(routing),
            current_plan=_copy_object(planning.current_plan),
            active_plan_run=_copy_object(planning.active_plan_run),
            goal=_copy_object(planning.goal),
            goal_snapshot_stream_seq=goal_snapshot_stream_seq,
            epoch=planning.epoch,
            hydration_complete=True,
            deferred_fields=deferred_fields,
        )


__all__ = [
    "DEFERRED_METADATA_FACETS",
    "SessionMetadataFacet",
    "SessionMetadataQuery",
    "SessionPendingInputReader",
    "SessionPlanningState",
    "SessionPlanningStateReader",
    "SessionReadApplication",
    "SessionReadMetadata",
    "SessionRoutingStateReader",
    "SessionRunModeLock",
    "SessionStreamPositionReader",
    "SessionTaskState",
    "SessionTaskStateReader",
    "SessionWorkspaceState",
    "SessionWorkspaceStateReader",
    "deferred_session_read_metadata",
]
