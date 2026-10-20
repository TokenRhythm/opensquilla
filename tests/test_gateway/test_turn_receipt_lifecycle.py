"""Real SQLite races for bounded marker lifetime; no provider or user profile."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from opensquilla.application.admission_errors import AdmissionError
from opensquilla.application.turn_acceptance import _close_rejected_resolvable_turn
from opensquilla.gateway.adapters.turn_admission import GatewayTurnAdmissionAdapter
from opensquilla.gateway.admission_storage import GatewayAdmissionStorage
from opensquilla.gateway.rpc import RpcHandlerError, get_dispatcher
from opensquilla.gateway.rpc_chat import _handle_chat_send_v2
from opensquilla.gateway.turn_receipts import decode_turn_receipt_query, resolve_turn_receipt
from opensquilla.persistence.turn_request_resolution import (
    EPOCH_VALID_HOURS,
    RESOLUTION_CLEANUP_BATCH,
    TurnReceiptEpochPolicy,
    parse_turn_receipt_epoch,
    request_binds_epoch,
)
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage, TurnRequestAdmissionClosedError
from tests.test_gateway.test_turn_receipt_resolution import (
    BOOT,
    EPOCH,
    KEY,
    PARAMS,
    accept,
    query,
    rpc_context,
)


@pytest.fixture
async def stores(tmp_path, monkeypatch):
    now = [0.0]
    policy = TurnReceiptEpochPolicy(BOOT, clock=lambda: now[0])
    monkeypatch.setattr(
        "opensquilla.gateway.turn_receipts.current_turn_receipt_epoch", policy.current_epoch,
    )
    path = tmp_path / "lifecycle.db"
    left, right = await SessionStorage.open(str(path)), await SessionStorage.open(str(path))
    for store in (left, right):
        store.configure_turn_receipt_epochs(policy)
    await left.upsert_session(SessionNode(session_key=KEY, session_id="original-session", epoch=1))
    try:
        yield left, right, policy, now
    finally:
        await right.close()
        await left.close()


async def resolve_raw(store, request=None):
    request = request or query()
    return await store.resolve_turn_request_attempt(
        source_scope=request.source_scope, request_session_key=request.session_key,
        client_request_id=request.client_request_id,
        request_fingerprint=request.request_fingerprint,
    )


async def marker_count(store):
    async with store.conn.execute("SELECT count(*) FROM turn_request_resolution") as cursor:
        return (await cursor.fetchone())[0]


def test_canonical_epoch_and_monotonic_expiry_cannot_be_renewed_by_clock_rollback():
    now = [0.0]
    policy = TurnReceiptEpochPolicy(BOOT, clock=lambda: now[0])
    epoch = parse_turn_receipt_epoch(policy.current_epoch())
    assert epoch is not None and epoch.expires_hour == 25
    now[0] = 24 * 3600 + 3599
    assert policy.accepts(epoch)
    now[0] += 1
    assert not policy.accepts(epoch)
    now[0] = -3600
    assert policy.current_hour == 25 and not policy.accepts(epoch)
    for malformed in (f"v2.{BOOT}.00.25", f"v2.{BOOT}.0.26", EPOCH.upper(), "", None):
        assert parse_turn_receipt_epoch(malformed) is None
    assert request_binds_epoch(PARAMS["clientRequestId"], EPOCH)
    assert not request_binds_epoch(PARAMS["clientRequestId"], policy.current_epoch())
    assert not request_binds_epoch(f"tr2:{EPOCH}:", EPOCH)
    assert not request_binds_epoch(f"tr2:{EPOCH}:" + "x" * 256, EPOCH)


async def test_expired_retirement_cleanup_never_reopens_request(stores):
    left, right, _, now = stores
    assert await resolve_raw(left) == "retired"
    now[0] = EPOCH_VALID_HOURS * 3600
    assert await resolve_raw(right) == "not_found"
    assert await marker_count(left) == 0
    with pytest.raises(TurnRequestAdmissionClosedError):
        await accept(left)
    assert await resolve_turn_receipt(right, query()) == {"status": "not_found", "accepted": None}
    assert await left.get_agent_task("original-task") is None
    assert await left.get_transcript("original-session") == []


async def test_cleanup_wins_against_writer_delayed_before_actual_transaction(stores, monkeypatch):
    left, right, _, now = stores
    assert await resolve_raw(right) == "retired"
    entered, release = asyncio.Event(), asyncio.Event()
    transaction = left._write_transaction

    @asynccontextmanager
    async def delay_before_writer(operation, **kwargs):
        if operation == "accept_turn":
            entered.set()
            await release.wait()
        async with transaction(operation, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(left, "_write_transaction", delay_before_writer)
    writer = asyncio.create_task(accept(left))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        now[0] = EPOCH_VALID_HOURS * 3600
        assert await resolve_raw(right) == "not_found"
        assert await marker_count(right) == 0
        release.set()
        with pytest.raises(TurnRequestAdmissionClosedError):
            await writer
        assert await left.get_transcript("original-session") == []
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)


async def test_valid_writer_linearizes_before_expiry_and_cleanup_preserves_receipt(
    stores, monkeypatch,
):
    left, right, _, now = stores
    entered, release = asyncio.Event(), asyncio.Event()
    commit = left._commit_transaction

    async def hold_commit(conn, operation, deadline, started):
        if operation == "accept_turn":
            entered.set()
            await release.wait()
        await commit(conn, operation, deadline, started)

    monkeypatch.setattr(left, "_commit_transaction", hold_commit)
    writer = asyncio.create_task(accept(left))
    cleaner = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        now[0] = EPOCH_VALID_HOURS * 3600
        cleaner = asyncio.create_task(resolve_raw(right))
        await asyncio.sleep(0.02)
        assert not cleaner.done()  # Acceptance still owns the real SQLite writer lock.
        release.set()
        accepted = await writer
        assert not accepted.replayed and await cleaner == "found"
        assert await marker_count(left) == 0
        assert (await accept(right)).replayed
        assert (await resolve_turn_receipt(right, query()))["status"] == "found"
        assert len(await left.get_transcript("original-session")) == 1
    finally:
        release.set()
        await asyncio.gather(
            writer, *([cleaner] if cleaner is not None else []), return_exceptions=True,
        )


async def test_cleaned_accepted_marker_without_receipt_remains_unknown(stores):
    left, right, _, now = stores
    await accept(left)
    await left.delete_session(KEY)
    async with left._write_transaction("fixture_remove_receipt") as conn:
        await conn.execute("DELETE FROM turn_ingress_receipts")
    now[0] = EPOCH_VALID_HOURS * 3600
    assert await resolve_raw(right) == "not_found"
    assert await marker_count(left) == 0
    with pytest.raises(TurnRequestAdmissionClosedError):
        await accept(left)


async def test_changed_epoch_metadata_does_not_renew_literal_wire_identity(stores):
    left, _, policy, now = stores
    now[0] = EPOCH_VALID_HOURS * 3600
    changed = {**PARAMS, "acceptanceEpoch": policy.current_epoch()}
    with pytest.raises(RpcHandlerError) as caught:
        await _handle_chat_send_v2(changed, rpc_context(left))
    assert caught.value.accepted is None
    request = decode_turn_receipt_query(
        {"operation": "chat.send.v2", "originalRequest": changed}, principal_role="operator",
    )
    assert await resolve_turn_receipt(left, request) == {"status": "not_found", "accepted": None}
    assert await marker_count(left) == 0


async def test_closed_preaccept_failure_does_not_claim_retirement(stores):
    from types import SimpleNamespace

    left, _, _, now = stores
    now[0] = EPOCH_VALID_HOURS * 3600
    command = GatewayTurnAdmissionAdapter.decode_webchat_command(PARAMS, KEY, resolvable=True)
    with pytest.raises(AdmissionError) as caught:
        await _close_rejected_resolvable_turn(
            command, SimpleNamespace(storage=GatewayAdmissionStorage(left)),
            AdmissionError("INVALID_REQUEST", "Not prepared", accepted=False),
        )
    assert caught.value.accepted is None and caught.value.kind == "UNAVAILABLE"
    assert await marker_count(left) == 0


async def test_cleanup_is_indexed_bounded_and_requires_foreign_boot_authority(stores):
    left, right, policy, now = stores
    for number in range(100):
        await resolve_raw(left, replace(query(), client_request_id=f"tr2:{EPOCH}:old-{number}"))
    now[0] = EPOCH_VALID_HOURS * 3600
    fresh = replace(query(), client_request_id=f"tr2:{policy.current_epoch()}:fresh")
    assert await resolve_raw(right, fresh) == "retired"
    assert await marker_count(left) == 100 - RESOLUTION_CLEANUP_BATCH + 1
    assert await resolve_raw(
        right, replace(fresh, client_request_id=f"{fresh.client_request_id}-2"),
    ) == "retired"
    assert await marker_count(left) == 2
    async with left.conn.execute(
        "EXPLAIN QUERY PLAN SELECT rowid FROM turn_request_resolution WHERE "
        "(validity_boot_id = ? AND validity_expires_hour <= ?) "
        "OR validity_boot_id < ? OR validity_boot_id > ? LIMIT ?",
        (BOOT, policy.current_hour, BOOT, BOOT, RESOLUTION_CLEANUP_BATCH),
    ) as cursor:
        plan = " ".join(str(tuple(row)) for row in await cursor.fetchall())
    assert "idx_turn_request_resolution_expiry" in plan
    assert "SCAN turn_request_resolution" not in plan
    other = TurnReceiptEpochPolicy("b" * 32)
    right.configure_turn_receipt_epochs(other)
    foreign = replace(fresh, client_request_id=f"tr2:{other.current_epoch()}:foreign")
    await resolve_raw(right, foreign)
    assert await marker_count(right) == 3  # A generic store cannot delete another live boot.
    # Simulate official handover only after the former writer has closed.
    await left.close()
    right.configure_turn_receipt_epochs(other, exclusive_gateway=True)
    await resolve_raw(right, replace(foreign, client_request_id=f"{foreign.client_request_id}-2"))
    assert await marker_count(right) == 2


async def test_registered_send_preserves_maximum_wire_identity_and_replays_after_expiry(
    tmp_path, monkeypatch,
):
    from tests.test_gateway.test_project_workspace_execution import add_project, open_stack

    now = [0.0]
    policy = TurnReceiptEpochPolicy(BOOT, clock=lambda: now[0])
    monkeypatch.setattr(
        "opensquilla.gateway.turn_receipts.current_turn_receipt_epoch", policy.current_epoch,
    )
    wire_id = f"tr2:{EPOCH}:" + "x" * (256 - len(f"tr2:{EPOCH}:"))
    async with open_stack(tmp_path / "rpc.db") as stack:
        stack.storage.configure_turn_receipt_epochs(policy)
        project = await add_project(stack, tmp_path / "project")
        await stack.manager.create(KEY, workspace_id=project.workspace_id)
        params = {
            **PARAMS, "clientRequestId": wire_id, "client_request_id": wire_id,
            "_source": {"runMode": "full"},
        }
        for invalid in (
            {**params, "clientRequestId": wire_id + "x"},
            {**params, "client_request_id": "different-id"},
        ):
            rejected = await get_dispatcher().dispatch(
                "invalid-wire", "chat.send.v2", invalid, stack.context,
            )
            assert not rejected.ok and rejected.error.code == "INVALID_REQUEST"
        assert stack.runs == []
        # A cached but still-valid bucket remains usable; rotation alone never
        # rejects a long-lived connection's request.
        now[0] = (EPOCH_VALID_HOURS - 1) * 3600
        accepted = await get_dispatcher().dispatch(
            "first", "chat.send.v2", params, stack.context,
        )
        assert accepted.ok, accepted.error
        assert accepted.payload["client_request_id"] == wire_id
        await asyncio.wait_for(stack.started.wait(), 3)
        now[0] = EPOCH_VALID_HOURS * 3600
        replay = await get_dispatcher().dispatch(
            "late", "chat.send.v2", params, stack.context,
        )
        assert replay.ok and replay.payload["replayed"]
        assert replay.payload["client_request_id"] == wire_id
        assert replay.payload["task_id"] == accepted.payload["task_id"]
        assert len(stack.runs) == 1
