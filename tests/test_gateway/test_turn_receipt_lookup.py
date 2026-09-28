"""Receipt recovery is a query, including after consumption, deletion, and restart."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.application.turn_admission import TurnAdmission
from opensquilla.compat.aiosqlite import _AsyncConnection
from opensquilla.gateway.adapters.turn_admission import GatewayTurnAdmissionAdapter
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.rpc_turn_receipts import _handle_turns_receipt_get
from opensquilla.gateway.turn_receipts import (
    TURN_RECEIPT_METHOD,
    can_read_turn_receipts,
    decode_turn_receipt_query,
    read_turn_receipt,
)
from opensquilla.gateway.websocket import _build_features, _should_detach_rpc_request
from opensquilla.session.models import AgentTaskRecord, SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage

KEY = "agent:main:webchat:receipt-fixture"
REQUEST_ID = "receipt-request-fixture"
HASH = "sha256:" + "a" * 64
OPERATIONS = (
    "chat.send", "sessions.send", "sessions.steer.v2",
    "sessions.pending_inputs.dispatch", "sessions.pending_inputs.steer",
)


@pytest.fixture
async def storage(tmp_path: Path):
    value = await SessionStorage.open(str(tmp_path / "receipts.sqlite"))
    try:
        yield value
    finally:
        await value.close()


def _params(operation: str = "chat.send") -> dict[str, Any]:
    original: dict[str, Any] = {
        "key": KEY, "clientRequestId": REQUEST_ID, "message": "Synthetic receipt material",
    }
    if operation.startswith("sessions.pending_inputs."):
        original.update(pendingInputId="pending-fixture", requestFingerprint=HASH)
        original.pop("message")
    if operation.endswith("steer") or operation.endswith("steer.v2"):
        original.update(
            clientMessageId="client-message-fixture", expectedTurnId="task-fixture",
            surfaceId="fixture-surface", expectedRevision=1,
        )
    return {"operation": operation, "originalRequest": original}


async def _seed(
    storage: SessionStorage, params: dict[str, Any], *, target_key: str = KEY,
) -> None:
    query = decode_turn_receipt_query(params, principal_role="operator")
    await storage.upsert_session(SessionNode(
        session_key=target_key, session_id="session-fixture", epoch=4,
    ))
    context = {}
    if query.is_steer:
        context = {
            "intent": "steer", "target_turn_id": query.expected_turn_id,
            "turn_id": query.expected_turn_id, "client_message_id": query.client_message_id,
            "surface_id": query.surface_id, "disposition": "steering", "revision": 1,
        }
    if query.pending_input_id:
        await storage.enqueue_pending_chat_input(
            pending_input_id=query.pending_input_id, session_key=KEY,
            source_scope=query.source_scope, client_request_id=query.client_request_id,
            client_message_id="client-message-fixture",
            request_fingerprint=query.request_fingerprint,
            payload={"message": "Synthetic pending input"},
        )
    await storage.accept_turn(
        TranscriptEntry(
            session_id="session-fixture", session_key=target_key,
            message_id="message-fixture", role="user", content="Synthetic receipt material",
            turn_context=context,
        ),
        expected_epoch=4, updated_at=100,
        task_record=AgentTaskRecord(
            session_key=target_key, task_id="task-fixture",
            details={"session_id": "session-fixture", "session_epoch": 4},
        ),
        source_scope=query.source_scope, request_session_key=query.session_key,
        client_request_id=query.client_request_id, request_fingerprint=query.request_fingerprint,
        pending_input_id=query.pending_input_id,
        pending_input_fingerprint=query.request_fingerprint if query.pending_input_id else None,
        pending_input_revision=1 if query.pending_input_id else None,
    )


def _context(storage: SessionStorage) -> RpcContext:
    return RpcContext(
        conn_id="fixture-connection", session_manager=SimpleNamespace(storage=storage),
    )


async def _read_only_receipt(
    storage: SessionStorage, params: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    # A SQLite authorizer rejects every mutation, including unnoticed transactions
    # that delete Meta drafts or consume pending input on existing replay paths.
    denied = {
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
    }
    mutations = []

    def authorize(action, *args):
        if action in denied:
            mutations.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    async def set_authorizer(callback):
        setter = storage.conn._conn.set_authorizer
        if isinstance(storage.conn, _AsyncConnection):
            # The sqlite3 fallback permits worker-thread access as well.
            await asyncio.to_thread(setter, callback)
        else:
            await storage.conn._execute(setter, callback)

    forbidden = AsyncMock(side_effect=AssertionError("Receipt lookup entered admission"))
    changes = storage.conn.total_changes
    with monkeypatch.context() as patch:
        for owner in (TurnAdmission, GatewayTurnAdmissionAdapter):
            patch.setattr(owner, "admit", forbidden)
            patch.setattr(owner, "steer", forbidden)
        patch.setattr(storage, "accept_turn", forbidden)
        patch.setattr(storage, "replay_turn_ingress_receipt", forbidden)
        await set_authorizer(authorize)
        try:
            result = await _handle_turns_receipt_get(params, _context(storage))
        finally:
            await set_authorizer(None)
    forbidden.assert_not_called()
    assert mutations == []
    assert storage.conn.total_changes == changes
    return result


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_existing_receipt_is_read_without_writes(
    storage: SessionStorage, operation: str, monkeypatch: pytest.MonkeyPatch,
):
    params = _params(operation)
    await _seed(storage, params)
    result = await _read_only_receipt(storage, params, monkeypatch)
    assert result["status"] == "found"
    assert result["accepted"] is True
    assert result["receipt"]["sessionEpoch"] == 4
    assert result["receipt"]["taskStatus"] == "queued"
    assert result["receipt"]["taskId"] == "task-fixture"
    if "steer" in operation:
        assert result["receipt"]["steer"]["fallback_safe"] is False
        assert result["receipt"]["steer"]["disposition"] == "steering"
    if "pending_inputs" in operation:
        assert await storage.get_pending_chat_input("pending-fixture") is None


async def test_handoff_returns_accepted_target_and_never_creates_source(storage: SessionStorage):
    params = _params()
    target = "agent:main:webchat:handoff-target-fixture"
    await _seed(storage, params, target_key=target)
    result = await _handle_turns_receipt_get(params, _context(storage))
    assert result["receipt"]["requestSessionKey"] == KEY
    assert result["receipt"]["sessionKey"] == target
    assert await storage.get_session(KEY) is None


async def test_deleted_receipt_is_unknown_even_after_same_key_recreated(storage: SessionStorage):
    params = _params()
    await _seed(storage, params)
    await storage.delete_session(KEY)
    await storage.upsert_session(SessionNode(session_key=KEY, session_id="replacement-fixture"))
    assert await _handle_turns_receipt_get(params, _context(storage)) == {
        "status": "not_found", "accepted": None,
    }
    assert await storage.get_transcript("replacement-fixture") == []


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_real_reset_never_rebinds_old_receipt_to_replacement_turn(
    storage: SessionStorage, operation: str, monkeypatch: pytest.MonkeyPatch,
):
    params = _params(operation)
    await _seed(storage, params)
    original = await _read_only_receipt(storage, params, monkeypatch)
    session = await storage.get_session(KEY)
    assert session is not None
    replacement = session.model_copy(update={"session_id": "session-after-reset", "epoch": 5})
    archives = []

    async def archive(snapshot):
        archives.append(snapshot)

    await storage.reset_session(
        replacement, expected_session_id="session-fixture", expected_epoch=4,
        archive_writer=archive,
    )
    assert len(archives) == 1
    assert archives[0].node.session_id == "session-fixture"
    assert [entry.message_id for entry in archives[0].entries] == ["message-fixture"]
    assert await storage.get_transcript("session-fixture") == []

    next_params = _params()
    next_params["originalRequest"]["clientRequestId"] = "request-after-reset"
    query = decode_turn_receipt_query(next_params, principal_role="operator")
    await storage.accept_turn(
        TranscriptEntry(
            session_id=replacement.session_id, session_key=KEY,
            message_id="message-after-reset", role="user", content="Synthetic replacement turn",
        ),
        expected_epoch=5, updated_at=200,
        task_record=AgentTaskRecord(
            session_key=KEY, task_id="task-after-reset",
            details={"session_id": replacement.session_id, "session_epoch": 5},
        ),
        source_scope=query.source_scope, request_session_key=KEY,
        client_request_id=query.client_request_id, request_fingerprint=query.request_fingerprint,
    )
    next_receipt = await _read_only_receipt(storage, next_params, monkeypatch)
    assert next_receipt["receipt"]["sessionId"] == replacement.session_id
    assert next_receipt["receipt"]["sessionEpoch"] == 5
    assert next_receipt["receipt"]["taskId"] == "task-after-reset"

    result = await _read_only_receipt(storage, params, monkeypatch)
    if operation in {"chat.send", "sessions.send"}:
        # Reset retains ingress receipts, which still own the old generation.
        assert result == original
        assert result["receipt"]["sessionId"] == "session-fixture"
        assert result["receipt"]["sessionEpoch"] == 4
        assert result["receipt"]["taskId"] == "task-fixture"
    else:
        # Reset removes the context required to prove pending/Steer identity.
        assert result == {"status": "not_found", "accepted": None}
    assert (await storage.get_session(KEY)).session_id == replacement.session_id
    assert {task.task_id for task in await storage.list_agent_tasks(KEY)} == {
        "task-fixture", "task-after-reset",
    }
    assert [entry.message_id for entry in await storage.get_transcript(replacement.session_id)] == [
        "message-after-reset",
    ]


def _sqlite_backup(source: Path, destination: Path) -> None:
    # SQLite's backup API reads one consistent snapshot including committed WAL
    # pages; copying only the main database file would not prove restoration.
    with (
        closing(sqlite3.connect(source)) as reader,
        closing(sqlite3.connect(destination)) as writer,
    ):
        reader.backup(writer)


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_sqlite_backup_restores_receipt_after_delete_without_readmission(
    tmp_path: Path, operation: str, monkeypatch: pytest.MonkeyPatch,
):
    path, backup = tmp_path / "live.sqlite", tmp_path / "consistent-backup.sqlite"
    params = _params(operation)
    storage = await SessionStorage.open(str(path))
    try:
        await _seed(storage, params)
        original = await _read_only_receipt(storage, params, monkeypatch)
        await asyncio.to_thread(_sqlite_backup, path, backup)
        await storage.delete_session(KEY)
        assert await _read_only_receipt(storage, params, monkeypatch) == {
            "status": "not_found", "accepted": None,
        }
        assert await storage.get_session(KEY) is None
        assert await storage.list_agent_tasks(KEY) == []
    finally:
        await storage.close()

    await asyncio.to_thread(_sqlite_backup, backup, path)
    restored = await SessionStorage.open(str(path))
    try:
        # Startup marks unstarted tasks abandoned. The later lookup must only
        # report that durable state; restoring a backup does not resume a task.
        expected = {**original, "receipt": {**original["receipt"], "taskStatus": "abandoned"}}
        assert await _read_only_receipt(restored, params, monkeypatch) == expected
        tasks = await restored.list_agent_tasks(KEY)
        assert [task.task_id for task in tasks] == ["task-fixture"]
        assert tasks[0].terminal_reason == "process_restart"
        assert tasks[0].details["session_id"] == "session-fixture"
        assert tasks[0].details["session_epoch"] == 4
        assert [entry.message_id for entry in await restored.get_transcript("session-fixture")] == [
            "message-fixture",
        ]
        if operation.startswith("sessions.pending_inputs."):
            assert await restored.get_pending_chat_input("pending-fixture") is None
            pending_receipt = await restored.get_pending_chat_input_dispatch_receipt(
                "pending-fixture",
            )
            assert pending_receipt is not None
    finally:
        await restored.close()


async def test_concurrent_delete_cannot_readmit_original(storage: SessionStorage, monkeypatch):
    params = _params()
    await _seed(storage, params)
    entered, proceed = asyncio.Event(), asyncio.Event()
    original_getter = storage.get_turn_ingress_receipt

    async def delayed(**kwargs):
        entered.set()
        await proceed.wait()
        return await original_getter(**kwargs)

    monkeypatch.setattr(storage, "get_turn_ingress_receipt", delayed)
    read = asyncio.create_task(_handle_turns_receipt_get(params, _context(storage)))
    await entered.wait()
    await storage.delete_session(KEY)
    proceed.set()
    assert await read == {"status": "not_found", "accepted": None}
    assert await storage.get_session(KEY) is None


async def test_lookup_survives_storage_reopen(tmp_path: Path):
    path = str(tmp_path / "restart.sqlite")
    storage = await SessionStorage.open(path)
    params = _params()
    await _seed(storage, params)
    await storage.close()
    reopened = await SessionStorage.open(path)
    try:
        result = await _handle_turns_receipt_get(params, _context(reopened))
        assert result["receipt"]["taskId"] == "task-fixture"
    finally:
        await reopened.close()


async def test_compacted_steer_context_is_read_without_hydrating_body(storage: SessionStorage):
    params = _params("sessions.steer.v2")
    await _seed(storage, params)
    session = await storage.get_session(KEY)
    assert session is not None
    entries = await storage.get_transcript(session.session_id)
    assert await storage.rewrite_compacted_session(
        node=session, summary=None, entries=[], archived_entries=entries,
        expected_session_id=session.session_id, expected_session_epoch=session.epoch,
    )
    assert await storage.get_transcript(session.session_id) == []
    result = await _handle_turns_receipt_get(params, _context(storage))
    assert result["receipt"]["steer"]["disposition"] == "steering"


async def test_receipt_owner_epoch_never_comes_from_mutable_session(storage: SessionStorage):
    params = _params()
    await _seed(storage, params)
    session = await storage.get_session(KEY)
    assert session is not None
    session.epoch += 1
    await storage.upsert_session(session)
    result = await _handle_turns_receipt_get(params, _context(storage))
    assert result["receipt"]["sessionEpoch"] == 4


async def test_fingerprint_mismatch_never_claims_nonacceptance(storage: SessionStorage):
    params = _params()
    await _seed(storage, params)
    params["originalRequest"]["message"] = "Different synthetic material"
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_turns_receipt_get(params, _context(storage))
    assert raised.value.code == "IDEMPOTENCY_CONFLICT"
    assert raised.value.accepted is None


async def test_known_fingerprint_omits_large_material(storage: SessionStorage):
    params = _params()
    params["originalRequest"]["message"] = "synthetic " * 100_000
    await _seed(storage, params)
    first = await _handle_turns_receipt_get(params, _context(storage))
    compact = {
        "operation": "chat.send", "originalRequest": {"key": KEY, "clientRequestId": REQUEST_ID},
        "requestFingerprint": first["requestFingerprint"],
    }
    assert await _handle_turns_receipt_get(compact, _context(storage)) == first
    compact["originalRequest"]["attachments"] = [{"name": "different-fixture.txt"}]
    with pytest.raises(RpcHandlerError, match="Invalid original"):
        await _handle_turns_receipt_get(compact, _context(storage))


async def test_source_scope_and_original_target_are_bound(storage: SessionStorage):
    params = _params("sessions.steer.v2")
    await _seed(storage, params)
    query = decode_turn_receipt_query(params, principal_role="operator")
    assert await read_turn_receipt(storage, replace(query, source_scope="cli:cli:operator")) == {
        "status": "not_found", "accepted": None,
    }
    with pytest.raises(RpcHandlerError, match="identity"):
        await read_turn_receipt(storage, replace(query, expected_turn_id="another-task-fixture"))


async def test_consumed_pending_steer_uses_durable_default_surface(storage: SessionStorage):
    params = _params("sessions.pending_inputs.steer")
    await _seed(storage, params)
    params["originalRequest"].pop("surfaceId")
    result = await _handle_turns_receipt_get(params, _context(storage))
    assert result["receipt"]["steer"]["surface_id"] == "fixture-surface"
    params["originalRequest"]["surfaceId"] = "different-surface-fixture"
    with pytest.raises(RpcHandlerError, match="identity"):
        await _handle_turns_receipt_get(params, _context(storage))


@pytest.mark.parametrize("principal", [
    Principal("operator", frozenset({"operator.read"}), False, False),
    Principal("operator", frozenset(), True, True),
    Principal("node", frozenset({"operator.admin"}), True, True),
    Principal("operator", frozenset({"operator.admin"}), True, True, auth_state="invalid"),
])
async def test_lookup_denies_unauthorized_principals_and_hides_capability(principal):
    assert not can_read_turn_receipts(principal)
    assert TURN_RECEIPT_METHOD not in _build_features(get_dispatcher(), principal=principal).methods
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_turns_receipt_get(
            _params(), RpcContext(conn_id="fixture", principal=principal),
        )
    assert raised.value.code == "UNAUTHORIZED"


def test_local_owner_with_read_scope_retains_lookup_capability():
    principal = Principal("operator", frozenset({"operator.read"}), True, False)
    assert can_read_turn_receipts(principal)
    assert TURN_RECEIPT_METHOD in _build_features(get_dispatcher(), principal=principal).methods
    assert _should_detach_rpc_request(TURN_RECEIPT_METHOD, _params())


async def test_registered_contract_validates_found_and_missing_results(storage: SessionStorage):
    params = _params()
    await _seed(storage, params)
    response = await get_dispatcher().dispatch(
        "lookup-fixture", TURN_RECEIPT_METHOD, params, _context(storage),
    )
    assert response.ok is True
    assert response.payload["status"] == "found"
    await storage.delete_session(KEY)
    response = await get_dispatcher().dispatch(
        "missing-fixture", TURN_RECEIPT_METHOD, params, _context(storage),
    )
    assert response.ok is True
    assert response.payload == {"status": "not_found", "accepted": None}


@pytest.mark.parametrize("params", [
    {"operation": "sessions.delete", "originalRequest": {"key": KEY}},
    {"operation": "chat.send", "originalRequest": {"key": KEY, "message": "fixture"}},
    {"operation": "chat.send", "originalRequest": {"key": KEY, "clientRequestId": REQUEST_ID}},
    {**_params(), "source_scope": "arbitrary"},
])
async def test_invalid_lookup_cannot_generate_request_identity(storage: SessionStorage, params):
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_turns_receipt_get(params, _context(storage))
    assert raised.value.code == "INVALID_REQUEST"
    assert raised.value.accepted is None
    assert await storage.get_session(KEY) is None
