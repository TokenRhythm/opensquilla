"""Durable channel-turn acceptance contracts backed by the real runtime stack."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import opensquilla.gateway.channel_dispatch as channel_dispatch_module
from opensquilla.channels.types import IncomingMessage, OutgoingMessage
from opensquilla.gateway._debounce import _DefaultDebounceCoordinator
from opensquilla.gateway.attachment_ingest import AttachmentIngestResult
from opensquilla.gateway.channel_dispatch import (
    _accept_channel_runtime_turn,
    _channel_ingress_identity,
    _channel_native_request_id,
    _deliver_runtime_channel_reply,
    _RuntimeChannelStreamRelay,
    run_channel_dispatch,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.goal_service import GoalService
from opensquilla.gateway.routing import RouteEnvelope, build_channel_route_envelope
from opensquilla.gateway.session_model_routing import (
    capture_accepted_model_routing_config,
)
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.gateway.websocket import get_registry
from opensquilla.project_workspaces import project_path_key
from opensquilla.session.goals import (
    GoalCommandRequest,
    GoalTurnContext,
    StartGoalMutation,
    new_goal,
)
from opensquilla.session.manager import SessionIntent, SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    TranscriptEntry,
)
from opensquilla.session.storage import (
    SessionStorage,
    StaleEpochError,
    StorageBusyError,
    TurnIngressConflictError,
)

SESSION_KEY = "agent:main:slack:channel-atomic-ingress"
CHANNEL_ID = "channel-123"
ACCOUNT_ID = "account-456"
NATIVE_MESSAGE_ID = "native-message-789"


class _FinalOnlyChannel:
    channel_id = "slack"
    STREAM_UPDATE_STRATEGY = "final_only"


class _RecordingFinalOnlyChannel(_FinalOnlyChannel):
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)


@dataclass
class _ChannelIngressStack:
    db_path: Path
    storage: SessionStorage
    manager: SessionManager
    runtime: TaskRuntime
    handler_started: asyncio.Event
    release_handler: asyncio.Event
    received_runs: list[Any] = field(default_factory=list)

    async def wait_until_running(self) -> None:
        await asyncio.wait_for(self.handler_started.wait(), timeout=2.0)


@asynccontextmanager
async def _open_stack(db_path: Path) -> AsyncIterator[_ChannelIngressStack]:
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    received_runs: list[Any] = []

    async def _turn_handler(run: Any) -> None:
        received_runs.append(run)
        handler_started.set()
        await release_handler.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_turn_handler,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
    )
    stack = _ChannelIngressStack(
        db_path=db_path,
        storage=storage,
        manager=manager,
        runtime=runtime,
        handler_started=handler_started,
        release_handler=release_handler,
        received_runs=received_runs,
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


def _message(content: str, *, native_message_id: str = NATIVE_MESSAGE_ID) -> IncomingMessage:
    return IncomingMessage(
        sender_id="sender-1",
        channel_id=CHANNEL_ID,
        content=content,
        metadata={
            "native_message_id": native_message_id,
            "account_id": ACCOUNT_ID,
            "thread_id": "thread-10",
            "is_group": True,
        },
    )


def _route(msg: IncomingMessage) -> RouteEnvelope:
    return build_channel_route_envelope(
        msg,
        session_key=SESSION_KEY,
        session_prefix="slack",
        agent_id="main",
        channel_type="slack",
    )


async def _accept(
    stack: _ChannelIngressStack,
    content: str,
    *,
    native_message_id: str = NATIVE_MESSAGE_ID,
    channel: Any | None = None,
    config: Any = None,
) -> tuple[Any | None, str, Any | None, bool]:
    msg = _message(content, native_message_id=native_message_id)
    return await _accept_channel_runtime_turn(
        channel=channel or _FinalOnlyChannel(),
        msg=msg,
        session_manager=stack.manager,
        session_key=SESSION_KEY,
        route_envelope=_route(msg),
        task_runtime=stack.runtime,
        ingested=AttachmentIngestResult(text=content),
        raw_content=content,
        config=config,
    )


async def _bind_project_session(
    stack: _ChannelIngressStack,
    project_path: Path,
) -> Any:
    project_path.mkdir()
    project = await stack.storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name=project_path.name,
        trusted_at=1,
    )
    await stack.manager.create(
        SESSION_KEY,
        agent_id="main",
        workspace_id=project.workspace_id,
    )
    return project


def _table_counts(db_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "sessions",
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


async def _seed_idle_active_goal(stack: _ChannelIngressStack) -> Any:
    """Create one settled Goal without installing process-local execution authority."""

    session = await stack.manager.create(SESSION_KEY, agent_id="main")
    task_id = "channel-goal-bootstrap-task"
    message_id = "channel-goal-bootstrap-message"
    objective = "Finish the channel-owned follow-up contract."
    command = GoalCommandRequest(
        source_scope="web:test-owner",
        request_session_key=SESSION_KEY,
        client_request_id=str(uuid.uuid4()),
        action="set",
        request_fingerprint=hashlib.sha256(b"channel-goal-bootstrap").hexdigest(),
    )
    goal = new_goal(
        goal_id="channel-goal-id",
        session_key=SESSION_KEY,
        session_id=session.session_id,
        session_epoch=session.epoch,
        objective=objective,
        task_id=task_id,
        created_at_ms=100,
    )
    accepted = await stack.storage.accept_turn(
        TranscriptEntry(
            session_id=session.session_id,
            session_key=SESSION_KEY,
            message_id=message_id,
            role="user",
            content=objective,
            created_at=100,
        ),
        expected_epoch=session.epoch,
        updated_at=100,
        task_record=AgentTaskRecord(
            task_id=task_id,
            session_key=SESSION_KEY,
            agent_id="main",
            source_kind="webui",
            queue_mode="followup",
            run_kind="session_turn",
            status=AgentTaskStatus.QUEUED,
            created_at=100,
            updated_at=100,
        ),
        source_scope=command.source_scope,
        request_session_key=SESSION_KEY,
        client_request_id=command.client_request_id,
        request_fingerprint=command.request_fingerprint,
        goal_mutation=StartGoalMutation(goal=goal, command=command),
    )
    assert accepted.goal_context is not None
    await stack.storage.update_agent_task(
        task_id,
        status=AgentTaskStatus.SUCCEEDED,
        started_at=110,
        finished_at=120,
        terminal_reason="completed",
    )
    settled = await stack.storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
    )
    assert settled is not None
    assert settled.status == "active"
    assert settled.active_task_id is None
    return settled


def _install_channel_goal_service(stack: _ChannelIngressStack) -> GoalService:
    async def _discard_event(
        _session_key: str,
        _event_name: str,
        _payload: dict[str, Any],
    ) -> None:
        return None

    service = GoalService(
        storage=stack.storage,
        session_manager=stack.manager,
        task_runtime=stack.runtime,
        event_emitter=_discard_event,
        subscription_manager=SimpleNamespace(),
        config=SimpleNamespace(
            execution_enabled=True,
            max_turns=50,
            runtime_budget_seconds=3_600,
        ),
    )
    stack.runtime.set_goal_service(service)
    stack.runtime.set_activation_listener(service.on_task_activation)
    stack.runtime.set_lifecycle_listener(service.on_task_lifecycle)
    return service


@pytest.mark.asyncio
async def test_channel_turn_atomically_creates_delivery_session_message_task_and_receipt(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        config = GatewayConfig()

        async def production_provider(*, session_key: str, run_kind: str) -> Any:
            return await capture_accepted_model_routing_config(
                config,
                stack.manager,
                session_key=session_key,
                run_kind=run_kind,
            )

        stack.runtime._accepted_config_provider = production_provider
        handle, persisted_text, stream_relay, replayed = await _accept(
            stack,
            "one durable channel turn",
            config=config,
        )
        await stack.wait_until_running()

        assert handle is not None
        assert persisted_text == "one durable channel turn"
        assert stream_relay is None
        assert replayed is False

        session = await stack.storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.last_channel == "slack"
        assert session.last_to == CHANNEL_ID
        assert session.last_account_id == ACCOUNT_ID
        assert session.last_thread_id == "thread-10"
        assert session.delivery_context is not None
        assert session.delivery_context["sender_id"] == "sender-1"
        assert session.delivery_context["channel_id"] == CHANNEL_ID

        transcript = await stack.manager.get_transcript(SESSION_KEY)
        assert len(transcript) == 1
        message = transcript[0]
        assert message.role == "user"
        assert message.content == "one durable channel turn"

        task = await stack.storage.get_agent_task(handle.task_id)
        assert task is not None
        assert task.session_key == SESSION_KEY
        assert task.details["persisted_user_message_id"] == message.message_id
        assert task.details["fresh_user_session"] is True
        assert task.details["accepted_model_routing"]["effective_mode"] == "direct"
        assert task.details["accepted_model_routing"]["source"] == "session"

        receipt_result = await stack.storage.get_turn_ingress_receipt(
            source_scope=f"channel:slack:{ACCOUNT_ID}",
            request_session_key=SESSION_KEY,
            client_request_id=f"native_message_id:{NATIVE_MESSAGE_ID}",
        )
        assert receipt_result is not None
        receipt = receipt_result.receipt
        assert receipt.accepted_session_key == SESSION_KEY
        assert receipt.session_id == session.session_id
        assert receipt.message_id == message.message_id
        assert receipt.task_id == task.task_id

        assert len(stack.received_runs) == 1
        assert stack.received_runs[0].persisted_user_message_id == message.message_id
        assert stack.received_runs[0].fresh_user_session is True
        assert stack.received_runs[0].accepted_config.session_mode == "direct"
        assert _table_counts(stack.db_path) == {
            # The accepted channel session plus the post-acceptance main-delivery fallback.
            "sessions": 2,
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_channel_reserve_waits_inside_atomic_session_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        reserve_called = asyncio.Event()
        admission_attempted = asyncio.Event()
        original_reserve = stack.runtime.reserve
        original_admission = stack.runtime.collect_admission

        async def observed_reserve(*args: Any, **kwargs: Any) -> Any:
            reserve_called.set()
            return await original_reserve(*args, **kwargs)

        @asynccontextmanager
        async def observed_admission(session_key: str) -> AsyncIterator[None]:
            admission_attempted.set()
            async with original_admission(session_key):
                yield

        monkeypatch.setattr(stack.runtime, "reserve", observed_reserve)
        monkeypatch.setattr(
            stack.runtime,
            "collect_admission",
            observed_admission,
        )
        async with original_admission(SESSION_KEY):
            accepting = asyncio.create_task(
                _accept(stack, "reserve must remain behind admission")
            )
            await asyncio.wait_for(admission_attempted.wait(), timeout=1.0)

            assert accepting.done() is False
            assert reserve_called.is_set() is False
            assert stack.runtime._reservations_by_session == {}
            assert await stack.storage.get_session(SESSION_KEY) is None

        handle, _text, _relay, replayed = await asyncio.wait_for(
            accepting,
            timeout=2.0,
        )
        assert handle is not None
        assert replayed is False


@pytest.mark.asyncio
async def test_channel_running_receipt_redelivery_attaches_reply_waiter_without_rerun(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        first_handle, _, _, first_replayed = await _accept(stack, "deliver exactly once")
        await stack.wait_until_running()
        channel = _RecordingFinalOnlyChannel()
        replay_handle, replay_text, replay_relay, replayed = await _accept(
            stack,
            "deliver exactly once",
            channel=channel,
        )

        assert first_handle is not None
        assert first_replayed is False
        assert replay_handle is not None
        assert replay_handle.task_id == first_handle.task_id
        assert replay_handle.status == AgentTaskStatus.RUNNING
        assert replay_text == "deliver exactly once"
        assert replay_relay is None
        assert replayed is True
        assert len(stack.received_runs) == 1

        assistant = await stack.manager.append_message(
            SESSION_KEY,
            role="assistant",
            content="Reply recovered by the redelivery waiter.",
        )
        sink = stack.received_runs[0].assistant_message_sink
        assert sink is not None
        sink(assistant.message_id, assistant.content)
        stack.release_handler.set()
        await _deliver_runtime_channel_reply(
            channel=channel,
            task_runtime=stack.runtime,
            session_manager=stack.manager,
            session_key=SESSION_KEY,
            task_id=replay_handle.task_id,
            route_envelope=_route(_message("deliver exactly once")),
            inbound=_message("deliver exactly once"),
            transcript_watermark=1,
            replayed=True,
        )
        assert [message.content for message in channel.sent] == [
            "Reply recovered by the redelivery waiter."
        ]
        assert [entry.content for entry in await stack.manager.get_transcript(SESSION_KEY)] == [
            "deliver exactly once",
            "Reply recovered by the redelivery waiter.",
        ]
        assert _table_counts(stack.db_path) == {
            "sessions": 2,
            "transcript_entries": 2,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }
        assert stack.runtime._reservations_by_session == {}


@pytest.mark.asyncio
async def test_channel_post_accept_notification_failure_keeps_turn_accepted(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        def _fail_notification(_entry: Any) -> None:
            raise RuntimeError("synthetic channel notification failure")

        stack.manager.notify_message_appended = _fail_notification  # type: ignore[method-assign]
        handle, _, _, replayed = await _accept(stack, "accepted before observer failure")
        await stack.wait_until_running()

        assert handle is not None
        assert handle.status == AgentTaskStatus.QUEUED
        assert replayed is False
        assert _table_counts(stack.db_path)["turn_ingress_receipts"] == 1


@pytest.mark.asyncio
async def test_channel_activation_failure_returns_failed_accepted_handle(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        async def _fail_activation(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("synthetic channel activation failure")

        stack.runtime.activate = _fail_activation  # type: ignore[method-assign]
        handle, _, _, replayed = await _accept(stack, "accepted before activation failure")

        assert handle is not None
        assert handle.status == AgentTaskStatus.FAILED
        assert replayed is False
        task = await stack.storage.get_agent_task(handle.task_id)
        assert task is not None
        assert task.status == AgentTaskStatus.FAILED
        assert task.terminal_reason == "activation_failed"
        assert stack.runtime._reservations_by_session == {}


@pytest.mark.asyncio
async def test_channel_terminal_receipt_replay_returns_handle_for_reply_delivery(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        async def _fail_activation(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("synthetic channel activation failure")

        stack.runtime.activate = _fail_activation  # type: ignore[method-assign]
        first_handle, _, _, first_replayed = await _accept(
            stack,
            "redeliver accepted terminal failure",
        )
        replay_handle, replay_text, replay_relay, replayed = await _accept(
            stack,
            "redeliver accepted terminal failure",
        )

        assert first_handle is not None
        assert first_handle.status == AgentTaskStatus.FAILED
        assert first_replayed is False
        assert replay_handle is not None
        assert replay_handle.task_id == first_handle.task_id
        assert replay_handle.status == AgentTaskStatus.FAILED
        assert replay_text == "redeliver accepted terminal failure"
        assert replay_relay is None
        assert replayed is True
        assert stack.received_runs == []
        assert _table_counts(stack.db_path) == {
            "sessions": 2,
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_channel_restart_replays_abandoned_acceptance_to_terminal_delivery(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    accepted_task_id = ""

    async with _open_stack(db_path) as first_stack:
        async def _simulate_exit_after_commit(*_args: Any, **_kwargs: Any) -> None:
            raise asyncio.CancelledError

        first_stack.runtime.activate = _simulate_exit_after_commit  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await _accept(first_stack, "recover after committed channel acceptance")

        receipt = await first_stack.storage.get_turn_ingress_receipt(
            source_scope=f"channel:slack:{ACCOUNT_ID}",
            request_session_key=SESSION_KEY,
            client_request_id=f"native_message_id:{NATIVE_MESSAGE_ID}",
        )
        assert receipt is not None
        assert receipt.receipt.task_id is not None
        accepted_task_id = receipt.receipt.task_id
        accepted_task = await first_stack.storage.get_agent_task(accepted_task_id)
        assert accepted_task is not None
        assert accepted_task.status == AgentTaskStatus.QUEUED

    async with _open_stack(db_path) as restarted_stack:
        recovered_task = await restarted_stack.storage.get_agent_task(accepted_task_id)
        assert recovered_task is not None
        assert recovered_task.status == AgentTaskStatus.ABANDONED
        assert recovered_task.terminal_reason == "process_restart"

        channel = _RecordingFinalOnlyChannel()
        inbound = _message("recover after committed channel acceptance")
        handle, _, stream_relay, replayed = await _accept(
            restarted_stack,
            "recover after committed channel acceptance",
            channel=channel,
        )

        assert handle is not None
        assert handle.task_id == accepted_task_id
        assert handle.status == AgentTaskStatus.ABANDONED
        assert stream_relay is None
        assert replayed is True
        assert restarted_stack.received_runs == []

        await _deliver_runtime_channel_reply(
            channel=channel,
            task_runtime=restarted_stack.runtime,
            session_manager=restarted_stack.manager,
            session_key=SESSION_KEY,
            task_id=handle.task_id,
            route_envelope=_route(inbound),
            inbound=inbound,
            transcript_watermark=0,
        )

        assert [message.content for message in channel.sent] == [
            "The task stopped before it could finish."
        ]


@pytest.mark.asyncio
async def test_channel_succeeded_receipt_replays_exact_persisted_assistant_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        first_handle, _, _, first_replayed = await _accept(
            stack,
            "complete before external channel delivery",
        )
        assert first_handle is not None
        assert first_replayed is False
        await stack.wait_until_running()

        assistant = await stack.manager.append_message(
            SESSION_KEY,
            role="assistant",
            content="The durable channel answer.",
        )
        sink = stack.received_runs[0].assistant_message_sink
        assert sink is not None
        sink(assistant.message_id, assistant.content)

        # A direct cron writer can append outside TaskRuntime's execution lock.
        # It must not replace the exact result captured by the turn finalizer.
        await stack.manager.append_message(
            SESSION_KEY,
            role="assistant",
            content="A newer unrelated cron assistant row.",
            provenance={"kind": "cron"},
        )
        stack.release_handler.set()
        terminal = await stack.runtime.wait(first_handle.task_id, timeout=2.0)
        assert terminal.status == AgentTaskStatus.SUCCEEDED
        assert terminal.details is not None
        assert terminal.details["terminal_assistant_message_id"] == assistant.message_id
        assert (
            terminal.details["terminal_assistant_message_content"]
            == "The durable channel answer."
        )

        live_channel = _RecordingFinalOnlyChannel()
        inbound = _message("complete before external channel delivery")
        await _deliver_runtime_channel_reply(
            channel=live_channel,
            task_runtime=stack.runtime,
            session_manager=stack.manager,
            session_key=SESSION_KEY,
            task_id=first_handle.task_id,
            route_envelope=_route(inbound),
            inbound=inbound,
            transcript_watermark=1,
        )
        assert [message.content for message in live_channel.sent] == [
            "The durable channel answer."
        ]

        # The task's compact outbox payload also survives a same-key reset,
        # even though the original transcript identity is archived and removed.
        monkeypatch.setenv(
            "OPENSQUILLA_SESSION_ARCHIVE_DIR",
            str(tmp_path / "archives"),
        )
        await stack.manager.apply_intent(SESSION_KEY, SessionIntent.RESET_SAME_KEY)

        channel = _RecordingFinalOnlyChannel()
        replay_handle, _, replay_relay, replayed = await _accept(
            stack,
            "complete before external channel delivery",
            channel=channel,
        )
        assert replay_handle is not None
        assert replay_handle.task_id == first_handle.task_id
        assert replay_handle.status == AgentTaskStatus.SUCCEEDED
        assert replay_relay is None
        assert replayed is True

        await _deliver_runtime_channel_reply(
            channel=channel,
            task_runtime=stack.runtime,
            session_manager=stack.manager,
            session_key=SESSION_KEY,
            task_id=replay_handle.task_id,
            route_envelope=_route(inbound),
            inbound=inbound,
            transcript_watermark=3,
            replayed=True,
        )

        assert [message.content for message in channel.sent] == [
            "The durable channel answer."
        ]


@pytest.mark.asyncio
async def test_channel_empty_success_replay_does_not_reuse_prior_assistant_reply(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        await stack.manager.create(SESSION_KEY, agent_id="main")
        await stack.manager.append_message(
            SESSION_KEY,
            role="assistant",
            content="An answer from an older task.",
        )

        first_handle, _, _, _ = await _accept(stack, "complete with no new output")
        assert first_handle is not None
        await stack.wait_until_running()
        await stack.manager.append_message(
            SESSION_KEY,
            role="assistant",
            content="An unrelated cron answer during the empty turn.",
            provenance={"kind": "cron"},
        )
        stack.release_handler.set()
        terminal = await stack.runtime.wait(first_handle.task_id, timeout=2.0)
        assert terminal.status == AgentTaskStatus.SUCCEEDED
        assert terminal.details is not None
        assert "terminal_assistant_message_id" not in terminal.details
        assert "terminal_assistant_message_content" not in terminal.details

        channel = _RecordingFinalOnlyChannel()
        inbound = _message("complete with no new output")
        replay_handle, _, _, replayed = await _accept(
            stack,
            "complete with no new output",
            channel=channel,
        )
        assert replay_handle is not None
        assert replayed is True

        await _deliver_runtime_channel_reply(
            channel=channel,
            task_runtime=stack.runtime,
            session_manager=stack.manager,
            session_key=SESSION_KEY,
            task_id=replay_handle.task_id,
            route_envelope=_route(inbound),
            inbound=inbound,
            transcript_watermark=2,
            replayed=True,
        )

        assert [message.content for message in channel.sent] == [
            "The task completed, but its original channel reply could not be recovered."
        ]


@pytest.mark.asyncio
async def test_channel_stale_epoch_aborts_reservation_without_accepting_turn(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        async def _raise_stale_epoch(*_args: Any, **_kwargs: Any) -> None:
            raise StaleEpochError("synthetic concurrent reset")

        stack.storage.accept_turn = _raise_stale_epoch  # type: ignore[method-assign]

        with pytest.raises(StaleEpochError, match="concurrent reset"):
            await _accept(stack, "retry after session rotation")

        _assert_no_runtime_acceptance_state(stack.runtime)
        assert _table_counts(stack.db_path) == {
            "sessions": 0,
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }


class _RelayProbe:
    def __init__(self) -> None:
        self.start_count = 0

    async def emit(self, _event: Any) -> None:
        return None

    def start(self) -> None:
        self.start_count += 1


@pytest.mark.asyncio
async def test_channel_turn_busy_failure_leaves_no_durable_or_runtime_acceptance_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        relay = _RelayProbe()
        monkeypatch.setattr(
            _RuntimeChannelStreamRelay,
            "maybe_create",
            classmethod(lambda cls, *args, **kwargs: relay),
        )
        writer = sqlite3.connect(stack.db_path, isolation_level=None)
        writer.execute("PRAGMA busy_timeout = 0")
        writer.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(StorageBusyError):
                await _accept(stack, "must remain unaccepted")
        finally:
            writer.execute("ROLLBACK")
            writer.close()

        assert relay.start_count == 0
        assert stack.handler_started.is_set() is False
        assert stack.received_runs == []
        _assert_no_runtime_acceptance_state(stack.runtime)
        assert _table_counts(stack.db_path) == {
            "sessions": 0,
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }


@pytest.mark.asyncio
async def test_channel_turn_rejects_native_message_id_reuse_with_different_content(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        first_handle, _, _, _ = await _accept(stack, "original channel payload")
        await stack.wait_until_running()

        with pytest.raises(TurnIngressConflictError):
            await _accept(stack, "different channel payload")

        assert first_handle is not None
        assert len(stack.received_runs) == 1
        assert [entry.content for entry in await stack.manager.get_transcript(SESSION_KEY)] == [
            "original channel payload"
        ]
        assert _table_counts(stack.db_path) == {
            "sessions": 2,
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_channel_project_replay_survives_removal_and_missing_directory(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await _bind_project_session(stack, project_path)
        first_handle, _, _, first_replayed = await _accept(
            stack,
            "project channel payload",
        )
        await stack.wait_until_running()
        await stack.storage.remove_project_workspace(project.workspace_id)
        project_path.rmdir()

        replay_handle, _, _, replayed = await _accept(
            stack,
            "project channel payload",
        )

        assert first_handle is not None
        assert first_replayed is False
        assert replay_handle is not None
        assert replay_handle.task_id == first_handle.task_id
        assert replayed is True
        assert len(stack.received_runs) == 1


@pytest.mark.asyncio
async def test_channel_project_conflict_precedes_workspace_unavailable(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await _bind_project_session(stack, project_path)
        first_handle, _, _, _ = await _accept(
            stack,
            "original project channel payload",
        )
        await stack.wait_until_running()
        await stack.storage.remove_project_workspace(project.workspace_id)
        project_path.rmdir()

        with pytest.raises(TurnIngressConflictError):
            await _accept(stack, "changed project channel payload")

        assert first_handle is not None
        assert len(stack.received_runs) == 1


@pytest.mark.asyncio
async def test_channel_default_turn_claims_goal_without_replacing_web_lease(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "channel-goal-claim.sqlite") as stack:
        goal = await _seed_idle_active_goal(stack)
        service = _install_channel_goal_service(stack)
        principal = SimpleNamespace(
            token_public_id="web-owner",
            guest_owner_id=None,
            is_owner=True,
            role="operator",
            scopes=frozenset({"operator.admin"}),
        )
        owner_connection_id = "web-owner-connection"
        service._subscriptions.get_message_subscribers = lambda _key: {
            owner_connection_id
        }
        get_registry().register(
            SimpleNamespace(conn_id=owner_connection_id, principal=principal)
        )
        owner_lease = service._install_lease(
            SimpleNamespace(
                conn_id=owner_connection_id,
                principal=principal,
                agent_id="main",
            ),
            goal=goal,
            source_kind="web",
        )
        try:
            handle, _, _, replayed = await _accept(
                stack,
                "Apply this channel follow-up to the active Goal.",
                native_message_id="native-channel-goal-claim",
            )
            assert handle is not None
            assert replayed is False
            await stack.wait_until_running()

            assert len(stack.received_runs) == 1
            context = GoalTurnContext.from_task_detail(
                stack.received_runs[0].goal_context
            )
            assert context is not None
            assert context.goal_id == goal.goal_id
            assert context.objective_revision == goal.objective_revision
            claimed = await stack.storage.get_goal(SESSION_KEY)
            assert claimed is not None
            assert claimed.active_task_id == handle.task_id
            assert service._leases[SESSION_KEY] is owner_lease

            stack.release_handler.set()
            await stack.runtime.wait(handle.task_id, timeout=2.0)
            settled = await stack.storage.get_goal(SESSION_KEY)
            assert settled is not None
            assert settled.active_task_id is None
            assert settled.status == "active"
            assert service._leases[SESSION_KEY] is owner_lease
        finally:
            stack.runtime.set_lifecycle_listener(None)
            await service.close()
            get_registry().unregister(owner_connection_id)


@pytest.mark.asyncio
async def test_channel_claim_without_owner_lease_defers_automatic_continuation(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "channel-goal-no-owner.sqlite") as stack:
        goal = await _seed_idle_active_goal(stack)
        service = _install_channel_goal_service(stack)
        stack.runtime.set_idle_listener(service.on_runtime_idle)
        try:
            assert service._leases == {}
            handle, _, _, replayed = await _accept(
                stack,
                "Claim the Goal, but do not create channel execution authority.",
                native_message_id="native-channel-goal-no-owner",
            )
            assert handle is not None
            assert replayed is False
            await stack.wait_until_running()
            context = GoalTurnContext.from_task_detail(
                stack.received_runs[0].goal_context
            )
            assert context is not None and context.goal_id == goal.goal_id
            assert service._leases == {}

            stack.release_handler.set()
            await stack.runtime.wait(handle.task_id, timeout=2.0)
            detached = None
            for _ in range(200):
                candidate = await stack.storage.get_goal(SESSION_KEY)
                if candidate is not None and candidate.active_task_id is None:
                    detached = candidate
                    break
                await asyncio.sleep(0.01)
            assert detached is not None
            assert detached.status == "active"
            assert detached.pause_reason is None
            snapshot = await service.snapshot(detached)
            assert snapshot is not None
            assert snapshot["continuationDeferredReason"] == "owner_disconnected"
            assert service._leases == {}
            assert len(stack.received_runs) == 1
        finally:
            stack.runtime.set_idle_listener(None)
            stack.runtime.set_lifecycle_listener(None)
            await service.close()


def test_debounced_channel_native_request_id_is_stable_and_order_sensitive() -> None:
    first = _message("combined")
    first.metadata["_opensquilla_debounce_native_message_ids"] = ["message-a", "message-b"]
    same = _message("combined")
    same.metadata["_opensquilla_debounce_native_message_ids"] = ["message-a", "message-b"]
    reversed_order = _message("combined")
    reversed_order.metadata["_opensquilla_debounce_native_message_ids"] = [
        "message-b",
        "message-a",
    ]

    first_id = _channel_native_request_id(first)
    assert first_id is not None
    assert first_id.startswith("debounce:")
    assert _channel_native_request_id(same) == first_id
    assert _channel_native_request_id(reversed_order) != first_id


@pytest.mark.asyncio
async def test_debounce_partial_native_ids_use_distinct_whole_batch_fallbacks() -> None:
    coordinator = _DefaultDebounceCoordinator()

    async def _fire_batch(missing_content: str) -> Any:
        fired = asyncio.get_running_loop().create_future()

        async def _capture(combined: Any) -> None:
            fired.set_result(combined)

        await coordinator.schedule(
            SESSION_KEY,
            _message("known", native_message_id="shared-native-id"),
            window_s=0.01,
            on_fire=_capture,
        )
        await coordinator.schedule(
            SESSION_KEY,
            _message(missing_content, native_message_id=""),
            window_s=0.01,
            on_fire=_capture,
        )
        return await asyncio.wait_for(fired, timeout=1.0)

    first = await _fire_batch("missing-b")
    second = await _fire_batch("missing-c")

    for combined in (first, second):
        assert "_opensquilla_debounce_native_message_ids" not in combined.message.metadata
        assert combined.message.metadata["native_message_id"] == "shared-native-id"
        assert combined.message.metadata["_opensquilla_debounce_native_ids_incomplete"] is True
        assert _channel_native_request_id(combined.message) is None

    first_identity = _channel_ingress_identity(
        msg=first.message,
        route_envelope=_route(first.message),
        session_key=SESSION_KEY,
        raw_content=first.message.content,
    )
    second_identity = _channel_ingress_identity(
        msg=second.message,
        route_envelope=_route(second.message),
        session_key=SESSION_KEY,
        raw_content=second.message.content,
    )
    assert first_identity.client_request_id != second_identity.client_request_id


@pytest.mark.asyncio
async def test_debounce_registers_user_intent_before_goal_idle_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A received channel message outranks Goal work during its debounce wait."""

    class _QueuedChannel(_FinalOnlyChannel):
        supports_slash_commands = False

        def __init__(self) -> None:
            self.inbound: asyncio.Queue[IncomingMessage] = asyncio.Queue()

        async def receive(self) -> IncomingMessage:
            return await self.inbound.get()

        async def send(self, _message: OutgoingMessage) -> None:
            return None

    async with _open_stack(tmp_path / "debounce-intent.sqlite") as stack:
        channel = _QueuedChannel()
        coordinator = _DefaultDebounceCoordinator()
        dispatched = asyncio.Event()

        async def _unexpected_fire(*_args: Any, **_kwargs: Any) -> None:
            dispatched.set()

        monkeypatch.setattr(
            channel_dispatch_module,
            "_dispatch_combined_message_after_debounce",
            _unexpected_fire,
        )
        dispatch = asyncio.create_task(
            run_channel_dispatch(
                channel=channel,
                turn_runner=object(),
                session_manager=stack.manager,
                session_key_builder=lambda _message: SESSION_KEY,
                session_prefix="slack",
                task_runtime=stack.runtime,
                debounce_coordinator=coordinator,
                debounce_window_s=60.0,
            )
        )
        try:
            message = _message("human input waiting in debounce")
            message.metadata["is_group"] = False
            await channel.inbound.put(message)
            for _ in range(100):
                if SESSION_KEY in coordinator._pending:
                    break
                await asyncio.sleep(0)
            assert SESSION_KEY in coordinator._pending

            followup = _message(
                "second human input in the same batch",
                native_message_id="native-message-790",
            )
            followup.metadata["is_group"] = False
            await channel.inbound.put(followup)
            for _ in range(100):
                if len(coordinator._pending[SESSION_KEY].buffer) == 2:
                    break
                await asyncio.sleep(0)
            assert len(coordinator._pending[SESSION_KEY].buffer) == 2
            assert await stack.runtime.has_explicit_ingress_intent(SESSION_KEY)

            # This is the exact gate used by Goal automatic continuation after
            # a task settles. It must observe the already-arrived channel turn.
            async with stack.runtime.collect_admission(SESSION_KEY):
                async with stack.runtime.automatic_ingress_fence(SESSION_KEY) as allowed:
                    assert allowed is False

            await coordinator.cancel(SESSION_KEY)
            assert dispatched.is_set() is False
            assert await stack.runtime.has_explicit_ingress_intent(SESSION_KEY) is False
            assert stack.runtime._ingress_intent_states == {}
        finally:
            dispatch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await dispatch
            await coordinator.cancel_all()
