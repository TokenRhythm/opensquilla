"""Explicit retirement is a durable fence, never an inference from a receipt miss."""

from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.contracts.generated.v4.gateway_contract_registry import GATEWAY_METHOD_CONTRACTS
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.rpc_turn_receipts import _handle_turns_receipt_resolve
from opensquilla.gateway.turn_receipts import decode_turn_receipt_query, read_turn_receipt
from opensquilla.session.models import AgentTaskRecord, SessionNode, TranscriptEntry
from opensquilla.session.storage import (
    SessionStorage,
    StorageBusyError,
    TurnIngressReceiptUnavailableError,
    TurnIngressRetiredError,
)

KEY = "agent:main:webchat:resolve-fixture"
PARAMS = {
    "operation": "chat.send",
    "originalRequest": {"key": KEY, "clientRequestId": "request-fixture", "message": "hello"},
}


def _identity(params=None):
    query = decode_turn_receipt_query(params or PARAMS, principal_role="operator")
    return {
        "source_scope": query.source_scope,
        "request_session_key": query.session_key,
        "client_request_id": query.client_request_id,
        "request_fingerprint": query.request_fingerprint,
    }


def _context(storage):
    return RpcContext(
        conn_id="resolution-fixture", session_manager=SimpleNamespace(storage=storage)
    )


async def _accept(storage, *, message="message-fixture", task="task-fixture"):
    return await storage.accept_turn(
        TranscriptEntry(
            session_id="session-fixture",
            session_key=KEY,
            message_id=message,
            role="user",
            content="hello",
        ),
        expected_epoch=0,
        updated_at=100,
        task_record=AgentTaskRecord(session_key=KEY, task_id=task),
        **_identity(),
    )


async def _counts(storage):
    values = {}
    for table in ("transcript_entries", "agent_tasks", "turn_ingress_receipts"):
        async with storage.conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
            values[table] = (await cursor.fetchone())[0]
    return values


@pytest.fixture
async def storage(tmp_path):
    value = await SessionStorage.open(str(tmp_path / "resolution.sqlite"))
    try:
        yield value
    finally:
        await value.close()


async def test_legacy_missing_receipt_remains_unknown_and_does_not_write(storage):
    before = storage.conn.total_changes
    assert await _handle_turns_receipt_resolve(PARAMS, _context(storage)) == {
        "status": "not_found",
        "accepted": None,
    }
    assert storage.conn.total_changes == before


async def test_registered_missing_request_retires_without_admitting_or_resending(storage):
    await storage.register_turn_request_attempt(**_identity())
    result = await _handle_turns_receipt_resolve(PARAMS, _context(storage))
    assert result == {
        "status": "retired",
        "accepted": False,
        "requestFingerprint": _identity()["request_fingerprint"],
        "clientRequestId": "request-fixture",
        "requestSessionKey": KEY,
    }
    assert await _handle_turns_receipt_resolve(PARAMS, _context(storage)) == result
    assert set((await _counts(storage)).values()) == {0}
    # Ordinary lookup stays read-only and does not turn a missing receipt into a negative.
    before = storage.conn.total_changes
    assert await read_turn_receipt(
        storage,
        decode_turn_receipt_query(PARAMS, principal_role="operator"),
    ) == {"status": "not_found", "accepted": None}
    assert storage.conn.total_changes == before


async def test_retired_request_fences_delayed_acceptance_and_reregistration(storage):
    await storage.upsert_session(SessionNode(session_key=KEY, session_id="session-fixture"))
    await storage.register_turn_request_attempt(**_identity())
    await _handle_turns_receipt_resolve(PARAMS, _context(storage))
    with pytest.raises(TurnIngressRetiredError):
        await _accept(storage)
    with pytest.raises(TurnIngressRetiredError):
        await storage.register_turn_request_attempt(**_identity())
    assert set((await _counts(storage)).values()) == {0}


async def test_positive_receipt_always_wins_and_duplicate_acceptance_executes_once(storage):
    await storage.upsert_session(SessionNode(session_key=KEY, session_id="session-fixture"))
    await storage.register_turn_request_attempt(**_identity())
    first = await _accept(storage)
    result = await _handle_turns_receipt_resolve(PARAMS, _context(storage))
    assert result["status"] == "found"
    assert result["accepted"] is True
    replay = await _accept(storage, message="duplicate-message", task="duplicate-task")
    assert replay.replayed and replay.receipt == first.receipt
    assert set((await _counts(storage)).values()) == {1}


async def test_accepted_deletion_never_becomes_nonacceptance(storage):
    await storage.upsert_session(SessionNode(session_key=KEY, session_id="session-fixture"))
    # Includes older accepted requests that never passed the registration hook.
    await _accept(storage)
    await storage.delete_session(KEY)
    assert await _handle_turns_receipt_resolve(PARAMS, _context(storage)) == {
        "status": "not_found",
        "accepted": None,
    }
    with pytest.raises(TurnIngressReceiptUnavailableError):
        await storage.register_turn_request_attempt(**_identity())
    with pytest.raises(TurnIngressReceiptUnavailableError):
        await _accept(storage)


async def test_restart_preserves_pending_and_retired_fences(tmp_path):
    path = str(tmp_path / "restart.sqlite")
    async with await SessionStorage.open(path) as storage:
        await storage.register_turn_request_attempt(**_identity())
    async with await SessionStorage.open(path) as storage:
        assert (await _handle_turns_receipt_resolve(PARAMS, _context(storage)))[
            "status"
        ] == "retired"
    async with await SessionStorage.open(path) as storage:
        assert (await _handle_turns_receipt_resolve(PARAMS, _context(storage)))[
            "status"
        ] == "retired"
        with pytest.raises(TurnIngressRetiredError):
            await storage.register_turn_request_attempt(**_identity())


async def test_wrong_payload_conflicts_without_retiring_original(storage):
    await storage.register_turn_request_attempt(**_identity())
    wrong = {**PARAMS, "originalRequest": {**PARAMS["originalRequest"], "message": "different"}}
    with pytest.raises(RpcHandlerError) as error:
        await _handle_turns_receipt_resolve(wrong, _context(storage))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert (await _handle_turns_receipt_resolve(PARAMS, _context(storage)))["status"] == "retired"


async def test_compact_known_fingerprint_resolves_without_message_body(storage):
    await storage.register_turn_request_attempt(**_identity())
    compact = {
        "operation": "chat.send",
        "requestFingerprint": _identity()["request_fingerprint"],
        "originalRequest": {"key": KEY, "clientRequestId": "request-fixture"},
    }
    assert (await _handle_turns_receipt_resolve(compact, _context(storage)))["status"] == "retired"
    wrong = {**compact, "requestFingerprint": "sha256:" + "a" * 64}
    with pytest.raises(RpcHandlerError) as error:
        await _handle_turns_receipt_resolve(wrong, _context(storage))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("scope", ["operator.read", "guest.safe"])
async def test_readonly_and_guest_cannot_retire(storage, scope):
    await storage.register_turn_request_attempt(**_identity())
    ctx = _context(storage)
    ctx.principal = Principal(
        role="operator",
        scopes=frozenset({scope}),
        is_owner=False,
        authenticated=scope != "guest.safe",
        auth_state="guest" if scope == "guest.safe" else "authenticated",
    )
    with pytest.raises(RpcHandlerError) as error:
        await _handle_turns_receipt_resolve(PARAMS, ctx)
    assert error.value.code == "UNAUTHORIZED"


async def test_other_source_or_session_cannot_retire_registered_identity(storage):
    await storage.register_turn_request_attempt(**_identity())
    assert (
        await storage.resolve_turn_request_attempt(
            **{**_identity(), "source_scope": "web:webchat:node"},
        )
        == "not_found"
    )
    wrong = {**PARAMS, "originalRequest": {**PARAMS["originalRequest"], "key": KEY + "-other"}}
    assert (await _handle_turns_receipt_resolve(wrong, _context(storage)))["status"] == "not_found"


async def test_retirement_trigger_failure_rolls_back_and_never_claims_false(storage):
    await storage.register_turn_request_attempt(**_identity())
    await storage.conn.execute("""CREATE TRIGGER fail_retirement
        BEFORE UPDATE ON turn_request_resolution
        WHEN NEW.state = 'retired' BEGIN SELECT RAISE(ABORT, 'injected retirement failure'); END""")
    await storage.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected retirement failure"):
        await _handle_turns_receipt_resolve(PARAMS, _context(storage))
    await storage.conn.execute("DROP TRIGGER fail_retirement")
    await storage.conn.commit()
    await storage.upsert_session(SessionNode(session_key=KEY, session_id="session-fixture"))
    await _accept(storage)
    assert (await _handle_turns_receipt_resolve(PARAMS, _context(storage)))["status"] == "found"


async def test_locked_real_database_is_unknown_until_later_retirement(tmp_path):
    path = str(tmp_path / "busy.sqlite")
    async with await SessionStorage.open(path) as storage:
        await storage.register_turn_request_attempt(**_identity())
        storage._busy_budget_seconds = 0.0
        locker = sqlite3.connect(path, timeout=0.1, isolation_level=None)
        try:
            locker.execute("BEGIN IMMEDIATE")
            with pytest.raises(StorageBusyError) as error:
                await _handle_turns_receipt_resolve(PARAMS, _context(storage))
            assert error.value.retry_after_ms > 0
            assert not storage.conn.in_transaction
        finally:
            locker.execute("ROLLBACK")
            locker.close()
        assert (await _handle_turns_receipt_resolve(PARAMS, _context(storage)))[
            "status"
        ] == "retired"


async def test_acceptance_marker_failure_rolls_back_receipt_task_and_transcript(storage):
    await storage.upsert_session(SessionNode(session_key=KEY, session_id="session-fixture"))
    await storage.register_turn_request_attempt(**_identity())
    await storage.conn.execute("""CREATE TRIGGER fail_accepted_marker
        BEFORE UPDATE ON turn_request_resolution WHEN NEW.state = 'accepted'
        BEGIN SELECT RAISE(ABORT, 'injected accepted marker failure'); END""")
    await storage.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected accepted marker failure"):
        await _accept(storage)
    assert set((await _counts(storage)).values()) == {0}
    assert (await _handle_turns_receipt_resolve(PARAMS, _context(storage)))["status"] == "retired"


async def test_independent_writer_races_acceptance_against_retirement(tmp_path):
    path = str(tmp_path / "race.sqlite")
    async with await SessionStorage.open(path) as first:
        await first.upsert_session(SessionNode(session_key=KEY, session_id="session-fixture"))
        await first.register_turn_request_attempt(**_identity())
        async with await SessionStorage.open(path) as second:
            outcomes = await asyncio.gather(
                _accept(first),
                _handle_turns_receipt_resolve(PARAMS, _context(second)),
                return_exceptions=True,
            )
            accepted, resolved = outcomes
            assert isinstance(resolved, dict)
            if resolved["status"] == "retired":
                assert isinstance(accepted, TurnIngressRetiredError)
                assert set((await _counts(first)).values()) == {0}
            else:
                assert resolved["status"] == "found"
                assert not isinstance(accepted, Exception)
                assert set((await _counts(first)).values()) == {1}


def test_resolution_is_generated_write_command_not_optional_read():
    from opensquilla.gateway.websocket import _CONCURRENT_OPTIONAL_READ_METHODS

    contract = GATEWAY_METHOD_CONTRACTS["turns.receipt.resolve"]
    assert contract.scope == "operator.write"
    assert contract.kind == "command"
    assert contract.idempotency == "idempotent"
    assert "turns.receipt.resolve" not in _CONCURRENT_OPTIONAL_READ_METHODS
    assert (
        get_dispatcher().get_entry("turns.receipt.resolve").generated_contract_name
        == "turns.receipt.resolve"
    )


@pytest.mark.parametrize("operation", ["sessions.send", "sessions.steer.v2"])
async def test_resolution_rejects_non_chat_operations(storage, operation):
    with pytest.raises(RpcHandlerError) as error:
        await _handle_turns_receipt_resolve({**PARAMS, "operation": operation}, _context(storage))
    assert error.value.code == "INVALID_REQUEST"


def test_migration_preserves_prior_accepted_identity_and_never_erases_fences(monkeypatch):
    import yoyo

    monkeypatch.setattr(yoyo, "step", lambda *args: args)
    path = Path(__file__).resolve().parents[2] / "migrations/V049__turn_request_resolution.py"
    spec = importlib.util.spec_from_file_location("resolution_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with sqlite3.connect(":memory:") as conn:
        conn.execute("""CREATE TABLE turn_ingress_receipts (
            source_scope TEXT, request_session_key TEXT, client_request_id TEXT,
            request_fingerprint TEXT, accepted_at INTEGER)""")
        conn.execute(
            "INSERT INTO turn_ingress_receipts VALUES (?, ?, ?, ?, 100)",
            (
                "web:webchat:operator",
                KEY,
                "legacy-accepted",
                "sha256:old",
            ),
        )
        module.apply_step(conn)
        module.apply_step(conn)
        conn.execute("DELETE FROM turn_ingress_receipts")
        module.rollback_step(conn)
        assert conn.execute(
            "SELECT client_request_id, state FROM turn_request_resolution"
        ).fetchall() == [
            ("legacy-accepted", "accepted"),
        ]
