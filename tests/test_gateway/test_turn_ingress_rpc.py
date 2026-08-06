"""End-to-end RPC contracts for durable, atomic turn acceptance."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.steps.meta_command import (
    format_meta_replay_sentinel,
    meta_command_launch,
    pending_meta_launch_peek,
    pending_meta_launch_pop,
    pending_meta_launch_put,
    pending_meta_launch_state,
)
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus
from opensquilla.session.storage import SessionStorage
from opensquilla.session.turn_context import current_turn_context, turn_context_scope

SESSION_KEY = "agent:main:webchat:atomic-ingress"
CLIENT_REQUEST_ID = "client-request-atomic-1"

_PRINCIPAL = Principal(
    role="operator",
    scopes=frozenset(["operator.admin"]),
    is_owner=True,
    authenticated=True,
)


@dataclass
class _RealIngressStack:
    db_path: Path
    storage: SessionStorage
    manager: SessionManager
    runtime: TaskRuntime
    context: RpcContext
    session_id: str
    handler_started: asyncio.Event
    release_handler: asyncio.Event

    async def wait_until_running(self) -> None:
        await asyncio.wait_for(self.handler_started.wait(), timeout=2.0)


@asynccontextmanager
async def _open_real_stack(
    db_path: Path,
    *,
    max_pending_per_session: int = 64,
) -> AsyncIterator[_RealIngressStack]:
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    session = await manager.create(
        SESSION_KEY,
        agent_id="main",
        display_name="Atomic ingress test",
    )
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def _turn_handler(_run: Any) -> None:
        handler_started.set()
        await release_handler.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_turn_handler,
        max_concurrency=1,
        max_pending_per_session=max_pending_per_session,
        running_heartbeat_interval_s=None,
    )
    context = RpcContext(
        conn_id="atomic-ingress-test",
        principal=_PRINCIPAL,
        config=GatewayConfig(
            workspace_dir=str(db_path.parent / "workspace"),
            memory={"flush_enabled": False},
            naming={"enabled": False},
        ),
        session_manager=manager,
        task_runtime=runtime,
    )
    stack = _RealIngressStack(
        db_path=db_path,
        storage=storage,
        manager=manager,
        runtime=runtime,
        context=context,
        session_id=session.session_id,
        handler_started=handler_started,
        release_handler=release_handler,
    )
    try:
        yield stack
    finally:
        release_handler.set()
        for reservations in list(runtime._reservations_by_session.values()):
            for reservation in list(reservations):
                await runtime.abort_reservation(reservation)
        await runtime.shutdown(cancel=True, timeout=2.0)
        await storage.close()


def _table_counts(db_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "transcript_entries",
                "agent_tasks",
                "turn_ingress_receipts",
            )
        }
    finally:
        connection.close()


def _assert_no_runtime_acceptance_state(runtime: TaskRuntime) -> None:
    assert runtime._reservations_by_session == {}
    assert runtime._tasks == {}
    assert runtime._pending_by_session == {}
    assert runtime._running_by_session == {}


@pytest.mark.asyncio
async def test_durable_manual_meta_control_survives_memory_loss_and_long_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = "durable-meta-control-after-restart"
    db_path = tmp_path / "sessions.db"
    response_payload: dict[str, Any]
    async with _open_real_stack(db_path) as stack:
        intent, disposition = await stack.storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
            meta_skill_name="meta-tiny",
        )
        assert disposition == "stamped"
        # No in-process launch cache exists: this models a Gateway restart and
        # also proves the old 15-minute monotonic staging TTL is irrelevant.
        assert pending_meta_launch_peek(SESSION_KEY, client_request_id=request_id) is None
        monkeypatch.setattr(
            "opensquilla.engine.steps.meta_command.time.monotonic",
            lambda: 10**12,
        )

        response = await get_dispatcher().dispatch(
            "rpc-durable-meta-control",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "/meta meta-tiny -- write a durable paper",
                "clientRequestId": request_id,
            },
            stack.context,
        )
        await stack.wait_until_running()
        assert response.ok is True
        response_payload = dict(response.payload)

        accepted = await stack.storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
        )
        assert accepted is not None
        assert accepted.intent_id == intent.intent_id
        assert accepted.status == "accepted"
        assert accepted.accepted_task_id == response.payload["task_id"]
        accepted_task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert accepted_task is not None
        assert accepted_task.queue_mode == "followup"

        duplicate = await get_dispatcher().dispatch(
            "rpc-durable-meta-control-duplicate",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "/meta meta-tiny -- write a durable paper",
                "clientRequestId": request_id,
            },
            stack.context,
        )
        assert duplicate.ok is True
        assert duplicate.payload["replayed"] is True
        assert duplicate.payload["task_id"] == response.payload["task_id"]
        stack.release_handler.set()
        await stack.runtime.wait(response.payload["task_id"], timeout=2.0)

    # Reopen SQLite after the accepted task completed. The exact server-bound
    # control remains on the transcript and can seed the engine without any
    # module-level pending marker.
    reopened = await SessionStorage.open(str(db_path))
    try:
        entries = await reopened.get_transcript(response_payload["session_id"])
        turn_context = entries[0].turn_context
        assert isinstance(turn_context, dict)
        assert turn_context["meta_control"]["intent_id"] == intent.intent_id
        launch_turn = SimpleNamespace(
            session_key=SESSION_KEY,
            message="/meta meta-tiny -- write a durable paper",
            semantic_message="/meta meta-tiny -- write a durable paper",
            metadata={},
        )
        with turn_context_scope(turn_context):
            await meta_command_launch(launch_turn)
        assert launch_turn.metadata["meta_launch"] == {
            "name": "meta-tiny",
            "request": "write a durable paper",
        }
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_durable_meta_control_ordinary_turn_cannot_consume(
    tmp_path: Path,
) -> None:
    request_id = "durable-meta-control-mismatch"
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        intent, _ = await stack.storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
            meta_skill_name="meta-tiny",
        )
        response = await get_dispatcher().dispatch(
            "rpc-durable-meta-control-mismatch",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "an ordinary message must not claim the staged control",
                "clientRequestId": request_id,
            },
            stack.context,
        )
        await stack.wait_until_running()
        assert response.ok is True
        untouched = await stack.storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
        )
        assert untouched is not None
        assert untouched.intent_id == intent.intent_id
        assert untouched.status == "staged"
        entries = await stack.storage.get_transcript(stack.session_id)
        assert "meta_control" not in (entries[0].turn_context or {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "/meta unstaged-skill",
        "/meta-replay 0123456789abcdef0123456789abcdef",
    ],
)
async def test_request_bound_meta_control_without_matching_stage_fails_closed(
    tmp_path: Path,
    message: str,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        response = await get_dispatcher().dispatch(
            "rpc-unstaged-meta-control",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": message,
                "clientRequestId": "unstaged-meta-control",
            },
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "META_CONTROL_NOT_STAGED"
        assert response.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }


@pytest.mark.asyncio
async def test_same_key_reset_invalidates_control_retained_by_another_client(
    tmp_path: Path,
) -> None:
    request_id = "meta-control-staged-before-reset"
    launch_text = "/meta meta-tiny -- must not cross the reset boundary"
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        original_session_id = stack.session_id
        staged, _ = await stack.storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
            meta_skill_name="meta-tiny",
        )

        reset = await get_dispatcher().dispatch(
            "rpc-reset-with-staged-control",
            "sessions.reset",
            {"key": SESSION_KEY},
            stack.context,
        )
        assert reset.ok is True
        assert reset.payload["session_id"] != original_session_id
        assert reset.payload["epoch"] == 1
        assert await stack.storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=staged.correlation_id,
        ) is None

        # Model a second tab whose browser outbox still holds the pre-reset
        # marker. Server-side reset fencing must reject it independently of any
        # client cleanup or synchronization.
        stale_send = await get_dispatcher().dispatch(
            "rpc-stale-control-after-reset",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": launch_text,
                "clientRequestId": request_id,
            },
            stack.context,
        )
        assert stale_send.ok is False
        assert stale_send.error is not None
        assert stale_send.error.code == "META_CONTROL_NOT_STAGED"
        assert stale_send.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }


@pytest.mark.asyncio
async def test_durable_failed_step_replay_survives_commit_then_memory_loss(
    tmp_path: Path,
) -> None:
    nonce = "0123456789abcdef0123456789abcdef"
    request_id = "durable-replay-control"
    db_path = tmp_path / "sessions.db"
    async with _open_real_stack(db_path) as stack:
        intent, _ = await stack.storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="replay",
            correlation_id=f"nonce:{nonce}",
            meta_skill_name="meta-tiny",
            replay_run_id="source-run-1",
            replay_mode="failed-step",
        )
        assert pending_meta_launch_peek(SESSION_KEY, client_request_id=request_id) is None
        launch_text = format_meta_replay_sentinel(nonce)
        response = await get_dispatcher().dispatch(
            "rpc-durable-replay-control",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": launch_text,
                "clientRequestId": request_id,
            },
            stack.context,
        )
        await stack.wait_until_running()
        assert response.ok is True
        entries = await stack.storage.get_transcript(stack.session_id)
        turn_context = entries[0].turn_context
        assert isinstance(turn_context, dict)
        assert turn_context["meta_control"]["intent_id"] == intent.intent_id

        replay_turn = SimpleNamespace(
            session_key=SESSION_KEY,
            message=launch_text,
            semantic_message=launch_text,
            metadata={},
        )
        with turn_context_scope(turn_context):
            await meta_command_launch(replay_turn)
        assert replay_turn.metadata["meta_replay"] == {
            "run_id": "source-run-1",
            "name": "meta-tiny",
            "mode": "failed-step",
        }


@pytest.mark.asyncio
async def test_queued_meta_control_reopens_and_reactivates_exactly_once(
    tmp_path: Path,
) -> None:
    """A crash after acceptance but before RUNNING cannot lose the launch."""

    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage)
    session = await manager.create(SESSION_KEY, agent_id="main")
    blocker_started = asyncio.Event()
    hold_blocker = asyncio.Event()

    async def _blocking_handler(_run: Any) -> None:
        blocker_started.set()
        await hold_blocker.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_blocking_handler,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
    )
    context = RpcContext(
        conn_id="meta-restart-before-start",
        principal=_PRINCIPAL,
        config=GatewayConfig(
            workspace_dir=str(tmp_path / "workspace"),
            memory={"flush_enabled": False},
            naming={"enabled": False},
        ),
        session_manager=manager,
        task_runtime=runtime,
    )
    blocker = await get_dispatcher().dispatch(
        "rpc-meta-restart-blocker",
        "chat.send",
        {
            "sessionKey": SESSION_KEY,
            "message": "occupy the only runtime slot",
            "clientRequestId": "meta-restart-blocker",
        },
        context,
    )
    assert blocker.ok is True
    await asyncio.wait_for(blocker_started.wait(), timeout=2.0)

    request_id = "meta-restart-accepted-control"
    launch_text = "/meta meta-tiny -- preserve exact semantic input"
    await storage.stage_meta_control_intent(
        session_key=SESSION_KEY,
        control_kind="manual",
        correlation_id=f"request:{request_id}",
        meta_skill_name="meta-tiny",
    )
    accepted = await get_dispatcher().dispatch(
        "rpc-meta-restart-control",
        "chat.send",
        {
            "sessionKey": SESSION_KEY,
            "message": launch_text,
            "clientRequestId": request_id,
        },
        context,
    )
    assert accepted.ok is True
    task_id = accepted.payload["task_id"]
    queued = await storage.get_agent_task(task_id)
    assert queued is not None
    assert queued.status == "queued"
    assert queued.details is not None
    assert queued.details["meta_control_message"] == launch_text
    assert queued.details["meta_control_semantic_message"] == launch_text
    transcript = await storage.get_transcript(session.session_id)
    control_entry = next(
        entry
        for entry in transcript
        if entry.message_id == accepted.payload["message_id"]
    )
    assert control_entry.content != launch_text  # SessionManager applied its timestamp prefix.

    # Model an abrupt process loss: close SQLite before cancelling in-memory
    # coroutines, so their cancellation cleanup cannot rewrite durable state.
    old_async_tasks = [
        task.asyncio_task
        for task in runtime._tasks.values()
        if task.asyncio_task is not None
    ]
    await storage.close()
    for old_task in old_async_tasks:
        old_task.cancel()
    await asyncio.gather(*old_async_tasks, return_exceptions=True)

    reopened = await SessionStorage.open(str(db_path))
    recovered_runs: list[tuple[Any, dict[str, Any] | None]] = []

    async def _capture_recovered(run: Any) -> None:
        turn_context = current_turn_context()
        recovered_runs.append((run, dict(turn_context) if turn_context is not None else None))

    recovered_runtime = TaskRuntime(
        storage=reopened,
        turn_handler=_capture_recovered,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
    )
    try:
        abandoned = await reopened.get_agent_task(task_id)
        assert abandoned is not None
        assert abandoned.status == "abandoned"
        assert abandoned.terminal_reason == "meta_control_restart_before_start"

        assert await recovered_runtime.recover_durable_meta_controls() == 1
        completed = await recovered_runtime.wait(task_id, timeout=2.0)
        assert completed.status == "succeeded"
        assert len(recovered_runs) == 1
        recovered_run, recovered_context = recovered_runs[0]
        assert recovered_run.task_id == task_id
        assert recovered_run.message == launch_text
        assert recovered_run.semantic_message == launch_text
        assert recovered_context is not None
        assert recovered_context["meta_control"]["name"] == "meta-tiny"

        # A second recovery pass and a response-loss retry both reuse the
        # accepted identity without creating or executing another task.
        assert await recovered_runtime.recover_durable_meta_controls() == 0
        reopened_manager = SessionManager(reopened)
        reopened_context = RpcContext(
            conn_id="meta-restart-retry",
            principal=_PRINCIPAL,
            config=context.config,
            session_manager=reopened_manager,
            task_runtime=recovered_runtime,
        )
        duplicate = await get_dispatcher().dispatch(
            "rpc-meta-restart-control-duplicate",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": launch_text,
                "clientRequestId": request_id,
            },
            reopened_context,
        )
        assert duplicate.ok is True
        assert duplicate.payload["replayed"] is True
        assert duplicate.payload["task_id"] == task_id
        assert len(recovered_runs) == 1
        assert _table_counts(db_path) == {
            "transcript_entries": 2,
            "agent_tasks": 2,
            "turn_ingress_receipts": 2,
        }
    finally:
        await recovered_runtime.shutdown(cancel=True, timeout=2.0)
        await reopened.close()


@pytest.mark.asyncio
async def test_meta_control_recovery_is_nonblocking_and_fair_to_other_sessions() -> None:
    session_key = "agent:main:webchat:recovery-batches"
    records: dict[str, AgentTaskRecord] = {}
    claims: list[Any] = []
    for index in range(3):
        task_id = f"recovery-task-{index}"
        message_id = f"recovery-message-{index}"
        message = f"/meta meta-tiny -- batch {index}"
        control = {
            "version": 1,
            "intent_id": f"intent-{index}",
            "kind": "manual",
            "name": "meta-tiny",
            "correlation_id": f"request:batch-{index}",
        }
        task = AgentTaskRecord(
            task_id=task_id,
            session_key=session_key,
            agent_id="main",
            source_kind="web",
            queue_mode="interrupt",
            run_kind="session_turn",
            status=AgentTaskStatus.ABANDONED,
            terminal_reason="meta_control_restart_before_start",
            details={
                "source_name": "RPC",
                "input_provenance": {},
                "metadata": {"meta_control": control},
                "persisted_user_message_id": message_id,
                "persisted_user_message_ids": [message_id],
                "meta_control_message": message,
                "meta_control_semantic_message": message,
            },
        )
        records[task_id] = task
        claims.append(SimpleNamespace(
            task=task,
            entry=SimpleNamespace(
                message_id=message_id,
                session_id="recovery-session-id",
                content=message,
            ),
        ))

    claim_calls = 0

    async def _claim(*, limit: int) -> list[Any]:
        nonlocal claim_calls
        claim_calls += 1
        claimed = claims[:limit]
        del claims[:limit]
        return claimed

    async def _update(task_id: str, **fields: Any) -> None:
        record = records[task_id]
        for field, value in fields.items():
            setattr(record, field, value)

    async def _create(record: AgentTaskRecord) -> None:
        records[record.task_id] = record

    async def _get(task_id: str) -> AgentTaskRecord | None:
        return records.get(task_id)

    async def _update_context(*_args: Any, **_kwargs: Any) -> bool:
        return True

    storage = SimpleNamespace(
        claim_recoverable_meta_control_tasks=_claim,
        create_agent_task=_create,
        update_agent_task=_update,
        get_agent_task=_get,
        update_transcript_turn_context=_update_context,
    )
    first_recovery_started = asyncio.Event()
    release_first_recovery = asyncio.Event()
    seen: list[tuple[str, str]] = []

    async def _handler(run: Any) -> None:
        seen.append((run.task_id, run.queue_mode))
        if run.task_id == "recovery-task-0":
            first_recovery_started.set()
            await release_first_recovery.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_handler,
        max_concurrency=1,
        max_pending_per_session=1,
        running_heartbeat_interval_s=None,
    )
    recovery = asyncio.create_task(runtime.recover_durable_meta_controls(limit=1))
    await asyncio.wait_for(first_recovery_started.wait(), timeout=2.0)
    assert await asyncio.wait_for(recovery, timeout=2.0) == 3

    await runtime.enqueue(
        RouteEnvelope(
            source_kind=SourceKind.WEB,
            source_name="RPC",
            agent_id="main",
            session_key="agent:main:webchat:ordinary-during-recovery",
            session_id="ordinary-session-id",
            input_provenance={},
            metadata={},
        ),
        "ordinary input",
        task_id="ordinary-task",
    )
    release_first_recovery.set()
    for task_id in records:
        assert (await runtime.wait(task_id, timeout=2.0)).status == "succeeded"
    assert claim_calls == 4
    assert sorted(seen) == [
        ("ordinary-task", "followup"),
        ("recovery-task-0", "followup"),
        ("recovery-task-1", "followup"),
        ("recovery-task-2", "followup"),
    ]
    started_task_ids = [task_id for task_id, _mode in seen]
    assert started_task_ids.index("ordinary-task") < started_task_ids.index("recovery-task-2")


@pytest.mark.asyncio
async def test_meta_launch_promotes_after_durable_acceptance_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = "meta-launch-promotion-order"
    assert (
        pending_meta_launch_put(
            SESSION_KEY,
            "meta-tiny",
            client_request_id=request_id,
        )
        == "stamped"
    )

    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        order: list[str] = []
        original_accept = stack.storage.accept_turn
        original_activate = stack.runtime.activate

        async def traced_accept(*args: Any, **kwargs: Any):
            acceptance = await original_accept(*args, **kwargs)
            order.append("durable")
            return acceptance

        async def traced_activate(*args: Any, **kwargs: Any):
            order.append(
                f"activate:{pending_meta_launch_state(SESSION_KEY, client_request_id=request_id)}"
            )
            return await original_activate(*args, **kwargs)

        monkeypatch.setattr(stack.storage, "accept_turn", traced_accept)
        monkeypatch.setattr(stack.runtime, "activate", traced_activate)
        params = {
            "sessionKey": SESSION_KEY,
            "message": "/meta meta-tiny",
            "clientRequestId": request_id,
        }
        response = await get_dispatcher().dispatch(
            "rpc-meta-promotion-order",
            "chat.send",
            params,
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert order == ["durable", "activate:accepted"]
        assert pending_meta_launch_state(
            SESSION_KEY,
            client_request_id=request_id,
        ) == "accepted"
        # Simulate the pipeline's exact one-shot claim, then replay the same
        # durable chat request. The receipt replay must not resurrect a marker.
        assert pending_meta_launch_pop(
            SESSION_KEY,
            client_request_id=request_id,
        ) == "meta-tiny"
        replay = await get_dispatcher().dispatch(
            "rpc-meta-promotion-replay",
            "chat.send",
            params,
            stack.context,
        )
        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert pending_meta_launch_state(
            SESSION_KEY,
            client_request_id=request_id,
        ) is None
        assert (
            pending_meta_launch_put(
                SESSION_KEY,
                "meta-tiny",
                client_request_id=request_id,
            )
            == "replayed"
        )


@pytest.mark.asyncio
async def test_durable_non_launch_message_does_not_promote_staged_marker(
    tmp_path: Path,
) -> None:
    request_id = "meta-launch-invalid-message"
    assert (
        pending_meta_launch_put(
            SESSION_KEY,
            "meta-tiny",
            client_request_id=request_id,
        )
        == "stamped"
    )

    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        response = await get_dispatcher().dispatch(
            "rpc-meta-invalid-promotion",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "ordinary chat text",
                "clientRequestId": request_id,
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert pending_meta_launch_state(
            SESSION_KEY,
            client_request_id=request_id,
        ) == "staged"
        assert pending_meta_launch_peek(
            SESSION_KEY,
            client_request_id=request_id,
        ) == "meta-tiny"

    pending_meta_launch_pop(SESSION_KEY, client_request_id=request_id)


@pytest.mark.asyncio
async def test_sessions_send_atomically_accepts_message_task_and_receipt(tmp_path: Path) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        response = await get_dispatcher().dispatch(
            "rpc-success",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "one durable turn",
                "clientRequestId": CLIENT_REQUEST_ID,
                "clientMessageId": "composer-message-1",
                "surfaceId": "tui:atomic-test",
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["clientRequestId"] == CLIENT_REQUEST_ID
        assert response.payload["message_id"]
        assert response.payload["task_id"]
        assert response.payload["turn_id"] == response.payload["task_id"]
        assert response.payload["user_message_id"] == response.payload["message_id"]
        assert response.payload["client_message_id"] == "composer-message-1"
        assert response.payload["surface_id"] == "tui:atomic-test"
        assert response.payload["replayed"] is False
        entries = await stack.storage.get_transcript(stack.session_id)
        assert entries[0].turn_context == {
            "turn_id": response.payload["task_id"],
            "client_request_id": CLIENT_REQUEST_ID,
            "client_message_id": "composer-message-1",
            "surface_id": "tui:atomic-test",
            "intent": "send",
            "disposition": "applied",
            "revision": 1,
        }
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_atomic_direct_send_enters_registry_admission_before_accept_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        stack.context.task_runtime = None
        registry = get_agent_task_registry()
        admission_attempted = asyncio.Event()
        accept_called = asyncio.Event()
        original_admission = registry.admission
        original_accept = stack.storage.accept_turn

        @asynccontextmanager
        async def observed_admission(session_key: str) -> AsyncIterator[None]:
            admission_attempted.set()
            async with original_admission(session_key):
                yield

        async def observed_accept(*args: Any, **kwargs: Any) -> Any:
            accept_called.set()
            return await original_accept(*args, **kwargs)

        monkeypatch.setattr(registry, "admission", observed_admission)
        monkeypatch.setattr(stack.storage, "accept_turn", observed_accept)
        admission_lock = registry._admission_locks.setdefault(
            SESSION_KEY,
            asyncio.Lock(),
        )

        async with admission_lock:
            sending = asyncio.create_task(
                get_dispatcher().dispatch(
                    "rpc-direct-admission",
                    "sessions.send",
                    {
                        "key": SESSION_KEY,
                        "message": "direct accepted turn",
                        "clientRequestId": "direct-admission-request",
                    },
                    stack.context,
                )
            )
            admission_wait = asyncio.create_task(admission_attempted.wait())
            accept_wait = asyncio.create_task(accept_called.wait())
            done, _pending = await asyncio.wait(
                {admission_wait, accept_wait},
                timeout=2.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            assert admission_wait in done
            assert accept_wait not in done
            assert sending.done() is False

        response = await asyncio.wait_for(sending, timeout=2.0)
        task = registry.get(SESSION_KEY)
        if task is not None:
            await task
        admission_wait.cancel()
        accept_wait.cancel()

        assert response.ok is True


@pytest.mark.asyncio
async def test_sessions_send_replays_same_request_without_duplicate_side_effects(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        params = {
            "key": SESSION_KEY,
            "message": "replay me exactly once",
            "clientRequestId": CLIENT_REQUEST_ID,
            "clientMessageId": "original-composer-message",
            "surfaceId": "tui:original",
        }
        first = await get_dispatcher().dispatch(
            "rpc-replay-first", "sessions.send", params, stack.context
        )
        await stack.wait_until_running()
        replay_params = {
            **params,
            "clientMessageId": "retry-composer-message",
            "surfaceId": "tui:retry",
        }
        replay = await get_dispatcher().dispatch(
            "rpc-replay-second", "sessions.send", replay_params, stack.context
        )

        assert first.ok is True
        assert replay.ok is True
        assert replay.payload["accepted"] is True
        assert replay.payload["replayed"] is True
        assert replay.payload["clientRequestId"] == CLIENT_REQUEST_ID
        assert replay.payload["message_id"] == first.payload["message_id"]
        assert replay.payload["task_id"] == first.payload["task_id"]
        assert replay.payload["turn_id"] == first.payload["turn_id"]
        assert replay.payload["client_message_id"] == "original-composer-message"
        assert replay.payload["surface_id"] == "tui:original"
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_sessions_send_fast_replay_consumes_legacy_meta_launch_draft(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        params = {
            "key": SESSION_KEY,
            "message": "accepted before the legacy Meta outbox was repaired",
            "clientRequestId": CLIENT_REQUEST_ID,
            "clientMessageId": "meta-control-message",
        }
        first = await get_dispatcher().dispatch(
            "rpc-meta-draft-first", "sessions.send", params, stack.context
        )
        assert first.ok is True

        # Reproduce an older build's crash window: ingress is committed, but a
        # browser-recovery draft with the same coordinates remains durable.
        await stack.storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=CLIENT_REQUEST_ID,
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- keep the original request",
        )
        assert await stack.storage.list_meta_launch_drafts(session_key=SESSION_KEY)

        replay = await get_dispatcher().dispatch(
            "rpc-meta-draft-replay", "sessions.send", params, stack.context
        )

        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert await stack.storage.list_meta_launch_drafts(session_key=SESSION_KEY) == []


@pytest.mark.asyncio
async def test_sessions_send_replay_exposes_terminal_task_status(tmp_path: Path) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        params = {
            "key": SESSION_KEY,
            "message": "finish before replay",
            "clientRequestId": CLIENT_REQUEST_ID,
        }
        first = await get_dispatcher().dispatch(
            "rpc-terminal-first", "sessions.send", params, stack.context
        )
        await stack.wait_until_running()
        stack.release_handler.set()
        terminal = await stack.runtime.wait(first.payload["task_id"], timeout=2.0)

        replay = await get_dispatcher().dispatch(
            "rpc-terminal-replay", "sessions.send", params, stack.context
        )

        assert str(terminal.status) == "succeeded"
        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert replay.payload["task_status"] == "succeeded"
        assert replay.payload["taskStatus"] == "succeeded"
        assert replay.payload["terminal_reason"] == "completed"
        assert replay.payload["terminal_message"] == "The task completed."


@pytest.mark.asyncio
async def test_activation_failure_is_returned_as_an_accepted_failed_task(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:

        async def _fail_activation(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("synthetic activation failure")

        stack.runtime.activate = _fail_activation  # type: ignore[method-assign]
        response = await get_dispatcher().dispatch(
            "rpc-activation-failure",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "accepted before activation fails",
                "clientRequestId": CLIENT_REQUEST_ID,
            },
            stack.context,
        )

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["task_status"] == "failed"
        assert response.payload["terminal_reason"] == "activation_failed"
        assert response.payload["terminal_message"] == "The task failed before it could finish."
        task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert task is not None
        assert task.status == "failed"
        assert stack.runtime._reservations_by_session == {}
        assert stack.runtime._tasks == {}


@pytest.mark.asyncio
async def test_post_accept_notification_failure_does_not_reject_the_turn(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:

        def _fail_notification(_entry: Any) -> None:
            raise RuntimeError("synthetic post-accept notification failure")

        stack.manager.notify_message_appended = _fail_notification  # type: ignore[method-assign]
        response = await get_dispatcher().dispatch(
            "rpc-post-accept-failure",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "durable despite observer failure",
                "clientRequestId": CLIENT_REQUEST_ID,
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["task_status"] == "queued"
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_cancellation_after_commit_still_activates_the_durable_task(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        original_accept_turn = stack.storage.accept_turn
        committed = asyncio.Event()
        release_accept = asyncio.Event()

        async def _pause_after_commit(*args: Any, **kwargs: Any) -> Any:
            result = await original_accept_turn(*args, **kwargs)
            committed.set()
            await release_accept.wait()
            return result

        stack.storage.accept_turn = _pause_after_commit  # type: ignore[method-assign]
        request = asyncio.create_task(
            get_dispatcher().dispatch(
                "rpc-cancel-after-commit",
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": "commit and activate despite disconnect",
                    "clientRequestId": CLIENT_REQUEST_ID,
                },
                stack.context,
            )
        )
        await asyncio.wait_for(committed.wait(), timeout=2.0)

        request.cancel()
        await asyncio.sleep(0)
        release_accept.set()
        response = await asyncio.wait_for(request, timeout=2.0)
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["task_id"] in stack.runtime._tasks
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }
        assert stack.runtime._reservations_by_session == {}


@pytest.mark.asyncio
async def test_sessions_send_rejects_request_id_reuse_with_different_payload(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        first = await get_dispatcher().dispatch(
            "rpc-conflict-first",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "original payload",
                "clientRequestId": CLIENT_REQUEST_ID,
            },
            stack.context,
        )
        await stack.wait_until_running()
        conflict = await get_dispatcher().dispatch(
            "rpc-conflict-second",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "different payload",
                "clientRequestId": CLIENT_REQUEST_ID,
            },
            stack.context,
        )

        assert first.ok is True
        assert conflict.ok is False
        assert conflict.error is not None
        assert conflict.error.code == "IDEMPOTENCY_CONFLICT"
        assert conflict.error.retryable is False
        assert conflict.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_sessions_send_storage_busy_is_retryable_and_has_no_acceptance_side_effects(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        stack.storage._busy_budget_seconds = 0.0
        await stack.storage.conn.execute("PRAGMA busy_timeout = 0")
        external_writer = sqlite3.connect(
            stack.db_path,
            isolation_level=None,
            timeout=0.0,
        )
        external_writer.execute("BEGIN IMMEDIATE")
        try:
            response = await get_dispatcher().dispatch(
                "rpc-busy",
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": "must remain unaccepted",
                    "clientRequestId": CLIENT_REQUEST_ID,
                },
                stack.context,
            )

            assert response.ok is False
            assert response.error is not None
            assert response.error.code == "STORAGE_BUSY"
            assert response.error.retryable is True
            assert response.error.accepted is False
            assert response.error.retry_after_ms is not None
            assert _table_counts(stack.db_path) == {
                "transcript_entries": 0,
                "agent_tasks": 0,
                "turn_ingress_receipts": 0,
            }
            _assert_no_runtime_acceptance_state(stack.runtime)
        finally:
            external_writer.execute("ROLLBACK")
            external_writer.close()


@pytest.mark.asyncio
async def test_sessions_send_stale_epoch_is_retryable_and_unaccepted(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        original_accept_turn = stack.storage.accept_turn

        async def _advance_epoch_before_accept(*args: Any, **kwargs: Any) -> Any:
            await stack.storage.increment_epoch(SESSION_KEY)
            return await original_accept_turn(*args, **kwargs)

        stack.storage.accept_turn = _advance_epoch_before_accept  # type: ignore[method-assign]
        response = await get_dispatcher().dispatch(
            "rpc-stale-epoch",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "retry after reset",
                "clientRequestId": CLIENT_REQUEST_ID,
            },
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "SESSION_CHANGED"
        assert response.error.retryable is True
        assert response.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }
        _assert_no_runtime_acceptance_state(stack.runtime)


@pytest.mark.asyncio
async def test_sessions_send_queue_full_is_unaccepted_and_does_not_persist_message(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(
        tmp_path / "sessions.db",
        max_pending_per_session=1,
    ) as stack:
        blocker = await stack.runtime.reserve(
            RouteEnvelope(
                source_kind=SourceKind.WEB,
                source_name="queue-capacity-test",
                agent_id="main",
                session_key=SESSION_KEY,
                input_provenance={"kind": "synthetic-test"},
            ),
            "reserve the only queue slot",
        )
        response = await get_dispatcher().dispatch(
            "rpc-queue-full",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "must not be persisted",
                "clientRequestId": CLIENT_REQUEST_ID,
                "queueMode": "followup",
            },
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "QUEUE_FULL"
        assert response.error.retryable is True
        assert response.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }
        assert stack.runtime._tasks == {}
        assert stack.runtime._pending_by_session == {}
        assert stack.runtime._reservations_by_session == {SESSION_KEY: [blocker]}

        await stack.runtime.abort_reservation(blocker)
        _assert_no_runtime_acceptance_state(stack.runtime)


@pytest.mark.asyncio
async def test_collect_mode_atomically_merges_message_and_receipt_into_queued_task(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        blocker = await get_dispatcher().dispatch(
            "rpc-collect-blocker",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "running blocker",
                "clientRequestId": "collect-blocker",
            },
            stack.context,
        )
        await stack.wait_until_running()
        first = await get_dispatcher().dispatch(
            "rpc-collect-first",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "collect one",
                "queueMode": "collect",
                "clientRequestId": "collect-first",
                "clientMessageId": "collect-composer-1",
                "surfaceId": "tui:collect-1",
            },
            stack.context,
        )
        second = await get_dispatcher().dispatch(
            "rpc-collect-second",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "collect two",
                "queueMode": "collect",
                "clientRequestId": "collect-second",
                "clientMessageId": "collect-composer-2",
                "surfaceId": "tui:collect-2",
            },
            stack.context,
        )

        assert blocker.ok is True
        assert first.ok is True
        assert second.ok is True
        assert second.payload["task_id"] == first.payload["task_id"]
        assert first.payload["turn_id"] == first.payload["task_id"]
        assert second.payload["turn_id"] == first.payload["task_id"]
        assert second.payload["client_message_id"] == "collect-composer-2"
        assert second.payload["surface_id"] == "tui:collect-2"
        candidate = stack.runtime._tasks[first.payload["task_id"]]
        assert candidate.message == "collect one\ncollect two"
        persisted = await stack.storage.get_agent_task(first.payload["task_id"])
        assert persisted is not None
        assert persisted.details is not None
        assert persisted.details["collected"] is True
        assert persisted.details["message_count"] == 2
        entries = await stack.storage.get_transcript(stack.session_id)
        assert entries[-2].turn_context == {
            "turn_id": first.payload["task_id"],
            "client_request_id": "collect-first",
            "client_message_id": "collect-composer-1",
            "surface_id": "tui:collect-1",
            "intent": "send",
            "disposition": "queued",
            "revision": 1,
            "sandbox_mode_resolution": {
                "desiredMode": "full",
                "effectiveMode": "full",
                "fallbackReason": None,
                "confirmationRequired": False,
            },
        }
        assert entries[-1].turn_context == {
            "turn_id": first.payload["task_id"],
            "client_request_id": "collect-second",
            "client_message_id": "collect-composer-2",
            "surface_id": "tui:collect-2",
            "intent": "send",
            "disposition": "queued",
            "target_turn_id": first.payload["task_id"],
            "revision": 2,
            "sandbox_mode_resolution": {
                "desiredMode": "full",
                "effectiveMode": "full",
                "fallbackReason": None,
                "confirmationRequired": False,
            },
        }
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 3,
            "agent_tasks": 2,
            "turn_ingress_receipts": 3,
        }


@pytest.mark.asyncio
async def test_concurrent_first_collects_share_one_admission_and_task(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        blocker = await get_dispatcher().dispatch(
            "rpc-concurrent-collect-blocker",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "running blocker",
                "clientRequestId": "concurrent-collect-blocker",
            },
            stack.context,
        )
        await stack.wait_until_running()

        original_reserve = stack.runtime.reserve
        first_reserved = asyncio.Event()
        release_first = asyncio.Event()
        pause_next_collect = True

        async def _pause_first_collect_reservation(
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            nonlocal pause_next_collect
            reservation = await original_reserve(*args, **kwargs)
            if kwargs.get("mode") == "collect" and pause_next_collect:
                pause_next_collect = False
                first_reserved.set()
                await release_first.wait()
            return reservation

        stack.runtime.reserve = _pause_first_collect_reservation  # type: ignore[method-assign]
        first_request = asyncio.create_task(
            get_dispatcher().dispatch(
                "rpc-concurrent-collect-first",
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": "collect first",
                    "queueMode": "collect",
                    "clientRequestId": "concurrent-collect-first",
                },
                stack.context,
            )
        )
        await asyncio.wait_for(first_reserved.wait(), timeout=2.0)
        second_request = asyncio.create_task(
            get_dispatcher().dispatch(
                "rpc-concurrent-collect-second",
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": "collect second",
                    "queueMode": "collect",
                    "clientRequestId": "concurrent-collect-second",
                },
                stack.context,
            )
        )
        await asyncio.sleep(0.05)

        assert second_request.done() is False
        release_first.set()
        first, second = await asyncio.gather(first_request, second_request)

        assert blocker.ok is True
        assert first.ok is True
        assert second.ok is True
        assert first.payload["task_id"] == second.payload["task_id"]
        candidate = stack.runtime._tasks[first.payload["task_id"]]
        assert candidate.message == "collect first\ncollect second"
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 3,
            "agent_tasks": 2,
            "turn_ingress_receipts": 3,
        }


@pytest.mark.asyncio
async def test_collect_storage_busy_leaves_transcript_receipt_and_candidate_unchanged(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        await get_dispatcher().dispatch(
            "rpc-collect-busy-blocker",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "running blocker",
                "clientRequestId": "collect-busy-blocker",
            },
            stack.context,
        )
        await stack.wait_until_running()
        queued = await get_dispatcher().dispatch(
            "rpc-collect-busy-first",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "collect before busy",
                "queueMode": "collect",
                "clientRequestId": "collect-busy-first",
            },
            stack.context,
        )
        candidate = stack.runtime._tasks[queued.payload["task_id"]]
        original_message = candidate.message
        original_details = (await stack.storage.get_agent_task(queued.payload["task_id"])).details

        stack.storage._busy_budget_seconds = 0.0
        await stack.storage.conn.execute("PRAGMA busy_timeout = 0")
        external_writer = sqlite3.connect(
            stack.db_path,
            isolation_level=None,
            timeout=0.0,
        )
        external_writer.execute("BEGIN IMMEDIATE")
        try:
            rejected = await get_dispatcher().dispatch(
                "rpc-collect-busy-second",
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": "must not be collected",
                    "queueMode": "collect",
                    "clientRequestId": "collect-busy-second",
                },
                stack.context,
            )

            assert rejected.ok is False
            assert rejected.error is not None
            assert rejected.error.code == "STORAGE_BUSY"
            assert rejected.error.accepted is False
            assert candidate.message == original_message
            persisted = await stack.storage.get_agent_task(queued.payload["task_id"])
            assert persisted is not None
            assert persisted.details == original_details
            assert _table_counts(stack.db_path) == {
                "transcript_entries": 2,
                "agent_tasks": 2,
                "turn_ingress_receipts": 2,
            }
        finally:
            external_writer.execute("ROLLBACK")
            external_writer.close()


@pytest.mark.asyncio
async def test_chat_send_forwards_client_request_id_into_atomic_acceptance(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        response = await get_dispatcher().dispatch(
            "rpc-chat-forward",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "forward this request identity",
                "queueMode": "steer",
                "clientRequestId": CLIENT_REQUEST_ID,
                "clientMessageId": "web-composer-message",
                "surfaceId": "webui:chat",
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["clientRequestId"] == CLIENT_REQUEST_ID
        assert response.payload["client_message_id"] == "web-composer-message"
        assert response.payload["surface_id"] == "webui:chat"
        acceptance = await stack.storage.get_turn_ingress_receipt(
            source_scope="web:webchat:operator",
            request_session_key=SESSION_KEY,
            client_request_id=CLIENT_REQUEST_ID,
        )
        assert acceptance is not None
        assert acceptance.receipt.client_request_id == CLIENT_REQUEST_ID
        assert acceptance.receipt.message_id == response.payload["message_id"]
        assert acceptance.receipt.task_id == response.payload["task_id"]
        task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert task is not None
        assert task.queue_mode == "interrupt"
        entries = await stack.storage.get_transcript(stack.session_id)
        assert entries[0].turn_context is not None
        assert entries[0].turn_context["client_request_id"] == CLIENT_REQUEST_ID
        assert entries[0].turn_context["client_message_id"] == "web-composer-message"
        assert entries[0].turn_context["surface_id"] == "webui:chat"
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }
