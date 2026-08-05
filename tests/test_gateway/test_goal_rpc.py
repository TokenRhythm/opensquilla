"""End-to-end gateway contracts for the Goal driver surface (goals.* RPC)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.goal_driver import get_goal_watcher_registry
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_goals import (
    _handle_goals_clear,
    _handle_goals_observe,
    _handle_goals_pause,
    _handle_goals_resume,
    _handle_goals_set,
    _handle_goals_status,
)
from opensquilla.gateway.rpc_sessions import (
    _handle_sessions_send,
    _hydrate_sessions_messages_metadata,
)
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
            "display_text": "Ship the goal mode.",
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
async def test_goal_driver_turn_injects_goal_run_into_runtime_services(
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
        tmp_path / "goal-inject.sqlite",
        handler=handler,
    ) as stack:
        response = await _handle_goals_set(
            {
                "sessionKey": SOURCE_KEY,
                "message": "Ship the goal mode.",
                "clientRequestId": "goal-inject-1",
            },
            stack.context,
        )
        terminal = await stack.runtime.wait(response["turnId"], timeout=2.0)
        assert terminal.status == AgentTaskStatus.SUCCEEDED

        assert len(captured) == 1
        envelope = captured[0].envelope
        plan_run = envelope.runtime_services.get("plan_run")
        assert plan_run is not None
        assert plan_run.driver_kind == "goal"
        goal_run = envelope.runtime_services.get("goal_run")
        assert goal_run is not None
        assert goal_run.goal_id == response["goalId"]
        assert goal_run.goal_text == "Ship the goal mode."
        assert goal_run.plan_run_id == plan_run.run_id

        tool_context = envelope.tool_context(is_owner=True)
        assert tool_context.goal_run is goal_run
        assert tool_context.plan_run is plan_run


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
async def test_goal_internal_plan_revision_stays_hidden_after_goal_finishes(
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
        tmp_path / "goal-hidden-plan.sqlite",
        handler=handler,
    ) as stack:
        response = await _handle_goals_set(
            {
                "sessionKey": SOURCE_KEY,
                "message": "Keep the internal plan private.",
                "clientRequestId": "goal-hidden-plan",
            },
            stack.context,
        )
        await stack.runtime.wait(response["turnId"], timeout=2.0)

        goal = await stack.storage.get_goal_run(response["goalId"])
        assert goal is not None
        completed_goal = await stack.storage.complete_goal_run(
            goal.goal_id,
            expected_updated_at=goal.updated_at,
        )
        plan_run = await stack.storage.get_plan_run(response["planRun"]["runId"])
        assert plan_run is not None
        await stack.storage.cancel_plan_run(
            plan_run.run_id,
            expected_state_revision=plan_run.state_revision,
            reason="goal_complete",
        )

        assert completed_goal.status == "complete"
        metadata = await _hydrate_sessions_messages_metadata(stack.context, SOURCE_KEY)
        assert metadata["currentPlan"] is None
        assert metadata["activePlanRun"] is None


@pytest.mark.asyncio
async def test_goals_set_send_failure_cancels_orphan_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(run: TaskRun) -> None:
        return None

    async def exploding_send(*_args: Any, **_kwargs: Any) -> dict:
        raise RuntimeError("send exploded")

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._handle_sessions_send",
        exploding_send,
    )
    async with _open_goal_rpc_stack(
        tmp_path / "goal-set-failure.sqlite",
        handler=handler,
    ) as stack:
        with pytest.raises(RuntimeError, match="send exploded"):
            await _handle_goals_set(
                {
                    "sessionKey": SOURCE_KEY,
                    "message": "Doomed goal.",
                },
                stack.context,
            )

        # The ledger row created before the send must not linger as an
        # unturned running orphan: it is cancelled best-effort with the
        # durable goal_set_failed terminal reason.
        goal = await stack.storage.get_latest_goal_run(SOURCE_KEY)
        assert goal is not None
        assert goal.status == "cancelled"
        assert goal.terminal_reason == "goal_set_failed"


@pytest.mark.asyncio
async def test_goals_status_falls_back_to_latest_completed_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[TaskRun] = []
    manager_holder: dict[str, Any] = {}
    markers: list[str | None] = [None, "[goal:complete]"]

    async def handler(run: TaskRun) -> None:
        captured.append(run)
        manager = manager_holder.get("manager")
        marker = markers.pop(0) if markers else None
        if marker is not None and manager is not None:
            await manager.append_message(SOURCE_KEY, "assistant", marker)

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    registry = get_goal_watcher_registry()
    registry.unobserve(SOURCE_KEY, "goal-rpc-test")  # defensive cleanup
    try:
        async with _open_goal_rpc_stack(
            tmp_path / "goal-status-complete.sqlite",
            handler=handler,
        ) as stack:
            manager_holder["manager"] = stack.manager
            registry.observe(SOURCE_KEY, "goal-rpc-test")
            set_response = await _handle_goals_set(
                {
                    "sessionKey": SOURCE_KEY,
                    "message": "Finish this goal.",
                },
                stack.context,
            )

            # Turn 2 ends with [goal:complete]: the driver terminalizes the
            # goal so no active run remains on the session.
            deadline = time.monotonic() + 5.0
            goal = await stack.storage.get_goal_run(set_response["goalId"])
            while (
                goal is not None
                and goal.status != "complete"
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.01)
                goal = await stack.storage.get_goal_run(set_response["goalId"])
            assert goal is not None
            assert goal.status == "complete"

            # R3: goals.status falls back to the most recent goal (any status)
            # instead of an empty active slot, so the completed outcome stays
            # visible with its plan run attached.
            status = await _handle_goals_status({"sessionKey": SOURCE_KEY}, stack.context)
            assert status["goal"] is not None
            assert status["goal"]["goalId"] == set_response["goalId"]
            assert status["goal"]["status"] == "complete"
            assert "terminalReason" in status["goal"]
            assert status["planRun"] is not None
            assert status["planRun"]["runId"] == set_response["planRun"]["runId"]
            assert status["planRun"]["status"] == "cancelled"
            assert status["planRun"]["terminalReason"] == "goal_complete"
    finally:
        registry.unobserve(SOURCE_KEY, "goal-rpc-test")


@pytest.mark.asyncio
async def test_goals_resume_restarts_after_transient_turn_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume must work from the goal_turn_failed anchor (failure recovery)."""

    captured: list[TaskRun] = []

    async def handler(run: TaskRun) -> None:
        captured.append(run)
        raise RuntimeError("simulated provider overload")

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    async with _open_goal_rpc_stack(
        tmp_path / "goal-resume-failed-anchor.sqlite",
        handler=handler,
    ) as stack:
        set_response = await _handle_goals_set(
            {
                "sessionKey": SOURCE_KEY,
                "message": "Resume after a transient failure.",
                "clientRequestId": "goal-resume-failed-set",
            },
            stack.context,
        )
        await stack.runtime.wait(set_response["turnId"], timeout=2.0)
        goal_id = set_response["goalId"]
        print(
            f"DEBUG same={stack.runtime._storage is stack.storage} "
            f"runtime_conn={stack.runtime._storage.conn is not None} "
            f"stack_conn={stack.storage.conn is not None}"
        )

        # Turn 1 failed: the driver parks the plan run at goal_turn_failed and
        # schedules a retry. The post-turn hook runs inside the turn's own
        # finally, which outlives ``runtime.wait``, so poll until it lands.
        deadline = time.monotonic() + 5.0
        goal = await stack.storage.get_goal_run(goal_id)
        while (
            goal is None or goal.failure_retries < 1
        ) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            goal = await stack.storage.get_goal_run(goal_id)
        assert goal is not None
        assert goal.status == "running"
        assert goal.failure_retries == 1
        assert goal.next_retry_at_ms is not None

        plan_run = await stack.storage.get_plan_run(
            set_response["planRun"]["runId"]
        )
        assert plan_run is not None
        assert plan_run.pause_reason == "goal_turn_failed"

        paused = await _handle_goals_pause({"sessionKey": SOURCE_KEY}, stack.context)
        assert paused["goal"]["status"] == "paused"
        assert paused["goal"]["pauseReason"] == "user_paused"
        assert paused["goal"]["nextRetryAtMs"] is None

        # Resume from the failed anchor enqueues a fresh goal_turn immediately.
        resumed = await _handle_goals_resume(
            {"sessionKey": SOURCE_KEY},
            stack.context,
        )
        assert resumed["goal"]["status"] == "running"
        assert resumed["goal"]["nextRetryAtMs"] is None
        assert resumed["goal"]["lastError"] is None
        assert resumed["taskId"]
        deadline = time.monotonic() + 5.0
        while len(captured) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert len(captured) == 2


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
        # R3: with no active run, status falls back to the most recent goal so
        # the cleared outcome stays visible instead of an empty slot.
        assert status["goal"] is not None
        assert status["goal"]["goalId"] == set_response["goalId"]
        assert status["goal"]["status"] == "cancelled"
        assert status["goal"]["terminalReason"] == "superseded_by_new_goal"
        assert status["planRun"] is not None
        assert status["planRun"]["runId"] == set_response["planRun"]["runId"]
        assert status["planRun"]["status"] == "cancelled"

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

@pytest.mark.asyncio
async def test_goals_observe_watch_flips_watcher_eligibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(run: TaskRun) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    registry = get_goal_watcher_registry()
    registry.unobserve(SOURCE_KEY, "goal-rpc-test")  # defensive cleanup
    try:
        async with _open_goal_rpc_stack(
            tmp_path / "goal-observe.sqlite",
            handler=handler,
        ) as stack:
            on = await _handle_goals_observe(
                {"sessionKey": SOURCE_KEY, "watch": True},
                stack.context,
            )
            assert on["sessionKey"] == SOURCE_KEY
            assert on["clientId"] == "goal-rpc-test"
            assert on["watching"] is True
            assert on["watchers"] >= 1
            assert registry.has_watchers(SOURCE_KEY) is True

            off = await _handle_goals_observe(
                {"sessionKey": SOURCE_KEY, "watch": False},
                stack.context,
            )
            assert off["watching"] is False
            assert off["watchers"] == 0
            assert registry.has_watchers(SOURCE_KEY) is False
    finally:
        registry.unobserve(SOURCE_KEY, "goal-rpc-test")


@pytest.mark.asyncio
async def test_goals_resume_enqueues_goal_turn_when_paused_at_finished_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[TaskRun] = []
    manager_holder: dict[str, Any] = {}
    markers: list[str | None] = [None, "[goal:complete]"]
    resumed_started = asyncio.Event()
    release_resumed = asyncio.Event()

    async def handler(run: TaskRun) -> None:
        captured.append(run)
        if len(captured) == 2:  # the goal_turn enqueued by goals.resume
            resumed_started.set()
            await release_resumed.wait()
        manager = manager_holder.get("manager")
        marker = markers.pop(0) if markers else None
        if marker is not None and manager is not None:
            await manager.append_message(SOURCE_KEY, "assistant", marker)

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    registry = get_goal_watcher_registry()
    registry.unobserve(SOURCE_KEY, "goal-rpc-test")  # defensive cleanup
    try:
        async with _open_goal_rpc_stack(
            tmp_path / "goal-resume-enqueue.sqlite",
            handler=handler,
        ) as stack:
            manager_holder["manager"] = stack.manager
            set_response = await _handle_goals_set(
                {
                    "sessionKey": SOURCE_KEY,
                    "message": "Resume this goal.",
                    "clientRequestId": "goal-resume-enqueue-set",
                },
                stack.context,
            )
            await stack.runtime.wait(set_response["turnId"], timeout=2.0)
            run_id = set_response["planRun"]["runId"]

            paused = await _handle_goals_pause(
                {"sessionKey": SOURCE_KEY},
                stack.context,
            )
            assert paused["goal"]["status"] == "paused"

            # Resume on a paused goal whose plan run sits at the
            # goal_turn_finished anchor flips the goal back to running and
            # enqueues the next goal_turn task immediately.
            resumed = await _handle_goals_resume(
                {"sessionKey": SOURCE_KEY},
                stack.context,
            )
            assert resumed["goal"]["status"] == "running"
            task_id = resumed["taskId"]
            assert task_id

            # Park the resumed turn in-flight so the plan-run claim and the
            # running-state guard are observable without racing the hook.
            await asyncio.wait_for(resumed_started.wait(), timeout=5.0)
            assert len(captured) == 2
            assert captured[1].run_kind == "goal_turn"
            plan_run = await stack.storage.get_plan_run(run_id)
            assert plan_run is not None
            assert plan_run.status == "running"
            assert plan_run.active_task_id == task_id

            continued_record = await stack.storage.get_agent_task(task_id)
            assert continued_record is not None
            assert continued_record.run_kind == "goal_turn"
            metadata = continued_record.details["metadata"]
            assert metadata["plan_run_id"] == run_id
            assert "task_id" not in metadata

            # While the goal is running again, a second resume is rejected and
            # must not double-trigger another continuation.
            with pytest.raises(RpcHandlerError) as running_resume:
                await _handle_goals_resume(
                    {"sessionKey": SOURCE_KEY},
                    stack.context,
                )
            assert running_resume.value.code == "GOAL_NOT_PAUSED"
            assert len(captured) == 2

            # With a watcher present, the resumed turn's [goal:complete]
            # finishes the loop: goal and plan run both reach a terminal
            # state, no further task enqueued.
            registry.observe(SOURCE_KEY, "goal-rpc-test")
            release_resumed.set()
            terminal = await stack.runtime.wait(task_id, timeout=2.0)
            assert terminal.status == AgentTaskStatus.SUCCEEDED

            deadline = time.monotonic() + 5.0
            goal = await stack.storage.get_goal_run(set_response["goalId"])
            while goal is not None and goal.status != "complete" and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
                goal = await stack.storage.get_goal_run(set_response["goalId"])
            assert goal is not None
            assert goal.status == "complete"
            final_run = await stack.storage.get_plan_run(run_id)
            assert final_run is not None
            assert final_run.status == "cancelled"
            assert final_run.terminal_reason == "goal_complete"
            assert len(captured) == 2
    finally:
        registry.unobserve(SOURCE_KEY, "goal-rpc-test")
