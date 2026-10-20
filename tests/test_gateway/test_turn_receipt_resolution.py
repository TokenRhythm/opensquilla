"""Versioned unknown sends close against the real SQLite acceptance boundary."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.application.admission_errors import AdmissionError
from opensquilla.application.admission_failures import AdmissionStorageBusyError
from opensquilla.application.turn_acceptance import (
    DurableTurnAdmission,
    _close_rejected_resolvable_turn,
)
from opensquilla.gateway.adapters.turn_admission import (
    GatewayTurnAdmissionAdapter,
    map_admission_error,
)
from opensquilla.gateway.admission_input import source_scope_from_hint
from opensquilla.gateway.admission_storage import GatewayAdmissionStorage
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.project_workspace_runtime import map_project_workspace_error
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.rpc_chat import _handle_chat_send_v2
from opensquilla.gateway.rpc_turn_receipts import _handle_turns_receipt_resolve
from opensquilla.gateway.turn_receipts import (
    current_turn_receipt_epoch,
    decode_turn_receipt_query,
    read_turn_receipt,
    resolve_turn_receipt,
)
from opensquilla.gateway.websocket import _build_features
from opensquilla.persistence.turn_request_resolution import TurnReceiptEpochPolicy
from opensquilla.project_workspaces import ProjectWorkspaceStateError
from opensquilla.session.models import AgentTaskRecord, SessionNode, TranscriptEntry
from opensquilla.session.storage import (
    SessionStorage,
    StorageBusyError,
    TurnIngressConflictError,
    TurnRequestAdmissionClosedError,
    TurnRequestResolvedError,
)

KEY = "agent:main:webchat:resolution"
BOOT = "a" * 32
EPOCH = f"v2.{BOOT}.0.25"
PARAMS = {
    "message": "synthetic input", "key": KEY, "clientRequestId": f"tr2:{EPOCH}:original-id",
    "acceptanceEpoch": EPOCH,
}


@pytest.fixture(autouse=True)
def gateway_epoch(monkeypatch):
    monkeypatch.setattr(
        "opensquilla.gateway.turn_receipts.current_turn_receipt_epoch", lambda: EPOCH,
    )


async def open_storage(path):
    storage = await SessionStorage.open(str(path))
    storage.configure_turn_receipt_epochs(TurnReceiptEpochPolicy(BOOT, clock=lambda: 0))
    return storage


def query(operation="chat.send.v2"):
    return decode_turn_receipt_query(
        {"operation": operation, "originalRequest": PARAMS},
        principal_role="operator",
    )


async def accept(storage, request=None):
    request = request or query()
    return await storage.accept_turn(
        TranscriptEntry(
            session_id="original-session",
            session_key=KEY,
            message_id="original-message",
            role="user",
            content=PARAMS["message"],
        ),
        expected_epoch=1,
        updated_at=100,
        task_record=AgentTaskRecord(session_key=KEY, task_id="original-task"),
        source_scope=request.source_scope,
        request_session_key=request.session_key,
        client_request_id=request.client_request_id,
        request_fingerprint=request.request_fingerprint,
    )


@pytest.fixture
async def pair(tmp_path):
    path = tmp_path / "resolution.sqlite"
    left = await open_storage(path)
    await left.upsert_session(SessionNode(session_key=KEY, session_id="original-session", epoch=1))
    right = await open_storage(path)
    try:
        yield left, right, path
    finally:
        await right.close()
        await left.close()


async def test_absent_retirement_fences_late_acceptance_and_survives_restart(pair):
    left, right, path = pair
    result = await resolve_turn_receipt(left, query())
    assert result["status"] == "retired" and result["accepted"] is False
    assert await read_turn_receipt(right, query()) == result
    with pytest.raises(TurnRequestResolvedError, match="retired"):
        await accept(right)
    restarted = await SessionStorage.open(str(path))
    try:
        assert await resolve_turn_receipt(restarted, query()) == result
        with pytest.raises(TurnRequestResolvedError):
            await accept(restarted)
        assert await restarted.get_transcript("original-session") == []
        assert await restarted.get_agent_task("original-task") is None
    finally:
        await restarted.close()


async def test_accepted_receipt_and_deleted_session_remain_positive(pair):
    left, right, path = pair
    initial = await accept(left)
    replay = await accept(right)
    assert replay.replayed and replay.receipt.receipt_id == initial.receipt.receipt_id
    assert (await resolve_turn_receipt(right, query()))["status"] == "found"
    await left.delete_session(KEY)
    # Older deleters also remove the receipt; acceptance evidence is independent.
    async with left._write_transaction("legacy_delete") as conn:
        await conn.execute(
            "DELETE FROM turn_ingress_receipts WHERE accepted_session_key = ?", (KEY,)
        )
    expected = await resolve_turn_receipt(right, query())
    assert expected["status"] == "accepted_unavailable" and expected["accepted"] is True
    restarted = await SessionStorage.open(str(path))
    try:
        assert await read_turn_receipt(restarted, query()) == expected
        await restarted.upsert_session(
            SessionNode(session_key=KEY, session_id="original-session", epoch=1)
        )
        with pytest.raises(TurnRequestResolvedError, match="Accepted"):
            await accept(restarted)
    finally:
        await restarted.close()


async def test_first_send_busy_without_any_marker_can_close_after_unlock(pair):
    left, right, path = pair
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    left._busy_budget_seconds = 0.05
    try:
        with pytest.raises(StorageBusyError):
            await accept(left)
    finally:
        writer.rollback()
        writer.close()
    assert (
        await left.get_turn_request_resolution(
            source_scope=query().source_scope,
            request_session_key=KEY,
            client_request_id=query().client_request_id,
            request_fingerprint=query().request_fingerprint,
        )
        is None
    )
    assert (await resolve_turn_receipt(right, query()))["status"] == "retired"
    with pytest.raises(TurnRequestResolvedError):
        await accept(left)


@pytest.mark.parametrize("accept_first", [False, True])
async def test_independent_writers_have_one_terminal_winner(pair, monkeypatch, accept_first):
    left, right, _ = pair
    entered, release = asyncio.Event(), asyncio.Event()
    transaction = left._write_transaction

    @asynccontextmanager
    async def hold_writer(operation, **kwargs):
        async with transaction(operation, **kwargs) as conn:
            entered.set()
            await release.wait()
            yield conn

    monkeypatch.setattr(left, "_write_transaction", hold_writer)
    first = asyncio.create_task(
        accept(left) if accept_first else resolve_turn_receipt(left, query())
    )
    await asyncio.wait_for(entered.wait(), 3)
    second = asyncio.create_task(
        resolve_turn_receipt(right, query()) if accept_first else accept(right)
    )
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 5)
    if accept_first:
        assert results[1]["status"] == "found"
        assert len(await right.get_transcript("original-session")) == 1
    else:
        assert results[0]["status"] == "retired"
        assert isinstance(results[1], TurnRequestResolvedError)
        assert await right.get_transcript("original-session") == []


@pytest.mark.parametrize("accepted", [False, True])
async def test_terminal_identity_rejects_different_fingerprint(pair, accepted):
    left, right, _ = pair
    if accepted:
        await accept(left)
    else:
        await resolve_turn_receipt(left, query())
    mismatch = replace(query(), request_fingerprint="sha256:" + "a" * 64)
    with pytest.raises(RpcHandlerError, match="identity"):
        await resolve_turn_receipt(right, mismatch)
    with pytest.raises(TurnIngressConflictError):
        await accept(right, mismatch)


async def test_legacy_absence_is_never_retired_or_replayed(pair):
    left, right, _ = pair
    await accept(left, query("chat.send"))
    await left.delete_session(KEY)
    assert await read_turn_receipt(right, query("chat.send")) == {
        "status": "not_found",
        "accepted": None,
    }
    with pytest.raises(ValueError, match="Only chat.send.v2"):
        await resolve_turn_receipt(right, query("chat.send"))


@pytest.mark.parametrize("accepted_first", [False, True])
async def test_typed_workspace_error_is_retired_or_returns_concurrent_acceptance(
    pair, accepted_first
):
    left, right, _ = pair
    if accepted_first:
        await accept(left)
    command = GatewayTurnAdmissionAdapter.decode_webchat_command(PARAMS, KEY, resolvable=True)
    ports = SimpleNamespace(
        storage=GatewayAdmissionStorage(right),
        workspace_error=partial(map_project_workspace_error, owner=True),
        accepted_response=AsyncMock(return_value={"ok": True, "sessionKey": KEY}),
    )
    error = ProjectWorkspaceStateError("not_found")
    if accepted_first:
        assert await _close_rejected_resolvable_turn(command, ports, error) == {
            "ok": True,
            "sessionKey": KEY,
        }
        ports.accepted_response.assert_awaited_once()
    else:
        with pytest.raises(RpcHandlerError) as raised:
            await _close_rejected_resolvable_turn(command, ports, error)
        assert raised.value.accepted is False
        assert raised.value.details["requestRetired"] is True
        assert (await resolve_turn_receipt(left, query()))["status"] == "retired"


@pytest.mark.parametrize("accepted_first", [False, True])
@pytest.mark.parametrize("workspace_error", [False, True])
async def test_admission_preaccept_failure_uses_original_identity(
    pair,
    monkeypatch,
    accepted_first,
    workspace_error,
):
    left, right, _ = pair
    command = replace(
        GatewayTurnAdmissionAdapter.decode_webchat_command(PARAMS, KEY, resolvable=True),
        explicit_intent_registered=True,
    )
    error = (
        ProjectWorkspaceStateError("removed")
        if workspace_error
        else AdmissionError("DEPENDENCY_STARTING", "fixture", accepted=False, retryable=True)
    )
    if accepted_first:
        await accept(left)
    ports = SimpleNamespace(
        sessions=object(),
        storage=GatewayAdmissionStorage(right),
        clear_compaction_marker=Mock(),
        workspace_error=partial(map_project_workspace_error, owner=True),
        accepted_response=AsyncMock(return_value={"status": "accepted"}),
    )
    monkeypatch.setattr(
        "opensquilla.application.turn_acceptance._accept_turn",
        AsyncMock(side_effect=error),
    )
    if accepted_first:
        assert await DurableTurnAdmission(ports).admit(command) == {
            "status": "accepted",
            "ok": True,
            "sessionKey": KEY,
        }
    else:
        with pytest.raises((AdmissionError, RpcHandlerError)) as caught:
            await DurableTurnAdmission(ports).admit(command)
        assert caught.value.accepted is False
        assert caught.value.details["requestRetired"] is True
        assert (await resolve_turn_receipt(left, query()))["status"] == "retired"


async def test_initial_session_read_busy_stays_unknown_and_can_be_closed(pair, monkeypatch):
    left, right, _ = pair
    storage = GatewayAdmissionStorage(left)
    monkeypatch.setattr(
        storage,
        "get_session",
        AsyncMock(
            side_effect=AdmissionStorageBusyError(
                "get_session",
                waited_ms=100,
                retry_after_ms=250,
            )
        ),
    )
    command = replace(
        GatewayTurnAdmissionAdapter.decode_webchat_command(PARAMS, KEY, resolvable=True),
        explicit_intent_registered=True,
    )
    with pytest.raises(AdmissionStorageBusyError) as caught:
        await DurableTurnAdmission(
            SimpleNamespace(
                storage=storage,
                sessions=object(),
                clear_compaction_marker=Mock(),
            )
        ).admit(command)
    wire = map_admission_error(caught.value)
    assert wire.accepted is None and wire.retry_after_ms == 250
    assert (await resolve_turn_receipt(right, query()))["status"] == "retired"


async def test_read_prefers_acceptance_committed_between_receipt_and_marker(pair, monkeypatch):
    left, right, _ = pair
    read_marker = right.get_turn_request_resolution

    async def accept_then_read_marker(**kwargs):
        await accept(left)
        return await read_marker(**kwargs)

    monkeypatch.setattr(right, "get_turn_request_resolution", accept_then_read_marker)
    assert (await read_turn_receipt(right, query()))["status"] == "found"


def test_new_scope_cannot_be_selected_by_legacy_fields():
    command = GatewayTurnAdmissionAdapter.decode_webchat_command(PARAMS, KEY, resolvable=True)
    old = GatewayTurnAdmissionAdapter.decode_webchat_command({**PARAMS, "resolvable": True}, KEY)
    assert command.source_scope != old.source_scope
    for channel in ["webchat:operator:resolvable.v1", "webchat:resolvable.v1"]:
        assert (
            source_scope_from_hint({"caller_kind": "web", "channel_kind": channel}, "operator")
            != command.source_scope
        )


@pytest.mark.parametrize(
    "principal",
    [
        Principal("operator", frozenset({"operator.read"}), True, True),
        Principal("operator", frozenset({"operator.write"}), False, False),
    ],
)
async def test_resolution_auth_and_feature_advertisement_preserve_legacy_send(principal):
    methods = _build_features(get_dispatcher(), principal=principal).methods
    assert "chat.send" in methods
    assert "chat.send.v2" not in methods and "turns.receipt.resolve" not in methods
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_turns_receipt_resolve(
            {"operation": "chat.send.v2", "originalRequest": PARAMS},
            RpcContext(conn_id="fixture", principal=principal),
        )
    assert raised.value.code == "UNAUTHORIZED"


def rpc_context(storage):
    return RpcContext(
        conn_id="fixture",
        principal=Principal("operator", frozenset({"operator.read", "operator.write"}), True, True),
        session_manager=SimpleNamespace(storage=storage),
    )


@pytest.mark.parametrize("epoch", [None, "previous-process-epoch"])
async def test_older_epoch_absence_never_retires_or_enters_admission(pair, monkeypatch, epoch):
    left, _, _ = pair
    params = {**PARAMS}
    if epoch is None:
        params.pop("acceptanceEpoch")
    else:
        params["acceptanceEpoch"] = epoch
    request = decode_turn_receipt_query(
        {"operation": "chat.send.v2", "originalRequest": params}, principal_role="operator",
    )
    application_factory = Mock(side_effect=AssertionError("stale request entered admission"))
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_chat._chat_turn_admission_adapter", application_factory,
    )
    assert await resolve_turn_receipt(left, request) == {"status": "not_found", "accepted": None}
    # Exercise the registered generated parameter AND result contracts. A pure
    # helper assertion would miss a wire union that still rejected not_found.
    resolved = await get_dispatcher().dispatch(
        "old-epoch-resolve", "turns.receipt.resolve",
        {"operation": "chat.send.v2", "originalRequest": params}, rpc_context(left),
    )
    assert resolved.ok
    assert resolved.payload == {"status": "not_found", "accepted": None}
    with pytest.raises(RpcHandlerError) as caught:
        await _handle_chat_send_v2(params, rpc_context(left))
    assert caught.value.code == "UNAVAILABLE"
    assert caught.value.accepted is None
    application_factory.assert_not_called()
    assert await left.get_turn_request_resolution(
        source_scope=request.source_scope, request_session_key=request.session_key,
        client_request_id=request.client_request_id,
        request_fingerprint=request.request_fingerprint,
    ) is None
    assert await left.get_transcript("original-session") == []
    assert await left.get_agent_task("original-task") is None


def test_epoch_comes_from_live_gateway_policy_and_is_absent_before_start(monkeypatch):
    import opensquilla.gateway.boot as boot

    for policy in (None, TurnReceiptEpochPolicy(BOOT), TurnReceiptEpochPolicy("b" * 32)):
        monkeypatch.setattr(boot, "_turn_receipt_epochs", policy)
        assert current_turn_receipt_epoch() == (policy.current_epoch() if policy else None)


async def test_missing_server_epoch_fails_closed_even_with_client_epoch(pair, monkeypatch):
    left, _, _ = pair
    monkeypatch.setattr(
        "opensquilla.gateway.turn_receipts.current_turn_receipt_epoch", lambda: None,
    )
    assert await resolve_turn_receipt(left, query()) == {"status": "not_found", "accepted": None}
    with pytest.raises(RpcHandlerError) as caught:
        await _handle_chat_send_v2(PARAMS, rpc_context(left))
    assert caught.value.accepted is None


@pytest.mark.parametrize("epoch", [None, "previous-process-epoch"])
@pytest.mark.parametrize("state", ["found", "accepted_unavailable", "retired"])
async def test_older_epoch_reads_terminal_evidence_without_readmission(
    pair, monkeypatch, epoch, state,
):
    left, _, _ = pair
    if state == "retired":
        await resolve_turn_receipt(left, query())
    else:
        await accept(left)
        if state == "accepted_unavailable":
            await left.delete_session(KEY)
            async with left._write_transaction("fixture_legacy_delete") as conn:
                await conn.execute("DELETE FROM turn_ingress_receipts")
    params = {**PARAMS}
    if epoch is None:
        params.pop("acceptanceEpoch")
    else:
        params["acceptanceEpoch"] = epoch
    request = decode_turn_receipt_query(
        {"operation": "chat.send.v2", "originalRequest": params}, principal_role="operator",
    )
    # Epoch is a transport proof, not a new content identity or dedup namespace.
    assert request.request_fingerprint == query().request_fingerprint
    assert request.source_scope == query().source_scope
    assert (await resolve_turn_receipt(left, request))["status"] == state
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_chat._chat_turn_admission_adapter",
        Mock(side_effect=AssertionError("stale request entered admission")),
    )
    if state == "found":
        replay = await _handle_chat_send_v2(params, rpc_context(left))
        assert replay["status"] == "accepted" and replay["replayed"]
        assert replay["task_id"] == "original-task"
        assert len(await left.get_transcript("original-session")) == 1
    else:
        with pytest.raises(RpcHandlerError) as caught:
            await _handle_chat_send_v2(params, rpc_context(left))
        assert caught.value.accepted is (state == "accepted_unavailable")
        if state == "retired":
            assert caught.value.details["requestRetired"] is True


async def test_current_epoch_send_uses_existing_admission_without_an_extra_read(pair, monkeypatch):
    left, _, _ = pair
    adapter = SimpleNamespace(admit=AsyncMock(return_value={"status": "accepted"}))
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_chat._chat_turn_admission_adapter", Mock(return_value=adapter),
    )
    monkeypatch.setattr(
        left, "get_turn_ingress_receipt", AsyncMock(side_effect=AssertionError("unneeded read")),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.turn_receipts.decode_turn_receipt_query",
        Mock(side_effect=AssertionError("unneeded payload decode/hash")),
    )
    assert await _handle_chat_send_v2(PARAMS, rpc_context(left)) == {"status": "accepted"}
    adapter.admit.assert_awaited_once_with(PARAMS, surface="webchat", resolvable=True)


async def test_restored_pre_acceptance_backup_cannot_retire_or_execute_old_request(
    tmp_path, monkeypatch,
):
    path, backup, restored = (tmp_path / name for name in ("live.db", "backup.db", "restored.db"))
    storage = await open_storage(path)
    try:
        await storage.upsert_session(
            SessionNode(session_key=KEY, session_id="original-session", epoch=1),
        )
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as snapshot:
            source.backup(snapshot)
        await accept(storage)
        assert (await read_turn_receipt(storage, query()))["status"] == "found"
        assert len(await storage.get_transcript("original-session")) == 1
    finally:
        await storage.close()
    # Both originals are closed before publishing the consistent old snapshot
    # to a different temporary database; no live database replacement is used.
    with (
        closing(sqlite3.connect(backup)) as source,
        closing(sqlite3.connect(restored)) as destination,
    ):
        source.backup(destination)
    restored_policy = TurnReceiptEpochPolicy("b" * 32)
    monkeypatch.setattr(
        "opensquilla.gateway.turn_receipts.current_turn_receipt_epoch",
        restored_policy.current_epoch,
    )
    reopened = await SessionStorage.open(str(restored))
    reopened.configure_turn_receipt_epochs(restored_policy, exclusive_gateway=True)
    try:
        assert await resolve_turn_receipt(reopened, query()) == {
            "status": "not_found", "accepted": None,
        }
        monkeypatch.setattr(
            "opensquilla.gateway.rpc_chat._chat_turn_admission_adapter",
            Mock(side_effect=AssertionError("old send re-executed after rollback")),
        )
        with pytest.raises(RpcHandlerError) as caught:
            await _handle_chat_send_v2(PARAMS, rpc_context(reopened))
        assert caught.value.accepted is None
        # The actual writer also fences an old request that passed an earlier
        # RPC check; restoring a database cannot recreate its process authority.
        with pytest.raises(TurnRequestAdmissionClosedError):
            await accept(reopened)
        with closing(sqlite3.connect(restored)) as check:
            for table in (
                "turn_request_resolution", "turn_ingress_receipts",
                "transcript_entries", "agent_tasks",
            ):
                assert check.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        # A newly composed request in this process can still close safely.
        current = replace(
            query(), client_request_id=f"tr2:{restored_policy.current_epoch()}:fresh-request",
            acceptance_epoch=restored_policy.current_epoch(),
        )
        assert (await resolve_turn_receipt(reopened, current))["status"] == "retired"
    finally:
        await reopened.close()
