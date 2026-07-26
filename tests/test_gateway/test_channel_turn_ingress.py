"""Durable channel-turn acceptance contracts backed by the real runtime stack."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from opensquilla.channels.types import IncomingMessage, OutgoingMessage
from opensquilla.gateway._debounce import _DefaultDebounceCoordinator
from opensquilla.gateway.attachment_ingest import AttachmentIngestResult
from opensquilla.gateway.channel_dispatch import (
    _accept_channel_runtime_turn,
    _channel_ingress_identity,
    _channel_native_request_id,
    _deliver_runtime_channel_reply,
    _RuntimeChannelStreamRelay,
)
from opensquilla.gateway.routing import RouteEnvelope, build_channel_route_envelope
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.manager import SessionIntent, SessionManager
from opensquilla.session.models import AgentTaskStatus
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
        config=None,
    )


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


@pytest.mark.asyncio
async def test_channel_turn_atomically_creates_delivery_session_message_task_and_receipt(
    tmp_path: Path,
) -> None:
    async with _open_stack(tmp_path / "sessions.db") as stack:
        handle, persisted_text, stream_relay, replayed = await _accept(
            stack,
            "one durable channel turn",
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
        assert _table_counts(stack.db_path) == {
            # The accepted channel session plus the post-acceptance main-delivery fallback.
            "sessions": 2,
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


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
