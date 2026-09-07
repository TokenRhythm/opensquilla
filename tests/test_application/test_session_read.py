"""Observable behavior tests for the Session read Application Module."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.application.conversation_runtime import (
    ConversationSnapshotApplication,
    ConversationSnapshotEvent,
    InMemoryConversationSnapshotReader,
    LiveConversationSnapshot,
)
from opensquilla.application.session_history import (
    SessionHistoryApplication,
    SessionHistoryQuery,
)
from opensquilla.application.session_read import (
    DEFERRED_METADATA_FACETS,
    SessionMetadataFacet,
    SessionMetadataQuery,
    SessionPlanningState,
    SessionReadApplication,
    SessionRunModeLock,
    SessionTaskState,
    SessionWorkspaceState,
)
from opensquilla.application.session_transcript import (
    SessionPreviewQuery,
    SessionTranscriptApplication,
)

SESSION_KEY = "agent:main:webchat:read"


def identity_projector(
    name: str,
    payload: Mapping[str, Any],
    _client_caps: frozenset[str],
) -> tuple[str, Mapping[str, Any]]:
    return name, payload


class ActiveHistoryPort:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    async def read_active_transcript(self, session_key: str) -> list[Any]:
        assert session_key == SESSION_KEY
        return self.rows


class SessionRecordPort:
    def __init__(self) -> None:
        self.row = SimpleNamespace(
            session_key=SESSION_KEY,
            session_id="session-id",
            display_name="Read lifecycle",
            derived_title=None,
            updated_at=123,
        )

    async def get_session(self, key: str) -> Any | None:
        return self.row if key == SESSION_KEY else None

    async def list_sessions(self, *, limit: int) -> list[Any]:
        return [self.row][:limit]


class PreviewContentPort:
    async def list_last_transcript_content_batch(
        self,
        session_ids: list[str],
        *,
        max_chars: int,
    ) -> dict[str, str]:
        assert session_ids == ["session-id"]
        assert max_chars == 120
        return {"session-id": "latest reply"}


class FixedClock:
    def now_ms(self) -> int:
        return 456


class StreamPositionPort:
    def __init__(self, calls: list[str], sequence: int = 17) -> None:
        self.calls = calls
        self.sequence = sequence

    def current_stream_seq(self, session_key: str) -> int:
        assert session_key == SESSION_KEY
        self.calls.append("cursor")
        return self.sequence


class TaskStatePort:
    def __init__(
        self,
        calls: list[str],
        *,
        task: dict[str, Any] | None = None,
    ) -> None:
        self.calls = calls
        self.task = task or {"task_id": "task-1", "status": "running"}

    async def read_task_state(self, session_key: str) -> SessionTaskState:
        assert session_key == SESSION_KEY
        self.calls.append("tasks")
        return SessionTaskState(
            tasks=(self.task,),
            active_task=self.task,
            last_task={"task_id": "task-0", "status": "succeeded"},
            run_status="running",
            queued_task_ids=("task-2",),
            active_task_group_ids=("group-1",),
            run_mode_lock=SessionRunModeLock(
                locked=True,
                run_mode="safe",
                source="task",
            ),
        )


class WorkspaceStatePort:
    def __init__(
        self,
        calls: list[str],
        project: dict[str, Any] | None = None,
    ) -> None:
        self.calls = calls
        self.project = project or {"id": "workspace-1", "available": True}

    async def read_workspace_state(
        self,
        session_key: str,
        *,
        include_project_workspace: bool,
    ) -> SessionWorkspaceState:
        assert session_key == SESSION_KEY
        self.calls.append(f"workspace:{include_project_workspace}")
        return SessionWorkspaceState(
            workspace_id="workspace-1",
            project_workspace=self.project if include_project_workspace else None,
            project_workspace_deferred=not include_project_workspace,
        )


class PendingInputPort:
    def __init__(self, calls: list[str], item: dict[str, Any] | None = None) -> None:
        self.calls = calls
        self.item = item or {"request_id": "input-1"}

    async def read_pending_inputs(self, session_key: str) -> list[dict[str, Any]]:
        assert session_key == SESSION_KEY
        self.calls.append("pending")
        return [self.item]


class RoutingPort:
    def __init__(self, calls: list[str], value: dict[str, Any] | None = None) -> None:
        self.calls = calls
        self.value = value or {"mode": "router", "revision": 3}

    async def read_routing(self, session_key: str) -> dict[str, Any]:
        assert session_key == SESSION_KEY
        self.calls.append("routing")
        return self.value


class PlanningPort:
    def __init__(self, calls: list[str], goal: dict[str, Any] | None = None) -> None:
        self.calls = calls
        self.goal = goal or {"goal_id": "goal-1"}

    async def read_planning_state(self, session_key: str) -> SessionPlanningState:
        assert session_key == SESSION_KEY
        self.calls.append("planning")
        return SessionPlanningState(
            collaboration={"mode": "plan", "revision": 2},
            current_plan={"revision_id": "plan-1"},
            active_plan_run={"run_id": "run-1"},
            goal=self.goal,
            epoch=4,
        )


def application(
    calls: list[str],
    *,
    stream_positions: Any | None = None,
    tasks: Any | None = None,
    workspaces: Any | None = None,
    pending_inputs: Any | None = None,
    routing: Any | None = None,
    planning: Any | None = None,
) -> SessionReadApplication:
    snapshot = LiveConversationSnapshot(
        task_id="task-live",
        stream_generation="generation-1",
        current_stream_seq=1,
        events=(
            ConversationSnapshotEvent(
                "session.event.text_delta",
                {"text": "hello"},
            ),
        ),
    )
    snapshots = ConversationSnapshotApplication(
        reader=InMemoryConversationSnapshotReader({SESSION_KEY: snapshot}),
        projector=identity_projector,
    )
    history = SessionHistoryApplication(
        active=ActiveHistoryPort(
            [SimpleNamespace(id=1, message_id="m1", created_at=1)]
        )
    )
    transcript = SessionTranscriptApplication(
        sessions=SessionRecordPort(),
        preview_content=PreviewContentPort(),
        clock=FixedClock(),
    )
    return SessionReadApplication(
        snapshots=snapshots,
        history=history,
        transcript=transcript,
        stream_positions=stream_positions or StreamPositionPort(calls),
        tasks=tasks or TaskStatePort(calls),
        workspaces=workspaces or WorkspaceStatePort(calls),
        pending_inputs=pending_inputs or PendingInputPort(calls),
        routing=routing or RoutingPort(calls),
        planning=planning or PlanningPort(calls),
    )


@pytest.mark.asyncio
async def test_composes_existing_snapshot_history_and_preview_applications() -> None:
    calls: list[str] = []
    app = application(calls)

    snapshot = app.read_snapshot(SESSION_KEY)
    history = await app.read_history(
        SessionHistoryQuery(session_key=SESSION_KEY, limit=20)
    )
    previews = await app.read_previews(SessionPreviewQuery(keys=(SESSION_KEY,)))

    assert snapshot.task_id == "task-live"
    assert snapshot.events[0].payload == {"text": "hello"}
    assert [getattr(row, "message_id") for row in history.entries] == ["m1"]
    assert previews.ts == 456
    assert previews.previews[0].title == "Read lifecycle"
    assert previews.previews[0].last_message == "latest reply"
    assert calls == []


@pytest.mark.asyncio
async def test_metadata_captures_cursor_first_and_projects_real_hydration_facets() -> None:
    calls: list[str] = []
    task = {"task_id": "task-1", "status": "running"}
    project = {"id": "workspace-1", "available": True}
    pending = {"request_id": "input-1"}
    routing = {"mode": "router", "revision": 3}
    goal = {"goal_id": "goal-1"}
    app = application(
        calls,
        tasks=TaskStatePort(calls, task=task),
        workspaces=WorkspaceStatePort(calls, project=project),
        pending_inputs=PendingInputPort(calls, item=pending),
        routing=RoutingPort(calls, value=routing),
        planning=PlanningPort(calls, goal=goal),
    )

    result = await app.read_metadata(
        SessionMetadataQuery(
            session_key=SESSION_KEY,
            include_project_workspace=True,
        )
    )

    assert calls == [
        "cursor",
        "tasks",
        "workspace:True",
        "pending",
        "routing",
        "planning",
    ]
    assert result.key == SESSION_KEY
    assert result.goal_snapshot_stream_seq == 17
    assert result.workspace_id == "workspace-1"
    assert result.project_workspace == project
    assert result.project_workspace_deferred is False
    assert result.active_task == task
    assert result.queued_task_ids == ("task-2",)
    assert result.active_task_group_ids == ("group-1",)
    assert result.run_mode_lock == SessionRunModeLock(
        locked=True,
        run_mode="safe",
        source="task",
    )
    assert result.pending_user_inputs == (pending,)
    assert result.routing == routing
    assert result.goal == goal
    assert result.epoch == 4
    assert result.hydration_complete is True
    assert result.deferred_fields == ()

    task["status"] = "failed"
    project["available"] = False
    pending["request_id"] = "changed"
    routing["mode"] = "direct"
    goal["goal_id"] = "changed"
    assert result.active_task == {"task_id": "task-1", "status": "running"}
    assert result.project_workspace == {"id": "workspace-1", "available": True}
    assert result.pending_user_inputs == ({"request_id": "input-1"},)
    assert result.routing == {"mode": "router", "revision": 3}
    assert result.goal == {"goal_id": "goal-1"}


@pytest.mark.asyncio
async def test_fast_ack_is_storage_free_and_hydration_can_defer_only_project_details() -> None:
    calls: list[str] = []
    app = application(calls)

    deferred = app.deferred_metadata(SESSION_KEY)

    assert calls == []
    assert deferred.hydration_complete is False
    assert deferred.project_workspace_deferred is True
    assert deferred.run_mode_lock == SessionRunModeLock(
        locked=True,
        source="deferred",
    )
    assert deferred.deferred_fields == DEFERRED_METADATA_FACETS
    assert deferred.tasks == ()
    assert deferred.pending_user_inputs == ()

    hydrated = await app.read_metadata(SessionMetadataQuery(session_key=SESSION_KEY))

    assert hydrated.hydration_complete is True
    assert hydrated.project_workspace is None
    assert hydrated.project_workspace_deferred is True
    assert hydrated.deferred_fields == (SessionMetadataFacet.PROJECT_WORKSPACE,)


@pytest.mark.asyncio
async def test_metadata_failure_propagates_without_reading_later_facets() -> None:
    calls: list[str] = []

    class BrokenWorkspacePort(WorkspaceStatePort):
        async def read_workspace_state(
            self,
            session_key: str,
            *,
            include_project_workspace: bool,
        ) -> SessionWorkspaceState:
            assert session_key == SESSION_KEY
            self.calls.append(f"workspace:{include_project_workspace}")
            raise RuntimeError("workspace storage busy")

    app = application(calls, workspaces=BrokenWorkspacePort(calls))

    with pytest.raises(RuntimeError, match="workspace storage busy"):
        await app.read_metadata(SessionMetadataQuery(session_key=SESSION_KEY))

    assert calls == ["cursor", "tasks", "workspace:False"]


@pytest.mark.asyncio
async def test_invalid_stream_position_fails_before_metadata_ports_are_read() -> None:
    calls: list[str] = []
    app = application(calls, stream_positions=StreamPositionPort(calls, sequence=-1))

    with pytest.raises(ValueError, match="non-negative integer"):
        await app.read_metadata(SessionMetadataQuery(session_key=SESSION_KEY))

    assert calls == ["cursor"]
