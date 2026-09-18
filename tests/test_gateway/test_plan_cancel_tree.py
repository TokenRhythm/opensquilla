"""Plan Stop uses the same exact task-tree cancellation as ordinary Stop."""

import asyncio
from pathlib import Path

import pytest
from test_plan_rpc import SOURCE_KEY, _ignore_subscriber_event, _open_plan_rpc_stack

from opensquilla.gateway import subagent_announce
from opensquilla.gateway.background_completion import BackgroundCompletionManager
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc_sessions import _handle_plans_cancel_run, _handle_plans_implement
from opensquilla.session.models import AgentTaskStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("child_running", [False, True])
async def test_plan_stop_cancels_exact_descendants_and_completion_without_stopping_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_running: bool,
) -> None:
    started: dict[str, asyncio.Event] = {}
    release = asyncio.Event()

    async def handler(run):
        started.setdefault(run.envelope.session_key, asyncio.Event()).set()
        await release.wait()

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers", _ignore_subscriber_event,
    )
    async with _open_plan_rpc_stack(
        tmp_path / "plan-stop-tree.sqlite", handler=handler,
        max_concurrency=3 if child_running else 2,
    ) as stack:
        manager = BackgroundCompletionManager(session_manager=stack.manager)
        group_released = asyncio.Event()
        manager.set_idle_listener(lambda _key: group_released.set())
        monkeypatch.setattr(subagent_announce, "_background_completion_manager", manager)
        try:
            parent = await _handle_plans_implement({
                "sessionKey": SOURCE_KEY,
                "planRevisionId": stack.source_revision.revision_id,
                "clientRequestId": "synthetic-plan-parent",
            }, stack.context)
            await asyncio.wait_for(
                started.setdefault(SOURCE_KEY, asyncio.Event()).wait(), timeout=2,
            )
            parent_id = parent["task_id"]

            async def enqueue_child(name, parent_task_id):
                key = f"agent:main:subagent:{name}"
                node = await stack.manager.create(
                    key, agent_id="main", parent_session_key=SOURCE_KEY,
                )
                handle = await stack.runtime.enqueue(RouteEnvelope(
                    source_kind=SourceKind.SYSTEM, source_name="sessions_spawn", agent_id="main",
                    session_key=key, session_id=node.session_id, session_epoch=node.epoch,
                    metadata={"parent_task_id": parent_task_id, "parent_session_key": SOURCE_KEY},
                ), "Synthetic task work", run_kind="subagent")
                return key, handle

            sibling_key, sibling = await enqueue_child("unrelated", "different-parent-task")
            await asyncio.wait_for(
                started.setdefault(sibling_key, asyncio.Event()).wait(), timeout=2,
            )
            child_key, child = await enqueue_child("owned", parent_id)
            if child_running:
                await asyncio.wait_for(
                    started.setdefault(child_key, asyncio.Event()).wait(), timeout=2,
                )
            else:
                assert (await stack.storage.get_agent_task(child.task_id)).status == "queued"
            await manager.emit_waiting(parent_session_key=SOURCE_KEY, parent_task_id=parent_id)
            run = await stack.storage.get_latest_plan_run_for_revision(
                stack.source_revision.revision_id,
            )
            response = await _handle_plans_cancel_run({
                "sessionKey": SOURCE_KEY, "runId": run.run_id,
                "expectedStateRevision": run.state_revision,
            }, stack.context)
            assert response["planRun"]["status"] == "cancelled"
            assert (await stack.runtime.wait(parent_id, timeout=2)).status == (
                AgentTaskStatus.CANCELLED
            )
            assert (await stack.runtime.wait(child.task_id, timeout=2)).status == (
                AgentTaskStatus.CANCELLED
            )
            await asyncio.wait_for(group_released.wait(), timeout=2)
            assert await manager.active_group_ids(SOURCE_KEY) == []
            assert (await stack.storage.get_agent_task(sibling.task_id)).status == "running"
            # A late completion for the cancelled parent cannot start another
            # turn, including after the PlanRun has become terminal.
            tasks_before = {row.task_id for row in await stack.runtime.list()}
            await manager.send_parent_wake(
                parent_session_key=SOURCE_KEY, parent_task_id=parent_id,
                payloads=[{"task_id": child.task_id, "status": "cancelled"}],
                task_runtime=stack.runtime, message="Synthetic late completion",
                provenance={"kind": "internal_system", "source_tool": "subagent_completion",
                            "parent_task_id": parent_id},
            )
            assert {row.task_id for row in await stack.runtime.list()} == tasks_before
        finally:
            release.set()
            await manager.close(timeout=2)
