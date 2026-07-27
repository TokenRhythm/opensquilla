"""End-to-end RPC contracts for durable, atomic turn acceptance."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

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
            "client_message_id": "collect-composer-1",
            "surface_id": "tui:collect-1",
            "intent": "send",
            "disposition": "queued",
            "revision": 1,
        }
        assert entries[-1].turn_context == {
            "turn_id": first.payload["task_id"],
            "client_message_id": "collect-composer-2",
            "surface_id": "tui:collect-2",
            "intent": "send",
            "disposition": "queued",
            "target_turn_id": first.payload["task_id"],
            "revision": 2,
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
        assert entries[0].turn_context["client_message_id"] == "web-composer-message"
        assert entries[0].turn_context["surface_id"] == "webui:chat"
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }
