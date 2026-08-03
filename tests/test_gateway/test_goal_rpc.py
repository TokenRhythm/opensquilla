"""End-to-end gateway contracts for the Goal driver surface (goals.* RPC)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_goals import (
    _handle_goals_clear,
    _handle_goals_pause,
    _handle_goals_resume,
    _handle_goals_set,
    _handle_goals_status,
)
from opensquilla.gateway.rpc_sessions import _handle_sessions_send
from opensquilla.gateway.task_runtime import TaskRun, TaskRuntime
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.plans import new_plan_revision
from opensquilla.session.storage import SessionStorage

SOURCE_KEY = "agent:main:webchat:goal-rpc-source"

_PRINCIPAL = Principal(
    role="operator",
    scopes=frozenset({"operator.admin"}),
    is_owner=True,
    authenticated=True,
)

_TurnHandler = Callable[[TaskRun], Awaitable[None]]


@dataclass
class _GoalRpcStack:
    storage: SessionStorage
    manager: SessionManager
    runtime: TaskRuntime
    context: RpcContext


@asynccontextmanager
async def _open_goal_rpc_stack(
    db_path: Path,
    *,
    handler: _TurnHandler,
    max_concurrency: int = 1,
) -> AsyncIterator[_GoalRpcStack]:
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_concurrency=max_concurrency,
        running_heartbeat_interval_s=None,
    )
    context = RpcContext(
        conn_id="goal-rpc-test",
        principal=_PRINCIPAL,
        config=GatewayConfig(
            workspace_dir=str(db_path.parent / "workspace"),
            memory={"flush_enabled": False},
            naming={"enabled": False},
        ),
        session_manager=manager,
        task_runtime=runtime,
    )
    await manager.create(SOURCE_KEY, agent_id="main")
    try:
        yield _GoalRpcStack(
            storage=storage,
            manager=manager,
            runtime=runtime,
            context=context,
        )
    finally:
        await runtime.shutdown(cancel=True, timeout=2.0)
        await storage.close()


async def _ignore_subscriber_event(*_args: Any, **_kwargs: Any) -> None:
    return None


def _expected_goal_message(goal_text: str) -> str:
    return (
        f"Pursue the goal: {goal_text}. "
        "Work toward it; end your reply with a goal marker line: "
        "[goal:continue] | [goal:complete] | [goal:blocked:<reason>]."
    )


@pytest.mark.asyncio
async def test_goals_set_creates_goal_plan_run_and_first_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[TaskRun] = []

    async def handler(run: TaskRun) -> None:
        captured.append(run)

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    async with _open_goal_rpc_stack(
        tmp_path / "goal-set.sqlite",
        handler=handler,
    ) as stack:
        response = await _handle_goals_set(
            {
                "sessionKey": SOURCE_KEY,
                "message": "Ship the goal mode.",
                "clientRequestId": "goal-set-1",
            },
            stack.context,
        )
        terminal = await stack.runtime.wait(response["turnId"], timeout=2.0)
        assert terminal.status == AgentTaskStatus.SUCCEEDED

        assert response["sessionKey"] == SOURCE_KEY
        goal_id = response["goalId"]
        assert goal_id
        assert response["goal"]["goalId"] == goal_id
        assert response["goal"]["sessionKey"] == SOURCE_KEY
        assert response["goal"]["status"] == "running"
        assert response["goal"]["goalText"] == "Ship the goal mode."
        assert response["goal"]["planRunId"] == response["planRun"]["runId"]

        plan_run = response["planRun"]
        assert plan_run["driverKind"] == "goal"
        assert plan_run["driverId"] == goal_id
        assert plan_run["status"] == "queued"
        assert plan_run["planRevisionId"]

        # The single goal turn terminates with the run paused at the
        # goal_turn_finished anchor that WO-4's continuation hook resumes.
        settled_run = await stack.storage.get_plan_run(plan_run["runId"])
        assert settled_run is not None
        assert settled_run.status == "paused"
        assert settled_run.pause_reason == "goal_turn_finished"

        expected_message = _expected_goal_message("Ship the goal mode.")
        assert len(captured) == 1
        assert captured[0].message == expected_message
        assert captured[0].no_memory_capture is True
        assert captured[0].envelope.input_provenance == {"kind": "goal_implementation"}

        transcript = await stack.manager.get_transcript(SOURCE_KEY)
        assert len(transcript) == 1
        persisted = json.loads(transcript[0].content)
        assert persisted == {
            "text": expected_message,
            "display_text": "",
            "attachments": [],
        }

        task = await stack.storage.get_agent_task(response["turnId"])
        assert task is not None
        assert task.details is not None
        assert task.details["metadata"]["plan_run_id"] == plan_run["runId"]
        assert task.details["metadata"]["plan_revision_id"] == plan_run["planRevisionId"]

        persisted_goal = await stack.storage.get_goal_run(goal_id)
        assert persisted_goal is not None
        assert persisted_goal.status == "running"
        assert persisted_goal.plan_run_id == plan_run["runId"]
        assert persisted_goal.turns == 0


@pytest.mark.asyncio
async def test_goals_status_snapshots_active_goal_and_plan_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(run: TaskRun) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    async with _open_goal_rpc_stack(
        tmp_path / "goal-status.sqlite",
        handler=handler,
    ) as stack:
        empty = await _handle_goals_status({"sessionKey": SOURCE_KEY}, stack.context)
        assert empty["goal"] is None
        assert empty["planRun"] is None

        set_response = await _handle_goals_set(
            {
                "sessionKey": SOURCE_KEY,
                "message": "Ship the goal mode.",
            },
            stack.context,
        )
        await stack.runtime.wait(set_response["turnId"], timeout=2.0)

        status = await _handle_goals_status({"sessionKey": SOURCE_KEY}, stack.context)
        assert status["goal"] is not None
        assert status["goal"]["goalId"] == set_response["goalId"]
        assert status["goal"]["status"] == "running"
        assert status["goal"]["planRunId"] == set_response["planRun"]["runId"]
        assert status["planRun"] is not None
        assert status["planRun"]["runId"] == set_response["planRun"]["runId"]
        assert status["planRun"]["driverKind"] == "goal"
        assert status["planRun"]["driverId"] == set_response["goalId"]
        assert status["planRun"]["status"] == "paused"


@pytest.mark.asyncio
async def test_goals_set_replaces_old_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(run: TaskRun) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    async with _open_goal_rpc_stack(
        tmp_path / "goal-replace.sqlite",
        handler=handler,
    ) as stack:
        first = await _handle_goals_set(
            {
                "sessionKey": SOURCE_KEY,
                "message": "First goal.",
                "clientRequestId": "goal-replace-1",
            },
            stack.context,
        )
        await stack.runtime.wait(first["turnId"], timeout=2.0)
        first_run_id = first["planRun"]["runId"]
        first_revision_id = first["planRun"]["planRevisionId"]

        second = await _handle_goals_set(
            {
                "sessionKey": SOURCE_KEY,
                "message": "Second goal.",
                "clientRequestId": "goal-replace-2",
            },
            stack.context,
        )
        await stack.runtime.wait(second["turnId"], timeout=2.0)
        assert second["goalId"] != first["goalId"]

        old_goal = await stack.storage.get_goal_run(first["goalId"])
        assert old_goal is not None
        assert old_goal.status == "cancelled"
        assert old_goal.terminal_reason == "superseded_by_new_goal"

        old_run = await stack.storage.get_plan_run(first_run_id)
        assert old_run is not None
        assert old_run.status == "superseded"
        assert old_run.terminal_reason == "superseded_by_new_goal"

        new_goal = await stack.storage.get_goal_run(second["goalId"])
        assert new_goal is not None
        assert new_goal.status == "running"
        assert new_goal.plan_run_id == second["planRun"]["runId"]

        new_run = await stack.storage.get_plan_run(second["planRun"]["runId"])
        assert new_run is not None
        assert new_run.driver_kind == "goal"
        assert new_run.driver_id == second["goalId"]
        assert new_run.status == "paused"
        assert new_run.pause_reason == "goal_turn_finished"

        # The replacement goal revision is a replan of the first goal's
        # revision so the storage layer could atomically swap it in.
        first_revision = await stack.storage.get_plan_revision(first_revision_id)
        new_revision = await stack.storage.get_plan_revision(
            second["planRun"]["planRevisionId"]
        )
        assert first_revision is not None and new_revision is not None
        assert new_revision.generation == first_revision.generation + 1
        assert new_revision.plan_id == first_revision.plan_id
        assert new_revision.parent_revision_id == first_revision.revision_id

        status = await _handle_goals_status({"sessionKey": SOURCE_KEY}, stack.context)
        assert status["goal"] is not None
        assert status["goal"]["goalId"] == second["goalId"]
        assert status["planRun"] is not None
        assert status["planRun"]["runId"] == second["planRun"]["runId"]


@pytest.mark.asyncio
async def test_goals_clear_returns_before_snapshot_and_cancels_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(run: TaskRun) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    async with _open_goal_rpc_stack(
        tmp_path / "goal-clear.sqlite",
        handler=handler,
    ) as stack:
        empty = await _handle_goals_clear({"sessionKey": SOURCE_KEY}, stack.context)
        assert empty["goal"] is None
        assert empty["planRun"] is None

        set_response = await _handle_goals_set(
            {
                "sessionKey": SOURCE_KEY,
                "message": "Ship the goal mode.",
            },
            stack.context,
        )
        await stack.runtime.wait(set_response["turnId"], timeout=2.0)

        cleared = await _handle_goals_clear({"sessionKey": SOURCE_KEY}, stack.context)
        assert cleared["goal"] is not None
        assert cleared["goal"]["goalId"] == set_response["goalId"]
        assert cleared["goal"]["status"] == "running"
        assert cleared["planRun"] is not None
        assert cleared["planRun"]["runId"] == set_response["planRun"]["runId"]

        status = await _handle_goals_status({"sessionKey": SOURCE_KEY}, stack.context)
        assert status["goal"] is None
        assert status["planRun"] is None

        goal = await stack.storage.get_goal_run(set_response["goalId"])
        assert goal is not None
        assert goal.status == "cancelled"
        assert goal.terminal_reason == "superseded_by_new_goal"

        plan_run = await stack.storage.get_plan_run(set_response["planRun"]["runId"])
        assert plan_run is not None
        assert plan_run.status == "cancelled"
        assert plan_run.terminal_reason == "cleared_by_goal_controller"


@pytest.mark.asyncio
async def test_goals_pause_and_resume_state_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(run: TaskRun) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    async with _open_goal_rpc_stack(
        tmp_path / "goal-pause.sqlite",
        handler=handler,
    ) as stack:
        with pytest.raises(RpcHandlerError) as no_goal:
            await _handle_goals_pause({"sessionKey": SOURCE_KEY}, stack.context)
        assert no_goal.value.code == "NO_ACTIVE_GOAL"

        set_response = await _handle_goals_set(
            {
                "sessionKey": SOURCE_KEY,
                "message": "Ship the goal mode.",
            },
            stack.context,
        )
        await stack.runtime.wait(set_response["turnId"], timeout=2.0)
        run_id = set_response["planRun"]["runId"]

        paused = await _handle_goals_pause({"sessionKey": SOURCE_KEY}, stack.context)
        assert paused["goal"]["status"] == "paused"

        with pytest.raises(RpcHandlerError) as already_paused:
            await _handle_goals_pause({"sessionKey": SOURCE_KEY}, stack.context)
        assert already_paused.value.code == "GOAL_NOT_RUNNING"

        resumed = await _handle_goals_resume({"sessionKey": SOURCE_KEY}, stack.context)
        assert resumed["goal"]["status"] == "running"

        with pytest.raises(RpcHandlerError) as not_paused:
            await _handle_goals_resume({"sessionKey": SOURCE_KEY}, stack.context)
        assert not_paused.value.code == "GOAL_NOT_PAUSED"

        # Pause/resume only flips the goal state machine; the plan run stays
        # paused at the goal_turn_finished anchor until WO-4 resumes it.
        plan_run = await stack.storage.get_plan_run(run_id)
        assert plan_run is not None
        assert plan_run.status == "paused"
        assert plan_run.pause_reason == "goal_turn_finished"


@pytest.mark.asyncio
async def test_send_passthrough_driver_kind_defaults_to_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[TaskRun] = []

    async def handler(run: TaskRun) -> None:
        captured.append(run)

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    async with _open_goal_rpc_stack(
        tmp_path / "goal-passthrough.sqlite",
        handler=handler,
    ) as stack:
        session = await stack.storage.get_session(SOURCE_KEY)
        assert session is not None
        revision = await stack.storage.create_plan_revision(
            new_plan_revision(
                source_session_key=SOURCE_KEY,
                source_session_id=session.session_id,
                source_epoch=int(session.epoch or 0),
                title="Ship the manual plan",
                markdown="## Manual plan\n\nWork the ordered steps.",
                steps=[
                    {"step_id": "inspect", "title": "Inspect"},
                    {"step_id": "verify", "title": "Verify"},
                ],
            ),
            expected_parent_revision_id=None,
        )

        # Default keeps the manual driver behavior untouched.
        manual_result = await _handle_sessions_send(
            {
                "key": SOURCE_KEY,
                "message": "Implement the manual plan.",
                "clientRequestId": "passthrough-manual",
                "intent": "continue",
                "queueMode": "followup",
                "inputProvenanceKind": "plan_implementation",
                "noMemoryCapture": True,
                "displayText": "",
                "source": {"caller_kind": "web", "source_name": "goals.set"},
            },
            stack.context,
            fingerprint_params={"action": "goals.set", "sessionKey": SOURCE_KEY},
            plan_revision_id=revision.revision_id,
            required_collaboration_mode="default",
        )
        await stack.runtime.wait(manual_result["turn_id"], timeout=2.0)
        manual_task = await stack.storage.get_agent_task(manual_result["turn_id"])
        assert manual_task is not None
        manual_run = await stack.storage.get_plan_run(
            manual_task.details["metadata"]["plan_run_id"]
        )
        assert manual_run is not None
        assert manual_run.driver_kind == "manual"
        assert manual_run.driver_id is None

        # goals.set supersedes any active plan run before starting its own;
        # mirror that so the goal send below creates a fresh goal-driven run.
        await stack.storage.supersede_active_plan_runs(
            SOURCE_KEY,
            reason="superseded_by_new_goal",
        )

        # Explicit goal driver binding is passed through to the created run.
        goal_result = await _handle_sessions_send(
            {
                "key": SOURCE_KEY,
                "message": _expected_goal_message("Passthrough goal."),
                "clientRequestId": "passthrough-goal",
                "intent": "continue",
                "queueMode": "followup",
                "inputProvenanceKind": "goal_implementation",
                "noMemoryCapture": True,
                "displayText": "",
                "source": {"caller_kind": "web", "source_name": "goals.set"},
            },
            stack.context,
            fingerprint_params={"action": "goals.set", "sessionKey": SOURCE_KEY},
            plan_revision_id=revision.revision_id,
            plan_run_driver_kind="goal",
            plan_run_driver_id="goal-passthrough",
            required_collaboration_mode="default",
        )
        await stack.runtime.wait(goal_result["turn_id"], timeout=2.0)
        goal_task = await stack.storage.get_agent_task(goal_result["turn_id"])
        assert goal_task is not None
        goal_run = await stack.storage.get_plan_run(
            goal_task.details["metadata"]["plan_run_id"]
        )
        assert goal_run is not None
        assert goal_run.driver_kind == "goal"
        assert goal_run.driver_id == "goal-passthrough"
        assert goal_run.status == "paused"
        assert goal_run.pause_reason == "goal_turn_finished"

        # Inconsistent driver bindings are rejected before any acceptance.
        with pytest.raises(ValueError, match="plan_run_driver_id is required"):
            await _handle_sessions_send(
                {
                    "key": SOURCE_KEY,
                    "message": "Bogus goal send.",
                    "clientRequestId": "passthrough-bad",
                    "intent": "continue",
                    "queueMode": "followup",
                    "inputProvenanceKind": "goal_implementation",
                    "noMemoryCapture": True,
                    "displayText": "",
                    "source": {"caller_kind": "web", "source_name": "goals.set"},
                },
                stack.context,
                plan_revision_id=revision.revision_id,
                plan_run_driver_kind="goal",
            )
        with pytest.raises(ValueError, match="plan_run_driver_kind must be manual or goal"):
            await _handle_sessions_send(
                {
                    "key": SOURCE_KEY,
                    "message": "Bogus driver kind.",
                    "clientRequestId": "passthrough-bad-kind",
                    "intent": "continue",
                    "queueMode": "followup",
                    "inputProvenanceKind": "plan_implementation",
                    "noMemoryCapture": True,
                    "displayText": "",
                    "source": {"caller_kind": "web", "source_name": "goals.set"},
                },
                stack.context,
                plan_revision_id=revision.revision_id,
                plan_run_driver_kind="hack",
            )
