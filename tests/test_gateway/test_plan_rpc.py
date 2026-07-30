"""End-to-end gateway contracts for Plan operations and PlanRun execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_sessions import (
    _handle_plans_cancel_run,
    _handle_plans_implement,
    _handle_plans_revise,
)
from opensquilla.gateway.task_runtime import TaskRun, TaskRuntime
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskStatus, PlanRevisionRecord
from opensquilla.session.plans import new_plan_revision
from opensquilla.session.storage import SessionStorage

SOURCE_KEY = "agent:main:webchat:plan-rpc-source"

_PRINCIPAL = Principal(
    role="operator",
    scopes=frozenset({"operator.admin"}),
    is_owner=True,
    authenticated=True,
)

_TurnHandler = Callable[[TaskRun], Awaitable[None]]


@dataclass
class _PlanRpcStack:
    storage: SessionStorage
    manager: SessionManager
    runtime: TaskRuntime
    context: RpcContext
    source_revision: PlanRevisionRecord


@asynccontextmanager
async def _open_plan_rpc_stack(
    db_path: Path,
    *,
    handler: _TurnHandler,
    max_concurrency: int = 1,
) -> AsyncIterator[_PlanRpcStack]:
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_concurrency=max_concurrency,
        running_heartbeat_interval_s=None,
    )
    context = RpcContext(
        conn_id="plan-rpc-test",
        principal=_PRINCIPAL,
        config=GatewayConfig(
            workspace_dir=str(db_path.parent / "workspace"),
            memory={"flush_enabled": False},
            naming={"enabled": False},
        ),
        session_manager=manager,
        task_runtime=runtime,
    )
    source = await manager.create(SOURCE_KEY, agent_id="main")
    revision = await storage.create_plan_revision(
        new_plan_revision(
            source_session_key=SOURCE_KEY,
            source_session_id=source.session_id,
            source_epoch=int(source.epoch or 0),
            title="Ship the plan feature",
            markdown=(
                "## Approved plan\n\n"
                "Preserve the exact plan body when the implementation turn starts."
            ),
            steps=[
                {
                    "step_id": "inspect",
                    "title": "Inspect the runtime boundary",
                    "details": "Verify the immutable revision reaches the model.",
                },
                {
                    "step_id": "verify",
                    "title": "Run regression checks",
                },
            ],
        ),
        expected_parent_revision_id=None,
    )
    try:
        yield _PlanRpcStack(
            storage=storage,
            manager=manager,
            runtime=runtime,
            context=context,
            source_revision=revision,
        )
    finally:
        await runtime.shutdown(cancel=True, timeout=2.0)
        await storage.close()


async def _ignore_subscriber_event(*_args: Any, **_kwargs: Any) -> None:
    return None


def _envelope(session_key: str, *, source_name: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name=source_name,
        agent_id="main",
        session_key=session_key,
        input_provenance={"kind": "synthetic-test"},
    )


@pytest.mark.asyncio
async def test_implement_binds_exact_run_injects_full_plan_and_rejects_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    captured: list[TaskRun] = []

    async def handler(run: TaskRun) -> None:
        captured.append(run)
        entered.set()
        await release.wait()

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    async with _open_plan_rpc_stack(
        tmp_path / "plan-rpc.sqlite",
        handler=handler,
    ) as stack:
        response = await _handle_plans_implement(
            {
                "sessionKey": SOURCE_KEY,
                "planRevisionId": stack.source_revision.revision_id,
                "clientRequestId": "implement-current-1",
                "intent": "continue",
            },
            stack.context,
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)

        accepted_run = response["planRun"]
        assert accepted_run["planRevisionId"] == stack.source_revision.revision_id
        expected_message = (
            f"Implement the approved plan “{stack.source_revision.title}”. "
            "Work through its ordered steps and record truthful checkpoints."
        )
        assert captured[0].message == expected_message
        assert captured[0].no_memory_capture is True
        assert captured[0].envelope.input_provenance == {
            "kind": "plan_implementation"
        }
        transcript = await stack.manager.get_transcript(SOURCE_KEY)
        assert len(transcript) == 1
        persisted = json.loads(transcript[0].content)
        assert persisted == {
            "text": expected_message,
            "display_text": "",
            "attachments": [],
        }
        task = await stack.storage.get_agent_task(response["turn_id"])
        assert task is not None
        assert task.details is not None
        assert task.details["metadata"]["plan_run_id"] == accepted_run["runId"]

        tool_context = captured[0].envelope.tool_context(is_owner=True)
        assert tool_context.collaboration_mode == "default"
        assert tool_context.plan_run_id == accepted_run["runId"]
        assert tool_context.plan_revision == stack.source_revision
        prompt_context = TurnRunner._extra_context_for_tool_context(tool_context)
        approved = prompt_context["Approved Plan Execution"]
        assert "Checkpoint every current step immediately" in approved
        assert "before starting work assigned to any later step" in approved
        assert "Never jump over the current step" in approved
        assert "one at a time in plan order" in approved
        assert "After the final completed checkpoint is accepted" in approved
        payload = json.loads(approved[approved.index("{") :])
        assert payload["markdown"] == stack.source_revision.markdown
        assert payload["steps"] == stack.source_revision.steps
        assert payload["content_hash"] == stack.source_revision.content_hash

        replay = await _handle_plans_implement(
            {
                "sessionKey": SOURCE_KEY,
                "planRevisionId": stack.source_revision.revision_id,
                "clientRequestId": "implement-current-1",
                "intent": "continue",
            },
            stack.context,
        )
        assert replay["turn_id"] == response["turn_id"]
        assert replay["planRun"]["runId"] == accepted_run["runId"]
        assert replay["planRevision"]["revisionId"] == stack.source_revision.revision_id

        with pytest.raises(RpcHandlerError) as duplicate:
            await _handle_plans_implement(
                {
                    "sessionKey": SOURCE_KEY,
                    "planRevisionId": stack.source_revision.revision_id,
                    "clientRequestId": "implement-current-2",
                    "intent": "continue",
                },
                stack.context,
            )
        assert duplicate.value.code == "PLAN_RUN_ACTIVE"
        assert duplicate.value.details["runId"] == accepted_run["runId"]

        release.set()
        terminal = await stack.runtime.wait(response["turn_id"], timeout=2.0)
        assert terminal.status == AgentTaskStatus.SUCCEEDED
        paused = await stack.storage.get_plan_run(accepted_run["runId"])
        assert paused is not None
        assert paused.status == "paused"
        assert paused.pause_reason == "manual_turn_finished"
        assert paused.active_task_id is None


@pytest.mark.asyncio
async def test_implement_keeps_explicit_message_visible(
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
    async with _open_plan_rpc_stack(
        tmp_path / "plan-explicit-message.sqlite",
        handler=handler,
    ) as stack:
        explicit_message = "Implement this plan while preserving the public compatibility note."
        response = await _handle_plans_implement(
            {
                "sessionKey": SOURCE_KEY,
                "planRevisionId": stack.source_revision.revision_id,
                "clientRequestId": "implement-explicit-message",
                "intent": "continue",
                "message": explicit_message,
            },
            stack.context,
        )
        await stack.runtime.wait(response["turn_id"], timeout=2.0)

        assert captured[0].message == explicit_message
        assert captured[0].no_memory_capture is True
        assert captured[0].envelope.input_provenance == {
            "kind": "plan_implementation"
        }
        transcript = await stack.manager.get_transcript(SOURCE_KEY)
        assert len(transcript) == 1
        assert transcript[0].content == explicit_message


@pytest.mark.asyncio
async def test_new_task_copies_independent_plan_lineages(
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
    async with _open_plan_rpc_stack(
        tmp_path / "plan-new-task.sqlite",
        handler=handler,
        max_concurrency=2,
    ) as stack:
        target_keys = (
            "agent:main:webchat:plan-copy-a",
            "agent:main:webchat:plan-copy-b",
        )
        responses: list[dict[str, Any]] = []
        for index, target_key in enumerate(target_keys, start=1):
            response = await _handle_plans_implement(
                {
                    "sessionKey": target_key,
                    "planRevisionId": stack.source_revision.revision_id,
                    "clientRequestId": f"implement-new-{index}",
                    "intent": "new_chat",
                },
                stack.context,
            )
            responses.append(response)
            await stack.runtime.wait(response["turn_id"], timeout=2.0)
            replay = await _handle_plans_implement(
                {
                    "sessionKey": target_key,
                    "planRevisionId": stack.source_revision.revision_id,
                    "clientRequestId": f"implement-new-{index}",
                    "intent": "new_chat",
                },
                stack.context,
            )
            assert replay["session_key"] == response["session_key"]
            assert replay["turn_id"] == response["turn_id"]
            assert replay["planRun"]["runId"] == response["planRun"]["runId"]
            assert (
                replay["planRevision"]["revisionId"]
                == response["planRevision"]["revisionId"]
            )

        copied = [
            await stack.storage.get_plan_revision(response["planRevision"]["revisionId"])
            for response in responses
        ]
        assert all(revision is not None for revision in copied)
        first, second = copied
        assert first is not None
        assert second is not None
        assert first.revision_id != second.revision_id
        assert first.plan_id != second.plan_id
        assert first.plan_id != stack.source_revision.plan_id
        assert second.plan_id != stack.source_revision.plan_id
        assert first.title == second.title == stack.source_revision.title
        assert first.markdown == second.markdown == stack.source_revision.markdown
        assert first.steps == second.steps == stack.source_revision.steps
        assert first.generation == second.generation == 1

        first_next = await stack.storage.create_plan_revision(
            new_plan_revision(
                source_session_key=target_keys[0],
                source_session_id=first.source_session_id,
                source_epoch=first.source_epoch,
                title="First independent replan",
                markdown="## First independent replan",
                steps=[{"step_id": "first", "title": "First"}],
                parent=first,
            ),
            expected_parent_revision_id=first.revision_id,
        )
        second_next = await stack.storage.create_plan_revision(
            new_plan_revision(
                source_session_key=target_keys[1],
                source_session_id=second.source_session_id,
                source_epoch=second.source_epoch,
                title="Second independent replan",
                markdown="## Second independent replan",
                steps=[{"step_id": "second", "title": "Second"}],
                parent=second,
            ),
            expected_parent_revision_id=second.revision_id,
        )
        assert first_next.generation == second_next.generation == 2
        assert first_next.plan_id != second_next.plan_id

        await stack.storage.delete_session(SOURCE_KEY)
        assert await stack.storage.get_plan_revision(first_next.revision_id) == first_next
        assert await stack.storage.get_plan_revision(second_next.revision_id) == second_next
        for index, (target_key, response) in enumerate(
            zip(target_keys, responses, strict=True),
            start=1,
        ):
            replay_after_source_delete = await _handle_plans_implement(
                {
                    "sessionKey": target_key,
                    "planRevisionId": stack.source_revision.revision_id,
                    "clientRequestId": f"implement-new-{index}",
                    "intent": "new_chat",
                },
                stack.context,
            )
            assert replay_after_source_delete["turn_id"] == response["turn_id"]
            assert (
                replay_after_source_delete["planRun"]["runId"]
                == response["planRun"]["runId"]
            )
        assert {
            run.envelope.runtime_services["plan_revision"].revision_id
            for run in captured
        } == {first.revision_id, second.revision_id}


@pytest.mark.asyncio
async def test_explicit_implement_mode_is_pinned_at_queued_execution_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker_entered = asyncio.Event()
    release_blocker = asyncio.Event()
    implementation_entered = asyncio.Event()
    observed_collaboration: list[tuple[str, int]] = []

    async def handler(run: TaskRun) -> None:
        if run.message == "block the only slot":
            blocker_entered.set()
            await release_blocker.wait()
            return
        tool_context = run.envelope.tool_context(is_owner=True)
        observed_collaboration.append(
            (
                tool_context.collaboration_mode,
                tool_context.collaboration_revision,
            )
        )
        implementation_entered.set()

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    async with _open_plan_rpc_stack(
        tmp_path / "plan-mode-pin.sqlite",
        handler=handler,
    ) as stack:
        blocker_key = "agent:main:webchat:plan-mode-blocker"
        await stack.manager.create(blocker_key, agent_id="main")
        blocker = await stack.runtime.enqueue(
            _envelope(blocker_key, source_name="blocker"),
            "block the only slot",
        )
        await asyncio.wait_for(blocker_entered.wait(), timeout=2.0)

        response = await _handle_plans_implement(
            {
                "sessionKey": SOURCE_KEY,
                "planRevisionId": stack.source_revision.revision_id,
                "clientRequestId": "implement-mode-pin",
                "intent": "continue",
            },
            stack.context,
        )
        current = await stack.storage.get_session(SOURCE_KEY)
        assert current is not None
        accepted_revision = int(current.collaboration_revision or 0)
        await stack.storage.set_collaboration_mode(
            SOURCE_KEY,
            "plan",
            expected_revision=int(current.collaboration_revision or 0),
        )

        release_blocker.set()
        await stack.runtime.wait(blocker.task_id, timeout=2.0)
        await asyncio.wait_for(implementation_entered.wait(), timeout=2.0)
        await stack.runtime.wait(response["turn_id"], timeout=2.0)
        assert observed_collaboration == [("default", accepted_revision)]


@pytest.mark.asyncio
async def test_replan_mode_and_revision_are_pinned_together(
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
    async with _open_plan_rpc_stack(
        tmp_path / "replan-mode-pin.sqlite",
        handler=handler,
    ) as stack:
        response = await _handle_plans_revise(
            {
                "sessionKey": SOURCE_KEY,
                "planRevisionId": stack.source_revision.revision_id,
                "prompt": "Tighten the verification step.",
                "clientRequestId": "replan-mode-pin",
            },
            stack.context,
        )
        await stack.runtime.wait(response["turn_id"], timeout=2.0)

        assert len(captured) == 1
        tool_context = captured[0].envelope.tool_context(is_owner=True)
        assert tool_context.collaboration_mode == "plan"
        assert tool_context.collaboration_revision == response["collaboration"]["revision"]


@pytest.mark.asyncio
async def test_cancel_stops_a_queued_implementation_before_handler_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker_entered = asyncio.Event()
    release_blocker = asyncio.Event()
    implementation_entered = asyncio.Event()

    async def handler(run: TaskRun) -> None:
        if run.message == "block the only slot":
            blocker_entered.set()
            await release_blocker.wait()
        else:
            implementation_entered.set()

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    async with _open_plan_rpc_stack(
        tmp_path / "plan-cancel-queued.sqlite",
        handler=handler,
    ) as stack:
        blocker_key = "agent:main:webchat:plan-cancel-blocker"
        await stack.manager.create(blocker_key, agent_id="main")
        blocker = await stack.runtime.enqueue(
            _envelope(blocker_key, source_name="blocker"),
            "block the only slot",
        )
        await asyncio.wait_for(blocker_entered.wait(), timeout=2.0)

        response = await _handle_plans_implement(
            {
                "sessionKey": SOURCE_KEY,
                "planRevisionId": stack.source_revision.revision_id,
                "clientRequestId": "implement-cancel-queued",
                "intent": "continue",
            },
            stack.context,
        )
        queued = await stack.storage.get_plan_run(response["planRun"]["runId"])
        assert queued is not None
        assert queued.status == "queued"
        assert queued.active_task_id == response["turn_id"]

        cancelled = await _handle_plans_cancel_run(
            {
                "sessionKey": SOURCE_KEY,
                "runId": queued.run_id,
                "expectedStateRevision": queued.state_revision,
            },
            stack.context,
        )
        terminal = await stack.runtime.wait(response["turn_id"], timeout=2.0)
        assert terminal.status == AgentTaskStatus.CANCELLED
        assert cancelled["planRun"]["status"] == "cancelled"
        assert not implementation_entered.is_set()

        release_blocker.set()
        await stack.runtime.wait(blocker.task_id, timeout=2.0)
