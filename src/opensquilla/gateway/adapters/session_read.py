"""Gateway Adapter for the application-owned Session read Module.

The v4 handlers retain authentication, scope enforcement, subscription
registration, replay delivery, and connection capability lookup.  This
Adapter supplies the narrow concrete Ports needed by
``SessionReadApplication`` and is the only layer that projects its canonical
metadata and snapshot results back to the legacy v4 field names.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from opensquilla.application.conversation_runtime import ProjectedConversationSnapshot
from opensquilla.application.session_read import (
    SessionMetadataFacet,
    SessionPlanningState,
    SessionReadApplication,
    SessionReadMetadata,
    SessionRoutingStateReader,
    SessionRunModeLock,
    SessionStreamPositionReader,
    SessionTaskState,
    SessionTaskStateReader,
    SessionWorkspaceState,
    SessionWorkspaceStateReader,
)
from opensquilla.application.session_transcript import (
    Clock,
    SessionRecord,
    SessionTranscriptApplication,
)
from opensquilla.gateway.adapters.conversation_runtime import (
    build_v4_conversation_snapshot_application,
    snapshot_result_to_v4,
)
from opensquilla.gateway.adapters.session_history import (
    build_session_history_application,
)
from opensquilla.gateway.adapters.session_preview import (
    SystemClock,
    build_session_preview_application,
)
from opensquilla.session.storage import SessionStorage

type JsonObject = Mapping[str, Any]
type TaskStateReader = Callable[[str], Awaitable[SessionTaskState]]
type WorkspaceStateReader = Callable[[str, bool], Awaitable[SessionWorkspaceState]]
type PendingInputReader = Callable[[str], Awaitable[Sequence[JsonObject]]]
type RoutingStateReader = Callable[[str], Awaitable[JsonObject]]
type PlanningStateReader = Callable[[str], Awaitable[SessionPlanningState]]


class GatewaySessionReadPorts:
    """Request-scoped Adapter implementing the Session metadata read Ports.

    The callbacks close over Gateway dependencies such as ``RpcContext`` and
    concrete storage.  Those details terminate here: the Application Module
    sees only session keys and domain read models.
    """

    def __init__(
        self,
        *,
        streams: object,
        read_tasks: TaskStateReader,
        read_workspace: WorkspaceStateReader,
        read_pending_inputs: PendingInputReader,
        read_routing: RoutingStateReader,
        read_planning: PlanningStateReader,
    ) -> None:
        self._streams = streams
        self._task_state_reader = read_tasks
        self._workspace_state_reader = read_workspace
        self._pending_input_reader = read_pending_inputs
        self._routing_state_reader = read_routing
        self._planning_state_reader = read_planning

    def current_stream_seq(self, session_key: str) -> int:
        replay = getattr(self._streams, "replay")(session_key, None)
        return int(getattr(replay, "current_stream_seq"))

    async def read_task_state(self, session_key: str) -> SessionTaskState:
        return await self._task_state_reader(session_key)

    async def read_workspace_state(
        self,
        session_key: str,
        *,
        include_project_workspace: bool,
    ) -> SessionWorkspaceState:
        return await self._workspace_state_reader(session_key, include_project_workspace)

    async def read_pending_inputs(
        self,
        session_key: str,
    ) -> Sequence[JsonObject]:
        return await self._pending_input_reader(session_key)

    async def read_routing(self, session_key: str) -> JsonObject:
        return await self._routing_state_reader(session_key)

    async def read_planning_state(self, session_key: str) -> SessionPlanningState:
        return await self._planning_state_reader(session_key)


class _UnavailableSessionPreviewPorts:
    """Production fallback matching the existing no-storage preview result."""

    async def get_session(self, _key: str) -> SessionRecord | None:
        return None

    async def list_sessions(self, *, limit: int) -> Sequence[SessionRecord]:
        del limit
        return ()

    async def list_last_transcript_content_batch(
        self,
        session_ids: list[str],
        *,
        max_chars: int,
    ) -> Mapping[str, str]:
        del session_ids, max_chars
        return {}


def _unavailable_preview_application(*, clock: Clock | None = None) -> SessionTranscriptApplication:
    ports = _UnavailableSessionPreviewPorts()
    return SessionTranscriptApplication(
        sessions=ports,
        preview_content=ports,
        clock=clock if clock is not None else SystemClock(),
    )


def build_v4_session_read_application(
    *,
    streams: object,
    session_manager: object,
    storage: SessionStorage | None,
    ports: GatewaySessionReadPorts,
    clock: Clock | None = None,
) -> SessionReadApplication:
    """Compose the complete Session read Module behind one production seam."""

    snapshots = build_v4_conversation_snapshot_application(streams)
    history = build_session_history_application(session_manager)
    transcript = (
        build_session_preview_application(storage, clock=clock)
        if storage is not None
        else _unavailable_preview_application(clock=clock)
    )

    # These assignments are explicit structural conformance checks.  A future
    # callback or Adapter signature drift therefore fails mypy at this seam.
    stream_positions: SessionStreamPositionReader = ports
    tasks: SessionTaskStateReader = ports
    workspaces: SessionWorkspaceStateReader = ports
    routing: SessionRoutingStateReader = ports
    return SessionReadApplication(
        snapshots=snapshots,
        history=history,
        transcript=transcript,
        stream_positions=stream_positions,
        tasks=tasks,
        workspaces=workspaces,
        pending_inputs=ports,
        routing=routing,
        planning=ports,
    )


_DEFERRED_FIELDS_BY_FACET: dict[SessionMetadataFacet, tuple[str, ...]] = {
    SessionMetadataFacet.WORKSPACE: ("workspaceId",),
    SessionMetadataFacet.PROJECT_WORKSPACE: ("projectWorkspace",),
    SessionMetadataFacet.TASKS: (
        "tasks",
        "active_task",
        "last_task",
        "run_status",
    ),
    SessionMetadataFacet.TASK_GROUPS: ("active_task_group_ids",),
    SessionMetadataFacet.RUN_MODE: ("run_mode_lock",),
    SessionMetadataFacet.PENDING_INPUTS: ("pendingUserInputs",),
    SessionMetadataFacet.COLLABORATION: ("collaboration",),
    SessionMetadataFacet.ROUTING: ("routing",),
    SessionMetadataFacet.PLAN: ("currentPlan", "activePlanRun"),
    SessionMetadataFacet.GOAL: ("goal", "goalSnapshotStreamSeq"),
    SessionMetadataFacet.EPOCH: ("epoch",),
}


def _run_mode_lock_to_v4(value: SessionRunModeLock) -> dict[str, Any]:
    result: dict[str, Any] = {"locked": value.locked}
    if value.run_mode is not None:
        result["runMode"] = value.run_mode
    if value.source is not None:
        result["source"] = value.source
    return result


def _deferred_fields_to_v4(
    facets: Sequence[SessionMetadataFacet],
) -> list[str]:
    fields: list[str] = []
    for facet in facets:
        fields.extend(_DEFERRED_FIELDS_BY_FACET[facet])
    return fields


def session_read_metadata_to_v4(
    metadata: SessionReadMetadata,
    *,
    include_key: bool = True,
) -> dict[str, Any]:
    """Project canonical metadata to the unchanged subscribe/hydrate shape."""

    result: dict[str, Any] = {
        "workspaceId": metadata.workspace_id,
        "projectWorkspace": (
            dict(metadata.project_workspace)
            if metadata.project_workspace is not None
            else None
        ),
        "projectWorkspaceDeferred": metadata.project_workspace_deferred,
        "active_task_group_ids": list(metadata.active_task_group_ids),
        "run_mode_lock": _run_mode_lock_to_v4(metadata.run_mode_lock),
        "pendingUserInputs": [dict(item) for item in metadata.pending_user_inputs],
        "collaboration": (
            dict(metadata.collaboration) if metadata.collaboration is not None else None
        ),
        "routing": dict(metadata.routing) if metadata.routing is not None else None,
        "currentPlan": (
            dict(metadata.current_plan) if metadata.current_plan is not None else None
        ),
        "activePlanRun": (
            dict(metadata.active_plan_run)
            if metadata.active_plan_run is not None
            else None
        ),
        "goal": dict(metadata.goal) if metadata.goal is not None else None,
        "goalSnapshotStreamSeq": metadata.goal_snapshot_stream_seq,
        "tasks": [dict(task) for task in metadata.tasks],
        "active_task": (
            dict(metadata.active_task) if metadata.active_task is not None else None
        ),
        "last_task": (
            dict(metadata.last_task) if metadata.last_task is not None else None
        ),
        "run_status": metadata.run_status,
        "hydration_complete": metadata.hydration_complete,
        "deferred_fields": _deferred_fields_to_v4(metadata.deferred_fields),
    }
    if include_key:
        result["key"] = metadata.key
    if metadata.epoch is not None:
        result["epoch"] = metadata.epoch
    if metadata.queued_task_ids is not None:
        result["queued_task_ids"] = list(metadata.queued_task_ids)
    return result


def session_read_snapshot_to_v4(
    session_key: str,
    snapshot: ProjectedConversationSnapshot,
) -> dict[str, Any]:
    """Project a capability-filtered live snapshot to the legacy v4 envelope."""

    return snapshot_result_to_v4(session_key, snapshot)


__all__ = [
    "GatewaySessionReadPorts",
    "build_v4_session_read_application",
    "session_read_metadata_to_v4",
    "session_read_snapshot_to_v4",
]
