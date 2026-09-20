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
from opensquilla.gateway.model_routing import model_routing_snapshot
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.session_model_routing import (
    capture_accepted_model_routing_config,
)
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import PlanRunRecord, SessionContextState, SessionSummary
from opensquilla.session.plans import new_plan_revision
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
    handler_runs: list[Any]

    async def wait_until_running(self) -> None:
        await asyncio.wait_for(self.handler_started.wait(), timeout=2.0)


@asynccontextmanager
async def _open_intent_stack(db_path: Path) -> AsyncIterator[_IntentStack]:
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    release_handler = asyncio.Event()

    handler_runs: list[Any] = []

    async def _turn_handler(run: Any) -> None:
        handler_runs.append(run)
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
            memory={},
            naming={"enabled": False},
        ),
        session_manager=manager,
        task_runtime=runtime,
    )
    manager._model_routing_mode_provider = lambda: model_routing_snapshot(context.config)["mode"]
    stack = _IntentStack(
        db_path=db_path,
        storage=storage,
        manager=manager,
        runtime=runtime,
        context=context,
        handler_started=handler_started,
        handler_cancelled=handler_cancelled,
        release_handler=release_handler,
        handler_runs=handler_runs,
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
async def test_chat_send_atomically_creates_first_plan_turn(tmp_path: Path) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        params = {
            "sessionKey": SESSION_KEY,
            "message": "inspect the repository and propose a plan",
            "intent": "new_chat",
            "collaborationMode": "plan",
            "clientRequestId": "new-chat-plan-success",
        }

        response = await get_dispatcher().dispatch(
            "rpc-new-chat-plan",
            "chat.send",
            params,
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["collaboration"] == {
            "mode": "plan",
            "revision": 1,
            "appliesTo": "next_turn",
        }
        assert response.payload["acceptedCollaboration"] == {
            "mode": "plan",
            "revision": 1,
        }
        created = await stack.storage.get_session(SESSION_KEY)
        assert created is not None
        assert created.collaboration_mode == "plan"
        assert created.collaboration_revision == 1

        transcript = await stack.storage.get_transcript(created.session_id)
        assert len(transcript) == 1
        assert transcript[0].content == params["message"]
        assert len(stack.handler_runs) == 1
        metadata = stack.handler_runs[0].envelope.metadata
        assert metadata["collaboration_mode"] == "plan"
        assert metadata["collaboration_revision"] == 1
        assert metadata["required_collaboration_mode"] == "plan"
        assert metadata["required_collaboration_revision"] == 1

        replay = await get_dispatcher().dispatch(
            "rpc-new-chat-plan-replay",
            "chat.send",
            params,
            stack.context,
        )
        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert replay.payload["collaboration"] == {
            "mode": "plan",
            "revision": 1,
            "appliesTo": "next_turn",
        }
        assert replay.payload["acceptedCollaboration"] == {
            "mode": "plan",
            "revision": 1,
        }

        await stack.storage.set_collaboration_mode(
            SESSION_KEY,
            "default",
            expected_revision=1,
        )
        replay_after_toggle = await get_dispatcher().dispatch(
            "rpc-new-chat-plan-replay-after-toggle",
            "chat.send",
            params,
            stack.context,
        )
        assert replay_after_toggle.ok is True
        assert replay_after_toggle.payload["replayed"] is True
        assert replay_after_toggle.payload["acceptedCollaboration"] == {
            "mode": "plan",
            "revision": 1,
        }
        assert replay_after_toggle.payload["collaboration"] == {
            "mode": "default",
            "revision": 2,
            "appliesTo": "next_turn",
        }

        fingerprint_conflict = await get_dispatcher().dispatch(
            "rpc-new-chat-plan-fingerprint-conflict",
            "chat.send",
            {
                **params,
                "collaborationMode": "default",
            },
            stack.context,
        )
        assert fingerprint_conflict.ok is False
        assert fingerprint_conflict.error is not None
        assert fingerprint_conflict.error.code == "IDEMPOTENCY_CONFLICT"
        assert _table_counts(stack.db_path) == {
            "sessions": 1,
            "transcript_entries": 1,
            "session_summaries": 0,
            "session_context_states": 0,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_chat_send_atomically_snapshots_initial_routing_mode(tmp_path: Path) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        async def production_provider(*, session_key: str, run_kind: str) -> Any:
            return await capture_accepted_model_routing_config(
                stack.context.config,
                stack.manager,
                session_key=session_key,
                run_kind=run_kind,
            )

        # Match Gateway boot: a provider that resolves durable Session state
        # would fail for this not-yet-committed node unless ingress supplies
        # the prepared-node snapshot to the pre-commit freeze.
        stack.runtime._accepted_config_provider = production_provider
        params = {
            "sessionKey": SESSION_KEY,
            "message": "route this first turn",
            "intent": "new_chat",
            "initialRoutingMode": "router",
            "clientRequestId": "new-chat-routing-success",
        }

        response = await get_dispatcher().dispatch(
            "rpc-new-chat-routing",
            "chat.send",
            params,
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["acceptedRouting"] == {"mode": "router"}
        assert response.payload["routing"] == {
            "mode": "router",
            "revision": 0,
            "source": "session",
            "initialized": False,
            "appliesTo": "next_accepted_turn",
            "modelSelection": None,
        }
        created = await stack.storage.get_session(SESSION_KEY)
        assert created is not None
        assert created.model_routing_mode == "router"
        assert created.model_routing_revision == 0
        tasks = await stack.storage.list_agent_tasks(session_key=SESSION_KEY)
        assert len(tasks) == 1
        assert tasks[0].details is not None
        accepted_audit = tasks[0].details["accepted_model_routing"]
        assert accepted_audit["effective_mode"] == "router"
        assert accepted_audit["session_revision"] == 0
        assert accepted_audit["source"] == "session"
        assert stack.handler_runs[0].accepted_config.session_mode == "router"

        replay = await get_dispatcher().dispatch(
            "rpc-new-chat-routing-replay",
            "chat.send",
            params,
            stack.context,
        )
        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert replay.payload["acceptedRouting"] == {"mode": "router"}

        conflict = await get_dispatcher().dispatch(
            "rpc-new-chat-routing-conflict",
            "chat.send",
            {**params, "initialRoutingMode": "direct"},
            stack.context,
        )
        assert conflict.ok is False
        assert conflict.error is not None
        assert conflict.error.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_chat_send_requires_explicit_new_chat_for_initial_mode(
    tmp_path: Path,
) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        response = await get_dispatcher().dispatch(
            "rpc-implicit-new-chat-initial-plan",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "must state the creation intent",
                "collaborationMode": "plan",
                "clientRequestId": "implicit-new-chat-initial-plan",
            },
            stack.context,
        )

        assert response.ok is False
        assert await stack.storage.get_session(SESSION_KEY) is None
        assert _table_counts(stack.db_path) == {
            "sessions": 0,
            "transcript_entries": 0,
            "session_summaries": 0,
            "session_context_states": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }
        assert stack.handler_started.is_set() is False


@pytest.mark.asyncio
async def test_chat_send_rejects_initial_mode_without_atomic_runtime(
    tmp_path: Path,
) -> None:
    context = RpcContext(
        conn_id="initial-plan-no-runtime",
        principal=_PRINCIPAL,
        config=GatewayConfig(workspace_dir=str(tmp_path / "workspace")),
    )

    response = await get_dispatcher().dispatch(
        "rpc-initial-plan-no-runtime",
        "chat.send",
        {
            "sessionKey": SESSION_KEY,
            "message": "must not report a false Plan acceptance",
            "intent": "new_chat",
            "collaborationMode": "plan",
            "clientRequestId": "initial-plan-no-runtime",
        },
        context,
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_chat_send_rejects_non_string_initial_mode(tmp_path: Path) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        response = await get_dispatcher().dispatch(
            "rpc-invalid-initial-plan-mode",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "invalid mode payload",
                "intent": "new_chat",
                "collaborationMode": {"mode": "plan"},
                "clientRequestId": "invalid-initial-plan-mode",
            },
            stack.context,
        )

        assert response.ok is False
        assert await stack.storage.get_session(SESSION_KEY) is None
        assert stack.handler_started.is_set() is False


@pytest.mark.asyncio
async def test_chat_send_rejects_initial_mode_for_existing_session(tmp_path: Path) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        existing = await stack.manager.create(SESSION_KEY, agent_id="main")

        response = await get_dispatcher().dispatch(
            "rpc-existing-chat-initial-plan",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "must not change this session",
                "collaborationMode": "plan",
                "clientRequestId": "existing-chat-initial-plan",
            },
            stack.context,
        )

        assert response.ok is False
        unchanged = await stack.storage.get_session(SESSION_KEY)
        assert unchanged is not None
        assert unchanged.session_id == existing.session_id
        assert unchanged.collaboration_mode == "default"
        assert unchanged.collaboration_revision == 0
        assert _table_counts(stack.db_path) == {
            "sessions": 1,
            "transcript_entries": 0,
            "session_summaries": 0,
            "session_context_states": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }
        assert stack.handler_started.is_set() is False


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
                    "collaborationMode": "plan",
                    "clientRequestId": "new-chat-busy",
                    "initialModel": "synthetic-model",
                    "initialProvider": "openai",
                    "initialRoutingMode": "direct",
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
        revision = await stack.storage.create_plan_revision(
            new_plan_revision(
                source_session_key=SESSION_KEY,
                source_session_id=old_session_id,
                source_epoch=old_epoch,
                title="Plan from the old epoch",
                markdown="## Plan from the old epoch",
                steps=[{"step_id": "old", "title": "Old step"}],
            ),
            expected_parent_revision_id=None,
        )
        before_reset = await stack.storage.get_session(SESSION_KEY)
        assert before_reset is not None
        await stack.storage.set_collaboration_mode(
            SESSION_KEY,
            "plan",
            expected_revision=int(before_reset.collaboration_revision or 0),
        )
        old_run = await stack.storage.start_plan_run(
            PlanRunRecord(
                run_id="atomic-reset-old-plan-run",
                session_key=SESSION_KEY,
                session_id=old_session_id,
                session_epoch=old_epoch,
                plan_revision_id=revision.revision_id,
                driver_kind="manual",
                status="queued",
            )
        )

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
        assert reset.collaboration_mode == "default"
        assert reset.collaboration_revision == 0
        assert reset.active_plan_revision_id is None
        superseded = await stack.storage.get_plan_run(old_run.run_id)
        assert superseded is not None
        assert superseded.status == "superseded"
        assert superseded.terminal_reason == "session_reset"

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
async def test_reset_archive_failure_rolls_back_atomic_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        old_session_id, old_epoch, _old_message_id = await _seed_reset_state(stack)

        async def fail_archive(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("archive unavailable")

        monkeypatch.setattr(stack.manager, "write_session_archive", fail_archive)
        response = await get_dispatcher().dispatch(
            "rpc-reset-archive-failure",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "must not replace old history",
                "intent": "reset_same_key",
                "clientRequestId": "reset-archive-failure",
            },
            stack.context,
        )

        assert response.ok is False
        persisted = await stack.storage.get_session(SESSION_KEY)
        assert persisted is not None
        assert persisted.session_id == old_session_id
        assert persisted.epoch == old_epoch
        transcript = await stack.storage.get_transcript(old_session_id)
        assert [entry.content for entry in transcript] == ["old transcript"]
        summaries = await stack.storage.get_all_summaries(old_session_id)
        assert [summary.summary_text for summary in summaries] == ["old summary"]
        context_states = await stack.manager.get_context_states(SESSION_KEY)
        assert len(context_states) == 1
        assert context_states[0].valid is True
        assert _table_counts(stack.db_path) == {
            "sessions": 1,
            "transcript_entries": 1,
            "session_summaries": 1,
            "session_context_states": 1,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
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


@pytest.mark.parametrize("provider", [None, "openai", "anthropic", "deepseek"])
async def test_chat_send_atomically_pins_first_model_and_replays_without_duplicate(
    tmp_path: Path, provider: str | None,
) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        gateway_defaults = stack.context.config.model_dump()
        params = {
            "sessionKey": SESSION_KEY, "message": "first pinned turn", "intent": "new_chat",
            "clientRequestId": "pin-request", "initialModel": "synthetic-model",
            "initialRoutingMode": "direct",
        }
        if provider:
            params["initialProvider"] = provider
        response = await get_dispatcher().dispatch("pin", "chat.send", params, stack.context)
        assert response.ok is True, response.error
        await stack.wait_until_running()
        assert response.payload["acceptedModel"] == {
            "model": "synthetic-model", "provider": provider,
        }
        node = await stack.storage.get_session(SESSION_KEY)
        assert node is not None
        assert node.model == "synthetic-model"
        assert node.provider_override == provider
        assert node.model_routing_mode == "direct"
        assert node.model_override is None
        assert node.model_provider is None
        assert stack.context.config.model_dump() == gateway_defaults
        counts = _table_counts(stack.db_path)
        assert counts["sessions"] == counts["transcript_entries"] == counts["agent_tasks"] == 1
        assert counts["turn_ingress_receipts"] == 1
        # A receipt describes accepted input even after deployment defaults change.
        stack.context.config.squilla_router.enabled = True
        replay = await get_dispatcher().dispatch("replay", "chat.send", params, stack.context)
        assert replay.ok is True, replay.error
        assert replay.payload["replayed"] is True
        assert replay.payload["acceptedModel"] == response.payload["acceptedModel"]
        assert _table_counts(stack.db_path) == counts
        assert len(stack.handler_runs) == 1
        for changed in ({"initialModel": "other-model"}, {"initialProvider": "ollama"}):
            conflict = await get_dispatcher().dispatch(
                "conflict", "chat.send", {**params, **changed}, stack.context,
            )
            assert conflict.ok is False
            assert conflict.error.code == "IDEMPOTENCY_CONFLICT"
        assert _table_counts(stack.db_path) == counts


@pytest.mark.parametrize("fields", [
    {}, {"intent": "continue"}, {"intent": "reset"}, {"intent": "fork"},
    {"intent": "new_chat", "initialRoutingMode": "router"},
    {"intent": "new_chat", "initialRoutingMode": "ensemble"},
    {"intent": "new_chat", "initialProvider": "not-a-provider"},
    {"intent": "new_chat", "forkBeforeMessageId": "prior-message"},
])
async def test_chat_send_rejects_invalid_model_pin_without_orphans(tmp_path: Path, fields) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        before = _table_counts(stack.db_path)
        response = await get_dispatcher().dispatch("reject-pin", "chat.send", {
            "sessionKey": SESSION_KEY, "message": "must not create", "initialModel": "model",
            "clientRequestId": "reject-pin", **fields,
        }, stack.context)
        assert response.ok is False
        assert _table_counts(stack.db_path) == before
        assert stack.handler_runs == []


@pytest.mark.parametrize("mode", ["router", "ensemble"])
async def test_chat_send_pin_does_not_silently_override_gateway_routing(
    tmp_path: Path, mode,
) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        stack.context.config.squilla_router.enabled = mode == "router"
        stack.context.config.llm_ensemble.enabled = mode == "ensemble"
        response = await get_dispatcher().dispatch("reject-pin", "chat.send", {
            "sessionKey": SESSION_KEY, "message": "must not create", "intent": "new_chat",
            "initialModel": "model", "clientRequestId": "reject-pin",
        }, stack.context)
        assert response.ok is False
        assert all(count == 0 for count in _table_counts(stack.db_path).values())
        assert model_routing_snapshot(stack.context.config)["mode"] == mode


async def test_chat_send_model_pin_requires_atomic_runtime_and_fresh_session(
    tmp_path: Path,
) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        params = {"sessionKey": SESSION_KEY, "message": "hello", "intent": "new_chat",
                  "initialModel": "new-model", "clientRequestId": "pin-request"}
        stack.context.task_runtime = None
        unavailable = await get_dispatcher().dispatch(
            "no-runtime", "chat.send", params, stack.context,
        )
        assert unavailable.ok is False
        assert all(count == 0 for count in _table_counts(stack.db_path).values())
        stack.context.task_runtime = stack.runtime
        await stack.manager.create(SESSION_KEY, agent_id="main", model="old-model")
        before = _table_counts(stack.db_path)
        existing = await get_dispatcher().dispatch("existing", "chat.send", params, stack.context)
        assert existing.ok is False
        assert _table_counts(stack.db_path) == before
        assert (await stack.storage.get_session(SESSION_KEY)).model == "old-model"


async def test_chat_send_without_pin_preserves_default_model_and_routing(tmp_path: Path) -> None:
    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        stack.context.config.squilla_router.enabled = True
        response = await get_dispatcher().dispatch("default", "chat.send", {
            "sessionKey": SESSION_KEY, "message": "hello", "intent": "new_chat",
            "clientRequestId": "default-request",
        }, stack.context)
        assert response.ok is True, response.error
        await stack.wait_until_running()
        assert "acceptedModel" not in response.payload
        node = await stack.storage.get_session(SESSION_KEY)
        assert node.model is None
        assert node.provider_override is None
        assert node.model_routing_mode == "router"


@pytest.mark.parametrize("provider", ["openai", "anthropic", "deepseek"])
async def test_accepted_model_pin_survives_restart_and_resolves_its_exact_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str,
) -> None:
    from types import SimpleNamespace

    from opensquilla.engine.turn_runner.provider_and_tools_stage import ProviderAndToolsStageInput
    from opensquilla.gateway.boot import build_turn_runner_from_services
    from opensquilla.gateway.config import LlmProviderProfile
    from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig

    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        response = await get_dispatcher().dispatch("pin", "chat.send", {
            "sessionKey": SESSION_KEY, "message": "same name across providers",
            "intent": "new_chat", "clientRequestId": "pin-request",
            "initialModel": "same-model-name", "initialProvider": provider,
            "initialRoutingMode": "direct",
        }, stack.context)
        assert response.ok is True, response.error
        await stack.wait_until_running()
        # A new storage handle/manager has no admission-time cache or UI state.
        reopened = await SessionStorage.open(str(stack.db_path))
        try:
            manager = SessionManager(reopened, inject_time_prefix=False)
            config = GatewayConfig()
            config.llm_profiles[provider] = LlmProviderProfile(
                api_key="synthetic-test-profile-key", base_url=f"https://{provider}.example/v1",
            )
            base = ProviderConfig(
                provider="ollama", model="gateway-default", base_url="http://127.0.0.1:11434",
            )
            selector = ModelSelector(SelectorConfig(primary=base))
            built = []

            def build(selected):
                built.append((selected.provider, selected.model, selected.base_url))
                return SimpleNamespace(name=selected.provider, model=selected.model)

            monkeypatch.setattr("opensquilla.provider.selector._build_provider", build)
            runner = build_turn_runner_from_services(SimpleNamespace(
                config=config, provider_selector=selector, session_manager=manager,
                tool_registry=None, skill_loader=None, usage_tracker=None,
            ))
            outcome = await runner._provider_and_tools_stage.run(ProviderAndToolsStageInput(
                session_key=SESSION_KEY, agent_id="main", tool_context=None,
                run_kind="default", input_mode="user",
            ))
            output = outcome.require_output()
            assert output.provider.name == provider
            assert output.provider.model == "same-model-name"
            assert built == [(provider, "same-model-name", f"https://{provider}.example/v1")]
            assert output.cloned_selector.has_fallback() is False
            # The same exact physical deployment feeds native image validation.
            from opensquilla.engine.runtime import _SelectorFallbackProvider
            from opensquilla.engine.turn_runner.harness import _TurnRunnerModelCatalogAdapter

            vision_calls = []

            class Catalog:
                def resolve_deployment_limits(self, _model, **_kwargs):
                    return SimpleNamespace(
                        context_window=100_000, max_output_tokens=4096,
                        max_output_tokens_known=True,
                    )

                def resolve_deployment_capabilities(self, _model, **_kwargs):
                    return None

                def resolve_deployment_vision_support(self, model, **kwargs):
                    vision_calls.append((model, kwargs["provider"], kwargs["base_url"]))
                    return "supported" if kwargs["provider"] == provider else "unsupported"

            runner._model_catalog = Catalog()
            wrapper = _SelectorFallbackProvider(output.provider, output.cloned_selector)
            # AgentBootstrapStage uses this physical-config API before native image dispatch.
            catalog = _TurnRunnerModelCatalogAdapter(runner).lookup_deployment(
                wrapper.active_deployment_config(),
            )
            assert catalog.vision_support == "supported"
            assert vision_calls == [
                ("same-model-name", provider, f"https://{provider}.example/v1"),
            ]
            assert selector.current_config.provider == "ollama"
            assert selector.current_config.model == "gateway-default"
        finally:
            await reopened.close()


@pytest.mark.parametrize("failure", ["unresolved", "build_failed"])
async def test_pinned_provider_failure_never_runs_gateway_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from types import SimpleNamespace

    from opensquilla.engine.turn_runner.provider_and_tools_stage import ProviderAndToolsStageInput
    from opensquilla.gateway.boot import build_turn_runner_from_services
    from opensquilla.gateway.config import LlmProviderProfile
    from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig

    async with _open_intent_stack(tmp_path / "sessions.db") as stack:
        await stack.manager.create(
            SESSION_KEY, agent_id="main", model="same-model", provider_override="anthropic",
        )
        config = GatewayConfig()
        monkeypatch.setattr("opensquilla.provider.deployment.environment_value", lambda _name: "")
        if failure == "build_failed":
            config.llm_profiles["anthropic"] = LlmProviderProfile(api_key="synthetic-test-key")
        base = ProviderConfig(provider="ollama", model="default", base_url="http://127.0.0.1:11434")
        built = []

        def fail_build(selected):
            built.append(selected.provider)
            raise RuntimeError("synthetic provider build failure")

        monkeypatch.setattr("opensquilla.provider.selector._build_provider", fail_build)
        runner = build_turn_runner_from_services(SimpleNamespace(
            config=config, provider_selector=ModelSelector(SelectorConfig(primary=base)),
            session_manager=stack.manager, tool_registry=None,
            skill_loader=None, usage_tracker=None,
        ))
        with pytest.raises((ValueError, RuntimeError)):
            await runner._provider_and_tools_stage.run(ProviderAndToolsStageInput(
                session_key=SESSION_KEY, agent_id="main", tool_context=None,
                run_kind="default", input_mode="user",
            ))
        assert built == (["anthropic"] if failure == "build_failed" else [])
