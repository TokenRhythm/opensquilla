"""Receipt recovery is a query, including after consumption, deletion, and restart."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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


@pytest.mark.parametrize("operation", [
    "chat.send", "sessions.send", "sessions.steer.v2",
    "sessions.pending_inputs.dispatch", "sessions.pending_inputs.steer",
])
async def test_existing_receipt_is_read_without_writes(storage: SessionStorage, operation: str):
    params = _params(operation)
    await _seed(storage, params)
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

    await storage.conn._execute(storage.conn._conn.set_authorizer, authorize)
    try:
        result = await _handle_turns_receipt_get(params, _context(storage))
    finally:
        await storage.conn._execute(storage.conn._conn.set_authorizer, None)
    assert mutations == []
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
