"""Atomic RPC contracts for session-creating and session-resetting turns."""

from __future__ import annotations

import asyncio
import json
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
from opensquilla.session.models import SessionContextState, SessionSummary
from opensquilla.session.storage import SessionStorage

SESSION_KEY = "agent:main:webchat:atomic-intents"

_PRINCIPAL = Principal(
    role="operator",
    scopes=frozenset(["operator.admin"]),
    is_owner=True,
    authenticated=True,
)


@dataclass
class _IntentStack:
    db_path: Path
    storage: SessionStorage
    manager: SessionManager
    runtime: TaskRuntime
    context: RpcContext
    handler_started: asyncio.Event
    handler_cancelled: asyncio.Event
    release_handler: asyncio.Event

    async def wait_until_running(self) -> None:
        await asyncio.wait_for(self.handler_started.wait(), timeout=2.0)


@asynccontextmanager
async def _open_intent_stack(db_path: Path) -> AsyncIterator[_IntentStack]:
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    release_handler = asyncio.Event()

    async def _turn_handler(_run: Any) -> None:
        handler_started.set()
        try:
            await release_handler.wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_turn_handler,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
    )
    context = RpcContext(
        conn_id="atomic-intent-test",
        principal=_PRINCIPAL,
        config=GatewayConfig(
            workspace_dir=str(db_path.parent / "workspace"),
            memory={"flush_enabled": False},
            naming={"enabled": False},
        ),
        session_manager=manager,
        task_runtime=runtime,
    )
    stack = _IntentStack(
        db_path=db_path,
        storage=storage,
        manager=manager,
        runtime=runtime,
        context=context,
        handler_started=handler_started,
        handler_cancelled=handler_cancelled,
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
                "sessions",
                "transcript_entries",
                "session_summaries",
                "session_context_states",
                "agent_tasks",
                "turn_ingress_receipts",
            )
        }
    finally:
        connection.close()


async def _seed_reset_state(stack: _IntentStack) -> tuple[str, int, str]:
    node = await stack.manager.create(
        SESSION_KEY,
        agent_id="main",
        display_name="Before reset",
    )
    old_entry = await stack.manager.append_message(SESSION_KEY, "user", "old transcript")
    await stack.storage.save_summary(
        SessionSummary(
            session_id=node.session_id,
            session_key=SESSION_KEY,
            summary_text="old summary",
        )
    )
    await stack.manager.save_context_state(
        SessionContextState(
            session_id=node.session_id,
            session_key=SESSION_KEY,
            provider="portable",
            state_kind="structured_summary_v1",
            payload={"user_goal": "old task"},
            covered_through_id=old_entry.id or 0,
            portable=True,
            cacheable=True,
            valid=True,
        )
    )
    return node.session_id, int(node.epoch or 0), old_entry.message_id


@pytest.mark.asyncio
async def test_chat_send_new_chat_atomically_creates_webchat_turn(tmp_path: Path) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        assert await stack.storage.get_session(SESSION_KEY) is None

        response = await get_dispatcher().dispatch(
            "rpc-new-chat",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "first webchat turn",
                "clientRequestId": "new-chat-success",
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        created = await stack.storage.get_session(SESSION_KEY)
        assert created is not None
        assert created.display_name == "WebChat"
        assert created.session_id == response.payload["session_id"]

        transcript = await stack.storage.get_transcript(created.session_id)
        assert len(transcript) == 1
        assert transcript[0].role == "user"
        assert transcript[0].content == "first webchat turn"
        assert transcript[0].message_id == response.payload["message_id"]

        task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert task.session_key == SESSION_KEY
        receipt = await stack.storage.get_turn_ingress_receipt(
            source_scope="web:webchat:operator",
            request_session_key=SESSION_KEY,
            client_request_id="new-chat-success",
        )
        assert receipt is not None
        assert receipt.receipt.session_id == created.session_id
        assert receipt.receipt.message_id == transcript[0].message_id
        assert receipt.receipt.task_id == task.task_id
        assert _table_counts(stack.db_path) == {
            "sessions": 1,
            "transcript_entries": 1,
            "session_summaries": 0,
            "session_context_states": 0,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_explicit_continue_first_turn_replays_with_original_fingerprint(
    tmp_path: Path,
) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        params = {
            "sessionKey": SESSION_KEY,
            "message": "explicit continue creates this draft",
            "intent": "continue",
            "clientRequestId": "explicit-continue-first-turn",
        }

        first = await get_dispatcher().dispatch(
            "rpc-explicit-continue-first",
            "chat.send",
            params,
            stack.context,
        )
        await stack.wait_until_running()
        replay = await get_dispatcher().dispatch(
            "rpc-explicit-continue-replay",
            "chat.send",
            params,
            stack.context,
        )

        assert first.ok is True
        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert replay.payload["message_id"] == first.payload["message_id"]
        assert replay.payload["task_id"] == first.payload["task_id"]
        assert _table_counts(stack.db_path) == {
            "sessions": 1,
            "transcript_entries": 1,
            "session_summaries": 0,
            "session_context_states": 0,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_direct_sessions_send_cannot_override_intent_fingerprint(
    tmp_path: Path,
) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        await stack.manager.create(SESSION_KEY, agent_id="main")
        first = await get_dispatcher().dispatch(
            "rpc-direct-intent-first",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "one logical turn",
                "intent": "continue",
                "clientRequestId": "direct-intent-fingerprint",
                "_fingerprintIntentProvided": False,
                "_fingerprintIntent": "reset_same_key",
            },
            stack.context,
        )
        await stack.wait_until_running()
        conflict = await get_dispatcher().dispatch(
            "rpc-direct-intent-conflict",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "one logical turn",
                "intent": "reset_same_key",
                "clientRequestId": "direct-intent-fingerprint",
                "_fingerprintIntentProvided": False,
                "_fingerprintIntent": "continue",
            },
            stack.context,
        )

        assert first.ok is True
        assert conflict.ok is False
        assert conflict.error is not None
        assert conflict.error.code == "IDEMPOTENCY_CONFLICT"
        assert conflict.error.accepted is False


@pytest.mark.asyncio
async def test_new_chat_storage_busy_does_not_create_session(tmp_path: Path) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        stack.storage._busy_budget_seconds = 0.0
        await stack.storage.conn.execute("PRAGMA busy_timeout = 0")
        external_writer = sqlite3.connect(stack.db_path, isolation_level=None, timeout=0.0)
        external_writer.execute("BEGIN IMMEDIATE")
        try:
            response = await get_dispatcher().dispatch(
                "rpc-new-chat-busy",
                "chat.send",
                {
                    "sessionKey": SESSION_KEY,
                    "message": "must not create a draft",
                    "intent": "new_chat",
                    "clientRequestId": "new-chat-busy",
                },
                stack.context,
            )

            assert response.ok is False
            assert response.error is not None
            assert response.error.code == "STORAGE_BUSY"
            assert response.error.retryable is True
            assert response.error.accepted is False
            assert response.error.retry_after_ms is not None
            assert await stack.storage.get_session(SESSION_KEY) is None
            assert _table_counts(stack.db_path) == {
                "sessions": 0,
                "transcript_entries": 0,
                "session_summaries": 0,
                "session_context_states": 0,
                "agent_tasks": 0,
                "turn_ingress_receipts": 0,
            }
            assert stack.runtime._reservations_by_session == {}
            assert stack.runtime._tasks == {}
            assert stack.handler_started.is_set() is False
        finally:
            external_writer.execute("ROLLBACK")
            external_writer.close()


@pytest.mark.asyncio
async def test_concurrent_first_turns_return_a_typed_session_conflict(
    tmp_path: Path,
) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        original_prepare_intent = stack.manager.prepare_intent
        both_prepared = asyncio.Event()
        prepared_count = 0

        async def _prepare_together(*args: Any, **kwargs: Any) -> Any:
            nonlocal prepared_count
            plan = await original_prepare_intent(*args, **kwargs)
            prepared_count += 1
            if prepared_count == 2:
                both_prepared.set()
            await asyncio.wait_for(both_prepared.wait(), timeout=2.0)
            return plan

        stack.manager.prepare_intent = _prepare_together  # type: ignore[method-assign]

        async def _send(request_id: str, message: str) -> Any:
            return await get_dispatcher().dispatch(
                request_id,
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": message,
                    "intent": "new_chat",
                    "clientRequestId": request_id,
                },
                stack.context,
            )

        responses = await asyncio.gather(
            _send("concurrent-new-a", "first contender"),
            _send("concurrent-new-b", "second contender"),
        )

        successes = [response for response in responses if response.ok]
        conflicts = [response for response in responses if not response.ok]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert conflicts[0].error is not None
        assert conflicts[0].error.code == "SESSION_CONFLICT"
        assert conflicts[0].error.retryable is False
        assert conflicts[0].error.accepted is False
        assert _table_counts(stack.db_path) == {
            "sessions": 1,
            "transcript_entries": 1,
            "session_summaries": 0,
            "session_context_states": 0,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_reset_same_key_atomically_rotates_and_accepts_new_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        old_session_id, old_epoch, _old_message_id = await _seed_reset_state(stack)

        response = await get_dispatcher().dispatch(
            "rpc-reset-success",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "first turn after reset",
                "intent": "reset_same_key",
                "clientRequestId": "reset-success",
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        reset = await stack.storage.get_session(SESSION_KEY)
        assert reset is not None
        assert reset.session_id != old_session_id
        assert reset.session_id == response.payload["session_id"]
        assert reset.epoch == old_epoch + 1
        assert reset.display_name == "Before reset"

        assert await stack.storage.get_transcript(old_session_id) == []
        transcript = await stack.storage.get_transcript(reset.session_id)
        assert len(transcript) == 1
        assert transcript[0].content == "first turn after reset"
        assert transcript[0].message_id == response.payload["message_id"]
        assert await stack.storage.get_all_summaries(old_session_id) == []
        assert await stack.manager.get_context_states(SESSION_KEY) == []
        invalidated = await stack.manager.get_context_states(SESSION_KEY, valid_only=False)
        assert len(invalidated) == 1
        assert invalidated[0].valid is False
        assert invalidated[0].invalid_reason == "session_reset"

        task = await stack.storage.get_agent_task(response.payload["task_id"])
        receipt = await stack.storage.get_turn_ingress_receipt(
            source_scope="web:webchat:operator",
            request_session_key=SESSION_KEY,
            client_request_id="reset-success",
        )
        assert receipt is not None
        assert receipt.receipt.session_id == reset.session_id
        assert receipt.receipt.message_id == transcript[0].message_id
        assert receipt.receipt.task_id == task.task_id
        assert _table_counts(stack.db_path) == {
            "sessions": 1,
            "transcript_entries": 1,
            "session_summaries": 0,
            "session_context_states": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_reset_archive_snapshot_includes_append_committed_before_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_dir = tmp_path / "archives"
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(archive_dir))
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        old_session_id, _old_epoch, _old_message_id = await _seed_reset_state(stack)
        acceptance_entered = asyncio.Event()
        release_acceptance = asyncio.Event()
        original_accept_turn = stack.storage.accept_turn

        async def _pause_before_acceptance(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("reset_from_session_id") is not None:
                acceptance_entered.set()
                await release_acceptance.wait()
            return await original_accept_turn(*args, **kwargs)

        monkeypatch.setattr(stack.storage, "accept_turn", _pause_before_acceptance)
        reset_request = asyncio.create_task(
            get_dispatcher().dispatch(
                "rpc-reset-archive-race",
                "chat.send",
                {
                    "sessionKey": SESSION_KEY,
                    "message": "first turn after reset",
                    "intent": "reset_same_key",
                    "clientRequestId": "reset-archive-race",
                },
                stack.context,
            )
        )
        try:
            await asyncio.wait_for(acceptance_entered.wait(), timeout=2.0)
            await stack.manager.append_message(
                SESSION_KEY,
                "user",
                "append committed before reset acceptance",
                token_count=7,
            )
        finally:
            release_acceptance.set()

        response = await asyncio.wait_for(reset_request, timeout=2.0)
        await stack.wait_until_running()

        assert response.ok is True
        archive_files = list(archive_dir.glob("*.json"))
        assert len(archive_files) == 1
        archived = json.loads(archive_files[0].read_text(encoding="utf-8"))
        assert archived["session_id"] == old_session_id
        assert [
            entry["content"] for entry in archived["transcript_entries"]
        ] == [
            "old transcript",
            "append committed before reset acceptance",
        ]
        assert [summary["summary_text"] for summary in archived["summaries"]] == [
            "old summary"
        ]
        assert archived["session"]["total_tokens"] == 7


@pytest.mark.asyncio
async def test_reset_forces_interrupt_even_when_request_asks_for_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        old_session_id, _old_epoch, _old_message_id = await _seed_reset_state(stack)
        old_handle = await stack.runtime.enqueue(
            RouteEnvelope(
                source_kind=SourceKind.WEB,
                source_name="pre-reset-runtime-turn",
                agent_id="main",
                session_key=SESSION_KEY,
                session_id=old_session_id,
                input_provenance={"kind": "synthetic-test"},
            ),
            "must not survive the epoch rotation",
        )
        await stack.wait_until_running()

        response = await get_dispatcher().dispatch(
            "rpc-reset-followup",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "new epoch turn",
                "intent": "reset_same_key",
                "queueMode": "followup",
                "clientRequestId": "reset-followup",
            },
            stack.context,
        )
        await asyncio.wait_for(stack.handler_cancelled.wait(), timeout=2.0)
        old_terminal = await stack.runtime.wait(old_handle.task_id, timeout=2.0)

        assert response.ok is True
        assert str(old_terminal.status) == "cancelled"
        reset = await stack.storage.get_session(SESSION_KEY)
        assert reset is not None
        assert reset.session_id != old_session_id
        assert await stack.storage.get_transcript(old_session_id) == []
        assert [
            entry.content for entry in await stack.storage.get_transcript(reset.session_id)
        ] == ["new epoch turn"]
        new_task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert new_task is not None
        assert new_task.queue_mode == "interrupt"


@pytest.mark.asyncio
async def test_reset_cannot_overtake_a_committed_continue_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        old_session_id, _old_epoch, _old_message_id = await _seed_reset_state(stack)
        continue_activation_entered = asyncio.Event()
        release_continue_activation = asyncio.Event()
        reset_waiting_for_admission = asyncio.Event()
        continue_task_id: str | None = None
        admission_calls = 0
        original_activate = stack.runtime.activate
        original_admission = stack.runtime.collect_admission

        async def _pause_continue_activation(
            reservation: Any,
            **kwargs: Any,
        ) -> Any:
            nonlocal continue_task_id
            if reservation.runtime_task.message == "continue before reset":
                continue_task_id = reservation.task_id
                continue_activation_entered.set()
                await release_continue_activation.wait()
            return await original_activate(reservation, **kwargs)

        @asynccontextmanager
        async def _observe_admission(session_key: str) -> AsyncIterator[None]:
            nonlocal admission_calls
            admission_calls += 1
            if admission_calls == 2:
                reset_waiting_for_admission.set()
            async with original_admission(session_key):
                yield

        monkeypatch.setattr(stack.runtime, "activate", _pause_continue_activation)
        monkeypatch.setattr(stack.runtime, "collect_admission", _observe_admission)

        continue_request = asyncio.create_task(
            get_dispatcher().dispatch(
                "rpc-continue-before-reset",
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": "continue before reset",
                    "intent": "continue",
                    "clientRequestId": "continue-before-reset",
                },
                stack.context,
            )
        )
        reset_request: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(continue_activation_entered.wait(), timeout=2.0)
            assert continue_task_id is not None
            assert await stack.storage.get_agent_task(continue_task_id) is not None

            reset_request = asyncio.create_task(
                get_dispatcher().dispatch(
                    "rpc-reset-after-committed-continue",
                    "sessions.send",
                    {
                        "key": SESSION_KEY,
                        "message": "reset turn",
                        "intent": "reset_same_key",
                        "clientRequestId": "reset-after-committed-continue",
                    },
                    stack.context,
                )
            )
            await asyncio.wait_for(reset_waiting_for_admission.wait(), timeout=2.0)

            assert reset_request.done() is False
            before_reset = await stack.storage.get_session(SESSION_KEY)
            assert before_reset is not None
            assert before_reset.session_id == old_session_id
            assert [
                entry.content
                for entry in await stack.storage.get_transcript(old_session_id)
            ] == ["old transcript", "continue before reset"]
        finally:
            release_continue_activation.set()

        assert reset_request is not None
        continue_response, reset_response = await asyncio.wait_for(
            asyncio.gather(continue_request, reset_request),
            timeout=4.0,
        )

        assert continue_response.ok is True
        assert reset_response.ok is True
        assert continue_task_id is not None
        continue_terminal = await stack.runtime.wait(continue_task_id, timeout=2.0)
        assert str(continue_terminal.status) == "cancelled"
        reset = await stack.storage.get_session(SESSION_KEY)
        assert reset is not None
        assert reset.session_id != old_session_id
        assert [
            entry.content for entry in await stack.storage.get_transcript(reset.session_id)
        ] == ["reset turn"]


@pytest.mark.asyncio
async def test_reset_storage_busy_preserves_old_state_and_running_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        old_session_id, old_epoch, old_message_id = await _seed_reset_state(stack)
        old_handle = await stack.runtime.enqueue(
            RouteEnvelope(
                source_kind=SourceKind.WEB,
                source_name="existing-runtime-turn",
                agent_id="main",
                session_key=SESSION_KEY,
                input_provenance={"kind": "synthetic-test"},
            ),
            "keep this runtime turn alive",
        )
        await stack.wait_until_running()

        stack.storage._busy_budget_seconds = 0.0
        await stack.storage.conn.execute("PRAGMA busy_timeout = 0")
        external_writer = sqlite3.connect(stack.db_path, isolation_level=None, timeout=0.0)
        external_writer.execute("BEGIN IMMEDIATE")
        try:
            response = await get_dispatcher().dispatch(
                "rpc-reset-busy",
                "chat.send",
                {
                    "sessionKey": SESSION_KEY,
                    "message": "must not reset or interrupt",
                    "intent": "reset_same_key",
                    "queueMode": "steer",
                    "clientRequestId": "reset-busy",
                },
                stack.context,
            )

            assert response.ok is False
            assert response.error is not None
            assert response.error.code == "STORAGE_BUSY"
            assert response.error.retryable is True
            assert response.error.accepted is False
            await asyncio.sleep(0)

            unchanged = await stack.storage.get_session(SESSION_KEY)
            assert unchanged is not None
            assert unchanged.session_id == old_session_id
            assert unchanged.epoch == old_epoch
            transcript = await stack.storage.get_transcript(old_session_id)
            assert [entry.message_id for entry in transcript] == [old_message_id]
            summaries = await stack.storage.get_all_summaries(old_session_id)
            assert [summary.summary_text for summary in summaries] == ["old summary"]
            context_states = await stack.manager.get_context_states(SESSION_KEY)
            assert len(context_states) == 1
            assert context_states[0].valid is True

            old_runtime_task = stack.runtime._tasks[old_handle.task_id]
            assert old_runtime_task.cancel_requested is False
            assert old_runtime_task.asyncio_task is not None
            assert old_runtime_task.asyncio_task.done() is False
            assert stack.handler_cancelled.is_set() is False
            assert stack.runtime._reservations_by_session == {}
            assert _table_counts(stack.db_path) == {
                "sessions": 1,
                "transcript_entries": 1,
                "session_summaries": 1,
                "session_context_states": 1,
                "agent_tasks": 1,
                "turn_ingress_receipts": 0,
            }
        finally:
            external_writer.execute("ROLLBACK")
            external_writer.close()
