from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.application.session_read import (
    SessionMetadataFacet,
    SessionMetadataQuery,
    SessionPlanningState,
    SessionReadMetadata,
    SessionRunModeLock,
    SessionTaskState,
    SessionWorkspaceState,
    deferred_session_read_metadata,
)
from opensquilla.gateway.adapters.session_read import (
    GatewaySessionReadPorts,
    build_v4_session_read_application,
    session_read_metadata_to_v4,
)

SESSION_KEY = "agent:main:webchat:session-read-adapter"


def test_metadata_projection_preserves_deferred_v4_ack_shape() -> None:
    payload = session_read_metadata_to_v4(
        deferred_session_read_metadata(SESSION_KEY),
        include_key=False,
    )

    assert payload == {
        "workspaceId": None,
        "projectWorkspace": None,
        "projectWorkspaceDeferred": True,
        "active_task_group_ids": [],
        "run_mode_lock": {"locked": True, "source": "deferred"},
        "pendingUserInputs": [],
        "collaboration": None,
        "routing": None,
        "currentPlan": None,
        "activePlanRun": None,
        "goal": None,
        "goalSnapshotStreamSeq": None,
        "tasks": [],
        "active_task": None,
        "last_task": None,
        "run_status": "idle",
        "hydration_complete": False,
        "deferred_fields": [
            "workspaceId",
            "projectWorkspace",
            "tasks",
            "active_task",
            "last_task",
            "run_status",
            "active_task_group_ids",
            "run_mode_lock",
            "pendingUserInputs",
            "collaboration",
            "routing",
            "currentPlan",
            "activePlanRun",
            "goal",
            "goalSnapshotStreamSeq",
            "epoch",
        ],
    }


@pytest.mark.asyncio
async def test_composed_gateway_ports_project_complete_v4_metadata() -> None:
    calls: list[str] = []

    class Streams:
        def replay(self, key: str, cursor: None) -> Any:
            assert key == SESSION_KEY
            assert cursor is None
            calls.append("cursor")
            return SimpleNamespace(current_stream_seq=29)

    async def read_tasks(key: str) -> SessionTaskState:
        assert key == SESSION_KEY
        calls.append("tasks")
        return SessionTaskState(
            tasks=({"task_id": "task-1", "status": "running"},),
            active_task={"task_id": "task-1", "status": "running"},
            last_task=None,
            run_status="running",
            queued_task_ids=(),
            active_task_group_ids=("group-1",),
            run_mode_lock=SessionRunModeLock(
                locked=True,
                run_mode="safe",
                source="task",
            ),
        )

    async def read_workspace(
        key: str,
        include_project_workspace: bool,
    ) -> SessionWorkspaceState:
        assert key == SESSION_KEY
        assert include_project_workspace is False
        calls.append("workspace")
        return SessionWorkspaceState(
            workspace_id="workspace-1",
            project_workspace=None,
            project_workspace_deferred=True,
        )

    async def read_pending(key: str) -> list[dict[str, Any]]:
        assert key == SESSION_KEY
        calls.append("pending")
        return [{"request_id": "input-1"}]

    async def read_routing(key: str) -> dict[str, Any]:
        assert key == SESSION_KEY
        calls.append("routing")
        return {"mode": "router"}

    async def read_planning(key: str) -> SessionPlanningState:
        assert key == SESSION_KEY
        calls.append("planning")
        return SessionPlanningState(
            collaboration={"mode": "plan"},
            current_plan={"revision_id": "plan-1"},
            active_plan_run=None,
            goal={"goal_id": "goal-1"},
            epoch=4,
        )

    streams = Streams()
    ports = GatewaySessionReadPorts(
        streams=streams,
        read_tasks=read_tasks,
        read_workspace=read_workspace,
        read_pending_inputs=read_pending,
        read_routing=read_routing,
        read_planning=read_planning,
    )
    application = build_v4_session_read_application(
        streams=streams,
        session_manager=None,
        storage=None,
        ports=ports,
    )

    metadata = await application.read_metadata(
        SessionMetadataQuery(session_key=SESSION_KEY)
    )
    payload = session_read_metadata_to_v4(metadata)

    assert calls == ["cursor", "tasks", "workspace", "pending", "routing", "planning"]
    assert payload["key"] == SESSION_KEY
    assert payload["goalSnapshotStreamSeq"] == 29
    assert payload["workspaceId"] == "workspace-1"
    assert payload["projectWorkspaceDeferred"] is True
    assert payload["deferred_fields"] == ["projectWorkspace"]
    assert payload["queued_task_ids"] == []
    assert payload["run_mode_lock"] == {
        "locked": True,
        "runMode": "safe",
        "source": "task",
    }
    assert payload["pendingUserInputs"] == [{"request_id": "input-1"}]
    assert payload["routing"] == {"mode": "router"}
    assert payload["currentPlan"] == {"revision_id": "plan-1"}
    assert payload["goal"] == {"goal_id": "goal-1"}
    assert payload["epoch"] == 4


def test_complete_projection_omits_optional_wire_fields_when_absent() -> None:
    metadata = SessionReadMetadata(
        key=SESSION_KEY,
        workspace_id=None,
        project_workspace=None,
        project_workspace_deferred=False,
        tasks=(),
        active_task=None,
        last_task=None,
        run_status="idle",
        queued_task_ids=None,
        active_task_group_ids=(),
        run_mode_lock=SessionRunModeLock(locked=False),
        pending_user_inputs=(),
        collaboration=None,
        routing=None,
        current_plan=None,
        active_plan_run=None,
        goal=None,
        goal_snapshot_stream_seq=0,
        epoch=None,
        hydration_complete=True,
        deferred_fields=(SessionMetadataFacet.PROJECT_WORKSPACE,),
    )

    payload = session_read_metadata_to_v4(metadata)

    assert payload["run_mode_lock"] == {"locked": False}
    assert payload["deferred_fields"] == ["projectWorkspace"]
    assert "queued_task_ids" not in payload
    assert "epoch" not in payload
