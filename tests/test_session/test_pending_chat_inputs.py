"""Durable staged chat-input storage contracts."""

from __future__ import annotations

import asyncio
import json

import pytest

from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import (
    MAX_PENDING_CHAT_INPUTS,
    PendingChatInputAlreadyDispatchedError,
    PendingChatInputCancelledError,
    PendingChatInputCapacityError,
    PendingChatInputConflictError,
    SessionStorage,
)

SESSION_KEY = "agent:main:webchat:pending-storage"
SESSION_ID = "session-pending-storage"


def _payload(index: int) -> dict[str, object]:
    return {
        "key": SESSION_KEY,
        "message": f"queued-{index}",
        "attachments": [],
        "queueMode": "followup",
        "clientRequestId": f"request-{index}",
        "clientMessageId": f"message-{index}",
        "_source": {"caller_kind": "web", "channel_kind": "web"},
    }


async def _stage(storage: SessionStorage, index: int):
    return await storage.enqueue_pending_chat_input(
        pending_input_id=f"pending-{index}",
        session_key=SESSION_KEY,
        source_scope="web:web:operator",
        client_request_id=f"request-{index}",
        client_message_id=f"message-{index}",
        request_fingerprint=f"sha256:fingerprint-{index}",
        payload=_payload(index),
    )


async def test_pending_inputs_are_bounded_and_enqueue_is_idempotent(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        first, replayed = await _stage(storage, 0)
        assert replayed is False
        repeated, replayed = await _stage(storage, 0)
        assert replayed is True
        assert repeated == first

        for index in range(1, MAX_PENDING_CHAT_INPUTS):
            await _stage(storage, index)
        with pytest.raises(PendingChatInputCapacityError):
            await _stage(storage, MAX_PENDING_CHAT_INPUTS)

        rows = await storage.list_pending_chat_inputs(SESSION_KEY)
        assert [row.pending_input_id for row in rows] == [
            f"pending-{index}" for index in range(MAX_PENDING_CHAT_INPUTS)
        ]
    finally:
        await storage.close()


async def test_pending_input_identity_reuse_with_different_payload_conflicts(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await _stage(storage, 0)
        with pytest.raises(PendingChatInputConflictError):
            await storage.enqueue_pending_chat_input(
                pending_input_id="pending-0",
                session_key=SESSION_KEY,
                source_scope="web:web:operator",
                client_request_id="request-0",
                client_message_id="message-0",
                request_fingerprint="sha256:different",
                payload={**_payload(0), "message": "different"},
            )
        assert len(await storage.list_pending_chat_inputs(SESSION_KEY)) == 1
    finally:
        await storage.close()


async def test_pending_input_update_and_cancel_use_revision_cas(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        row, _ = await _stage(storage, 0)
        moved = await storage.update_pending_chat_input(
            row.pending_input_id,
            session_key=SESSION_KEY,
            expected_revision=row.state_revision,
            position=4,
        )
        assert moved.position == 4
        assert moved.state_revision == 2
        with pytest.raises(PendingChatInputConflictError):
            await storage.cancel_pending_chat_input(
                row.pending_input_id,
                session_key=SESSION_KEY,
                expected_revision=1,
            )
        assert await storage.cancel_pending_chat_input(
            row.pending_input_id,
            session_key=SESSION_KEY,
            expected_revision=2,
        )
        assert not await storage.cancel_pending_chat_input(
            row.pending_input_id,
            session_key=SESSION_KEY,
        )
    finally:
        await storage.close()


async def test_pending_input_reorder_is_complete_atomic_and_revision_guarded(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        staged = [(await _stage(storage, index))[0] for index in range(3)]
        reordered = await storage.reorder_pending_chat_inputs(
            session_key=SESSION_KEY,
            expected_revisions=[
                (staged[2].pending_input_id, staged[2].state_revision),
                (staged[0].pending_input_id, staged[0].state_revision),
                (staged[1].pending_input_id, staged[1].state_revision),
            ],
        )
        assert [row.pending_input_id for row in reordered] == [
            "pending-2",
            "pending-0",
            "pending-1",
        ]
        assert [row.position for row in reordered] == [0, 1, 2]
        assert [row.state_revision for row in reordered] == [2, 2, 2]

        with pytest.raises(PendingChatInputConflictError):
            await storage.reorder_pending_chat_inputs(
                session_key=SESSION_KEY,
                expected_revisions=[
                    ("pending-1", 2),
                    ("pending-2", 1),
                    ("pending-0", 2),
                ],
            )
        unchanged = await storage.list_pending_chat_inputs(SESSION_KEY)
        assert [row.pending_input_id for row in unchanged] == [
            "pending-2",
            "pending-0",
            "pending-1",
        ]
        assert [row.state_revision for row in unchanged] == [2, 2, 2]

        with pytest.raises(PendingChatInputConflictError):
            await storage.reorder_pending_chat_inputs(
                session_key=SESSION_KEY,
                expected_revisions=[("pending-0", 2), ("pending-1", 2)],
            )

        with pytest.raises(ValueError, match="unique"):
            await storage.reorder_pending_chat_inputs(
                session_key=SESSION_KEY,
                expected_revisions=[
                    ("pending-2", 2),
                    ("pending-2", 2),
                    ("pending-0", 2),
                ],
            )

        other, _ = await storage.enqueue_pending_chat_input(
            pending_input_id="pending-other-session",
            session_key="agent:main:webchat:other-session",
            source_scope="web:web:operator",
            client_request_id="request-other-session",
            client_message_id="message-other-session",
            request_fingerprint="sha256:fingerprint-other-session",
            payload={**_payload(9), "key": "agent:main:webchat:other-session"},
        )
        with pytest.raises(PendingChatInputConflictError):
            await storage.reorder_pending_chat_inputs(
                session_key=SESSION_KEY,
                expected_revisions=[
                    ("pending-2", 2),
                    ("pending-0", 2),
                    (other.pending_input_id, other.state_revision),
                ],
            )
    finally:
        await storage.close()


async def test_pending_input_reorder_rolls_back_and_loses_cleanly_to_cancel(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        staged = [(await _stage(storage, index))[0] for index in range(3)]
        original_decoder = storage._pending_chat_input_from_row
        decode_count = 0

        def fail_after_updates(row):
            nonlocal decode_count
            decode_count += 1
            if decode_count == 4:
                raise RuntimeError("synthetic reorder result failure")
            return original_decoder(row)

        monkeypatch.setattr(storage, "_pending_chat_input_from_row", fail_after_updates)
        with pytest.raises(RuntimeError, match="synthetic reorder result failure"):
            await storage.reorder_pending_chat_inputs(
                session_key=SESSION_KEY,
                expected_revisions=[
                    (staged[2].pending_input_id, 1),
                    (staged[0].pending_input_id, 1),
                    (staged[1].pending_input_id, 1),
                ],
            )
        monkeypatch.setattr(storage, "_pending_chat_input_from_row", original_decoder)
        unchanged = await storage.list_pending_chat_inputs(SESSION_KEY)
        assert [row.pending_input_id for row in unchanged] == [
            "pending-0",
            "pending-1",
            "pending-2",
        ]
        assert [row.state_revision for row in unchanged] == [1, 1, 1]

        reordered, cancelled = await asyncio.gather(
            storage.reorder_pending_chat_inputs(
                session_key=SESSION_KEY,
                expected_revisions=[
                    (staged[2].pending_input_id, 1),
                    (staged[0].pending_input_id, 1),
                    (staged[1].pending_input_id, 1),
                ],
            ),
            storage.cancel_pending_chat_input(
                staged[1].pending_input_id,
                session_key=SESSION_KEY,
                expected_revision=1,
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, PendingChatInputConflictError) for result in (
            reordered,
            cancelled,
        )) == 1
        assert sum(not isinstance(result, BaseException) for result in (
            reordered,
            cancelled,
        )) == 1
    finally:
        await storage.close()


async def test_cancel_missing_tombstone_rejects_delayed_enqueue(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        assert not await storage.cancel_pending_chat_input(
            "pending-0",
            session_key=SESSION_KEY,
        )
        with pytest.raises(PendingChatInputCancelledError):
            await _stage(storage, 0)
        assert await storage.list_pending_chat_inputs(SESSION_KEY) == []

        # Cancellation is idempotent, but the globally stable pending id may
        # not be rebound to another session.
        assert not await storage.cancel_pending_chat_input(
            "pending-0",
            session_key=SESSION_KEY,
        )
        with pytest.raises(PendingChatInputConflictError):
            await storage.cancel_pending_chat_input(
                "pending-0",
                session_key="agent:main:webchat:other",
            )
    finally:
        await storage.close()


async def test_dispatch_consumes_pending_row_with_transcript_task_and_receipt(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                created_at=1,
                updated_at=1,
                epoch=0,
            )
        )
        pending, _ = await _stage(storage, 0)
        result = await storage.accept_turn(
            TranscriptEntry(
                session_id=SESSION_ID,
                session_key=SESSION_KEY,
                message_id="accepted-message",
                role="user",
                content="queued-0",
                created_at=2,
            ),
            expected_epoch=0,
            updated_at=2,
            task_record=AgentTaskRecord(
                task_id="accepted-task",
                session_key=SESSION_KEY,
                source_kind="webui",
                queue_mode="followup",
                run_kind="session_turn",
                status=AgentTaskStatus.QUEUED,
                created_at=2,
                updated_at=2,
            ),
            source_scope=pending.source_scope,
            request_session_key=SESSION_KEY,
            client_request_id=pending.client_request_id,
            request_fingerprint=pending.request_fingerprint,
            pending_input_id=pending.pending_input_id,
            pending_input_fingerprint=pending.request_fingerprint,
            pending_input_revision=pending.state_revision,
        )

        assert result.replayed is False
        assert await storage.list_pending_chat_inputs(SESSION_KEY) == []
        assert [entry.message_id for entry in await storage.get_transcript(SESSION_ID)] == [
            "accepted-message"
        ]
        assert (await storage.get_agent_task("accepted-task")) is not None
        receipt = await storage.get_turn_ingress_receipt(
            source_scope=pending.source_scope,
            request_session_key=SESSION_KEY,
            client_request_id=pending.client_request_id,
        )
        assert receipt is not None
        assert receipt.receipt.task_id == "accepted-task"
        dispatch_receipt = await storage.get_pending_chat_input_dispatch_receipt(
            pending.pending_input_id
        )
        assert dispatch_receipt is not None
        assert dispatch_receipt.client_request_id == pending.client_request_id
        assert dispatch_receipt.client_message_id == pending.client_message_id
        assert dispatch_receipt.request_fingerprint == pending.request_fingerprint
        with pytest.raises(PendingChatInputAlreadyDispatchedError):
            await _stage(storage, 0)
        with pytest.raises(PendingChatInputConflictError):
            await storage.enqueue_pending_chat_input(
                pending_input_id=pending.pending_input_id,
                session_key=SESSION_KEY,
                source_scope=pending.source_scope,
                client_request_id=pending.client_request_id,
                client_message_id="changed-message-id",
                request_fingerprint=pending.request_fingerprint,
                payload={**pending.payload, "clientMessageId": "changed-message-id"},
            )
        with pytest.raises(PendingChatInputConflictError):
            await storage.enqueue_pending_chat_input(
                pending_input_id="pending-arbitrary",
                session_key=SESSION_KEY,
                source_scope=pending.source_scope,
                client_request_id=pending.client_request_id,
                client_message_id=pending.client_message_id,
                request_fingerprint=pending.request_fingerprint,
                payload=pending.payload,
            )
        with pytest.raises(PendingChatInputConflictError):
            await storage.enqueue_pending_chat_input(
                pending_input_id="pending-new-request",
                session_key=SESSION_KEY,
                source_scope=pending.source_scope,
                client_request_id="request-new",
                client_message_id=pending.client_message_id,
                request_fingerprint=pending.request_fingerprint,
                payload={**pending.payload, "clientRequestId": "request-new"},
            )

        # Simulate a row resurrected by a pre-fix tab after this dispatch was
        # already accepted. The replay repair may consume it only because the
        # durable request and message identities still match the receipt.
        ghost_id = "pending-legacy-ghost"
        now = pending.updated_at + 1
        async with storage._write_transaction("test_insert_legacy_pending_ghost") as conn:
            await conn.execute(
                """
                INSERT INTO pending_chat_inputs (
                    pending_input_id, session_key, source_scope,
                    client_request_id, client_message_id, request_fingerprint,
                    payload_json, position, state_revision, created_at, updated_at,
                    schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, 1)
                """,
                (
                    ghost_id,
                    pending.session_key,
                    pending.source_scope,
                    pending.client_request_id,
                    pending.client_message_id,
                    pending.request_fingerprint,
                    json.dumps(pending.payload, sort_keys=True, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        assert await storage.consume_replayed_pending_chat_input(
            pending_input_id=ghost_id,
            session_key=pending.session_key,
            source_scope=pending.source_scope,
            client_request_id=pending.client_request_id,
            client_message_id=pending.client_message_id,
            request_fingerprint=pending.request_fingerprint,
            expected_revision=1,
        )
        assert await storage.list_pending_chat_inputs(SESSION_KEY) == []

        replay = await storage.accept_turn(
            TranscriptEntry(
                session_id=SESSION_ID,
                session_key=SESSION_KEY,
                message_id="ignored-replay-message",
                role="user",
                content="queued-0",
                created_at=3,
            ),
            expected_epoch=0,
            updated_at=3,
            task_record=None,
            source_scope=pending.source_scope,
            request_session_key=SESSION_KEY,
            client_request_id=pending.client_request_id,
            request_fingerprint=pending.request_fingerprint,
            pending_input_id=pending.pending_input_id,
            pending_input_fingerprint=pending.request_fingerprint,
            pending_input_revision=pending.state_revision,
        )
        assert replay.replayed is True

        with pytest.raises(PendingChatInputConflictError):
            await storage.accept_turn(
                TranscriptEntry(
                    session_id=SESSION_ID,
                    session_key=SESSION_KEY,
                    message_id="arbitrary-pending-replay",
                    role="user",
                    content="queued-0",
                    created_at=4,
                ),
                expected_epoch=0,
                updated_at=4,
                task_record=None,
                source_scope=pending.source_scope,
                request_session_key=SESSION_KEY,
                client_request_id=pending.client_request_id,
                request_fingerprint=pending.request_fingerprint,
                pending_input_id="pending-arbitrary",
                pending_input_fingerprint=pending.request_fingerprint,
                pending_input_revision=1,
            )
    finally:
        await storage.close()


async def test_dispatch_requires_one_fingerprint_for_pending_and_turn_receipts(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                created_at=1,
                updated_at=1,
                epoch=0,
            )
        )
        pending, _ = await _stage(storage, 0)
        with pytest.raises(PendingChatInputConflictError):
            await storage.accept_turn(
                TranscriptEntry(
                    session_id=SESSION_ID,
                    session_key=SESSION_KEY,
                    message_id="must-not-commit",
                    role="user",
                    content="queued-0",
                    created_at=2,
                ),
                expected_epoch=0,
                updated_at=2,
                task_record=None,
                source_scope=pending.source_scope,
                request_session_key=SESSION_KEY,
                client_request_id=pending.client_request_id,
                request_fingerprint="sha256:different-turn-fingerprint",
                pending_input_id=pending.pending_input_id,
                pending_input_fingerprint=pending.request_fingerprint,
                pending_input_revision=pending.state_revision,
            )

        assert [row.pending_input_id for row in await storage.list_pending_chat_inputs(
            SESSION_KEY
        )] == [pending.pending_input_id]
        assert await storage.get_transcript(SESSION_ID) == []
        assert await storage.get_pending_chat_input_dispatch_receipt(
            pending.pending_input_id
        ) is None
    finally:
        await storage.close()
